"""Smoke-test isolation and VNC transport checks without Docker or user data."""
import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("docker_smoke", Path(__file__).resolve().parents[1] / "smoke_test.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class IsolationContracts(unittest.TestCase):
    def test_both_explicit_flags_are_required(self):
        for explicit, value in ((False, None), (True, None), (False, "1"), (True, "0")):
            with self.subTest(explicit=explicit, value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["AUTOHUNT_DOCKER_TEST"] = value
                with self.assertRaises(RuntimeError):
                    smoke.require_isolation(explicit)
        with patch.dict(os.environ, {"AUTOHUNT_DOCKER_TEST": "1"}, clear=True):
            smoke.require_isolation(True)

    def test_runtime_config_requires_both_persistent_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "runtime.env"
            for raw in ("AUTOHUNT_API_KEY=synthetic\n", "AUTOHUNT_DATA_KEY=synthetic\n"):
                path.write_text(raw)
                with self.subTest(raw=raw), self.assertRaises(AssertionError):
                    smoke.read_runtime_config(path)

    def test_runtime_config_digest_changes_if_a_key_is_regenerated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "runtime.env"
            path.write_text("AUTOHUNT_API_KEY=synthetic\nAUTOHUNT_DATA_KEY=first\n")
            token, before = smoke.read_runtime_config(path)
            self.assertEqual(token, "synthetic")
            self.assertEqual(smoke.read_runtime_config(path)[1], before)
            path.write_text("AUTOHUNT_API_KEY=synthetic\nAUTOHUNT_DATA_KEY=replaced\n")
            self.assertNotEqual(smoke.read_runtime_config(path)[1], before)


class FakeSocket:
    def __init__(self, parts):
        self.parts = iter(parts)
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def settimeout(self, _):
        pass

    def sendall(self, raw):
        self.sent += raw

    def recv(self, _):
        return next(self.parts, b"")


class ViewerTransportContracts(unittest.TestCase):
    def header(self, *, status="101 Switching Protocols", signature=None):
        key = base64.b64encode(b"x" * 16).decode()
        signature = signature or base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        return (f"HTTP/1.1 {status}\r\nUpgrade: websocket\r\n"
                f"Sec-WebSocket-Accept: {signature}\r\n\r\n").encode()

    def check_transport(self, parts):
        connection = FakeSocket(parts)
        with patch.object(smoke.secrets, "token_bytes", return_value=b"x" * 16), \
                patch.object(smoke.socket, "create_connection", return_value=connection):
            smoke.check_novnc_websocket()
        return connection

    def test_fragmented_upgrade_and_live_vnc_greeting(self):
        header = self.header()
        connection = self.check_transport([header[:15], header[15:], b"\x82\x0cRFB 003.008\n"])
        self.assertIn(b"GET /websockify HTTP/1.1", connection.sent)
        self.assertIn(b"Sec-WebSocket-Protocol: binary", connection.sent)

    def test_http_page_is_not_mistaken_for_websocket(self):
        with self.assertRaisesRegex(AssertionError, "upgrade failed"):
            self.check_transport([self.header(status="200 OK")])

    def test_invalid_upgrade_signature_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, "signature"):
            self.check_transport([self.header(signature="invalid") + b"RFB 003.008\n"])

    def test_upgraded_socket_without_a_vnc_server_is_failure(self):
        with self.assertRaisesRegex(AssertionError, "visible browser desktop"):
            self.check_transport([self.header(), b""])


if __name__ == "__main__":
    unittest.main()
