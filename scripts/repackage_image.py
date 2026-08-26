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


FINGERPRINT_SCHEMA = 1
LABEL_FINGERPRINT = "io.obot.mcp.input-fingerprint"
LABEL_VERSION = "io.obot.mcp.application.version"
LABEL_REVISION = "io.obot.mcp.image.revision"

COMMON_FILES = ("Dockerfile.mmmcp", "scripts/mmmcp.sh")
TYPE_FILES = {
    "node": ("Dockerfile.base-node", "repackaging/Dockerfile.mcp-node"),
    "python": ("Dockerfile.base-python", "repackaging/Dockerfile.mcp-python"),
    "docker": ("repackaging/Dockerfile.mcp-docker",),
}


class RepackageError(RuntimeError):
    pass


def canonical_image(image: dict[str, Any]) -> dict[str, Any]:
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
    files = TYPE_FILES[image_type]
    if image_type in ("node", "python"):
        files += COMMON_FILES
    return files


def compute_fingerprint(
    root: Path,
    image: dict[str, Any],
    dependencies: dict[str, str],
) -> tuple[str, dict[str, Any]]:
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
    try:
        return run_crane(["ls", repository]).splitlines()
    except RepackageError as exc:
        message = str(exc).lower()
        if any(marker in message for marker in ("name_unknown", "not found", "404")):
            return []
        raise


def matching_revisions(tags: Iterable[str], version: str) -> list[int]:
    pattern = re.compile(rf"^{re.escape(version)}-obot([1-9][0-9]*)$")
    return sorted(
        int(match.group(1))
        for tag in tags
        if (match := pattern.fullmatch(tag.strip()))
    )


def image_labels(reference: str, platform: str) -> dict[str, str]:
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
    digest = run_crane(["digest", reference]).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RepackageError(f"crane returned an invalid digest for {reference}: {digest}")
    return f"{reference}@{digest}"


def wrapper_reference(root: Path) -> str:
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
    normalized = canonical_image(image)
    image_type = normalized["type"]
    dependencies: dict[str, str]
    build_inputs: dict[str, str] = {}

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


def parse_dependencies(values: Iterable[str]) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for value in values:
        name, separator, digest = value.partition("=")
        if not separator or not name or not digest:
            raise RepackageError(
                f"invalid dependency {value!r}; expected NAME=IMMUTABLE_REFERENCE"
            )
        dependencies[name] = digest
    return dependencies


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    fingerprint_parser = subparsers.add_parser("fingerprint")
    fingerprint_parser.add_argument("--root", type=Path, default=Path("."))
    fingerprint_parser.add_argument("--image-json", required=True)
    fingerprint_parser.add_argument(
        "--dependency", action="append", default=[], metavar="NAME=REFERENCE"
    )
    fingerprint_parser.add_argument("--explain", action="store_true")

    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("--repository", required=True)
    resolve_parser.add_argument("--version", required=True)
    resolve_parser.add_argument("--fingerprint", required=True)
    resolve_parser.add_argument("--platform", default="linux/amd64")

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--root", type=Path, default=Path("."))
    plan_parser.add_argument("--image-json", required=True)
    plan_parser.add_argument("--registry-prefix", required=True)
    plan_parser.add_argument("--platform", default="linux/amd64")
    return parser


def main() -> int:
    args = create_parser().parse_args()
    try:
        if args.command == "fingerprint":
            image = json.loads(args.image_json)
            fingerprint, payload = compute_fingerprint(
                args.root, image, parse_dependencies(args.dependency)
            )
            result: dict[str, Any] = {"fingerprint": fingerprint}
            if args.explain:
                result["inputs"] = payload
        elif args.command == "resolve":
            result = resolve_revision(
                args.repository, args.version, args.fingerprint, args.platform
            )
        else:
            result = plan_image(
                args.root,
                json.loads(args.image_json),
                args.registry_prefix.rstrip("/"),
                args.platform,
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, RepackageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
