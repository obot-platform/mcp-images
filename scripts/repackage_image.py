#!/usr/bin/env python3
"""Compute repackaged-image fingerprints and resolve immutable revisions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


# Bump this when the meaning of the fingerprint changes. That deliberately makes
# every image compare as changed without introducing a separate global version.
FINGERPRINT_SCHEMA = 1

# The standard OCI revision label records source provenance. The Obot labels are
# the machine-readable contract used to decide whether an immutable tag is reusable.
LABEL_FINGERPRINT = "io.obot.mcp.input-fingerprint"
LABEL_VERSION = "io.obot.mcp.application.version"
LABEL_REVISION = "io.obot.mcp.image.revision"

# These paths describe the transitive repository inputs for each image family.
# File contents are hashed, so shared changes fan out only to affected families.
COMMON_FILES = ("Dockerfile.mmmcp", "scripts/mmmcp.sh")
TYPE_FILES = {
    "node": ("Dockerfile.base-node", "repackaging/Dockerfile.mcp-node"),
    "python": ("Dockerfile.base-python", "repackaging/Dockerfile.mcp-python"),
    "docker": ("repackaging/Dockerfile.mcp-docker",),
}


class RepackageError(RuntimeError):
    pass


def canonical_image(image: dict[str, Any]) -> dict[str, Any]:
    """Validate and retain only manifest fields that can affect an image."""
    image_type = image.get("type")
    if image_type not in TYPE_FILES:
        raise RepackageError(f"unsupported image type: {image_type!r}")

    for key in ("name", "package", "version"):
        value = image.get(key)
        if not isinstance(value, str) or not value:
            raise RepackageError(f"image {key} must be a non-empty string")
    for field in ("constraints", "overrides"):
        values = image.get(field)
        if values is not None and (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value for value in values)
        ):
            raise RepackageError(
                f"image {field} must be a list of non-empty strings"
            )

    result: dict[str, Any] = {}
    for key in ("name", "type", "package", "version", "constraints", "overrides"):
        if key in image and image[key] is not None:
            result[key] = image[key]
    return result


def fingerprint_files(image_type: str) -> tuple[str, ...]:
    """Return repository inputs that transitively contribute to an image type."""
    files = TYPE_FILES[image_type]
    if image_type in ("node", "python"):
        files += COMMON_FILES
    return files


def compute_fingerprint(
    root: Path,
    image: dict[str, Any],
    dependencies: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    """Hash a canonical description of all known effective build inputs."""
    normalized = canonical_image(image)
    file_records = []
    for relative_path in fingerprint_files(normalized["type"]):
        content = (root / relative_path).read_bytes()
        file_records.append(
            {
                "path": relative_path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    payload = {
        "schema": FINGERPRINT_SCHEMA,
        "image": normalized,
        "files": file_records,
        "dependencies": dict(sorted(dependencies.items())),
    }
    # Canonical JSON avoids revisions caused by dictionary insertion order or
    # formatting differences in the resolver itself.
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}", payload


def run_crane(arguments: Iterable[str]) -> str:
    command = ["crane", *arguments]
    try:
        result = subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RepackageError("crane is required but was not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip()
        raise RepackageError(f"{' '.join(command)} failed: {detail}") from exc
    return result.stdout


def repository_tags(repository: str) -> list[str]:
    """List tags, treating a repository that has never been pushed as empty."""
    try:
        return run_crane(["ls", repository]).splitlines()
    except RepackageError as exc:
        message = str(exc).lower()
        if any(marker in message for marker in ("name_unknown", "not found", "404")):
            return []
        raise


def matching_revisions(tags: Iterable[str], version: str) -> list[int]:
    """Return numeric revisions from this exact application-version lineage."""
    pattern = re.compile(rf"^{re.escape(version)}-obot([1-9][0-9]*)$")
    return sorted(
        int(match.group(1))
        for tag in tags
        if (match := pattern.fullmatch(tag.strip()))
    )


def image_labels(reference: str, platform: str) -> dict[str, str]:
    """Read labels from one platform config of a multi-platform image."""
    config = json.loads(run_crane(["config", "--platform", platform, reference]))
    labels = config.get("config", {}).get("Labels") or {}
    if not isinstance(labels, dict):
        return {}
    return {str(key): str(value) for key, value in labels.items()}


def resolve_revision(
    repository: str,
    version: str,
    fingerprint: str,
    platform: str = "linux/amd64",
) -> dict[str, Any]:
    """Reuse an identical immutable image or allocate its next revision."""
    revisions = matching_revisions(repository_tags(repository), version)
    if not revisions:
        revision = 1
        return {
            "build": True,
            "revision": revision,
            "tag": f"{version}-obot{revision}",
            "reason": "new-version-lineage",
        }

    latest_revision = revisions[-1]
    latest_tag = f"{version}-obot{latest_revision}"
    labels = image_labels(f"{repository}:{latest_tag}", platform)
    # A fingerprint alone is insufficient: validating all identity labels keeps
    # malformed or legacy images from being silently treated as current.
    if (
        labels.get(LABEL_FINGERPRINT) == fingerprint
        and labels.get(LABEL_VERSION) == version
        and labels.get(LABEL_REVISION) == str(latest_revision)
    ):
        return {
            "build": False,
            "revision": latest_revision,
            "tag": latest_tag,
            "reason": "fingerprint-match",
        }

    revision = latest_revision + 1
    return {
        "build": True,
        "revision": revision,
        "tag": f"{version}-obot{revision}",
        "reason": "fingerprint-changed",
        "previous_tag": latest_tag,
    }


def pinned_reference(reference: str) -> str:
    """Resolve a mutable image reference to the digest used by the build."""
    digest = run_crane(["digest", reference]).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RepackageError(f"crane returned an invalid digest for {reference}: {digest}")
    return f"{reference}@{digest}"


def wrapper_reference(root: Path) -> str:
    """Keep the wrapper source declared once, in Dockerfile.mmmcp."""
    dockerfile = (root / "Dockerfile.mmmcp").read_text(encoding="utf-8")
    match = re.search(r"^ARG\s+MMMCP_IMAGE=([^\s]+)", dockerfile, re.MULTILINE)
    if not match:
        raise RepackageError("Dockerfile.mmmcp has no default MMMCP_IMAGE")
    return match.group(1)


def plan_image(
    root: Path,
    image: dict[str, Any],
    registry_prefix: str,
    platform: str = "linux/amd64",
) -> dict[str, Any]:
    """Resolve dependencies, fingerprint inputs, and select the publish tag."""
    normalized = canonical_image(image)
    image_type = normalized["type"]
    dependencies: dict[str, str]
    build_inputs: dict[str, str] = {}

    # Parent references are included in the fingerprint and returned to the
    # workflow so revision selection and the subsequent build use identical bits.
    if image_type in ("node", "python"):
        base_reference = f"{registry_prefix}/base-{image_type}:main"
        wrapper = wrapper_reference(root)
        build_inputs["base_image"] = pinned_reference(base_reference)
        build_inputs["wrapper_image"] = pinned_reference(wrapper)
        dependencies = dict(build_inputs)
    else:
        source_reference = f"{normalized['package']}:{normalized['version']}"
        build_inputs["source_image"] = pinned_reference(source_reference)
        dependencies = dict(build_inputs)

    fingerprint, inputs = compute_fingerprint(root, normalized, dependencies)
    repository = f"{registry_prefix}/{normalized['name']}"
    result = resolve_revision(
        repository, str(normalized["version"]), fingerprint, platform
    )
    result.update(
        {
            "repository": repository,
            "fingerprint": fingerprint,
            "fingerprint_inputs": inputs,
            **build_inputs,
        }
    )
    return result


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-json", required=True)
    parser.add_argument("--registry-prefix", required=True)
    return parser


def main() -> int:
    args = create_parser().parse_args()
    try:
        result = plan_image(
            Path("."),
            json.loads(args.image_json),
            args.registry_prefix.rstrip("/"),
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, RepackageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
