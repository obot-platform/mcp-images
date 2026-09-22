"""Confine FastMCP's HTTP execution and attach credentials per request."""

import asyncio
import json
import socket
import ssl
from urllib.parse import quote

import httpcore
import httpx
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from httpcore._backends.auto import AutoBackend

from config import Config, checked_address

REQUEST_TIMEOUT = 30
MAX_RESPONSE = 10 * 1024 * 1024
MAX_CONCURRENT = 32


class CheckedBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, validate all answers, and connect to a validated numeric IP.

    httpcore still performs TLS using the original hostname. AutoBackend is the
    only private httpcore adapter used here; its version is pinned and tested.
    """

    def __init__(self):
        self.backend = AutoBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        async with asyncio.timeout(timeout):
            answers = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM,
            )
            addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
            if not addresses:
                raise ValueError("Destination did not resolve")
            for address in addresses:
                checked_address(address)
            for address in addresses:
                try:
                    return await self.backend.connect_tcp(
                        address, port, timeout, local_address, socket_options,
                    )
                except (httpcore.ConnectError, httpcore.ConnectTimeout):
                    continue
            raise httpcore.ConnectError("Unable to connect to configured destination")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class CoreStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class CheckedTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(), network_backend=CheckedBackend(),
            max_connections=MAX_CONCURRENT, max_keepalive_connections=MAX_CONCURRENT,
        )

    async def handle_async_request(self, request):
        response = await self.pool.handle_async_request(httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions,
        ))
        return httpx.Response(response.status, headers=response.headers,
                              stream=CoreStream(response.stream), extensions=response.extensions)

    async def aclose(self):
        await self.pool.aclose()


class IsolatedDirector:
    """Capture native generated headers before FastMCP adds incoming MCP headers.

    This deliberately small adapter depends on FastMCP 3.4.7's tool._director.
    Execution and all OpenAPI serialization remain owned by FastMCP.
    """

    def __init__(self, director):
        self.director = director

    def build(self, *args, **kwargs):
        request = self.director.build(*args, **kwargs)
        request.extensions["openapi.generated_headers"] = list(request.headers.raw)
        return request


def origin(url):
    return url.scheme, url.host, url.port


def contains_credentials(content: bytes, credentials: dict[str, str]) -> bool:
    text = content.decode("utf-8", errors="replace")
    try:
        decoded = json.dumps(json.loads(text), ensure_ascii=False)
    except (ValueError, RecursionError):
        decoded = text
    for name, value in credentials.items():
        secrets = [value]
        if name == "authorization" and " " in value:
            secrets.append(value.split(" ", 1)[1])
        for secret in secrets:
            if secret and any(candidate in text or candidate in decoded for candidate in (
                secret, quote(secret, safe=""), json.dumps(secret, ensure_ascii=True)[1:-1],
            )):
                return True
    return False


class APIClient(httpx.AsyncClient):
    def __init__(self, config: Config, *, transport=None):
        self.api_transport = transport or CheckedTransport()
        super().__init__(base_url=config.base_url, transport=self.api_transport,
                         timeout=REQUEST_TIMEOUT, trust_env=False, follow_redirects=False)
        self.config = config
        self.slots = asyncio.Semaphore(MAX_CONCURRENT)

    async def send(self, request, **kwargs):
        # Do not call AsyncClient.send: it extracts response cookies into a shared
        # jar. The transport owns connection pooling but no credential state.
        if origin(request.url) != origin(self.base_url):
            raise ToolError("Request destination differs from the configured API")
        generated = request.extensions.get("openapi.generated_headers")
        if generated is None:
            raise ToolError("Missing generated request metadata")
        incoming = get_http_headers(include_all=True)
        credentials = {}
        for name in self.config.credential_headers:
            value = incoming.get(name)
            if not value or not value.strip():
                raise ToolError(f"Missing required credential header: {name}")
            credentials[name] = value
        request.headers = httpx.Headers(generated)
        for name in ("cookie", "authorization", *self.config.credential_headers):
            request.headers.pop(name, None)
        request.headers.update(credentials)
        # Prevent decompression bombs: only accept identity-encoded bodies. The
        # decoded and wire byte limits are consequently identical.
        request.headers["accept-encoding"] = "identity"
        request.extensions["timeout"] = dict.fromkeys(("connect", "read", "write", "pool"), REQUEST_TIMEOUT)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT), self.slots:
                response = await self.api_transport.handle_async_request(request)
                try:
                    if 300 <= response.status_code < 400:
                        raise ToolError("Upstream redirects are disabled")
                    if response.status_code >= 400:
                        raise ToolError(f"Upstream API returned HTTP {response.status_code}")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ToolError("Upstream must honor Accept-Encoding: identity")
                    chunks = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(chunks) + len(chunk) > MAX_RESPONSE:
                            raise ToolError("Upstream response exceeds 10 MiB")
                        chunks.extend(chunk)
                    content = bytes(chunks)
                    if contains_credentials(content, credentials):
                        raise ToolError("Upstream response contained a credential and was withheld")
                    return httpx.Response(response.status_code, content=content, request=request,
                                          headers={"content-type": response.headers.get("content-type", "")})
                finally:
                    await response.aclose()
        except ToolError:
            raise
        except (TimeoutError, httpx.TimeoutException, httpcore.TimeoutException):
            raise ToolError("Upstream request timed out") from None
        except Exception:
            # Network errors can include URLs, bodies, or raw header values.
            raise ToolError("Upstream request failed or destination was rejected") from None
