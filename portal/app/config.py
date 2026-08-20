from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .security import password_hash_is_valid


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    value = default if raw is None else int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _ports(raw: str) -> frozenset[int]:
    try:
        ports = frozenset(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError("PORTAL_ALLOWED_OUTBOUND_PORTS must be comma-separated ports") from exc
    if not ports or any(port < 1 or port > 65535 for port in ports):
        raise ValueError("PORTAL_ALLOWED_OUTBOUND_PORTS contains an invalid port")
    return ports


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    database_path: Path
    password_hash: str
    public_url: str
    session_ttl: int
    cookie_secure: bool
    trust_proxy: bool
    allow_private_urls: bool
    allowed_outbound_ports: frozenset[int]
    check_timeout: int
    feed_timeout: int
    monitor_interval: int
    feed_refresh_interval: int
    static_dir: Path
    reference_dir: Path
    migrations_dir: Path

    @classmethod
    def from_env(cls, *, require_password: bool = True) -> "Config":
        package_dir = Path(__file__).resolve().parent
        portal_dir = package_dir.parent
        password_hash = os.getenv("PORTAL_PASSWORD_HASH", "").strip()
        if require_password and not password_hash:
            raise ValueError(
                "PORTAL_PASSWORD_HASH is required; generate it with "
                "`python3 -m app hash-password`"
            )
        if password_hash and not password_hash_is_valid(password_hash):
            raise ValueError(
                "PORTAL_PASSWORD_HASH is malformed; regenerate it with "
                "`python3 -m app hash-password`"
            )

        public_url = os.getenv("PORTAL_PUBLIC_URL", "https://localhost").rstrip("/")
        parsed_public_url = urlsplit(public_url)
        if parsed_public_url.scheme not in {"http", "https"} or not parsed_public_url.hostname:
            raise ValueError("PORTAL_PUBLIC_URL must be an absolute HTTP(S) URL")
        if parsed_public_url.username or parsed_public_url.password:
            raise ValueError("PORTAL_PUBLIC_URL cannot contain credentials")

        return cls(
            host=os.getenv("PORTAL_HOST", "127.0.0.1"),
            port=_integer("PORTAL_PORT", 8080, 1, 65535),
            database_path=Path(os.getenv("PORTAL_DB", "/data/portal.db")),
            password_hash=password_hash,
            public_url=public_url,
            session_ttl=_integer("PORTAL_SESSION_TTL", 43_200, 300, 2_592_000),
            cookie_secure=_boolean("PORTAL_COOKIE_SECURE", True),
            trust_proxy=_boolean("PORTAL_TRUST_PROXY", False),
            allow_private_urls=_boolean("PORTAL_ALLOW_PRIVATE_URLS", False),
            allowed_outbound_ports=_ports(
                os.getenv("PORTAL_ALLOWED_OUTBOUND_PORTS", "80,443")
            ),
            check_timeout=_integer("PORTAL_CHECK_TIMEOUT", 5, 1, 30),
            feed_timeout=_integer("PORTAL_FEED_TIMEOUT", 10, 1, 60),
            monitor_interval=_integer("PORTAL_MONITOR_INTERVAL", 60, 0, 86_400),
            feed_refresh_interval=_integer(
                "PORTAL_FEED_REFRESH_INTERVAL", 900, 0, 86_400
            ),
            static_dir=Path(os.getenv("PORTAL_STATIC_DIR", package_dir / "static")),
            reference_dir=Path(
                os.getenv("PORTAL_REFERENCE_DIR", portal_dir / "content" / "reference")
            ),
            migrations_dir=Path(
                os.getenv("PORTAL_MIGRATIONS_DIR", portal_dir / "migrations")
            ),
        )
