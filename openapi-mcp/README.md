# OpenAPI MCP wrapper

Serve an OpenAPI document as MCP tools using FastMCP. The wrapper reads a mounted
JSON schema once at startup and exposes Streamable HTTP at `/mcp`.

## Configuration

| Environment variable | Value |
| --- | --- |
| `OPENAPI_SPEC_FILE` | Required path to an OpenAPI JSON file, at most 1 MiB |
| `OPENAPI_CONFIG_JSON` | Settings object, at most 96 KiB; defaults to `{}` |
| `PORT` | Listening port; defaults to `8080` |

`OPENAPI_CONFIG_JSON` accepts:

| Setting | Description |
| --- | --- |
| `baseURL` | Override the API destination; defaults to the specification's first usable server URL |
| `credentialHeaders` | Required credential header names to forward from each MCP request; defaults to `[]`. Requires HTTPS; values and prefixes are forwarded unchanged |
| `toolSearch` | Expose `search_tools` and `call_tool` instead of listing API tools; defaults to `false` |
| `exclude` | Disable operations matching `method`, `pathPattern` (regex), or `tag`; defaults to `[]`. Obot permits these settings only with Tool Search; the wrapper applies any rules it receives in either mode |

Exclusion fields within a rule must all match; any matching rule excludes the
operation. For example:

```json
{"toolSearch": true, "exclude": [{"method": "DELETE"}, {"pathPattern": "^/admin/"}]}
```

## Health and startup errors

`/healthz` reports process liveness and is used by the image health check.
`/readyz` returns `{"status":"ok"}` when the OpenAPI tools are ready. If the
schema or settings cannot be loaded or converted, the process stays running:
`/healthz` still succeeds, `/readyz` returns HTTP 503 with a safe error, and
MCP requests receive that error without exposing any tools. Fix the mounted
snapshot or settings and restart the deployment to retry conversion. An invalid
`PORT` remains fatal because the process cannot bind its configured listener.

## Example

Build and run with Frankfurter's currency API from the repository root:

```sh
docker build --build-arg UV_IMAGE="$(cat UV_IMAGE)" \
  -t openapi-mcp:test -f mcp-servers/Dockerfile.openapi-mcp .

schema_dir="$(cd /tmp && pwd -P)"
curl -fsS https://api.frankfurter.dev/v2/openapi.json -o "$schema_dir/frankfurter-openapi.json"
docker run --rm --name frankfurter-mcp \
  -p 127.0.0.1:8086:8080 \
  --mount "type=bind,source=$schema_dir/frankfurter-openapi.json,target=/files/openapi.json,readonly" \
  -e OPENAPI_SPEC_FILE=/files/openapi.json \
  -e 'OPENAPI_CONFIG_JSON={"toolSearch":false}' \
  openapi-mcp:test
```

Connect a Streamable HTTP MCP client to `http://localhost:8086/mcp` and invoke
`getRate` with `{"base":"EUR","quote":"USD","date":"2024-01-15"}`.
