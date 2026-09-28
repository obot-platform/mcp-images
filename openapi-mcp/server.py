"""Serve a fixed OpenAPI snapshot over stateless Streamable HTTP."""

import asyncio
import logging
import os
import signal
from contextlib import contextmanager

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.openapi import MCPType, RouteMap
from fastmcp.server.transforms.search import BM25SearchTransform
from mcp import McpError
from mcp.types import ErrorData
from starlette.responses import JSONResponse

from config import ConfigError, json_object, load_spec, prepare
from network import APIClient, IsolatedDirector


class StartupTimeout(BaseException):
    """Escape library handlers that catch Exception and continue conversion."""


class Unavailable(Middleware):
    """Reject every MCP request while an invalid snapshot is deployed."""

    def __init__(self, reason):
        self.reason = reason

    async def on_request(self, context, call_next):
        raise McpError(ErrorData(code=-32000, message=self.reason))


def add_status_routes(server, error=None):
    @server.custom_route("/healthz", methods=["GET"])
    async def health(request):
        return JSONResponse({"status": "ok"})

    @server.custom_route("/readyz", methods=["GET"])
    async def ready(request):
        if error is not None:
            return JSONResponse({"status": "error", "error": error}, status_code=503)
        return JSONResponse({"status": "ok"})


def create_error_server(reason):
    server = FastMCP(name="OpenAPI unavailable", middleware=[Unavailable(reason)])
    add_status_routes(server, reason)
    return server


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
        validate_output=False,
    )
    if config.tool_search:
        server.add_transform(BM25SearchTransform())

    add_status_routes(server)
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
        port = int(os.environ.get("PORT", "8080"))
    except ValueError:
        raise SystemExit("Invalid configuration: PORT must be an integer") from None
    if not 1 <= port <= 65535:
        raise SystemExit("Invalid configuration: PORT must be between 1 and 65535")

    client = None
    try:
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
    except ConfigError as error:
        reason = f"Invalid configuration: {error}"
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
        # Library validation errors may contain arbitrary document content.
        reason = "Invalid OpenAPI document or configuration; check the supported input contract"
    except StartupTimeout:
        reason = "Schema conversion exceeded 30 seconds"
    except Exception:
        # Conversion libraries may raise other exception types containing schema data.
        reason = "OpenAPI conversion failed; check the supported input contract"
    else:
        reason = None

    if reason is not None:
        logging.getLogger(__name__).error("%s", reason)
        server = create_error_server(reason)
        await server.run_http_async(host="0.0.0.0", port=port, path="/mcp",
                                    stateless_http=True, show_banner=False,
                                    uvicorn_config={"access_log": False})
    else:
        async with client:
            await server.run_http_async(host="0.0.0.0", port=port, path="/mcp",
                                        stateless_http=True, show_banner=False,
                                        uvicorn_config={"access_log": False})


if __name__ == "__main__":
    asyncio.run(serve())
