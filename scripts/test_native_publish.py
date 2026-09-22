"""Exercise the publication shell steps without pushing to a registry."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
AMD64 = "sha256:" + "a" * 64
ARM64 = "sha256:" + "b" * 64
MANIFEST = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [{"digest": AMD64}, {"digest": ARM64}],
}


class NativePublishTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.commands = self.root / "commands.jsonl"
        self.output = self.root / "github-output"
        self.output.touch()
        binaries = self.root / "bin"
        binaries.mkdir()
        stub = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

command = Path(sys.argv[0]).name
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps([command, *sys.argv[1:]]) + "\\n")
if command == "crane":
    if sys.argv[1] == "manifest":
        print(os.environ.get("REGISTRY_MANIFEST", "{}"))
        print(os.environ.get("REGISTRY_ERROR", ""), file=sys.stderr)
        sys.exit(int(os.environ.get("REGISTRY_STATUS", "0")))
    print("sha256:" + "c" * 64)
elif "--dry-run" in sys.argv:
    print(os.environ["EXPECTED_MANIFEST"])
"""
        for command in ("docker", "crane"):
            executable = binaries / command
            executable.write_text(stub)
            executable.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(self.root),
            "GITHUB_OUTPUT": str(self.output),
            "COMMAND_LOG": str(self.commands),
            "REPOSITORY": "ghcr.io/example/images/browserbase",
            "REFERENCE": "ghcr.io/example/images/browserbase:3.0.0-obot2",
            "TAG": "3.0.0-obot2",
            "TAGS": "ghcr.io/example/images/browserbase:3.0.0-obot2",
            "EXPECTED_MANIFEST": json.dumps(MANIFEST),
        }
        for arch, digest in (("amd64", AMD64), ("arm64", ARM64)):
            directory = self.root / "digests" / arch
            directory.mkdir(parents=True)
            for filename in ("image", "application"):
                (directory / filename).write_text(digest + "\n")

    def run_step(self, workflow, name, **env):
        jobs = yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"]
        step = next(step for step in jobs["publish"]["steps"] if step.get("name") == name)
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
            env={**self.env, **env}, cwd=self.root, text=True, capture_output=True,
        )

    def calls(self):
        if not self.commands.exists():
            return []
        return [json.loads(line) for line in self.commands.read_text().splitlines()]

    def test_merges_both_architectures_for_every_image_kind(self):
        for workflow, step in (
            ("base-images.yml", "Publish image manifest"),
            ("publish-image.yml", "Publish application base manifest"),
            ("publish-image.yml", "Publish image manifest"),
        ):
            with self.subTest(workflow=workflow, step=step):
                self.commands.write_text("")
                result = self.run_step(workflow, step)
                self.assertEqual(result.returncode, 0, result.stderr)
                docker_calls = [call for call in self.calls() if call[0] == "docker"]
                self.assertEqual(docker_calls, [[
                    "docker", "buildx", "imagetools", "create", "--tag", self.env["REFERENCE"],
                    f"{self.env['REPOSITORY']}@{AMD64}", f"{self.env['REPOSITORY']}@{ARM64}",
                ]])

    def test_missing_architecture_never_publishes_partial_image(self):
        for filename in ("image", "application"):
            (self.root / "digests" / "arm64" / filename).unlink()
        for workflow, step in (
            ("base-images.yml", "Publish image manifest"),
            ("publish-image.yml", "Publish application base manifest"),
            ("publish-image.yml", "Publish image manifest"),
        ):
            with self.subTest(workflow=workflow, step=step):
                result = self.run_step(workflow, step)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_all_planned_tags_use_the_same_combined_manifest(self):
        tags = self.env["TAGS"] + "\n" + self.env["REPOSITORY"] + ":another-tag"
        result = self.run_step("publish-image.yml", "Publish image manifest", TAGS=tags)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[0][4:8], [
            "--tag", self.env["REFERENCE"], "--tag", self.env["REPOSITORY"] + ":another-tag",
        ])

    def test_unused_immutable_tag_is_allowed(self):
        result = self.run_step(
            "publish-image.yml", "Refuse immutable tag races",
            REGISTRY_STATUS="1", REGISTRY_ERROR="MANIFEST_UNKNOWN: manifest unknown",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "")

    def test_registry_failure_blocks_publication(self):
        result = self.run_step(
            "publish-image.yml", "Refuse immutable tag races",
            REGISTRY_STATUS="1", REGISTRY_ERROR="UNAUTHORIZED: authentication required",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not verify", result.stderr)
        self.assertFalse(any(call[0] == "docker" for call in self.calls()))

    def test_conflicting_immutable_tag_is_rejected(self):
        conflicting = {**MANIFEST, "manifests": [{"digest": "sha256:" + "c" * 64}]}
        result = self.run_step(
            "publish-image.yml", "Refuse immutable tag races",
            REGISTRY_MANIFEST=json.dumps(conflicting),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Immutable tag already exists", result.stderr)
        self.assertEqual(self.output.read_text(), "")

    def test_retry_reuses_only_the_exact_published_manifest(self):
        result = self.run_step(
            "publish-image.yml", "Refuse immutable tag races",
            REGISTRY_MANIFEST=json.dumps(MANIFEST, indent=2, sort_keys=True),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "reuse=true\n")
        docker_calls = [call for call in self.calls() if call[0] == "docker"]
        self.assertEqual(len(docker_calls), 1)
        self.assertIn("--dry-run", docker_calls[0])


if __name__ == "__main__":
    unittest.main()
