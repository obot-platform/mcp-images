import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import MAX_SPEC, ConfigError, checked_address, json_object, load_spec, prepare

SPEC = json.loads((Path(__file__).parent / "fixtures/api.json").read_text())


class ConfigTests(unittest.TestCase):
    def test_file_snapshot_accepts_large_schema_and_projected_volume_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "snapshot.json"
            mounted = Path(directory) / "OPENAPI_SPEC_FILE"
            spec = {**SPEC, "x-padding": "x" * (MAX_SPEC // 2)}
            target.write_text(json.dumps(spec), encoding="utf-8")
            mounted.symlink_to(target)
            snapshot = load_spec(str(mounted))
            self.assertEqual(snapshot, spec)
            document, _ = prepare(snapshot)
            self.assertEqual(document["info"]["title"], SPEC["info"]["title"])
            target.write_text("{}", encoding="utf-8")
            self.assertEqual(snapshot, spec)  # no live file-backed state
            self.assertEqual(load_spec(str(mounted)), {})

    def test_file_input_failures_and_byte_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "snapshot.json"
            for filename in ("", str(target), directory):
                with self.subTest(filename=filename), self.assertRaises(ConfigError):
                    load_spec(filename)
            for raw, message in ((b"\xff", "UTF-8"), (b"{", "valid JSON"),
                                 (b"[]", "JSON object"), (b" " * (MAX_SPEC + 1), "exceeds")):
                target.write_bytes(raw)
                with self.subTest(message=message), self.assertRaisesRegex(ConfigError, message):
                    load_spec(str(target))
            target.write_bytes(b"{}" + b" " * (MAX_SPEC - 2))
            self.assertEqual(load_spec(str(target)), {})
            with patch.object(Path, "open", side_effect=PermissionError("private details")):
                with self.assertRaisesRegex(ConfigError, "path and file permissions") as error:
                    load_spec(str(target))
                self.assertNotIn("private details", str(error.exception))

    def test_defaults_versions_and_unchanged_snapshot(self):
        for version in ("3.0.0", "3.0.3", "3.0.4", "3.1.0", "3.1.1", "3.1.2"):
            spec = copy.deepcopy(SPEC)
            spec["openapi"] = version
            original = copy.deepcopy(spec)
            document, config = prepare(spec)
            self.assertEqual(spec, original)
            self.assertEqual(config.credential_headers, ())
            self.assertEqual(config.base_url, "https://api.example.test/v1/")

    def test_credential_header_list(self):
        _, config = prepare(SPEC, credential_headers=" Authorization, X-Key ")
        self.assertEqual(config.credential_headers, ("authorization", "x-key"))
        for value in ("", "  "):
            self.assertEqual(prepare(SPEC, credential_headers=value)[1].credential_headers, ())
        for value in ("Host", "Cookie", "bad\nname", "X-Key,x-key", ",X-Key",
                      "X-Key,", "X-Key,,Authorization", ["X-Key"]):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                prepare(SPEC, credential_headers=value)

    def test_references_auth_and_credentials_not_tool_parameters(self):
        spec = copy.deepcopy(SPEC)
        spec["components"] = {
            "securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Key"}},
            "parameters": {"key": {"name": "X-Key", "in": "header", "schema": {"type": "string"}}},
        }
        spec["paths"]["/items"]["get"]["parameters"] = [{"$ref": "#/components/parameters/key"}]
        with self.assertRaisesRegex(ValueError, "OPENAPI_CREDENTIAL_HEADERS"):
            prepare(spec)
        document, config = prepare(spec, credential_headers="X-Key")
        self.assertEqual(document["paths"]["/items"]["get"]["parameters"], [])
        for scheme in ({"type": "oauth2"}, {"type": "apiKey", "in": "query", "name": "key"}):
            spec["components"]["securitySchemes"]["key"] = scheme
            with self.assertRaisesRegex(ValueError, "Only header"):
                prepare(spec)
        for ref in ("https://other.test/schema", "file:///tmp/schema", "#/missing"):
            bad = copy.deepcopy(SPEC)
            bad["components"] = {"schemas": {"test": {"$ref": ref}}}
            with self.assertRaises(ValueError):
                prepare(bad)

    def test_destination_and_override(self):
        for value in ("/relative", "https://user:password@api.test", "http://localhost",
                      "http://127.0.0.1", "http://[::1]", "http://169.254.169.254",
                      "http://api.test?key=secret", "https://{host}", "https://api.test:invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare(SPEC, base_url=value)
        document, config = prepare(SPEC, base_url="http://10.0.0.8/api")
        self.assertEqual(config.base_url, "http://10.0.0.8/api/")
        self.assertEqual(prepare(SPEC, base_url="")[1].base_url, "https://api.example.test/v1/")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            prepare(SPEC, base_url="http://10.0.0.8", credential_headers="Authorization")

    def test_address_policy(self):
        for address in ("127.0.0.2", "::1", "::", "0.0.0.0", "0.1.2.3", "169.254.169.254",
                        "fe80::1", "224.0.0.1", "ff02::1", "::ffff:127.0.0.1"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                checked_address(address)
        for address in ("10.1.2.3", "172.16.0.1", "192.168.0.1", "fd00::1", "8.8.8.8"):
            checked_address(address)

    def test_input_bounds(self):
        for raw in ("[]", "{", '{"x": NaN}', " " * (MAX_SPEC + 1)):
            with self.assertRaises(ValueError):
                json_object(raw, "test")


if __name__ == "__main__":
    unittest.main()
