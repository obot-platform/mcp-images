"""Validate deployment configuration without fetching schemas or references."""

import copy
import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import httpx

class ConfigError(ValueError):
    """A validation error whose message is safe to display at startup."""


MAX_INPUT = 96 * 1024
MAX_SPEC = 1024 * 1024
METHODS = {"GET", "PUT", "POST", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"}
RESERVED_HEADERS = {
    "host", "cookie", "set-cookie", "content-length", "content-type", "accept",
    "connection", "transfer-encoding", "upgrade", "te", "trailer", "keep-alive",
    "expect", "proxy-authorization", "proxy-authenticate", "accept-encoding",
}


def checked_address(value: str):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address):
        address = address.ipv4_mapped or address
    if (address.is_loopback or address.is_link_local or address.is_unspecified
            or address.is_multicast or getattr(address, "scope_id", None)
            or address in ipaddress.ip_network("0.0.0.0/8")):
        raise ConfigError("Local, link-local, unspecified, and multicast destinations are prohibited")
    return address


def destination(value: str) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in "{}\\\r\n\t"):
        raise ConfigError("baseURL must be an absolute HTTP(S) URL")
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ConfigError("baseURL must be a valid absolute HTTP(S) URL") from None
    if (url.scheme not in ("http", "https") or not url.host or url.userinfo
            or url.query or url.fragment):
        raise ConfigError("baseURL must be absolute HTTP(S), without credentials, query, or fragment")
    if url.host.rstrip(".").lower() == "localhost" or url.host.lower().endswith(".localhost"):
        raise ConfigError("Local destinations are prohibited")
    try:
        ipaddress.ip_address(url.host)
    except ValueError:
        pass  # DNS results are checked again at connection time.
    else:
        checked_address(url.host)
    return str(url).rstrip("/") + "/"


def json_object(raw: str, name: str, *, max_bytes: int = MAX_INPUT) -> dict:
    def reject_constant(value):
        raise ValueError("Non-finite JSON number")

    if len(raw.encode()) > max_bytes:
        raise ConfigError(f"{name} exceeds {max_bytes // 1024} KiB")
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
            raw = source.read(MAX_SPEC + 1)
    except (OSError, ValueError):
        raise ConfigError("Cannot read OPENAPI_SPEC_FILE; check the path and file permissions") from None
    if len(raw) > MAX_SPEC:
        raise ConfigError("OPENAPI_SPEC_FILE exceeds 1024 KiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError("OPENAPI_SPEC_FILE must contain UTF-8 JSON") from None
    return json_object(text, "OPENAPI_SPEC_FILE", max_bytes=MAX_SPEC)


@dataclass(frozen=True)
class Config:
    base_url: str
    credential_headers: tuple[str, ...]
    tool_search: bool
    exclude: tuple[dict, ...]


def prepare(spec: dict, settings: dict) -> tuple[dict, Config]:
    if settings.keys() - {"baseURL", "credentialHeaders", "toolSearch", "exclude"}:
        raise ConfigError("Unknown configuration field")
    search = settings.get("toolSearch", False)
    headers = settings.get("credentialHeaders", [])
    rules = settings.get("exclude", [])
    if type(search) is not bool or not isinstance(rules, list):
        raise ConfigError("toolSearch must be a boolean and exclude must be a list")
    if not isinstance(headers, list) or any(
        not isinstance(h, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", h)
        or h.lower() in RESERVED_HEADERS or h.lower().startswith(("mcp-", "sec-", "proxy-"))
        for h in headers
    ):
        raise ConfigError("credentialHeaders must list valid non-transport header names")
    headers = tuple(h.lower() for h in headers)
    if len(set(headers)) != len(headers):
        raise ConfigError("Duplicate credential header names")
    if rules and not search:
        raise ConfigError("Exclusions require toolSearch=true")
    for rule in rules:
        if (not isinstance(rule, dict) or not rule or rule.keys() - {"method", "pathPattern", "tag"}
                or any(not isinstance(v, str) or not v.strip() for v in rule.values())):
            raise ConfigError("Exclusion rules require nonempty method, pathPattern, or tag strings")
        if "method" in rule and rule["method"] not in METHODS:
            raise ConfigError("Exclusion method must be an uppercase HTTP method")
        try:
            re.compile(rule.get("pathPattern", ".*"))
        except re.error:
            raise ConfigError("Invalid exclusion pathPattern regular expression") from None

    document = copy.deepcopy(spec)
    version = document.get("openapi", "")
    if not isinstance(version, str) or not re.fullmatch(r"3\.(0\.[0-4]|1\.[0-2])", version):
        raise ConfigError("Supported OpenAPI versions are 3.0.0–3.0.4 and 3.1.0–3.1.2")
    # openapi-pydantic's supported patch labels lag the specifications.
    document["openapi"] = "3.0.3" if version.startswith("3.0.") else "3.1.1"

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
                reference(node["$ref"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(document)
    for scheme in document.get("components", {}).get("securitySchemes", {}).values():
        scheme = resolve(scheme)
        if scheme.get("type") == "apiKey" and scheme.get("in") == "header":
            name = scheme.get("name", "").lower()
        elif scheme.get("type") == "http" and scheme.get("scheme", "").lower() == "bearer":
            name = "authorization"
        else:
            raise ConfigError("Only header API keys and pre-issued bearer credentials are supported")
        if name not in headers:
            raise ConfigError("Declare every security scheme's header in credentialHeaders")

    if "baseURL" in settings:
        base = destination(settings["baseURL"])
    else:
        base = None
        for server in document.get("servers", []):
            try:
                base = destination(server.get("url"))
                break
            except (ValueError, httpx.InvalidURL):
                continue
        if base is None:
            raise ConfigError("No usable server URL; configure baseURL")
    if headers and httpx.URL(base).scheme != "https":
        raise ConfigError("Credential forwarding requires an HTTPS destination")
    if document.get("webhooks"):
        raise ConfigError("OpenAPI webhooks are unsupported")
    document["servers"] = [{"url": base}]
    for path, item in document.get("paths", {}).items():
        if not path.startswith("/") or path.startswith("//") or "$ref" in item:
            raise ConfigError("Paths must be local templates; referenced path items are unsupported")
        for owner in [item, *(item[m.lower()] for m in METHODS if m.lower() in item)]:
            owner.pop("servers", None)
            if owner.get("callbacks"):
                raise ConfigError("OpenAPI callbacks are unsupported")
            parameters = []
            for parameter in owner.get("parameters", []):
                parameter = resolve(parameter)
                if parameter.get("in") == "header":
                    name = parameter.get("name", "").lower()
                    if name in headers:
                        continue
                    if name in RESERVED_HEADERS or name.startswith(("mcp-", "proxy-", "sec-")):
                        raise ConfigError("Transport headers cannot be tool parameters")
                if parameter.get("in") == "cookie":
                    raise ConfigError("Cookie parameters are unsupported")
                parameters.append(parameter)
            owner["parameters"] = parameters
    return document, Config(base, headers, search, tuple(rules))
