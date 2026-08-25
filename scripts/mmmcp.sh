#!/bin/sh

set -e

command="$1"
shift

# JSON is valid YAML. Let jq encode argv exactly, and escape dollars so mmmcp
# does not interpret literal ${NAME} sequences supplied to the child process.
jq -n \
    --arg command "$command" \
    --arg meta_env "${MMMCP_META_ENV:-${NANOBOT_META_ENV:-}}" \
    --args '
        def escape_dollars: gsub("\\$"; "$$");

        ($meta_env | split(",") | map(select(length > 0))) as $env_names
        | {
            listen: ":8099",
            servers: [
                {
                    name: "MCP Server",
                    command: ($command | escape_dollars),
                    args: ($ARGS.positional | map(escape_dollars))
                }
                + if ($env_names | length) > 0 then {
                    env: ($env_names | map({key: ., value: ("${" + . + "}")}) | from_entries)
                } else {} end
            ]
        }
    ' -- "$@" > /home/user/mmmcp.yaml

exec mmmcp --config /home/user/mmmcp.yaml
