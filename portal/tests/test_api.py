from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path


PORTAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PORTAL_ROOT))

from app.config import Config  # noqa: E402
from app.main import PortalApplication, PortalHTTPServer  # noqa: E402
from app.security import hash_password  # noqa: E402


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        root = Path(cls.temp_dir.name)
        static = root / "static"
        reference = root / "reference"
        static.mkdir()
        reference.mkdir()
        (static / "index.html").write_text("<!doctype html><title>Portal</title>", encoding="utf-8")
        (reference / "git.md").write_text(
            "# Git notes\n<!-- tags: git -->\n\nUseful summary.\n", encoding="utf-8"
        )
        config = Config(
            host="127.0.0.1",
            port=0,
            database_path=root / "portal.db",
            password_hash=hash_password("test-password", iterations=100_000),
            public_url="https://portal.example",
            session_ttl=3600,
            cookie_secure=False,
            trust_proxy=False,
            allow_private_urls=False,
            allowed_outbound_ports=frozenset({80, 443}),
            check_timeout=2,
            feed_timeout=2,
            monitor_interval=0,
            feed_refresh_interval=0,
            static_dir=static,
            reference_dir=reference,
            migrations_dir=PORTAL_ROOT / "migrations",
        )
        cls.app = PortalApplication(config)
        cls.server = PortalHTTPServer((config.host, 0), cls.app)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.app.stop()
        cls.temp_dir.cleanup()

    def request(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        request_headers = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(body))
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = dict(response.getheaders())
        connection.close()
        data = json.loads(raw) if raw and response_headers.get("Content-Type", "").startswith("application/json") else raw
        return response.status, response_headers, data

    def login(self):
        status, headers, payload = self.request("POST", "/api/auth/login", {"password": "test-password"})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0], payload["csrf_token"]

    def test_health_static_and_reference(self):
        status, headers, payload = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(headers["Server"], "Portal")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Portal", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

        status, _, body = self.request("GET", "/static/index.html")
        self.assertEqual(status, 200)
        self.assertIn(b"Portal", body)

        status, _, payload = self.request("GET", "/api/reference/git")
        self.assertEqual(status, 200)
        self.assertEqual(payload["title"], "Git notes")
        self.assertIn("Useful summary", payload["content"])

    def test_login_rejects_cross_origin(self):
        status, _, payload = self.request(
            "POST",
            "/api/auth/login",
            {"password": "test-password"},
            {"Origin": "https://attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "origin")

    def test_csrf_short_link_and_redirect(self):
        cookie, csrf = self.login()
        status, _, payload = self.request(
            "POST",
            "/api/links",
            {"target_url": "https://example.com/article", "code": "docs1"},
            {"Cookie": cookie},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "csrf")

        status, _, payload = self.request(
            "POST",
            "/api/links",
            {"target_url": "https://example.com/article", "code": "docs1"},
            {"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["short_url"], "https://portal.example/s/docs1")

        status, headers, _ = self.request("GET", "/s/docs1")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://example.com/article")

        status, _, payload = self.request("GET", "/api/links", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(payload["links"][0]["clicks"], 1)

    def test_private_status_target_is_rejected(self):
        cookie, csrf = self.login()
        status, _, payload = self.request(
            "POST",
            "/api/status/services",
            {"name": "Local", "url": "http://127.0.0.1/health"},
            {"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_service")

    def test_path_traversal_is_not_served(self):
        status, _, _ = self.request("GET", "/%2e%2e/migrations/001_initial.sql")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
