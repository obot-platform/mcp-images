#!/usr/bin/env bash

set -euo pipefail

initial_sbom_commit="2c0e7b5"
execute=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [--execute]

Find version-specific dependency snapshot correlators from images.yaml history.
The default is a dry run; --execute submits an empty replacement for each one.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --execute)
            execute=true
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

for command in git mktemp ruby sort; do
    if ! command -v "$command" >/dev/null 2>&1; then
        printf 'Required command not found: %s\n' "$command" >&2
        exit 1
    fi
done

if ! git cat-file -e "${initial_sbom_commit}^{commit}" 2>/dev/null; then
    printf 'Initial SBOM commit not found: %s\n' "$initial_sbom_commit" >&2
    exit 1
fi

versions_file="$(mktemp)"
trap 'rm -f "$versions_file"' EXIT

commits=("$initial_sbom_commit")
while IFS= read -r commit; do
    commits+=("$commit")
done < <(git rev-list --reverse "${initial_sbom_commit}..HEAD" -- repackaging/images.yaml)

for commit in "${commits[@]}"; do
    if ! versions="$(
        git show "${commit}:repackaging/images.yaml" |
            ruby -ryaml -e '
            data = YAML.safe_load(STDIN.read, aliases: true) || {}
            data.fetch("images", []).each do |image|
              next unless image["type"] && image["name"] && image["version"]
              puts [image["name"], image["version"]].join("\t")
            end
        ' 2>/dev/null
    )"; then
        printf 'Skipping malformed images.yaml at commit %s\n' "$commit" >&2
        continue
    fi
    printf '%s\n' "$versions" >>"$versions_file"
done

sorted_correlators=()
while IFS=$'\t' read -r name version; do
    [[ -n "$name" && -n "$version" ]] || continue
    sorted_correlators+=("Repackage MCP Images_repackage_sbom-${name}-${version}.spdx.json")
done < <(sort -u "$versions_file")

printf 'Found %d legacy dependency snapshot correlators.\n' "${#sorted_correlators[@]}"

if [[ "$execute" == false ]]; then
    printf '%s\n' "${sorted_correlators[@]}"
    printf '\nDry run complete. Run %s --execute to retire these snapshots.\n' "$(basename "$0")"
    exit 0
fi

for command in gh jq; do
    if ! command -v "$command" >/dev/null 2>&1; then
        printf 'Required command not found: %s\n' "$command" >&2
        exit 1
    fi
done

repository="$(gh repo view --json nameWithOwner --jq '.nameWithOwner')"
default_branch="$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name')"
default_sha="$(gh api "repos/${repository}/commits/${default_branch}" --jq '.sha')"

for correlator in "${sorted_correlators[@]}"; do
    printf 'Retiring %s\n' "$correlator"
    GITHUB_REPOSITORY="$repository" \
    GITHUB_REF="refs/heads/${default_branch}" \
    GITHUB_SHA="$default_sha" \
        scripts/submit-empty-dependency-snapshot.sh "$correlator" >/dev/null
done

printf 'Retired %d legacy dependency snapshots.\n' "${#sorted_correlators[@]}"
