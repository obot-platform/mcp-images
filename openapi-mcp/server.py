"""Serve a fixed OpenAPI snapshot over stateless Streamable HTTP."""

import asyncio
import logging
import os
import signal
from contextlib import contextmanager

from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap
from fastmcp.server.transforms.search import BM25SearchTransform
from starlette.responses import JSONResponse

from config import ConfigError, json_object, load_spec, prepare
from network import APIClient, IsolatedDirector


class StartupTimeout(BaseException):
    """Escape library handlers that catch Exception and continue conversion."""


def create_server(document, config, client):
    def isolate_headers(route, tool):
        tool._director = IsolatedDirector(tool._director)

    mappings = [RouteMap(
        methods=[rule["method"]] if "method" in rule else "*",
        pattern=rule.get("pathPattern", ".*"),
        tags={rule["tag"]} if "tag" in rule else set(),
        mcp_type=MCPType.EXCLUDE,
    ) for rule in config.exclude]
    server = FastMCP.from_openapi(
        openapi_spec=document, client=client, name=document.get("info", {}).get("title", "OpenAPI"),
        route_maps=mappings, mcp_component_fn=isolate_headers, mask_error_details=True,
    )
    if config.tool_search:
        server.add_transform(BM25SearchTransform())

    @server.custom_route("/healthz", methods=["GET"])
    async def health(request):
        return JSONResponse({"status": "ok"})

    return server


@contextmanager
def startup_deadline(seconds=30):
    # Conversion is synchronous. An asyncio timeout cannot interrupt a parser or
    # regex occupying the event loop. The image runs on POSIX in the main thread.
    def expired(signum, frame):
        raise StartupTimeout()

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


async def serve():
    # Library diagnostics can include tool arguments and full upstream errors.
    # Keep production logs to lifecycle messages, without request/access logging.
    for name in list(logging.root.manager.loggerDict):
        if name.startswith(("fastmcp", "httpx", "httpcore", "mcp")):
            logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        try:
            port = int(os.environ.get("PORT", "8080"))
        except ValueError:
            raise ConfigError("PORT must be an integer") from None
        if not 1 <= port <= 65535:
            raise ConfigError("PORT must be between 1 and 65535")
        with startup_deadline():
            document, config = prepare(
                load_spec(os.environ.get("OPENAPI_SPEC_FILE", "")),
                json_object(os.environ.get("OPENAPI_CONFIG_JSON", "{}"), "OPENAPI_CONFIG_JSON"),
            )
            client = APIClient(config)
            try:
                server = create_server(document, config, client)
            except BaseException:
                await client.aclose()
                raise
        async with client:
            await server.run_http_async(host="0.0.0.0", port=port, path="/mcp",
                                        stateless_http=True, show_banner=False,
                                        uvicorn_config={"access_log": False})
    except ConfigError as error:
        raise SystemExit(f"Invalid configuration: {error}") from None
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
        # Library validation errors may contain arbitrary document content.
        raise SystemExit("Invalid OpenAPI document or configuration; check the supported input contract") from None
    except StartupTimeout:
        raise SystemExit("Schema conversion exceeded 30 seconds") from None


if __name__ == "__main__":
    asyncio.run(serve())
