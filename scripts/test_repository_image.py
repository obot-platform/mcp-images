#!/usr/bin/env python3

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import repository_image


class RepositoryFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        files = {
            ".dockerignore": ".git\n",
            ".github/workflows/repository-images.yml": "name: publish\n",
            "scripts/repackage_image.py": "shared resolver\n",
            "scripts/repository_image.py": "resolver\n",
            "mcp-servers/Dockerfile.example": "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n",
            "example/package.json": "{}\n",
            "example/server.js": "main();\n",
        }
        for path, contents in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        self.image = {
            "name": "example",
            "dockerfile": "mcp-servers/Dockerfile.example",
            "paths": ["example"],
            "parents": [{"arg": "BASE_IMAGE", "image": "example/base:main"}],
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def test_fingerprint_is_deterministic(self):
        first, _ = repository_image.compute_fingerprint(
            self.root, self.image, "main", {"b": "2", "a": "1"}
        )
        second, _ = repository_image.compute_fingerprint(
            self.root, self.image, "main", {"a": "1", "b": "2"}
        )
        self.assertEqual(first, second)

    def test_declared_directory_change_updates_fingerprint(self):
        first, _ = repository_image.compute_fingerprint(
            self.root, self.image, "main", {}
        )
        (self.root / "example/server.js").write_text("changed();\n", encoding="utf-8")
        second, _ = repository_image.compute_fingerprint(
            self.root, self.image, "main", {}
        )
        self.assertNotEqual(first, second)

    def test_parent_digest_change_updates_fingerprint(self):
        first, _ = repository_image.compute_fingerprint(
            self.root, self.image, "main", {"parent:BASE_IMAGE": "sha256:a"}
        )
        second, _ = repository_image.compute_fingerprint(
            self.root, self.image, "main", {"parent:BASE_IMAGE": "sha256:b"}
        )
        self.assertNotEqual(first, second)

    def test_version_change_updates_fingerprint(self):
        first, _ = repository_image.compute_fingerprint(
            self.root, self.image, "1.0.0", {}
        )
        second, _ = repository_image.compute_fingerprint(
            self.root, self.image, "1.1.0", {}
        )
        self.assertNotEqual(first, second)

    def test_path_traversal_is_rejected(self):
        self.image["paths"] = ["../secret"]
        with self.assertRaisesRegex(repository_image.RepositoryImageError, "repository"):
            repository_image.canonical_image(self.image)


class RepositoryPlanTests(unittest.TestCase):
    @mock.patch("repository_image.repackage_image.resolve_revision")
    @mock.patch("repository_image.resolve_source")
    @mock.patch("repository_image.repackage_image.pinned_reference")
    def test_plan_pins_remote_inputs_for_fingerprint_and_build(
        self, pin, source, revision
    ):
        pin.return_value = "example/base:main@sha256:parent"
        source.return_value = "a" * 40
        revision.return_value = {
            "build": True,
            "revision": 1,
            "tag": "main-obot1",
        }
        image = {
            "name": "example",
            "version": "1.2.3",
            "dockerfile": "mcp-servers/Dockerfile.example",
            "paths": ["example"],
            "parents": [{"arg": "BASE_IMAGE", "image": "example/base:main"}],
            "sources": [
                {
                    "arg": "SOURCE_COMMIT",
                    "repository": "https://example.test/source.git",
                    "ref": "main",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                ".dockerignore": "",
                ".github/workflows/repository-images.yml": "workflow",
                "scripts/repackage_image.py": "shared resolver",
                "scripts/repository_image.py": "resolver",
                "mcp-servers/Dockerfile.example": "FROM example",
                "example/file": "contents",
            }
            for path, contents in files.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding="utf-8")

            result = repository_image.plan_image(
                root, image, "main", "ghcr.io/example/images"
            )

        self.assertEqual(
            result["build_args"],
            {
                "BASE_IMAGE": "example/base:main@sha256:parent",
                "SOURCE_COMMIT": "a" * 40,
            },
        )
        self.assertEqual(result["version"], "1.2.3")
        revision.assert_called_once_with(
            "ghcr.io/example/images/example", "1.2.3", result["fingerprint"]
        )


if __name__ == "__main__":
    unittest.main()
