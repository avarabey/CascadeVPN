from __future__ import annotations

import sys
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path


PORTAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PORTAL_ROOT))

from app.config import Config  # noqa: E402
from app.main import (  # noqa: E402
    LOGIN_FAILURE_LIMIT,
    PortalApplication,
    _LoginLimiter,
    _rate_limit_client,
)
from app.security import SESSION_COOKIE, hash_password, token_hash  # noqa: E402


def _config(root: Path, password_hash: str) -> Config:
    static = root / "static"
    reference = root / "reference"
    static.mkdir(exist_ok=True)
    reference.mkdir(exist_ok=True)
    return Config(
        host="127.0.0.1",
        port=8080,
        database_path=root / "portal.db",
        password_hash=password_hash,
        public_url="https://portal.example",
        session_ttl=3600,
        cookie_secure=True,
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


class AuthHardeningTests(unittest.TestCase):
    def test_migration_invalidates_unbound_legacy_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, hash_password("current-password", iterations=100_000))
            raw_token = "legacy-session-token"
            now = int(time.time())
            with sqlite3.connect(config.database_path) as connection:
                connection.execute(
                    "CREATE TABLE schema_migrations "
                    "(version TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)"
                )
                connection.executescript(
                    (PORTAL_ROOT / "migrations" / "001_initial.sql").read_text(
                        encoding="utf-8"
                    )
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    ("001_initial.sql", now),
                )
                connection.execute(
                    "INSERT INTO sessions(token_hash, csrf_token, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?)",
                    (token_hash(raw_token), "legacy-csrf", now, now + 3600),
                )

            app = PortalApplication(config)
            self.assertIsNone(app.get_session(f"{SESSION_COOKIE}={raw_token}"))
            with app.database.connect() as connection:
                row = connection.execute(
                    "SELECT password_binding FROM sessions WHERE token_hash = ?",
                    (token_hash(raw_token),),
                ).fetchone()
            self.assertEqual(row["password_binding"], "")

    def test_password_hash_rotation_invalidates_existing_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = _config(root, hash_password("first-password", iterations=100_000))
            first_app = PortalApplication(original)
            raw_token, _ = first_app.create_session()
            cookie = f"{SESSION_COOKIE}={raw_token}"
            self.assertIsNotNone(first_app.get_session(cookie))

            rotated = replace(
                original,
                password_hash=hash_password("second-password", iterations=100_000),
            )
            rotated_app = PortalApplication(rotated)
            self.assertIsNone(rotated_app.get_session(cookie))

    def test_concurrent_reservations_cannot_bypass_client_failure_limit(self):
        clock = lambda: 1000.0
        limiter = _LoginLimiter(global_burst=LOGIN_FAILURE_LIMIT + 2, clock=clock)
        for _ in range(LOGIN_FAILURE_LIMIT):
            self.assertTrue(limiter.begin("198.51.100.10"))
        self.assertFalse(limiter.begin("198.51.100.10"))
        for _ in range(LOGIN_FAILURE_LIMIT):
            limiter.finish("198.51.100.10", succeeded=False)
        self.assertFalse(limiter.begin("198.51.100.10"))

    def test_shared_loopback_uses_refillable_global_budget_not_long_lockout(self):
        now = [1000.0]
        limiter = _LoginLimiter(global_rate=1.0, global_burst=1, clock=lambda: now[0])
        for _ in range(LOGIN_FAILURE_LIMIT + 2):
            self.assertTrue(limiter.begin(None))
            limiter.finish(None, succeeded=False)
            self.assertFalse(limiter.begin(None))
            now[0] += 1.0

    def test_forwarded_client_is_only_trusted_from_explicit_loopback_proxy(self):
        self.assertEqual(
            _rate_limit_client(
                "198.51.100.2",
                "203.0.113.9",
                trust_proxy=True,
            ),
            "198.51.100.2",
        )
        self.assertIsNone(
            _rate_limit_client("127.0.0.1", "203.0.113.9", trust_proxy=False)
        )
        self.assertEqual(
            _rate_limit_client(
                "127.0.0.1",
                "192.0.2.77, 203.0.113.9",
                trust_proxy=True,
            ),
            "203.0.113.9",
        )


if __name__ == "__main__":
    unittest.main()
