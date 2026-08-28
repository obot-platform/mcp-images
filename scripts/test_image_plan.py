#!/usr/bin/env python3

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import image_plan


def entry(family, name, **fields):
    return {"family": family, "image": {"name": name, **fields}}


class RevisionTests(unittest.TestCase):
    def test_matching_revisions_are_exact_version_scoped_and_numeric(self):
        tags = [
            "1.2.3-obot0",
            "1.2.3-obot2",
            "1.2.3-obot10",
            "1.2.30-obot99",
            "1.2.3-main",
            "1.2.3-obotx",
        ]
        self.assertEqual(image_plan.matching_revisions(tags, "1.2.3"), [2, 10])

    @mock.patch("image_plan.repository_tags", return_value=[])
    def test_new_version_starts_at_one(self, _tags):
        self.assertEqual(
            image_plan.resolve_revision("ghcr.io/org/image", "2.0.0"),
            {"revision": 1, "tag": "2.0.0-obot1"},
        )

    @mock.patch(
        "image_plan.repository_tags",
        return_value=["1.2.3-obot9", "1.2.3-obot10"],
    )
    def test_existing_version_increments_highest_revision(self, _tags):
        self.assertEqual(
            image_plan.resolve_revision("ghcr.io/org/image", "1.2.3"),
            {"revision": 11, "tag": "1.2.3-obot11"},
        )

    @mock.patch("image_plan.repository_tags", return_value=["1.2.3-obot2", "1.2.3-obot10"])
    def test_current_revision_uses_highest_existing_tag(self, _tags):
        self.assertEqual(
            image_plan.current_revision_tag("ghcr.io/org/image", "1.2.3"),
            "1.2.3-obot10",
        )

    @mock.patch("image_plan.run_crane")
    def test_missing_repository_is_empty_but_other_errors_fail_closed(self, crane):
        crane.side_effect = image_plan.ImagePlanError("NAME_UNKNOWN")
        self.assertEqual(image_plan.repository_tags("ghcr.io/org/image"), [])
        crane.side_effect = image_plan.ImagePlanError("unauthorized")
        with self.assertRaisesRegex(image_plan.ImagePlanError, "unauthorized"):
            image_plan.repository_tags("ghcr.io/org/image")


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.repackages = [
            entry("repackage", "node-a", type="node", package="a", version="1"),
            entry("repackage", "node-b", type="node", package="b", version="1"),
            entry("repackage", "python-a", type="python", package="p", version="1"),
            entry("repackage", "docker-a", type="docker", package="d", version="1"),
        ]

    def names(self, selected):
        return [item["image"]["name"] for item in selected]

    def test_manifest_loader_builds_family_matrices(self):
        contents = """
images:
  - name: versioned
    group: mcp-server
    version: 1.2.3
    dockerfile: Dockerfile.versioned
  - name: utility
    group: utility
    dockerfile: Dockerfile.utility
"""
        repository = image_plan.manifest_entries(contents, "repository")
        utilities = image_plan.manifest_entries(contents, "utility")
        self.assertEqual(self.names(repository), ["versioned"])
        self.assertEqual(self.names(utilities), ["utility"])
        self.assertNotIn("group", repository[0]["image"])

    def test_manifest_change_selects_only_changed_current_entry(self):
        previous = [dict(item) for item in self.repackages]
        previous = [
            entry("repackage", "node-a", type="node", package="a", version="0"),
            *previous[1:],
        ]
        selected = image_plan.select_affected(
            "repackage",
            self.repackages,
            previous,
            {"repackaging/images.yaml"},
        )
        self.assertEqual(self.names(selected), ["node-a"])

    def test_shared_runtime_inputs_select_only_dependents(self):
        node = image_plan.select_affected(
            "repackage",
            self.repackages,
            self.repackages,
            {"Dockerfile.base-node"},
        )
        self.assertEqual(self.names(node), ["node-a", "node-b"])
        wrapper = image_plan.select_affected(
            "repackage",
            self.repackages,
            self.repackages,
            {"Dockerfile.mmmcp"},
        )
        self.assertEqual(self.names(wrapper), ["node-a", "node-b", "python-a"])
        base_workflow = image_plan.select_affected(
            "repackage",
            self.repackages,
            self.repackages,
            {".github/workflows/base-images.yml"},
        )
        self.assertEqual(
            self.names(base_workflow), ["node-a", "node-b", "python-a"]
        )

    def test_type_dockerfile_selects_that_type(self):
        selected = image_plan.select_affected(
            "repackage",
            self.repackages,
            self.repackages,
            {"repackaging/Dockerfile.mcp-docker"},
        )
        self.assertEqual(self.names(selected), ["docker-a"])

    def test_manual_target_selects_one_and_rejects_unknown(self):
        selected = image_plan.select_affected(
            "repackage", self.repackages, [], set(), "python-a"
        )
        self.assertEqual(self.names(selected), ["python-a"])
        with self.assertRaisesRegex(image_plan.ImagePlanError, "unknown"):
            image_plan.select_affected(
                "repackage", self.repackages, [], set(), "missing"
            )

    def test_manual_target_never_selects_other_families(self):
        repository = [
            entry(
                "repository",
                "github",
                version="1",
                dockerfile="mcp-servers/Dockerfile.github",
            )
        ]
        self.assertEqual(
            image_plan.select_affected(
                "repository", repository, [], set(), "github"
            ),
            [],
        )

    def test_repository_owned_path_selects_only_owner(self):
        entries = [
            entry(
                "repository",
                "github",
                version="1",
                dockerfile="mcp-servers/Dockerfile.github",
                paths=[],
            ),
            entry(
                "repository",
                "tableau",
                version="1",
                dockerfile="mcp-servers/Dockerfile.tableau",
                paths=["tableau/config.json"],
            ),
        ]
        selected = image_plan.select_affected(
            "repository", entries, entries, {"tableau/config.json"}
        )
        self.assertEqual(self.names(selected), ["tableau"])

    def test_unrelated_change_selects_nothing(self):
        self.assertEqual(
            image_plan.select_affected(
                "repackage", self.repackages, self.repackages, {"README.md"}
            ),
            [],
        )


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "Dockerfile.mmmcp").write_text(
            "ARG MMMCP_IMAGE=example/wrapper:v1\n", encoding="utf-8"
        )

    def tearDown(self):
        self.tempdir.cleanup()

    @mock.patch("image_plan.resolve_revision")
    @mock.patch("image_plan.pinned_reference")
    def test_repackage_publishes_only_revisioned_final_tag(self, pin, revision):
        pin.side_effect = ["base@sha256:aaa", "wrapper@sha256:bbb"]
        revision.return_value = {"revision": 3, "tag": "1.0.0-obot3"}
        result = image_plan.plan_image(
            self.root,
            "repackage",
            {"name": "example", "type": "node", "package": "pkg", "version": "1.0.0"},
            "ghcr.io/org/repo",
            "main",
            "branch",
            "sha",
        )
        self.assertEqual(result["tags"], ["ghcr.io/org/repo/example:1.0.0-obot3"])
        self.assertEqual(result["application_repository"], "ghcr.io/org/repo/example-base")
        self.assertEqual(result["application_tag"], "1.0.0")
        self.assertNotIn("aliases", result)
        self.assertEqual(
            result["catalog_payload"],
            {
                "image_name": "ghcr.io/org/repo/example",
                "new_tag": "1.0.0-obot3",
            },
        )

    @mock.patch("image_plan.resolve_revision")
    @mock.patch("image_plan.pinned_reference", return_value="parent@sha256:aaa")
    def test_versioned_repository_image_uses_same_revision_scheme(self, _pin, revision):
        revision.return_value = {"revision": 2, "tag": "4.0.5-obot2"}
        result = image_plan.plan_image(
            self.root,
            "repository",
            {
                "name": "tableau",
                "version": "4.0.5",
                "dockerfile": "mcp-servers/Dockerfile.tableau",
                "parents": [{"arg": "NODE_IMAGE", "image": "node:22-alpine"}],
            },
            "ghcr.io/org/repo",
            "main",
            "branch",
            "sha",
        )
        self.assertEqual(result["tag"], "4.0.5-obot2")
        self.assertTrue(result["immutable"])

    @mock.patch("image_plan.image_labels", return_value=None)
    def test_unversioned_release_behavior_is_preserved(self, _exists):
        result = image_plan.plan_image(
            self.root,
            "utility",
            {"name": "utility", "dockerfile": "Dockerfile.utility"},
            "ghcr.io/org/repo",
            "v1.2.3",
            "tag",
            "sha",
        )
        self.assertEqual(result["tag"], "v1.2.3")
        self.assertTrue(result["latest"])

    @mock.patch(
        "image_plan.image_labels",
        return_value={image_plan.LABEL_SOURCE_REVISION: "sha"},
    )
    def test_unversioned_release_retry_reuses_existing_tag(self, _labels):
        result = image_plan.plan_image(
            self.root,
            "utility",
            {"name": "utility", "dockerfile": "Dockerfile.utility"},
            "ghcr.io/org/repo",
            "v1.2.3",
            "tag",
            "sha",
        )
        self.assertFalse(result["build"])


if __name__ == "__main__":
    unittest.main()
