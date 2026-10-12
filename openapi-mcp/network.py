"""Confine FastMCP's HTTP execution and attach credentials per request."""

import asyncio
import json
import socket
import ssl
from urllib.parse import quote, unquote

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
    """Enforce the network policy at connection time, including after DNS changes.

    Checking only the configured URL would leave a DNS-rebinding gap. Resolve
    and check every answer, then connect to one checked IP while httpcore uses
    the original hostname for TLS. AutoBackend is pinned and tested here.
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
    """Bridge httpx to a pool that uses CheckedBackend for every connection.

    httpx's standard transport does not expose httpcore's network_backend.
    """

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

    FastMCP copies arbitrary caller headers onto outbound requests after build;
    APIClient restores this snapshot and then adds only declared credentials.
    This adapter depends on FastMCP 3.4.7's tool._director. Execution and all
    OpenAPI serialization remain owned by FastMCP.
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
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError("Upstream response must be UTF-8 text") from None
    # BOM-less UTF-16/32 can decode as UTF-8 with embedded NULs. FastMCP may
    # later parse those bytes as JSON and expose a credential this scan missed.
    if "\x00" in text:
        raise ToolError("Upstream response must be UTF-8 text")
    try:
        decoded = json.dumps(json.loads(text), ensure_ascii=False)
    except (ValueError, RecursionError):
        decoded = text
    # Percent escapes are case-insensitive and can encode only part of a secret.
    percent_decoded = unquote(decoded)
    for value in credentials.values():
        secrets = [value]
        parts = value.split(maxsplit=1)
        if len(parts) == 2:
            secrets.append(parts[1])
        for secret in secrets:
            if not secret:
                continue
            candidates = (secret, quote(secret, safe=""), json.dumps(secret, ensure_ascii=True)[1:-1])
            if secret in percent_decoded or any(
                candidate in text or candidate in decoded for candidate in candidates
            ):
                return True
    return False


def json_byte_sizes(value):
    """Measure JSON with default separators without building escaped large strings."""
    if isinstance(value, str):
        yield 2  # Surrounding quotes.
        for offset in range(0, len(value), 4096):
            chunk = json.dumps(value[offset:offset + 4096], ensure_ascii=False)
            yield len(chunk.encode("utf-8")) - 2
    elif isinstance(value, dict):
        yield 2 + max(0, len(value) - 1) * 2 + len(value) * 2
        for key, item in value.items():
            yield from json_byte_sizes(key)
            yield from json_byte_sizes(item)
    elif isinstance(value, list):
        yield 2 + max(0, len(value) - 1) * 2
        for item in value:
            yield from json_byte_sizes(item)
    else:
        yield len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def http_error_message(status, body):
    payload = {"status": status, "body": body}
    size = 0
    for part in json_byte_sizes(payload):
        size += part
        if size > MAX_RESPONSE:
            return json.dumps({"status": status, "error": "Serialized upstream error exceeds 10 MiB"},
                              ensure_ascii=False)
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


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
        # Obot owns credential requiredness; the upstream API decides whether
        # requests without credentials are authorized.
        for name in self.config.credential_headers:
            value = incoming.get(name)
            if not value or not value.strip():
                continue
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
                    error_status = response.status_code if response.status_code >= 400 else None
                    try:
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
                    except ToolError as error:
                        if error_status:
                            raise ToolError(json.dumps({"status": error_status, "error": str(error)},
                                                       ensure_ascii=False)) from None
                        raise
                    if error_status:
                        body = content.decode("utf-8")
                        try:
                            message = http_error_message(error_status, json.loads(body))
                        except (ValueError, RecursionError):
                            # Preserve non-JSON bodies as strings; the outer error
                            # remains valid JSON even for malformed upstream JSON.
                            message = http_error_message(error_status, body if body else None)
                        raise ToolError(message)
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
