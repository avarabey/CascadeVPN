from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NGINX = ROOT / "nginx"


class NginxRealityTopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module_wrapper = (
            NGINX / "modules-enabled/70-ffknd-stream.conf"
        ).read_text(encoding="utf-8")
        cls.router = (
            NGINX / "stream-conf.d/ffknd-router.conf"
        ).read_text(encoding="utf-8")
        cls.http = (
            NGINX / "sites-available/ffknd.ru-http.conf"
        ).read_text(encoding="utf-8")
        cls.portal = (
            NGINX / "sites-available/ffknd.ru-portal.conf"
        ).read_text(encoding="utf-8")

    def test_ubuntu_dynamic_module_wrapper_is_non_invasive(self) -> None:
        self.assertNotIn("load_module", self.module_wrapper)
        self.assertRegex(self.module_wrapper, r"(?s)\bstream\s*\{.*include\s+/etc/nginx/stream-conf\.d/\*\.conf;")

    def test_exact_portal_sni_and_default_reality_route(self) -> None:
        match = re.search(
            r"map\s+\$ssl_preread_server_name\s+\$ffknd_tls_backend\s*\{(?P<body>.*?)\}",
            self.router,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        routes = {
            key: value
            for key, value in re.findall(r"^\s*([^#\s]+)\s+([^;\s]+);", body, re.MULTILINE)
        }
        self.assertEqual(
            routes,
            {
                "ffknd.ru": "127.0.0.1:9443",
                "www.ffknd.ru": "127.0.0.1:9443",
                "default": "127.0.0.1:10443",
            },
        )
        self.assertNotIn("hostnames;", body)
        self.assertNotRegex(body, r"[~*]")

    def test_public_listener_is_tls_passthrough(self) -> None:
        self.assertRegex(self.router, r"(?m)^\s*listen\s+443\s")
        self.assertIn("ssl_preread on;", self.router)
        self.assertIn("proxy_pass $ffknd_tls_backend;", self.router)
        self.assertIn("proxy_timeout 7d;", self.router)
        self.assertIn("proxy_socket_keepalive on;", self.router)
        self.assertNotRegex(self.router, r"(?m)^\s*listen\s+[^;]*\bssl\b")
        self.assertNotRegex(self.router, r"(?m)^\s*proxy_protocol\s+on;")
        self.assertNotRegex(self.router, r"(?m)^\s*listen\s+[^;]*\budp\b")

    def test_portal_tls_endpoint_is_loopback_only(self) -> None:
        listens = re.findall(r"(?m)^\s*listen\s+([^;]+);", self.portal)
        self.assertEqual(listens, ["127.0.0.1:9443 ssl http2"])
        self.assertIn("proxy_pass http://127.0.0.1:8080;", self.portal)
        self.assertIn("ssl_certificate /etc/letsencrypt/live/ffknd.ru/fullchain.pem;", self.portal)
        self.assertIn("ssl_certificate_key /etc/letsencrypt/live/ffknd.ru/privkey.pem;", self.portal)
        self.assertIn("server_name ffknd.ru www.ffknd.ru;", self.portal)
        self.assertIn("if ($host = www.ffknd.ru)", self.portal)
        self.assertIn("return 308 https://ffknd.ru$request_uri;", self.portal)
        self.assertIn("if ($host != ffknd.ru)", self.portal)
        self.assertNotRegex(self.portal, r"(?m)^\s*listen\s+(?:\[::\]:)?443\b")

    def test_http_listener_keeps_acme_before_exact_redirect(self) -> None:
        self.assertIn("location ^~ /.well-known/acme-challenge/", self.http)
        self.assertIn("root /var/www/letsencrypt;", self.http)
        self.assertIn("server_name ffknd.ru www.ffknd.ru;", self.http)
        self.assertIn("return 308 https://ffknd.ru$request_uri;", self.http)
        self.assertNotIn("listen 443", self.http)
        self.assertNotIn("proxy_pass", self.http)

    def test_portal_tls_policy_and_forwarded_headers_are_bounded(self) -> None:
        self.assertIn("ssl_protocols TLSv1.2 TLSv1.3;", self.portal)
        self.assertIn("ssl_session_tickets off;", self.portal)
        self.assertIn("Strict-Transport-Security", self.portal)
        self.assertIn("Content-Security-Policy", self.portal)
        self.assertIn("client_max_body_size 128k;", self.portal)
        self.assertIn("proxy_connect_timeout 5s;", self.portal)
        self.assertIn('proxy_set_header X-Forwarded-For "";', self.portal)
        self.assertIn("proxy_set_header X-Forwarded-Proto https;", self.portal)


if __name__ == "__main__":
    unittest.main()
