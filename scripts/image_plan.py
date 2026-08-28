#!/usr/bin/env python3
"""Select affected images and create registry-backed publication plans."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

LABEL_SOURCE_REVISION = "org.opencontainers.image.revision"


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


def run_command(command: list[str]) -> str:
    try:
        result = subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise ImagePlanError(f"{command[0]} is required but was not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip()
        raise ImagePlanError(f"{' '.join(command)} failed: {detail}") from exc
    return result.stdout


def run_crane(arguments: Iterable[str]) -> str:
    return run_command(["crane", *arguments])


def _missing_reference(exc: ImagePlanError) -> bool:
    message = str(exc).lower()
    return "manifest_unknown" in message or "name_unknown" in message


def repository_tags(repository: str) -> list[str]:
    try:
        return run_crane(["ls", repository]).splitlines()
    except ImagePlanError as exc:
        if _missing_reference(exc):
            return []
        raise


def matching_revisions(tags: Iterable[str], version: str) -> list[int]:
    pattern = re.compile(rf"^{re.escape(version)}-obot([1-9][0-9]*)$")
    return sorted(int(m.group(1)) for tag in tags if (m := pattern.fullmatch(tag.strip())))


def resolve_revision(repository: str, version: str) -> dict[str, Any]:
    revisions = matching_revisions(repository_tags(repository), version)
    revision = revisions[-1] + 1 if revisions else 1
    return {"revision": revision, "tag": f"{version}-obot{revision}"}


def current_revision_tag(repository: str, version: str) -> str:
    revisions = matching_revisions(repository_tags(repository), version)
    if not revisions:
        raise ImagePlanError(f"no published revision for {repository}:{version}")
    return f"{version}-obot{revisions[-1]}"


def image_labels(reference: str, platform: str = "linux/amd64") -> dict[str, str] | None:
    try:
        run_crane(["manifest", reference])
    except ImagePlanError as exc:
        if _missing_reference(exc):
            return None
        raise
    config = json.loads(run_crane(["config", "--platform", platform, reference]))
    labels = config.get("config", {}).get("Labels") or {}
    return {str(key): str(value) for key, value in labels.items()} if isinstance(labels, dict) else {}


def pinned_reference(reference: str) -> str:
    digest = run_crane(["digest", reference]).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ImagePlanError(f"crane returned an invalid digest for {reference}: {digest}")
    return f"{reference}@{digest}"


def _string_list(image: dict[str, Any], field: str) -> list[str]:
    values = image.get(field, [])
    if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
        raise ImagePlanError(f"image {field} must be a list of non-empty strings")
    return values


def _wrapper_reference(root: Path) -> str:
    dockerfile = root / "Dockerfile.mmmcp"
    match = re.search(r"^ARG\s+MMMCP_IMAGE=([^\s]+)", dockerfile.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise ImagePlanError(f"{dockerfile} has no default MMMCP_IMAGE")
    return match.group(1)


def repackage_adapter(root: Path, image: dict[str, Any], registry_prefix: str) -> dict[str, Any]:
    if not isinstance(image, dict):
        raise ImagePlanError("image definition must be an object")
    image_type = image.get("type")
    if image_type not in ("node", "python", "docker"):
        raise ImagePlanError(f"unsupported image type: {image_type!r}")
    name = _nonempty_string(image.get("name"), "name")
    package = _nonempty_string(image.get("package"), "package")
    version = _nonempty_string(image.get("version"), "version")
    constraints = _string_list(image, "constraints")
    overrides = _string_list(image, "overrides")
    if image_type in ("node", "python"):
        base = pinned_reference(f"{registry_prefix}/base-{image_type}:main")
        wrapper = pinned_reference(_wrapper_reference(root))
        build_args = {"MCP_PACKAGE": package, "MCP_VERSION": version, "BASE_IMAGE": base, "MMMCP_IMAGE": wrapper}
        if image_type == "python":
            build_args.update({"MCP_CONSTRAINTS": " ".join(constraints), "MCP_OVERRIDES": " ".join(overrides)})
    else:
        build_args = {"BASE_IMAGE": pinned_reference(f"{package}:{version}")}
    return {
        "name": name, "version": version, "dockerfile": f"repackaging/Dockerfile.mcp-{image_type}",
        "build_args": build_args, "catalog": True,
        "application_base": image_type in ("node", "python"),
    }


def repository_adapter(image: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(image, dict):
        raise ImagePlanError("image definition must be an object")
    name = _nonempty_string(image.get("name"), "name")
    dockerfile = _relative_path(image.get("dockerfile"), "dockerfile")
    paths = image.get("paths", [])
    if not isinstance(paths, list):
        raise ImagePlanError("image paths must be a list")
    for path in paths:
        _relative_path(path, "paths entry")
    parents = image.get("parents", [])
    if not isinstance(parents, list):
        raise ImagePlanError("image parents must be a list")
    build_args = {}
    for parent in parents:
        if not isinstance(parent, dict):
            raise ImagePlanError("image parents entries must be objects")
        argument = _nonempty_string(parent.get("arg"), "parents arg")
        reference = _nonempty_string(parent.get("image"), "parents image")
        if argument in build_args:
            raise ImagePlanError("image parents contains duplicate build args")
        build_args[argument] = pinned_reference(reference)
    if "sources" in image:
        raise ImagePlanError("image sources are not supported")
    version = image.get("version")
    if version is not None:
        version = _nonempty_string(version, "version")
    return {"name": name, "version": version, "dockerfile": dockerfile, "build_args": build_args,
            "catalog": image.get("catalog") is True, "application_base": False}


def plan_image(root: Path, family: str, image: dict[str, Any], registry_prefix: str,
               ref_name: str, ref_type: str, source_revision: str) -> dict[str, Any]:
    if family == "repackage":
        adapted = repackage_adapter(root, image, registry_prefix)
    elif family in ("repository", "utility"):
        adapted = repository_adapter(image)
    else:
        raise ImagePlanError(f"unsupported image family: {family}")
    configured_version = adapted["version"]
    version = configured_version or ref_name
    repository = f"{registry_prefix.rstrip('/')}/{adapted['name']}"
    latest = False
    if configured_version:
        selected = resolve_revision(repository, version)
        selected["build"] = True
        immutable = True
    elif ref_type == "tag":
        labels = image_labels(f"{repository}:{ref_name}")
        if labels is None:
            selected = {"build": True, "revision": None, "tag": ref_name}
        elif labels.get(LABEL_SOURCE_REVISION) == source_revision:
            selected = {"build": False, "revision": None, "tag": ref_name}
        else:
            raise ImagePlanError(f"immutable release tag already exists: {repository}:{ref_name}")
        immutable = True
        latest = "-rc" not in ref_name
    else:
        selected = {"build": True, "revision": None, "tag": ref_name}
        immutable = False
    labels = {LABEL_SOURCE_REVISION: source_revision}
    selected.update({
        "name": adapted["name"], "family": family, "repository": repository,
        "version": version, "dockerfile": adapted["dockerfile"], "build_args": adapted["build_args"],
        "tags": [f"{repository}:{selected['tag']}"], "labels": labels, "immutable": immutable,
        "latest": latest, "catalog": adapted["catalog"] and configured_version is not None,
        "application_base": adapted["application_base"],
    })
    selected["catalog_payload"] = {"image_name": repository, "new_tag": selected["tag"]}
    if adapted["application_base"]:
        application_args = dict(adapted["build_args"])
        wrapper_image = application_args.pop("MMMCP_IMAGE")
        selected.update({"application_build_args": application_args, "application_repository": f"{repository}-base",
                         "application_tag": version, "final_dockerfile": "Dockerfile.mmmcp", "wrapper_image": wrapper_image})
    return selected


def _by_name(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for entry in entries:
        image = entry.get("image") if isinstance(entry, dict) else None
        name = image.get("name") if isinstance(image, dict) else None
        if not isinstance(name, str) or not name:
            raise ImagePlanError("every matrix entry must contain an image name")
        if name in result:
            raise ImagePlanError(f"duplicate image name: {name}")
        result[name] = entry
    return result


def manifest_entries(contents: str, family: str) -> list[dict[str, Any]]:
    try:
        import yaml
    except ImportError as exc:
        raise ImagePlanError(
            "PyYAML is required for manifest commands; install scripts/image-requirements.txt"
        ) from exc
    document = yaml.safe_load(contents)
    images = document.get("images") if isinstance(document, dict) else None
    if not isinstance(images, list):
        raise ImagePlanError("image manifest must contain an images list")
    entries = []
    for image in images:
        if not isinstance(image, dict):
            raise ImagePlanError("image manifest entries must be objects")
        configured_version = image.get("version") is not None
        if family == "repository" and not configured_version:
            continue
        if family == "utility" and configured_version:
            continue
        normalized = dict(image)
        normalized.pop("group", None)
        entries.append({"family": family, "image": normalized})
    return entries


def read_manifest(path: Path, family: str, git_ref: str = "") -> list[dict[str, Any]]:
    if git_ref:
        try:
            contents = run_command(["git", "show", f"{git_ref}:{path.as_posix()}"])
        except ImagePlanError:
            if family in ("repository", "utility"):
                return []
            raise
    else:
        contents = path.read_text(encoding="utf-8")
    return manifest_entries(contents, family)


def changed_paths(base_ref: str, head_ref: str) -> set[str]:
    return set(run_command(["git", "diff", "--name-only", base_ref, head_ref]).splitlines())


def select_affected(family: str, entries: list[dict[str, Any]], previous_entries: list[dict[str, Any]],
                    changed_paths: set[str], target: str = "") -> list[dict[str, Any]]:
    current = _by_name(entries)
    if target:
        if family != "repackage":
            return []
        if target not in current:
            raise ImagePlanError(f"unknown repackage image: {target}")
        return [current[target]]
    previous = _by_name(previous_entries)
    selected = {name for name, entry in current.items() if previous.get(name) != entry}
    common = {".dockerignore", ".github/workflows/publish-image.yml", ".github/workflows/publish-images.yml", "scripts/image_plan.py"}
    if family == "repackage":
        types: set[str] = {"node", "python", "docker"} if changed_paths & common else set()
        if changed_paths & {"Dockerfile.base-node", "repackaging/Dockerfile.mcp-node"}: types.add("node")
        if changed_paths & {"Dockerfile.base-python", "repackaging/Dockerfile.mcp-python"}: types.add("python")
        if changed_paths & {
            ".github/workflows/base-images.yml",
            "Dockerfile.mmmcp",
            "scripts/mmmcp.sh",
        }:
            types.update(("node", "python"))
        if "repackaging/Dockerfile.mcp-docker" in changed_paths: types.add("docker")
        selected.update(name for name, entry in current.items() if entry["image"].get("type") in types)
    else:
        if changed_paths & common:
            selected.update(current)
        for name, entry in current.items():
            image = entry["image"]
            owned = {image.get("dockerfile"), *image.get("paths", [])}
            if changed_paths & {path for path in owned if isinstance(path, str)}:
                selected.add(name)
    return [entry for entry in entries if entry["image"]["name"] in selected]


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select, plan, and inspect MCP image publications."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser(
        "plan",
        help="create a publication plan for one already-selected image",
        description=(
            "Create a complete publication plan for one image. Versioned images "
            "receive the next registry-backed {version}-obotN tag; unversioned "
            "utilities use the supplied Git ref. The JSON result is consumed by "
            "the reusable image publisher."
        ),
    )
    plan.add_argument("--family", required=True, help="repackage, repository, or utility")
    plan.add_argument("--image-json", required=True, help="one normalized manifest image as JSON")
    plan.add_argument("--registry-prefix", required=True, help="registry path preceding the image name")
    plan.add_argument("--ref-name", required=True, help="Git branch or tag name; used by unversioned utilities")
    plan.add_argument("--ref-type", required=True, help="Git ref type (branch or tag); used by unversioned utilities")
    plan.add_argument("--source-revision", required=True, help="source commit recorded as an OCI image label")

    select = commands.add_parser(
        "select",
        help="select manifest images affected by a Git diff or manual target",
        description=(
            "Read a YAML manifest and emit a JSON build matrix containing only "
            "affected images. Automatic selection compares current entries and "
            "paths with a previous Git ref; --target selects one repackage for a "
            "manual rebuild."
        ),
    )
    select.add_argument("--family", required=True, help="manifest family to normalize and select")
    select.add_argument("--manifest", required=True, help="path to the current YAML image manifest")
    select.add_argument("--previous-ref", default="", help="Git ref containing the previous manifest; omit for manual selection")
    select.add_argument("--base-ref", default="", help="base Git ref for changed-path selection")
    select.add_argument("--head-ref", default="", help="head Git ref for changed-path selection")
    select.add_argument("--target", default="", help="exact repackage name for a manual rebuild")

    matrix = commands.add_parser(
        "matrix",
        help="emit every normalized image in one manifest family",
        description=(
            "Read a YAML manifest and emit every image belonging to the requested "
            "family as a JSON matrix. This intentionally ignores Git diffs and is "
            "used for release-tag utility builds and full security rescans."
        ),
    )
    matrix.add_argument("--family", required=True, help="manifest family to normalize")
    matrix.add_argument("--manifest", required=True, help="path to the YAML image manifest")

    changes = commands.add_parser(
        "changes",
        help="emit changed repository paths between two Git refs",
        description=(
            "Run git diff --name-only for two refs and emit a sorted JSON path "
            "array. The coordinator uses this for shared-base rebuild and "
            "security-rescan decisions outside individual image selection."
        ),
    )
    changes.add_argument("--base-ref", required=True, help="base Git ref")
    changes.add_argument("--head-ref", required=True, help="head Git ref")

    current = commands.add_parser(
        "current",
        help="resolve the highest published immutable revision tag",
        description=(
            "List registry tags for one image/version and print the highest exact "
            "{version}-obotN tag. This does not allocate a revision and is used by "
            "scan-only workflows."
        ),
    )
    current.add_argument("--repository", required=True, help="full image repository without a tag")
    current.add_argument("--version", required=True, help="application version whose revisions should be inspected")
    return parser


def main() -> int:
    args = create_parser().parse_args()
    try:
        if args.command == "plan":
            result = plan_image(Path("."), args.family, json.loads(args.image_json), args.registry_prefix,
                                args.ref_name, args.ref_type, args.source_revision)
        elif args.command == "select":
            if bool(args.base_ref) != bool(args.head_ref):
                raise ImagePlanError("select requires both --base-ref and --head-ref")
            entries = read_manifest(Path(args.manifest), args.family)
            previous = (
                read_manifest(Path(args.manifest), args.family, args.previous_ref)
                if args.previous_ref
                else []
            )
            paths = changed_paths(args.base_ref, args.head_ref) if args.base_ref else set()
            result = select_affected(args.family, entries, previous, paths, args.target)
        elif args.command == "matrix":
            result = read_manifest(Path(args.manifest), args.family)
        elif args.command == "changes":
            result = sorted(changed_paths(args.base_ref, args.head_ref))
        else:
            result = current_revision_tag(args.repository, args.version)
        print(result if isinstance(result, str) else json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, ImagePlanError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
