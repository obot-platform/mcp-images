#!/usr/bin/env python3
"""Create complete build plans for every image publication family."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


FINGERPRINT_SCHEMA = 1
LABEL_FINGERPRINT = "io.obot.mcp.input-fingerprint"
LABEL_VERSION = "io.obot.mcp.application.version"
LABEL_REVISION = "io.obot.mcp.image.revision"
LABEL_SOURCE_REVISION = "org.opencontainers.image.revision"

COMMON_PATHS = (
    ".dockerignore",
    ".github/workflows/publish-image.yml",
    "scripts/image_plan.py",
)
REPACKAGE_PATHS = {
    "node": (
        "repackaging/Dockerfile.mcp-node",
        "Dockerfile.mmmcp",
        "scripts/mmmcp.sh",
    ),
    "python": (
        "repackaging/Dockerfile.mcp-python",
        "Dockerfile.mmmcp",
        "scripts/mmmcp.sh",
    ),
    "docker": ("repackaging/Dockerfile.mcp-docker",),
}


class ImagePlanError(RuntimeError):
    pass


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ImagePlanError(f"image {field} must be a non-empty string")
    return value


def _relative_path(value: Any, field: str) -> str:
    path = PurePosixPath(_nonempty_string(value, field))
    if path.is_absolute() or ".." in path.parts:
        raise ImagePlanError(f"image {field} must stay within the repository")
    return str(path)


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
        raise ImagePlanError("crane is required but was not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip()
        raise ImagePlanError(f"{' '.join(command)} failed: {detail}") from exc
    return result.stdout


def _missing_reference(exc: ImagePlanError) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("manifest_unknown", "name_unknown", "not found", "404")
    )


def repository_tags(repository: str) -> list[str]:
    try:
        return run_crane(["ls", repository]).splitlines()
    except ImagePlanError as exc:
        if _missing_reference(exc):
            return []
        raise


def matching_revisions(tags: Iterable[str], version: str) -> list[int]:
    pattern = re.compile(rf"^{re.escape(version)}-obot([1-9][0-9]*)$")
    return sorted(
        int(match.group(1))
        for tag in tags
        if (match := pattern.fullmatch(tag.strip()))
    )


def image_labels(reference: str, platform: str = "linux/amd64") -> dict[str, str]:
    config = json.loads(run_crane(["config", "--platform", platform, reference]))
    labels = config.get("config", {}).get("Labels") or {}
    if not isinstance(labels, dict):
        return {}
    return {str(key): str(value) for key, value in labels.items()}


def existing_labels(reference: str, platform: str = "linux/amd64") -> dict[str, str] | None:
    try:
        run_crane(["manifest", reference])
    except ImagePlanError as exc:
        if _missing_reference(exc):
            return None
        raise
    return image_labels(reference, platform)


def pinned_reference(reference: str) -> str:
    digest = run_crane(["digest", reference]).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ImagePlanError(f"crane returned an invalid digest for {reference}: {digest}")
    return f"{reference}@{digest}"


def resolve_revision(
    repository: str,
    version: str,
    fingerprint: str,
    platform: str = "linux/amd64",
) -> dict[str, Any]:
    """Reuse the newest matching immutable revision, or allocate the next one."""
    revisions = matching_revisions(repository_tags(repository), version)
    if not revisions:
        return {
            "build": True,
            "revision": 1,
            "tag": f"{version}-obot1",
            "reason": "new-version-lineage",
        }

    revision = revisions[-1]
    tag = f"{version}-obot{revision}"
    labels = image_labels(f"{repository}:{tag}", platform)
    if (
        labels.get(LABEL_FINGERPRINT) == fingerprint
        and labels.get(LABEL_VERSION) == version
        and labels.get(LABEL_REVISION) == str(revision)
    ):
        return {
            "build": False,
            "revision": revision,
            "tag": tag,
            "reason": "fingerprint-match",
        }

    revision += 1
    return {
        "build": True,
        "revision": revision,
        "tag": f"{version}-obot{revision}",
        "reason": "fingerprint-changed",
        "previous_tag": tag,
    }


def fingerprint_records(root: Path, requested: Iterable[str]) -> list[dict[str, Any]]:
    records = []
    for relative in sorted(set((*COMMON_PATHS, *requested))):
        target = root / relative
        if target.is_symlink() or not target.is_file():
            raise ImagePlanError(
                f"fingerprint input must be a regular file: {relative}"
            )
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )
    return records


def compute_fingerprint(
    root: Path,
    canonical_image: dict[str, Any],
    version: str,
    paths: Iterable[str],
    dependencies: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    """Hash normalized metadata, local build inputs, and pinned parent images."""
    payload = {
        "schema": FINGERPRINT_SCHEMA,
        "version": version,
        "image": canonical_image,
        "files": fingerprint_records(root, paths),
        "dependencies": dict(sorted(dependencies.items())),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}", payload


def _string_list(image: dict[str, Any], field: str) -> list[str]:
    values = image.get(field, [])
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value for value in values
    ):
        raise ImagePlanError(f"image {field} must be a list of non-empty strings")
    return values


def _wrapper_reference(root: Path) -> str:
    dockerfile = root / "Dockerfile.mmmcp"
    match = re.search(
        r"^ARG\s+MMMCP_IMAGE=([^\s]+)",
        dockerfile.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        raise ImagePlanError(f"{dockerfile} has no default MMMCP_IMAGE")
    return match.group(1)


def repackage_adapter(
    root: Path, image: dict[str, Any], registry_prefix: str
) -> dict[str, Any]:
    """Translate an NPX/UV/upstream-image entry into generic build inputs.

    Repackages share Dockerfiles by runtime type, so the adapter derives their
    local inputs and pins every mutable base before fingerprinting and building.
    """
    if not isinstance(image, dict):
        raise ImagePlanError("image definition must be an object")
    image_type = image.get("type")
    if image_type not in REPACKAGE_PATHS:
        raise ImagePlanError(f"unsupported image type: {image_type!r}")
    name = _nonempty_string(image.get("name"), "name")
    package = _nonempty_string(image.get("package"), "package")
    version = _nonempty_string(image.get("version"), "version")
    constraints = _string_list(image, "constraints")
    overrides = _string_list(image, "overrides")
    canonical = {
        "name": name,
        "type": image_type,
        "package": package,
        "version": version,
        "constraints": constraints,
        "overrides": overrides,
    }
    dockerfile = f"repackaging/Dockerfile.mcp-{image_type}"
    dependencies: dict[str, str] = {}
    build_args: dict[str, str]

    if image_type in ("node", "python"):
        base = pinned_reference(f"{registry_prefix}/base-{image_type}:main")
        wrapper = pinned_reference(_wrapper_reference(root))
        dependencies = {"base_image": base, "wrapper_image": wrapper}
        build_args = {
            "MCP_PACKAGE": package,
            "MCP_VERSION": version,
            "BASE_IMAGE": base,
            "MMMCP_IMAGE": wrapper,
        }
        if image_type == "python":
            build_args.update(
                {
                    "MCP_CONSTRAINTS": " ".join(constraints),
                    "MCP_OVERRIDES": " ".join(overrides),
                }
            )
    else:
        source = pinned_reference(f"{package}:{version}")
        dependencies = {"source_image": source}
        build_args = {"BASE_IMAGE": source}

    return {
        "canonical": canonical,
        "name": name,
        "version": version,
        "dockerfile": dockerfile,
        "paths": REPACKAGE_PATHS[image_type],
        "dependencies": dependencies,
        "build_args": build_args,
        "catalog": True,
        "catalog_package": package,
        "catalog_type": image_type,
        "application_base": image_type in ("node", "python"),
    }


def repository_adapter(image: dict[str, Any]) -> dict[str, Any]:
    """Translate a repository-owned image entry into generic build inputs.

    These images declare their own Dockerfile, source files, and parent build
    arguments. Parent references are pinned so planning and building use the
    same content, while local files remain explicit fingerprint inputs.
    """
    if not isinstance(image, dict):
        raise ImagePlanError("image definition must be an object")
    name = _nonempty_string(image.get("name"), "name")
    dockerfile = _relative_path(image.get("dockerfile"), "dockerfile")
    declared_paths = image.get("paths", [])
    if not isinstance(declared_paths, list):
        raise ImagePlanError("image paths must be a list")
    paths = [_relative_path(path, "paths entry") for path in declared_paths]
    parents = image.get("parents", [])
    if not isinstance(parents, list):
        raise ImagePlanError("image parents must be a list")
    normalized_parents = []
    build_args = {}
    dependencies = {}
    for parent in parents:
        if not isinstance(parent, dict):
            raise ImagePlanError("image parents entries must be objects")
        argument = _nonempty_string(parent.get("arg"), "parents arg")
        reference = _nonempty_string(parent.get("image"), "parents image")
        if argument in build_args:
            raise ImagePlanError("image parents contains duplicate build args")
        pinned = pinned_reference(reference)
        normalized_parents.append({"arg": argument, "image": reference})
        build_args[argument] = pinned
        dependencies[f"parent:{argument}"] = pinned

    canonical: dict[str, Any] = {
        "name": name,
        "dockerfile": dockerfile,
        "paths": paths,
        "parents": normalized_parents,
    }
    version = image.get("version")
    if version is not None:
        canonical["version"] = _nonempty_string(version, "version")
    if "sources" in image:
        raise ImagePlanError("image sources are not supported")
    return {
        "canonical": canonical,
        "name": name,
        "version": version,
        "dockerfile": dockerfile,
        "paths": [dockerfile, *paths],
        "dependencies": dependencies,
        "build_args": build_args,
        "catalog": image.get("catalog") is True,
        "catalog_package": "",
        "catalog_type": "",
        "application_base": False,
    }


def _labels(
    source_revision: str, fingerprint: str, version: str, revision: int | None
) -> dict[str, str]:
    labels = {
        LABEL_SOURCE_REVISION: source_revision,
        LABEL_FINGERPRINT: fingerprint,
        LABEL_VERSION: version,
    }
    if revision is not None:
        labels[LABEL_REVISION] = str(revision)
    return labels


def plan_image(
    root: Path,
    family: str,
    image: dict[str, Any],
    registry_prefix: str,
    ref_name: str,
    ref_type: str,
    source_revision: str,
) -> dict[str, Any]:
    """Convert any manifest entry into the generic publisher's complete plan."""
    if family == "repackage":
        adapted = repackage_adapter(root, image, registry_prefix)
    elif family in ("repository", "utility"):
        adapted = repository_adapter(image)
    else:
        raise ImagePlanError(f"unsupported image family: {family}")

    configured_version = adapted["version"]
    version = configured_version or ref_name
    fingerprint, inputs = compute_fingerprint(
        root,
        adapted["canonical"],
        version,
        adapted["paths"],
        adapted["dependencies"],
    )
    repository = f"{registry_prefix.rstrip('/')}/{adapted['name']}"
    aliases: list[str] = []
    latest = False

    if configured_version:
        selected = resolve_revision(repository, version, fingerprint)
        immutable = True
        aliases = [f"{version}-{ref_name}"] if ref_type == "branch" else []
    elif ref_type == "tag":
        reference = f"{repository}:{ref_name}"
        labels = existing_labels(reference)
        if labels is None:
            selected = {
                "build": True,
                "revision": None,
                "tag": ref_name,
                "reason": "new-release-tag",
            }
        elif labels.get(LABEL_SOURCE_REVISION) == source_revision:
            selected = {
                "build": False,
                "revision": None,
                "tag": ref_name,
                "reason": "release-retry",
            }
        else:
            raise ImagePlanError(
                f"immutable release tag has different source revision: {reference}"
            )
        immutable = True
        latest = "-rc" not in ref_name
    else:
        tag = ref_name
        labels = existing_labels(f"{repository}:{tag}")
        matching = labels is not None and labels.get(LABEL_FINGERPRINT) == fingerprint
        selected = {
            "build": not matching,
            "revision": None,
            "tag": tag,
            "reason": "fingerprint-match" if matching else "moving-preview-changed",
        }
        immutable = False

    selected.update(
        {
            "name": adapted["name"],
            "family": family,
            "repository": repository,
            "version": version,
            "fingerprint": fingerprint,
            "fingerprint_inputs": inputs,
            "dockerfile": adapted["dockerfile"],
            "build_args": adapted["build_args"],
            "tags": [f"{repository}:{selected['tag']}"],
            "aliases": aliases,
            "labels": _labels(
                source_revision, fingerprint, version, selected.get("revision")
            ),
            "immutable": immutable,
            "latest": latest,
            "catalog": adapted["catalog"] and configured_version is not None,
            "catalog_package": adapted["catalog_package"],
            "catalog_type": adapted["catalog_type"],
            "application_base": adapted["application_base"],
        }
    )
    selected["catalog_payload"] = {
        "image_name": repository,
        "new_tag": selected["tag"],
    }
    if family == "repackage":
        selected["catalog_payload"].update(
            {
                "name": adapted["name"],
                "package": adapted["catalog_package"],
                "type": adapted["catalog_type"],
            }
        )
    if selected["build"]:
        selected["tags"].extend(f"{repository}:{alias}" for alias in aliases)
    if adapted["application_base"]:
        application_args = dict(adapted["build_args"])
        wrapper_image = application_args.pop("MMMCP_IMAGE")
        selected.update(
            {
                "application_build_args": application_args,
                "application_repository": f"{repository}-base",
                "application_tag": version,
                "final_dockerfile": "Dockerfile.mmmcp",
                "wrapper_image": wrapper_image,
            }
        )
    return selected


def render_preview(
    root: Path,
    entries: list[dict[str, Any]],
    registry_prefix: str,
    source_revision: str,
    rebuilt_bases: set[str] | None = None,
) -> str:
    rebuilt_bases = rebuilt_bases or set()
    sections = (("Revisioned images", True), ("Moving utility images", False))
    plans = [
        (
            entry,
            plan_image(
                root,
                entry["family"],
                entry["image"],
                registry_prefix,
                "main",
                "branch",
                source_revision,
            ),
        )
        for entry in entries
    ]
    lines = []
    for title, revisioned in sections:
        selected = [
            (entry, plan)
            for entry, plan in plans
            if (plan["revision"] is not None) == revisioned
        ]
        if not selected:
            continue
        lines.extend(
            [
                f"## {title}",
                "",
                "| Image | Proposed tag | Action | Reason |",
                "| --- | --- | --- | --- |",
            ]
        )
        for entry, plan in selected:
            base_will_rebuild = (
                entry["family"] == "repackage"
                and entry["image"].get("type") in rebuilt_bases
            )
            action = "build" if plan["build"] or base_will_rebuild else "reuse"
            tag = plan["tag"]
            reason = plan["reason"]
            if base_will_rebuild:
                reason = "base-image-will-rebuild"
                if not plan["build"]:
                    revision = plan["revision"] + 1
                    tag = f"{plan['version']}-obot{revision}"
            lines.append(
                "| {name} | `{tag}` | {action} | {reason} |".format(
                    name=html.escape(plan["name"]),
                    tag=html.escape(tag),
                    action=action,
                    reason=html.escape(reason),
                )
            )
        lines.append("")
    return "\n".join(lines)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--family", required=True)
    plan.add_argument("--image-json", required=True)
    plan.add_argument("--registry-prefix", required=True)
    plan.add_argument("--ref-name", required=True)
    plan.add_argument("--ref-type", required=True)
    plan.add_argument("--source-revision", required=True)
    preview = subparsers.add_parser("preview")
    preview.add_argument("--entries-json", required=True)
    preview.add_argument("--registry-prefix", required=True)
    preview.add_argument("--rebuilt-bases-json", default="[]")
    preview.add_argument("--source-revision", required=True)
    return parser


def main() -> int:
    args = create_parser().parse_args()
    try:
        if args.command == "plan":
            result: Any = plan_image(
                Path("."),
                args.family,
                json.loads(args.image_json),
                args.registry_prefix,
                args.ref_name,
                args.ref_type,
                args.source_revision,
            )
        else:
            result = render_preview(
                Path("."),
                json.loads(args.entries_json),
                args.registry_prefix,
                args.source_revision,
                set(json.loads(args.rebuilt_bases_json)),
            )
        print(result if isinstance(result, str) else json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, ImagePlanError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
