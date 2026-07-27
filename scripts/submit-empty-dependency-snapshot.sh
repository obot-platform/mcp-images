#!/usr/bin/env bash

set -euo pipefail

dry_run=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [--dry-run] CORRELATOR

Submit an empty GitHub dependency snapshot for CORRELATOR. The latest snapshot
for a correlator and detector replaces the previous dependency inventory.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            dry_run=true
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        --*)
            printf 'Unknown argument: %s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
        *)
            break
            ;;
    esac
    shift
done

if [[ $# -ne 1 ]]; then
    usage >&2
    exit 2
fi

correlator="$1"

for command in gh jq git; do
    if ! command -v "$command" >/dev/null 2>&1; then
        printf 'Required command not found: %s\n' "$command" >&2
        exit 1
    fi
done

repository="${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner --jq '.nameWithOwner')}"
sha="${GITHUB_SHA:-$(git rev-parse HEAD)}"
ref="${GITHUB_REF:-}"
if [[ -z "$ref" ]]; then
    branch="$(git branch --show-current)"
    ref="refs/heads/${branch:-main}"
fi

run_id="${GITHUB_RUN_ID:-manual-$(date -u +%Y%m%d%H%M%S)}"
server_url="${GITHUB_SERVER_URL:-https://github.com}"
scanned="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

payload="$(
    jq -n \
        --arg sha "$sha" \
        --arg ref "$ref" \
        --arg correlator "$correlator" \
        --arg run_id "$run_id" \
        --arg job_url "${server_url}/${repository}/actions/runs/${run_id}" \
        --arg scanned "$scanned" \
        '{
            version: 0,
            sha: $sha,
            ref: $ref,
            job: {
                correlator: $correlator,
                id: $run_id,
                html_url: $job_url
            },
            detector: {
                name: "syft",
                version: "retired",
                url: "https://github.com/anchore/syft"
            },
            scanned: $scanned,
            manifests: {}
        }'
)"

if [[ "$dry_run" == true ]]; then
    jq . <<<"$payload"
    exit 0
fi

gh api \
    --method POST \
    "repos/${repository}/dependency-graph/snapshots" \
    --input - <<<"$payload"
