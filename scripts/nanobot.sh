#!/bin/sh

set -e

# Extract command from first argument
command="$1"
shift

# Build args array for YAML
yaml_args="["
first_arg=true
for arg in "$@"; do
    if [ "$first_arg" = true ]; then
        yaml_args="${yaml_args}\"${arg}\""
        first_arg=false
    else
        yaml_args="${yaml_args}, \"${arg}\""
    fi
done
yaml_args="${yaml_args}]"

# Start building the YAML content
yaml_content="publish:
  mcpServers: [server]

mcpServers:
  server:
    command: ${command}
    args: ${yaml_args}
"

# NANOBOT_META_ENV contains a comma-separated list of environment variables that are used by the nanobot
# For each one, we need to append -e ENV_VAR_NAME to the command line arguments for nanobot
# The environment variables are added to the YAML file and the command line arguments for nanobot
nanobot_args=""
if [ -n "${NANOBOT_META_ENV}" ]; then
    yaml_content="${yaml_content}
    env:"
    for env_var in $(echo "${NANOBOT_META_ENV}" | tr ',' ' '); do
        yaml_content="${yaml_content}
      ${env_var}: \${${env_var}}"
        nanobot_args="${nanobot_args} -e ${env_var}"
    done
fi

# Write the YAML file
echo "$yaml_content" > /home/user/nanobot.yaml

nanobot run --listen-address :8099 ${nanobot_args} /home/user/nanobot.yaml
