import asyncio
import socket
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpcore
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastmcp.exceptions import ToolError
from httpcore._backends.auto import AutoBackend

from config import prepare
from network import APIClient, CheckedBackend, CheckedTransport, contains_credentials
from test_config import SPEC


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_checked_connection_retains_original_tls_hostname(self):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.example.test")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("api.example.test")]), critical=False)
                .sign(key, hashes.SHA256()))
        with tempfile.TemporaryDirectory() as directory:
            certfile, keyfile = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ssl.load_cert_chain(certfile, keyfile)
            client_ssl = ssl.create_default_context(cafile=str(certfile))
            names, received = [], []
            server_ssl.set_servername_callback(lambda sock, hostname, context: names.append(hostname))

            async def handler(reader, writer):
                try:
                    received.append(await reader.readuntil(b"\r\n\r\n"))
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"ok":true}')
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=server_ssl)
            port = server.sockets[0].getsockname()[1]
            checked, actual = CheckedBackend(), AutoBackend()

            async def connect(address, target_port, timeout, local_address, socket_options):
                self.assertEqual(address, "8.8.8.8")
                return await actual.connect_tcp("127.0.0.1", target_port, timeout, local_address, socket_options)

            checked.backend = AsyncMock()
            checked.backend.connect_tcp.side_effect = connect
            _, config = prepare(SPEC, base_url=f"https://api.example.test:{port}",
                                credential_headers="Authorization")
            try:
                with patch.object(asyncio.get_running_loop(), "getaddrinfo", new=AsyncMock(return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
                ])), patch("network.CheckedBackend", return_value=checked), patch(
                    "network.ssl.create_default_context", return_value=client_ssl,
                ), patch("network.get_http_headers", return_value={"authorization": "Bearer tls-secret"}):
                    async with APIClient(config, transport=CheckedTransport()) as client:
                        request = httpx.Request("GET", config.base_url)
                        request.extensions["openapi.generated_headers"] = list(request.headers.raw)
                        result = await client.send(request)
                        self.assertEqual(result.json(), {"ok": True})
                self.assertEqual(names, ["api.example.test"])
                self.assertIn(b"authorization: Bearer tls-secret", received[0])
                self.assertIn(f"host: api.example.test:{port}".encode(), received[0].lower())
            finally:
                server.close()
                await server.wait_closed()

    async def test_resolve_once_connect_numeric_and_recheck_new_connections(self):
        backend = CheckedBackend()
        backend.backend = AsyncMock()
        loop = asyncio.get_running_loop()

        def answers(addresses):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]

        with patch.object(loop, "getaddrinfo", new=AsyncMock(side_effect=[
            answers(["8.8.8.8"]), answers(["127.0.0.1"]),
            answers(["8.8.8.8", "169.254.169.254"]), answers(["10.1.2.3"]),
            answers(["127.0.0.1"]),
        ])) as resolve:
            await backend.connect_tcp("api.test", 443, timeout=1)
            backend.backend.connect_tcp.assert_awaited_once_with("8.8.8.8", 443, 1, None, None)
            for host in ("api.test", "api.test", "api.test", "2130706433"):
                with self.assertRaises(ValueError):
                    await backend.connect_tcp(host, 443, timeout=1)
            self.assertEqual(resolve.await_count, 5)
            self.assertEqual(backend.backend.connect_tcp.await_count, 1)

    async def test_all_checked_addresses_can_fail_without_leaking_error(self):
        backend = CheckedBackend()
        backend.backend = AsyncMock()
        backend.backend.connect_tcp.side_effect = httpcore.ConnectError("private details")
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", new=AsyncMock(return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ])):
            with self.assertRaisesRegex(httpcore.ConnectError, "configured destination"):
                await backend.connect_tcp("api.test", 443)

    async def test_origin_escape_and_missing_director_metadata_never_send(self):
        _, config = prepare(SPEC)
        handler = AsyncMock()
        async with APIClient(config, transport=httpx.MockTransport(handler)) as client:
            for url in ("https://other.test/v1", "http://api.example.test/v1", "https://api.example.test:8443/v1"):
                with self.assertRaisesRegex(ToolError, "destination"):
                    await client.send(httpx.Request("GET", url))
            with self.assertRaisesRegex(ToolError, "metadata"):
                await client.send(httpx.Request("GET", config.base_url))
        handler.assert_not_awaited()

    async def test_declared_credentials_override_generated_and_tool_headers(self):
        _, config = prepare(SPEC, credential_headers="X-Key")

        received = []

        async def handler(request):
            received.append(dict(request.headers))
            return httpx.Response(200, stream=httpx.ByteStream(b'{"ok":true}'))

        with patch("network.get_http_headers", return_value={
            "x-key": "real-secret", "x-tool": "caller-supplied",
        }):
            async with APIClient(config, transport=httpx.MockTransport(handler)) as client:
                request = httpx.Request("GET", config.base_url, headers={"x-key": "generated"})
                request.extensions["openapi.generated_headers"] = list(request.headers.raw)
                request.headers["x-key"] = "tool-supplied"
                request.headers["x-tool"] = "caller-supplied"
                response = await client.send(request)
                self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(received[0]["x-key"], "real-secret")
        self.assertNotIn("x-tool", received[0])

    async def test_network_exceptions_are_sanitized(self):
        _, config = prepare(SPEC)
        handler = AsyncMock(side_effect=RuntimeError("secret-in-exception"))
        async with APIClient(config, transport=httpx.MockTransport(handler)) as client:
            request = httpx.Request("GET", config.base_url)
            request.extensions["openapi.generated_headers"] = list(request.headers.raw)
            with self.assertRaises(ToolError) as error:
                await client.send(request)
            self.assertNotIn("secret-in-exception", str(error.exception))

    async def test_concurrency_and_total_deadline_include_waiting(self):
        _, config = prepare(SPEC)
        active = peak = 0

        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(1)
            finally:
                active -= 1

        with patch("network.MAX_CONCURRENT", 2), patch("network.REQUEST_TIMEOUT", 0.05):
            async with APIClient(config, transport=httpx.MockTransport(handler)) as client:
                async def send():
                    request = httpx.Request("GET", config.base_url)
                    request.extensions["openapi.generated_headers"] = list(request.headers.raw)
                    with self.assertRaisesRegex(ToolError, "timed out"):
                        await client.send(request)
                await asyncio.gather(*(send() for _ in range(5)))
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)

    def test_credential_echo_variants(self):
        headers = {"authorization": "Bearer example-secret", "x-key": "key/value"}
        for content in (b'example-secret', b'{"x": "example\\u002dsecret"}', b'key%2Fvalue'):
            self.assertTrue(contains_credentials(content, headers))
        for authorization in ("Bearer  example-secret", "Bearer\texample-secret"):
            with self.subTest(authorization=authorization):
                self.assertTrue(contains_credentials(
                    b'example-secret', {"authorization": authorization},
                ))
        self.assertFalse(contains_credentials(b'{"ok": true}', headers))


if __name__ == "__main__":
    unittest.main()
