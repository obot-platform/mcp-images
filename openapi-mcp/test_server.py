import asyncio
import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from pathlib import Path

import httpx
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from config import prepare
from network import APIClient
from server import StartupTimeout, create_error_server, create_server, serve, startup_deadline
from test_config import SPEC


def response(data, status=200, **headers):
    return httpx.Response(status, stream=httpx.ByteStream(json.dumps(data).encode()),
                          headers={"content-type": "application/json", **headers})


@asynccontextmanager
async def running_http(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_startup_stays_alive_and_reports_safe_errors(self):
        cases = (
            ({"OPENAPI_SPEC_FILE": ""}, "OPENAPI_SPEC_FILE must name a readable"),
            ({"OPENAPI_CONFIG_JSON": '{"baseURL":"http://localhost"}'},
             "Local destinations are prohibited"),
            ({"OPENAPI_CONFIG_JSON": '{"baseURL":"https://do-not-log-this-secret.test",'},
             "OPENAPI_CONFIG_JSON must contain valid JSON"),
        )
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "openapi.json"
            filename.write_text(json.dumps(SPEC), encoding="utf-8")
            invalid_filename = Path(directory) / "invalid-openapi.json"
            invalid_filename.write_text(json.dumps({"info": {"title": "do-not-log-this-secret"}}),
                                        encoding="utf-8")
            cases += (({"OPENAPI_SPEC_FILE": str(invalid_filename)},
                       "Supported OpenAPI versions"),)
            for overrides, expected in cases:
                with self.subTest(expected=expected):
                    with socket.socket() as sock:
                        sock.bind(("127.0.0.1", 0))
                        port = sock.getsockname()[1]
                    env = {"PORT": str(port), "OPENAPI_SPEC_FILE": str(filename),
                           "OPENAPI_CONFIG_JSON": "{}", **overrides}
                    process = subprocess.Popen(
                        [sys.executable, str(Path(__file__).parent / "server.py")],
                        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                    try:
                        async with httpx.AsyncClient(trust_env=False) as http:
                            for _ in range(100):
                                self.assertIsNone(process.poll(), "container exited during startup")
                                try:
                                    live = await http.get(f"http://127.0.0.1:{port}/healthz")
                                    break
                                except httpx.ConnectError:
                                    await asyncio.sleep(0.05)
                            else:
                                self.fail("container did not start HTTP")
                            self.assertEqual(live.status_code, 200)
                            ready = await http.get(f"http://127.0.0.1:{port}/readyz")
                            self.assertEqual(ready.status_code, 503)
                            self.assertIn(expected, ready.text)
                            initialize = await http.post(
                                f"http://127.0.0.1:{port}/mcp",
                                headers={"accept": "application/json, text/event-stream"},
                                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                                 "clientInfo": {"name": "test", "version": "1"}}},
                            )
                            self.assertIn(expected, initialize.text)
                            self.assertIn('"code":-32000', initialize.text)
                            for method, params in (("tools/list", {}),
                                                   ("tools/call", {"name": "getItem", "arguments": {"id": "1"}})):
                                denied = await http.post(
                                    f"http://127.0.0.1:{port}/mcp",
                                    headers={"accept": "application/json, text/event-stream"},
                                    json={"jsonrpc": "2.0", "id": 2, "method": method,
                                          "params": params},
                                )
                                self.assertIn(expected, denied.text)
                                self.assertNotIn("getItem", denied.text)
                            self.assertIsNone(process.poll())
                    finally:
                        process.terminate()
                        stdout, stderr = process.communicate(timeout=5)
                    self.assertIn(expected, stderr)
                    self.assertNotIn("do-not-log-this-secret", stdout + stderr + ready.text + initialize.text)
                    self.assertNotIn("Traceback", stderr)

    async def test_conversion_failures_become_safe_error_state(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "openapi.json"
            filename.write_text(json.dumps(SPEC), encoding="utf-8")
            for failure, expected in (
                (RuntimeError("do-not-log-this-secret"), "OpenAPI conversion failed"),
                (StartupTimeout(), "Schema conversion exceeded 30 seconds"),
            ):
                with self.subTest(expected=expected):
                    with patch.dict(os.environ, {"OPENAPI_SPEC_FILE": str(filename),
                                              "OPENAPI_CONFIG_JSON": "{}", "PORT": "8080"}, clear=True), \
                         patch("server.create_server", side_effect=failure), \
                         patch("server.FastMCP.run_http_async", new_callable=AsyncMock), \
                         patch("server.create_error_server", wraps=create_error_server) as fallback, \
                         self.assertLogs("server", level="ERROR") as logs:
                        await serve()
                    fallback.assert_called_once()
                    self.assertIn(expected, fallback.call_args.args[0])
                    self.assertNotIn("do-not-log-this-secret", " ".join(logs.output))

    async def test_invalid_port_remains_fatal(self):
        with patch.dict(os.environ, {"PORT": "invalid"}, clear=True):
            with self.assertRaisesRegex(SystemExit, "PORT must be an integer"):
                await serve()

    async def test_versions_and_native_path_query_body_headers(self):
        calls = []

        def backend(request):
            calls.append(request)
            return response({"ok": True})

        for version in ("3.0.0", "3.0.3", "3.0.4", "3.1.0", "3.1.1", "3.1.2"):
            spec = copy.deepcopy(SPEC)
            spec["openapi"] = version
            document, config = prepare(spec, {"baseURL": "https://override.test/base"})
            async with APIClient(config, transport=httpx.MockTransport(backend)) as http:
                async with Client(create_server(document, config, http)) as client:
                    tools = await client.list_tools()
                    self.assertEqual(len(tools), 6)
                    tool = next(t for t in tools if t.name == "getItem")
                    self.assertEqual(tool.description, "Get an item")
                    await client.call_tool("getItem", {"id": "abc", "detail": "full", "X-View": "compact"})
                    self.assertEqual(str(calls[-1].url), "https://override.test/base/items/abc?detail=full")
                    self.assertEqual(calls[-1].headers["X-View"], "compact")
                    await client.call_tool("createItem", {"title": "hello"})
                    self.assertEqual(json.loads(calls[-1].content), {"title": "hello"})

    async def test_upstream_response_does_not_require_matching_openapi_output_schema(self):
        spec = copy.deepcopy(SPEC)
        spec["paths"]["/items/{id}"]["get"]["responses"]["200"]["content"] = {
            "application/json": {"schema": {
                "type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]
            }}
        }

        def backend(request):
            return response({"id": "upstream-string"})

        for tool_search in (False, True):
            with self.subTest(tool_search=tool_search):
                document, config = prepare(spec, {"toolSearch": tool_search})
                async with APIClient(config, transport=httpx.MockTransport(backend)) as http:
                    async with Client(create_server(document, config, http)) as client:
                        tool, args = (("call_tool", {"name": "getItem", "arguments": {"id": "1"}})
                                      if tool_search else ("getItem", {"id": "1"}))
                        result = await client.call_tool(tool, args, raise_on_error=False)
                        self.assertFalse(result.is_error, str(result))
                        self.assertIn("upstream-string", str(result))

    async def test_exclusions_all_invocation_paths(self):
        cases = [
            ([{"method": "DELETE"}, {"pathPattern": "^/admin/"}, {"tag": "internal"}],
             {"deleteItem", "adminStatus", "internalStatus"}),
            ([{"method": "POST", "pathPattern": "^/items$"}], {"createItem"}),
            ([{"tag": "Internal"}], set()),
        ]
        for rules, excluded in cases:
            document, config = prepare(SPEC, {"toolSearch": True, "exclude": rules})
            calls = []

            def backend(request):
                calls.append(request)
                return response({"ok": True})

            async with APIClient(config, transport=httpx.MockTransport(backend)) as http:
                async with Client(create_server(document, config, http)) as client:
                    self.assertEqual({t.name for t in await client.list_tools()}, {"search_tools", "call_tool"})
                    for name in excluded:
                        found = await client.call_tool("search_tools", {"query": name})
                        self.assertNotIn(name, str(found))
                        for tool, args in ((name, {"id": "1"}),
                                           ("call_tool", {"name": name, "arguments": {"id": "1"}})):
                            result = await client.call_tool(tool, args, raise_on_error=False)
                            self.assertTrue(result.is_error)
                    self.assertEqual(calls, [])
                    for tool, args in (("getItem", {"id": "1"}),
                                       ("call_tool", {"name": "getItem", "arguments": {"id": "1"}})):
                        result = await client.call_tool(tool, args)
                        self.assertFalse(result.is_error)
                    self.assertEqual(len(calls), 2)

    async def test_direct_mode_exclusions_cannot_be_called(self):
        document, config = prepare(SPEC, {"exclude": [{"method": "DELETE"}]})
        calls = []

        def backend(request):
            calls.append(request)
            return response({"ok": True})

        async with APIClient(config, transport=httpx.MockTransport(backend)) as http:
            async with Client(create_server(document, config, http)) as client:
                self.assertNotIn("deleteItem", {tool.name for tool in await client.list_tools()})
                result = await client.call_tool("deleteItem", {"id": "1"}, raise_on_error=False)
                self.assertTrue(result.is_error)
                self.assertEqual(calls, [])

    async def test_http_concurrent_credentials_health_and_cookie_isolation(self):
        for search in (False, True):
            calls = []
            both_started = asyncio.Event()

            async def backend(request):
                calls.append(request)
                if len(calls) >= 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), 5)
                return response({"ok": True}, **{"set-cookie": "session=not-for-another-user"})

            document, config = prepare(SPEC, {"toolSearch": search,
                                             "credentialHeaders": ["Authorization", "X-Key"]})
            async with APIClient(config, transport=httpx.MockTransport(backend)) as http:
                server = create_server(document, config, http)
                async with running_http(server.http_app(stateless_http=True)) as url:
                    async with httpx.AsyncClient(trust_env=False) as health:
                        self.assertEqual((await health.get(url + "/healthz")).json(), {"status": "ok"})
                        self.assertEqual((await health.get(url + "/readyz")).json(), {"status": "ok"})

                    async def user(key, missing=False, direct=False):
                        headers = {"Authorization": f"Bearer {key}", "X-Key": key,
                                   "X-Unwanted": "login-token", "Cookie": "session=client",
                                   "X-View": "incoming-not-an-argument"}
                        if missing:
                            del headers["X-Key"]
                        async with Client(StreamableHttpTransport(url + "/mcp", headers=headers)) as client:
                            tools = await client.list_tools()
                            self.assertNotIn(key, str(tools))
                            tool, args = ("call_tool", {"name": "getItem", "arguments": {"id": key}}) if search and not direct else ("getItem", {"id": key})
                            return await client.call_tool(tool, args, raise_on_error=False)

                    results = await asyncio.gather(user("user-a-secret"), user("user-b-secret"))
                    self.assertTrue(all(not r.is_error for r in results))
                    missing = await user("missing-secret", missing=True)
                    self.assertTrue(missing.is_error)
                    self.assertEqual(len(calls), 2)
                    await user("user-c-secret", direct=True)  # fresh connection and hidden direct tool
                    self.assertEqual(len(calls), 3)
                    for request in calls:
                        key = request.url.path.rsplit("/", 1)[1]
                        self.assertEqual(request.headers["Authorization"], f"Bearer {key}")
                        self.assertEqual(request.headers["X-Key"], key)
                        for header in ("X-Unwanted", "Cookie", "X-View", "Mcp-Session-Id"):
                            self.assertNotIn(header, request.headers)
                    self.assertFalse(http.cookies)

    async def test_upstream_failures_limits_and_echoes(self):
        async def slow(request):
            await asyncio.sleep(1)
            return response({})

        cases = [
            (lambda _: response({"secret": "test-credential"}, 401), "401"),
            (lambda _: response({}, 302, location="https://other.test"), "redirect"),
            (lambda _: response({"secret": "test-credential"}), "withheld"),
            (lambda _: response({"secret": "test-credential" * 100}), "exceeds"),
            (lambda _: response({}, **{"content-encoding": "gzip"}), "identity"),
            (slow, "timed out"),
        ]
        document, config = prepare(SPEC, {"credentialHeaders": ["Authorization"]})
        for backend, message in cases:
            with self.subTest(message=message), patch("network.get_http_headers", return_value={"authorization": "Bearer test-credential"}), patch("network.MAX_RESPONSE", 100), patch("network.REQUEST_TIMEOUT", 0.05):
                async with APIClient(config, transport=httpx.MockTransport(backend)) as http:
                    async with Client(create_server(document, config, http)) as client:
                        result = await client.call_tool("getItem", {"id": "1"}, raise_on_error=False)
                        self.assertTrue(result.is_error)
                        self.assertIn(message, str(result))
                        self.assertNotIn("test-credential", str(result))

    def test_synchronous_conversion_timeout(self):
        with self.assertRaises(StartupTimeout), startup_deadline(0.01):
            while True:
                pass


if __name__ == "__main__":
    unittest.main()
