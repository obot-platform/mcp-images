# OpenAPI MCP wrapper

Serve an OpenAPI document as MCP tools using FastMCP. The wrapper reads a mounted
JSON schema once at startup and exposes Streamable HTTP at `/mcp`.

## Configuration

| Environment variable | Value |
| --- | --- |
| `OPENAPI_SPEC_FILE` | Required path to an OpenAPI JSON file |
| `OPENAPI_BASE_URL` | Optional API destination override; defaults to the specification's first usable server URL |
| `OPENAPI_CREDENTIAL_HEADERS` | Optional comma-separated credential header names, such as `Authorization, X-API-Key`; defaults to none. Requires HTTPS; values and prefixes are forwarded unchanged |
| `PORT` | Listening port; defaults to `8080` |

The header setting lists names only. Obot sends their values on each MCP request.
The wrapper exposes generated API operations directly as MCP tools.

## Health and startup errors

`/healthz` reports process liveness and is used by the image health check.
`/readyz` returns `{"status":"ok"}` when the OpenAPI tools are ready. If the
schema or settings cannot be loaded or converted, the process stays running:
`/healthz` still succeeds, `/readyz` returns HTTP 503 with a safe error, and
MCP requests receive that error without exposing any tools. Fix the mounted
snapshot or settings and restart the deployment to retry conversion. An invalid
`PORT` is fatal because the process cannot bind its configured listener.

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
  openapi-mcp:test
```

Connect a Streamable HTTP MCP client to `http://localhost:8086/mcp` and invoke
`getRate` with `{"base":"EUR","quote":"USD","date":"2024-01-15"}`.
