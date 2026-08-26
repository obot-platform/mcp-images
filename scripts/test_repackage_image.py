#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import repackage_image


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        for path in (
            "Dockerfile.base-node",
            "repackaging/Dockerfile.mcp-node",
            "Dockerfile.mmmcp",
            "scripts/mmmcp.sh",
        ):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(path, encoding="utf-8")
        self.image = {
            "name": "example",
            "type": "node",
            "package": "example-mcp",
            "version": "1.2.3",
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def test_fingerprint_is_deterministic(self):
        first, _ = repackage_image.compute_fingerprint(
            self.root, self.image, {"wrapper": "sha256:bbb", "base": "sha256:aaa"}
        )
        second, _ = repackage_image.compute_fingerprint(
            self.root, self.image, {"base": "sha256:aaa", "wrapper": "sha256:bbb"}
        )
        self.assertEqual(first, second)

    def test_relevant_file_changes_fingerprint(self):
        first, _ = repackage_image.compute_fingerprint(self.root, self.image, {})
        (self.root / "scripts/mmmcp.sh").write_text("changed", encoding="utf-8")
        second, _ = repackage_image.compute_fingerprint(self.root, self.image, {})
        self.assertNotEqual(first, second)

    def test_dependency_digest_changes_fingerprint(self):
        first, _ = repackage_image.compute_fingerprint(
            self.root, self.image, {"base": "sha256:aaa"}
        )
        second, _ = repackage_image.compute_fingerprint(
            self.root, self.image, {"base": "sha256:bbb"}
        )
        self.assertNotEqual(first, second)

    def test_missing_required_manifest_field_is_rejected(self):
        del self.image["package"]
        with self.assertRaisesRegex(repackage_image.RepackageError, "package"):
            repackage_image.compute_fingerprint(self.root, self.image, {})


class RevisionTests(unittest.TestCase):
    def test_matching_revisions_are_numeric_and_version_scoped(self):
        tags = [
            "1.2.3-obot0",
            "1.2.3-obot9",
            "1.2.3-obot10",
            "1.2.30-obot99",
            "1.2.3-main",
        ]
        self.assertEqual(repackage_image.matching_revisions(tags, "1.2.3"), [9, 10])

    @mock.patch("repackage_image.run_crane")
    def test_new_version_starts_at_one(self, crane):
        crane.return_value = "1.0.0-obot4\n"
        result = repackage_image.resolve_revision(
            "ghcr.io/example/image", "2.0.0", "sha256:new"
        )
        self.assertEqual(result["tag"], "2.0.0-obot1")
        self.assertTrue(result["build"])

    @mock.patch("repackage_image.run_crane")
    def test_matching_fingerprint_reuses_latest_revision(self, crane):
        labels = {
            repackage_image.LABEL_FINGERPRINT: "sha256:same",
            repackage_image.LABEL_VERSION: "1.2.3",
            repackage_image.LABEL_REVISION: "10",
        }
        crane.side_effect = [
            "1.2.3-obot9\n1.2.3-obot10\n",
            json.dumps({"config": {"Labels": labels}}),
        ]
        result = repackage_image.resolve_revision(
            "ghcr.io/example/image", "1.2.3", "sha256:same"
        )
        self.assertEqual(result["tag"], "1.2.3-obot10")
        self.assertFalse(result["build"])

    @mock.patch("repackage_image.run_crane")
    def test_changed_fingerprint_increments_numeric_revision(self, crane):
        crane.side_effect = [
            "1.2.3-obot9\n1.2.3-obot10\n",
            json.dumps({"config": {"Labels": {}}}),
        ]
        result = repackage_image.resolve_revision(
            "ghcr.io/example/image", "1.2.3", "sha256:new"
        )
        self.assertEqual(result["tag"], "1.2.3-obot11")
        self.assertTrue(result["build"])

    @mock.patch("repackage_image.run_crane")
    def test_missing_repository_starts_at_one(self, crane):
        crane.side_effect = repackage_image.RepackageError(
            "crane ls ghcr.io/example/new failed: NAME_UNKNOWN"
        )
        result = repackage_image.resolve_revision(
            "ghcr.io/example/new", "1.0.0", "sha256:new"
        )
        self.assertEqual(result["tag"], "1.0.0-obot1")


class PlanTests(unittest.TestCase):
    @mock.patch("repackage_image.resolve_revision")
    @mock.patch("repackage_image.run_crane")
    def test_node_plan_pins_base_and_wrapper(self, crane, resolve):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "Dockerfile.base-node": "FROM example/base",
                "repackaging/Dockerfile.mcp-node": "FROM base",
                "Dockerfile.mmmcp": "ARG MMMCP_IMAGE=example/wrapper:v1\n",
                "scripts/mmmcp.sh": "#!/bin/sh",
            }
            for path, contents in files.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding="utf-8")
            crane.side_effect = [f"sha256:{'a' * 64}\n", f"sha256:{'b' * 64}\n"]
            resolve.return_value = {
                "build": True,
                "revision": 1,
                "tag": "1.0.0-obot1",
            }

            result = repackage_image.plan_image(
                root,
                {
                    "name": "example",
                    "type": "node",
                    "package": "example",
                    "version": "1.0.0",
                },
                "ghcr.io/example/images",
            )

            self.assertEqual(
                result["base_image"],
                f"ghcr.io/example/images/base-node:main@sha256:{'a' * 64}",
            )
            self.assertEqual(
                result["wrapper_image"], f"example/wrapper:v1@sha256:{'b' * 64}"
            )
            self.assertTrue(result["fingerprint"].startswith("sha256:"))

    @mock.patch("repackage_image.resolve_revision")
    @mock.patch("repackage_image.run_crane")
    def test_docker_plan_pins_upstream_image(self, crane, resolve):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dockerfile = root / "repackaging/Dockerfile.mcp-docker"
            dockerfile.parent.mkdir(parents=True)
            dockerfile.write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n", encoding="utf-8")
            crane.return_value = f"sha256:{'c' * 64}\n"
            resolve.return_value = {
                "build": True,
                "revision": 1,
                "tag": "latest-obot1",
            }

            result = repackage_image.plan_image(
                root,
                {
                    "name": "example",
                    "type": "docker",
                    "package": "example/upstream",
                    "version": "latest",
                },
                "ghcr.io/example/images",
            )

            self.assertEqual(
                result["source_image"],
                f"example/upstream:latest@sha256:{'c' * 64}",
            )


class CommandTests(unittest.TestCase):
    @mock.patch("repackage_image.plan_image")
    @mock.patch("builtins.print")
    def test_cli_exposes_only_the_complete_plan_operation(self, output, plan):
        plan.return_value = {"tag": "1.0.0-obot1"}
        arguments = [
            "repackage_image.py",
            "--image-json",
            '{"name":"example"}',
            "--registry-prefix",
            "ghcr.io/example/images/",
        ]

        with mock.patch("sys.argv", arguments):
            self.assertEqual(repackage_image.main(), 0)

        plan.assert_called_once_with(
            Path("."), {"name": "example"}, "ghcr.io/example/images"
        )
        output.assert_called_once_with('{"tag": "1.0.0-obot1"}')


if __name__ == "__main__":
    unittest.main()
