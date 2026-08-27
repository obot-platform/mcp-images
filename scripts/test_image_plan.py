#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import image_plan


class ImagePlanTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.write(".dockerignore", ".git\n")
        self.write(".github/workflows/publish-image.yml", "name: publish\n")
        self.write("scripts/image_plan.py", "planner\n")

    def tearDown(self):
        self.tempdir.cleanup()

    def write(self, path, contents):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")

    def repository_image(self, version="1.2.3"):
        image = {
            "name": "example",
            "dockerfile": "Dockerfile.example",
            "paths": ["example"],
            "parents": [],
        }
        if version is not None:
            image["version"] = version
        self.write("Dockerfile.example", "FROM scratch\n")
        self.write("example/server.js", "main();\n")
        return image

    def test_fingerprint_is_deterministic_and_tracks_files_and_dependencies(self):
        image = self.repository_image()
        adapted = image_plan.repository_adapter(image)
        first, _ = image_plan.compute_fingerprint(
            self.root, adapted["canonical"], "1.2.3", adapted["paths"], {"b": "2", "a": "1"}
        )
        repeated, _ = image_plan.compute_fingerprint(
            self.root, adapted["canonical"], "1.2.3", adapted["paths"], {"a": "1", "b": "2"}
        )
        self.assertEqual(first, repeated)

        self.write("example/server.js", "changed();\n")
        changed_file, _ = image_plan.compute_fingerprint(
            self.root, adapted["canonical"], "1.2.3", adapted["paths"], {"a": "1", "b": "2"}
        )
        self.assertNotEqual(first, changed_file)

        changed_dependency, _ = image_plan.compute_fingerprint(
            self.root, adapted["canonical"], "1.2.3", adapted["paths"], {"a": "1", "b": "3"}
        )
        self.assertNotEqual(changed_file, changed_dependency)

    def test_repository_adapter_validates_paths_and_pins_parents(self):
        image = self.repository_image()
        image["parents"] = [{"arg": "BASE_IMAGE", "image": "example/base:main"}]
        with mock.patch("image_plan.pinned_reference", return_value="example/base:main@sha256:abc"):
            adapted = image_plan.repository_adapter(image)
        self.assertEqual(adapted["paths"], ["Dockerfile.example", "example"])
        self.assertEqual(adapted["build_args"]["BASE_IMAGE"], "example/base:main@sha256:abc")

        image["paths"] = ["../secret"]
        with self.assertRaisesRegex(image_plan.ImagePlanError, "within the repository"):
            image_plan.repository_adapter(image)

    def test_repository_adapter_rejects_remote_sources(self):
        image = self.repository_image()
        image["sources"] = [{"repository": "external"}]
        with self.assertRaisesRegex(image_plan.ImagePlanError, "sources"):
            image_plan.repository_adapter(image)

    @mock.patch("image_plan.resolve_revision")
    @mock.patch("image_plan.pinned_reference")
    def test_repackage_plans_pin_node_dependencies(self, pin, revision):
        self.write(
            "repackaging/Dockerfile.mcp-node",
            "ARG MMMCP_IMAGE=example/wrapper:v1\nFROM scratch\n",
        )
        self.write("scripts/mmmcp.sh", "#!/bin/sh\n")
        pin.side_effect = ["base@sha256:aaa", "wrapper@sha256:bbb"]
        revision.return_value = {
            "build": True,
            "revision": 1,
            "tag": "1.0.0-obot1",
            "reason": "new-version-lineage",
        }

        result = image_plan.plan_image(
            self.root,
            "repackage",
            {"name": "example", "type": "node", "package": "pkg", "version": "1.0.0"},
            "ghcr.io/org/repo",
            "main",
            "branch",
            "source-sha",
        )

        self.assertEqual(result["build_args"]["BASE_IMAGE"], "base@sha256:aaa")
        self.assertEqual(result["build_args"]["MMMCP_IMAGE"], "wrapper@sha256:bbb")
        self.assertEqual(result["aliases"], ["1.0.0-main"])
        self.assertEqual(result["tags"], ["ghcr.io/org/repo/example:1.0.0-obot1", "ghcr.io/org/repo/example:1.0.0-main"])
        self.assertEqual(result["catalog_payload"]["package"], "pkg")

    @mock.patch("image_plan.existing_labels")
    def test_utility_main_builds_only_when_fingerprint_changes(self, labels):
        image = self.repository_image(version=None)
        labels.return_value = None
        changed = image_plan.plan_image(
            self.root, "utility", image, "ghcr.io/org/repo", "main", "branch", "sha"
        )
        self.assertTrue(changed["build"])
        self.assertFalse(changed["immutable"])

        labels.return_value = {image_plan.LABEL_FINGERPRINT: changed["fingerprint"]}
        unchanged = image_plan.plan_image(
            self.root, "utility", image, "ghcr.io/org/repo", "main", "branch", "sha"
        )
        self.assertFalse(unchanged["build"])
        self.assertEqual(unchanged["tag"], "main")

    @mock.patch("image_plan.existing_labels")
    def test_utility_release_uses_exact_tag_and_updates_latest(self, labels):
        image = self.repository_image(version=None)
        labels.return_value = None
        result = image_plan.plan_image(
            self.root, "utility", image, "ghcr.io/org/repo", "v1.2.3", "tag", "sha"
        )
        self.assertTrue(result["build"])
        self.assertTrue(result["immutable"])
        self.assertTrue(result["latest"])
        self.assertEqual(result["tag"], "v1.2.3")

        rc = image_plan.plan_image(
            self.root, "utility", image, "ghcr.io/org/repo", "v1.2.4-rc.1", "tag", "sha"
        )
        self.assertFalse(rc["latest"])

    @mock.patch("image_plan.existing_labels")
    def test_utility_release_retry_requires_the_same_source(self, labels):
        image = self.repository_image(version=None)
        labels.return_value = {image_plan.LABEL_SOURCE_REVISION: "sha"}
        retry = image_plan.plan_image(
            self.root, "utility", image, "ghcr.io/org/repo", "v1.2.3", "tag", "sha"
        )
        self.assertFalse(retry["build"])

        labels.return_value = {image_plan.LABEL_SOURCE_REVISION: "other"}
        with self.assertRaisesRegex(image_plan.ImagePlanError, "different source revision"):
            image_plan.plan_image(
                self.root, "utility", image, "ghcr.io/org/repo", "v1.2.3", "tag", "sha"
            )

    @mock.patch("image_plan.plan_image")
    def test_preview_advances_reused_descendants_of_rebuilt_bases(self, plan):
        entries = [
            {
                "family": "repackage",
                "image": {"name": "node-example", "type": "node"},
            },
            {
                "family": "repackage",
                "image": {"name": "python-example", "type": "python"},
            },
        ]
        plan.side_effect = [
            {
                "name": "node-example",
                "version": "1.2.3",
                "revision": 4,
                "tag": "1.2.3-obot4",
                "build": False,
                "reason": "fingerprint-match",
            },
            {
                "name": "python-example",
                "version": "2.0.0",
                "revision": 7,
                "tag": "2.0.0-obot7",
                "build": False,
                "reason": "fingerprint-match",
            },
        ]

        report = image_plan.render_preview(
            self.root,
            entries,
            "ghcr.io/org/repo",
            "sha",
            {"node"},
        )

        self.assertIn(
            "| node-example | `1.2.3-obot5` | build | base-image-will-rebuild |",
            report,
        )
        self.assertIn(
            "| python-example | `2.0.0-obot7` | reuse | fingerprint-match |",
            report,
        )

    @mock.patch("image_plan.plan_image")
    def test_preview_does_not_double_advance_an_existing_build(self, plan):
        entries = [
            {
                "family": "repackage",
                "image": {"name": "python-example", "type": "python"},
            }
        ]
        plan.return_value = {
            "name": "python-example",
            "version": "2.0.0",
            "revision": 8,
            "tag": "2.0.0-obot8",
            "build": True,
            "reason": "fingerprint-changed",
        }

        report = image_plan.render_preview(
            self.root,
            entries,
            "ghcr.io/org/repo",
            "sha",
            {"python"},
        )

        self.assertIn(
            "| python-example | `2.0.0-obot8` | build | base-image-will-rebuild |",
            report,
        )

    @mock.patch("image_plan.plan_image")
    def test_preview_marks_both_language_families(self, plan):
        entries = [
            {"family": "repackage", "image": {"name": "node", "type": "node"}},
            {
                "family": "repackage",
                "image": {"name": "python", "type": "python"},
            },
        ]
        plan.side_effect = [
            {
                "name": "node",
                "version": "1.0.0",
                "revision": 1,
                "tag": "1.0.0-obot1",
                "build": False,
                "reason": "fingerprint-match",
            },
            {
                "name": "python",
                "version": "2.0.0",
                "revision": 2,
                "tag": "2.0.0-obot2",
                "build": False,
                "reason": "fingerprint-match",
            },
        ]

        report = image_plan.render_preview(
            self.root,
            entries,
            "ghcr.io/org/repo",
            "sha",
            {"node", "python"},
        )

        self.assertIn("| node | `1.0.0-obot2` | build |", report)
        self.assertIn("| python | `2.0.0-obot3` | build |", report)


class RevisionTests(unittest.TestCase):
    def test_matching_revisions_are_version_scoped_and_numeric(self):
        tags = ["1.2.3-obot0", "1.2.3-obot9", "1.2.3-obot10", "1.2.30-obot99", "1.2.3-main"]
        self.assertEqual(image_plan.matching_revisions(tags, "1.2.3"), [9, 10])

    @mock.patch("image_plan.run_crane")
    def test_new_lineage_starts_at_one(self, crane):
        crane.return_value = "1.0.0-obot4\n"
        result = image_plan.resolve_revision("ghcr.io/org/image", "2.0.0", "sha256:new")
        self.assertEqual(result["tag"], "2.0.0-obot1")

    @mock.patch("image_plan.run_crane")
    def test_matching_fingerprint_reuses_latest_revision(self, crane):
        labels = {
            image_plan.LABEL_FINGERPRINT: "sha256:same",
            image_plan.LABEL_VERSION: "1.2.3",
            image_plan.LABEL_REVISION: "10",
        }
        crane.side_effect = ["1.2.3-obot9\n1.2.3-obot10\n", json.dumps({"config": {"Labels": labels}})]
        result = image_plan.resolve_revision("ghcr.io/org/image", "1.2.3", "sha256:same")
        self.assertFalse(result["build"])
        self.assertEqual(result["tag"], "1.2.3-obot10")

    @mock.patch("image_plan.run_crane")
    def test_changed_fingerprint_increments_latest_revision(self, crane):
        crane.side_effect = ["1.2.3-obot9\n1.2.3-obot10\n", json.dumps({"config": {"Labels": {}}})]
        result = image_plan.resolve_revision("ghcr.io/org/image", "1.2.3", "sha256:new")
        self.assertTrue(result["build"])
        self.assertEqual(result["tag"], "1.2.3-obot11")


if __name__ == "__main__":
    unittest.main()
