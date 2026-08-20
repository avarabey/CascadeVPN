from __future__ import annotations

import gzip
import io
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


PORTAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PORTAL_ROOT))

from app.outbound import (  # noqa: E402
    FetchError,
    UnsafeURLError,
    _read_limited,
    fetch_url,
    validate_outbound_url,
    validate_redirect_target,
)
from app.config import Config  # noqa: E402
from app.security import hash_password, password_hash_is_valid, verify_password  # noqa: E402


class _FakeResponse:
    def __init__(self, body: bytes, encoding: str = ""):
        self.stream = io.BytesIO(body)
        self.encoding = encoding

    def read(self, amount: int = -1) -> bytes:
        return self.stream.read(amount)

    def getheader(self, name: str, default: str = "") -> str:
        return self.encoding if name.lower() == "content-encoding" else default


class SecurityTests(unittest.TestCase):
    def test_password_hash_verification(self):
        encoded = hash_password("correct horse", iterations=100_000)
        self.assertTrue(password_hash_is_valid(encoded))
        self.assertTrue(verify_password("correct horse", encoded))
        self.assertFalse(verify_password("wrong", encoded))
        self.assertFalse(verify_password("correct horse", "invalid"))

    def test_password_hash_generator_and_parser_share_iteration_bounds(self):
        for iterations in (99_999, 10_000_001):
            with self.subTest(iterations=iterations), self.assertRaisesRegex(
                ValueError, "between 100000 and 10000000"
            ):
                hash_password("correct horse", iterations=iterations)

    def test_malformed_password_hash_fails_during_config_load(self):
        with mock.patch.dict(os.environ, {"PORTAL_PASSWORD_HASH": "not-a-hash"}, clear=True):
            with self.assertRaisesRegex(ValueError, "malformed"):
                Config.from_env()

    def test_ssrf_blocks_loopback_by_default(self):
        with self.assertRaises(UnsafeURLError):
            validate_outbound_url(
                "http://127.0.0.1/secret",
                allow_private=False,
                allowed_ports=frozenset({80, 443}),
            )

    def test_ssrf_blocks_link_local_even_when_private_is_enabled(self):
        with self.assertRaises(UnsafeURLError):
            validate_outbound_url(
                "http://169.254.169.254/latest/meta-data/",
                allow_private=True,
                allowed_ports=frozenset({80, 443}),
            )

    def test_ssrf_blocks_metadata_addresses_even_when_private_is_enabled(self):
        for address in (
            "100.100.100.200",
            "168.63.129.16",
            "fd00:ec2::254",
            "::ffff:100.100.100.200",
        ):
            with self.subTest(address=address), self.assertRaises(UnsafeURLError):
                validate_outbound_url(
                    f"http://[{address}]/" if ":" in address else f"http://{address}/",
                    allow_private=True,
                    allowed_ports=frozenset({80, 443}),
                )

    def test_private_loopback_can_be_explicitly_enabled(self):
        target = validate_outbound_url(
            "http://127.0.0.1/test",
            allow_private=True,
            allowed_ports=frozenset({80}),
        )
        self.assertEqual(target.target_ip, "127.0.0.1")

    def test_gzip_decompression_is_bounded(self):
        compressed = gzip.compress(b"A" * 100_000)
        with self.assertRaises(FetchError):
            _read_limited(_FakeResponse(compressed, "gzip"), 256)

    def test_redirect_target_is_safe_for_location_header(self):
        target = validate_redirect_target("https://пример.рф/путь?q=тест#часть")
        self.assertEqual(
            target,
            "https://xn--e1afmkfd.xn--p1ai/%D0%BF%D1%83%D1%82%D1%8C"
            "?q=%D1%82%D0%B5%D1%81%D1%82#%D1%87%D0%B0%D1%81%D1%82%D1%8C",
        )

    def test_fetch_pins_ip_but_preserves_original_host_header(self):
        captured: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                captured["host"] = self.headers["Host"]
                captured["path"] = self.path
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _fmt, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_getaddrinfo = socket.getaddrinfo

        def resolver(host, port, *args, **kwargs):
            if host == "feed.example.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            return original_getaddrinfo(host, port, *args, **kwargs)

        try:
            with mock.patch("app.outbound.socket.getaddrinfo", side_effect=resolver):
                result = fetch_url(
                    f"http://feed.example.test:{server.server_port}/rss",
                    allow_private=True,
                    allowed_ports=frozenset({server.server_port}),
                    timeout=2,
                    max_bytes=1024,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(result.body, b"ok")
        self.assertEqual(captured["host"], f"feed.example.test:{server.server_port}")
        self.assertEqual(captured["path"], "/rss")


if __name__ == "__main__":
    unittest.main()
