#!/usr/bin/env python3
"""Plan immutable revisions for images built directly from this repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import repackage_image


FINGERPRINT_SCHEMA = 1
COMMON_PATHS = (
    ".dockerignore",
    ".github/workflows/repository-images.yml",
    "scripts/repackage_image.py",
    "scripts/repository_image.py",
)


class RepositoryImageError(RuntimeError):
    pass


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RepositoryImageError(f"image {field} must be a non-empty string")
    return value


def _relative_path(value: Any, field: str) -> str:
    path = PurePosixPath(_nonempty_string(value, field))
    if path.is_absolute() or ".." in path.parts:
        raise RepositoryImageError(f"image {field} must stay within the repository")
    return str(path)


def canonical_image(image: dict[str, Any]) -> dict[str, Any]:
    """Validate the declarative build inputs for one repository-owned image."""
    if not isinstance(image, dict):
        raise RepositoryImageError("image definition must be an object")

    result: dict[str, Any] = {
        "name": _nonempty_string(image.get("name"), "name"),
        "dockerfile": _relative_path(image.get("dockerfile"), "dockerfile"),
    }
    if "version" in image:
        result["version"] = _nonempty_string(image.get("version"), "version")
    if "sources" in image:
        raise RepositoryImageError("image sources are not supported")

    paths = image.get("paths", [])
    if not isinstance(paths, list):
        raise RepositoryImageError("image paths must be a list")
    result["paths"] = [_relative_path(path, "paths entry") for path in paths]

    parents = image.get("parents", [])
    if not isinstance(parents, list):
        raise RepositoryImageError("image parents must be a list")
    normalized_parents = []
    for parent in parents:
        if not isinstance(parent, dict):
            raise RepositoryImageError("image parents entries must be objects")
        normalized_parents.append(
            {
                "arg": _nonempty_string(parent.get("arg"), "parents arg"),
                "image": _nonempty_string(parent.get("image"), "parents image"),
            }
        )
    if len({parent["arg"] for parent in normalized_parents}) != len(
        normalized_parents
    ):
        raise RepositoryImageError("image parents contains duplicate build args")
    result["parents"] = normalized_parents
    return result


def _path_record(root: Path, path: Path) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if path.is_symlink():
        payload = os.readlink(path).encode()
        kind = "symlink"
    else:
        payload = path.read_bytes()
        kind = "file"
    return {
        "path": relative,
        "kind": kind,
        "mode": mode,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def fingerprint_records(root: Path, image: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand declared files/directories into deterministic content records."""
    requested = [*COMMON_PATHS, image["dockerfile"], *image["paths"]]
    files: dict[str, Path] = {}
    for relative in requested:
        target = root / relative
        if not target.exists() and not target.is_symlink():
            raise RepositoryImageError(f"fingerprint input does not exist: {relative}")
        if target.is_symlink() or target.is_file():
            files[target.relative_to(root).as_posix()] = target
            continue
        if not target.is_dir():
            raise RepositoryImageError(f"unsupported fingerprint input: {relative}")
        for child in target.rglob("*"):
            if child.is_symlink() or child.is_file():
                files[child.relative_to(root).as_posix()] = child
    return [_path_record(root, files[path]) for path in sorted(files)]


def compute_fingerprint(
    root: Path,
    image: dict[str, Any],
    version: str,
    dependencies: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    normalized = canonical_image(image)
    payload = {
        "schema": FINGERPRINT_SCHEMA,
        "version": _nonempty_string(version, "version"),
        "image": normalized,
        "files": fingerprint_records(root, normalized),
        "dependencies": dict(sorted(dependencies.items())),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}", payload


def plan_image(
    root: Path,
    image: dict[str, Any],
    version: str,
    registry_prefix: str,
) -> dict[str, Any]:
    """Pin parent images, fingerprint declared files, and allocate a revision."""
    normalized = canonical_image(image)
    version = normalized.get("version", version)
    dependencies: dict[str, str] = {}
    build_args: dict[str, str] = {}

    for parent in normalized["parents"]:
        pinned = repackage_image.pinned_reference(parent["image"])
        build_args[parent["arg"]] = pinned
        dependencies[f"parent:{parent['arg']}"] = pinned

    fingerprint, inputs = compute_fingerprint(root, normalized, version, dependencies)
    repository = f"{registry_prefix.rstrip('/')}/{normalized['name']}"
    result = repackage_image.resolve_revision(repository, version, fingerprint)
    result.update(
        {
            "repository": repository,
            "version": version,
            "fingerprint": fingerprint,
            "fingerprint_inputs": inputs,
            "build_args": build_args,
        }
    )
    return result


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-json", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--registry-prefix", required=True)
    return parser


def main() -> int:
    args = create_parser().parse_args()
    try:
        result = plan_image(
            Path("."),
            json.loads(args.image_json),
            args.version,
            args.registry_prefix,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        RepositoryImageError,
        repackage_image.RepackageError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
