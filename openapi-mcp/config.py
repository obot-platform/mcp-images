"""Prepare an OpenAPI snapshot and enforce the wrapper's runtime boundaries."""

import copy
import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import httpx


class ConfigError(ValueError):
    """A runtime input error whose message is safe to display at startup."""


# Operations can override the document's server URL; remove those overrides below.
METHODS = {"GET", "PUT", "POST", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"}
# Never use HTTP transport/control headers as API credentials or tool parameters.
RESERVED_HEADERS = {
    "host", "cookie", "set-cookie", "content-length", "content-type", "accept",
    "connection", "transfer-encoding", "upgrade", "te", "trailer", "keep-alive",
    "expect", "proxy-authorization", "proxy-authenticate", "accept-encoding",
}


def checked_address(value: str):
    """Enforce outbound network policy for literal IPs and each DNS connection."""
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif (address in ipaddress.ip_network("::/96")
              or address in ipaddress.ip_network("64:ff9b::/96")):
            # IPv4-compatible and well-known NAT64 addresses retain an IPv4 target.
            address = ipaddress.IPv4Address(int(address) & 0xffffffff)
    # The destination may have resolved since Obot validated its hostname.
    # Refuse every non-public answer so the dialer cannot fall back to one.
    if not address.is_global or address.is_multicast or getattr(address, "scope_id", None):
        raise ConfigError("Prohibited outbound destination address")
    return address


def destination(value: str) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in "{}\\\r\n\t"):
        raise ConfigError("OPENAPI_BASE_URL must be an absolute HTTP(S) URL")
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ConfigError("OPENAPI_BASE_URL must be a valid absolute HTTP(S) URL") from None
    if (url.scheme not in ("http", "https") or not url.host or url.userinfo
            or url.query or url.fragment):
        raise ConfigError("OPENAPI_BASE_URL must be absolute HTTP(S), without credentials, query, or fragment")
    if url.host.rstrip(".").lower() == "localhost" or url.host.lower().endswith(".localhost"):
        raise ConfigError("Local destinations are prohibited")
    try:
        ipaddress.ip_address(url.host)
    except ValueError:
        pass  # DNS results are checked again at connection time.
    else:
        checked_address(url.host)
    return str(url).rstrip("/") + "/"


def json_object(raw: str, name: str) -> dict:
    def reject_constant(value):
        raise ValueError("Non-finite JSON number")

    try:
        result = json.loads(raw, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        raise ConfigError(f"{name} must contain valid JSON") from None
    if not isinstance(result, dict):
        raise ConfigError(f"{name} must contain a JSON object")
    return result


def load_spec(filename: str) -> dict:
    """Read one local snapshot, including Kubernetes projected-volume symlinks."""
    if not filename:
        raise ConfigError("OPENAPI_SPEC_FILE must name a readable OpenAPI JSON file")
    try:
        with Path(filename).open("rb") as source:
            raw = source.read()
    except (OSError, ValueError):
        raise ConfigError("Cannot read OPENAPI_SPEC_FILE; check the path and file permissions") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError("OPENAPI_SPEC_FILE must contain UTF-8 JSON") from None
    return json_object(text, "OPENAPI_SPEC_FILE")


@dataclass(frozen=True)
class Config:
    base_url: str
    credential_headers: tuple[str, ...]


def prepare(spec: dict, *, base_url: str | None = None,
            credential_headers: str = "") -> tuple[dict, Config]:
    # Obot validates catalog settings. These checks keep malformed environment
    # values from changing how the shared HTTP client sends credential headers.
    if not isinstance(credential_headers, str):
        raise ConfigError("OPENAPI_CREDENTIAL_HEADERS must be comma-separated header names")
    headers = ([header.strip() for header in credential_headers.split(",")]
               if credential_headers.strip() else [])
    if any(
        not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", h)
        or h.lower() in RESERVED_HEADERS or h.lower().startswith(("mcp-", "sec-", "proxy-"))
        for h in headers
    ):
        raise ConfigError("OPENAPI_CREDENTIAL_HEADERS must list valid non-transport header names")
    headers = tuple(h.lower() for h in headers)
    if len(set(headers)) != len(headers):
        raise ConfigError("Duplicate credential header names")

    document = copy.deepcopy(spec)
    version = document.get("openapi")
    # openapi-pydantic's supported patch labels lag the specifications. Obot
    # validates the input version; FastMCP handles anything outside these ranges.
    if isinstance(version, str) and re.fullmatch(r"3\.0\.[0-4]", version):
        document["openapi"] = "3.0.3"
    elif isinstance(version, str) and re.fullmatch(r"3\.1\.[0-2]", version):
        document["openapi"] = "3.1.1"

    def reference(ref):
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ConfigError("Only local JSON pointer references are supported")
        node = document
        try:
            for part in unquote(ref[2:]).split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                node = node[int(part)] if isinstance(node, list) else node[part]
        except (KeyError, TypeError, IndexError, ValueError):
            raise ConfigError("Unresolved local reference") from None
        return node

    def resolve(node):
        seen = set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                raise ConfigError("Cyclic parameter or security references are unsupported")
            seen.add(ref)
            node = reference(ref)
        return node

    def walk(node):
        if isinstance(node, dict):
            if "$dynamicRef" in node or "$recursiveRef" in node:
                raise ConfigError("Dynamic and recursive JSON Schema references are unsupported")
            if "$ref" in node:
                # The container cannot fetch remote references at runtime.
                if not isinstance(node["$ref"], str) or not node["$ref"].startswith("#/"):
                    raise ConfigError("Only local JSON pointer references are supported")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(document)
    for scheme in document.get("components", {}).get("securitySchemes", {}).values():
        scheme = resolve(scheme)
        if scheme.get("type") == "oauth2":
            continue  # OAuth declarations do not produce forwarded header credentials.
        if scheme.get("type") == "apiKey" and scheme.get("in") == "header":
            name = scheme.get("name", "").lower()
        elif scheme.get("type") == "http" and scheme.get("scheme", "").lower() == "bearer":
            name = "authorization"
        else:
            raise ConfigError("Only header API keys and pre-issued bearer credentials are supported")
        if name not in headers:
            raise ConfigError("Declare every security scheme's header in OPENAPI_CREDENTIAL_HEADERS")

    if base_url:
        base = destination(base_url)
    else:
        base = None
        for server in document.get("servers", []):
            try:
                base = destination(server.get("url"))
                break
            except (ValueError, httpx.InvalidURL):
                continue
        if base is None:
            raise ConfigError("No usable server URL; configure OPENAPI_BASE_URL")
    if headers and httpx.URL(base).scheme != "https":
        raise ConfigError("Credential forwarding requires an HTTPS destination")
    document["servers"] = [{"url": base}]
    for item in document.get("paths", {}).values():
        for owner in [item, *(item[m.lower()] for m in METHODS if m.lower() in item)]:
            owner.pop("servers", None)
            parameters = []
            for parameter in owner.get("parameters", []):
                parameter = resolve(parameter)
                if parameter.get("in") == "header":
                    name = parameter.get("name", "").lower()
                    if name == "authorization" or name in headers:
                        continue
                    if name in RESERVED_HEADERS or name.startswith(("mcp-", "proxy-", "sec-")):
                        raise ConfigError("Transport headers cannot be tool parameters")
                if parameter.get("in") == "cookie":
                    raise ConfigError("Cookie parameters are unsupported")
                parameters.append(parameter)
            owner["parameters"] = parameters
    return document, Config(base, headers)
