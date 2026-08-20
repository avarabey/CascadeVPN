from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import logging
import mimetypes
import re
import signal
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from .config import Config
from .database import Database
from .outbound import UnsafeURLError, validate_redirect_target
from .security import (
    SAFE_CODE,
    SESSION_COOKIE,
    hash_password,
    random_short_code,
    random_token,
    session_password_binding,
    token_hash,
    verify_password,
)
from .services import monitor, rss_reader


LOG = logging.getLogger("portal")
SERVICE_ID_PATH = re.compile(r"^/api/status/services/(\d+)$")
FEED_ID_PATH = re.compile(r"^/api/feeds/(\d+)$")
LINK_CODE_PATH = re.compile(r"^/api/links/([A-Za-z0-9_-]{4,32})$")
SHORT_LINK_PATH = re.compile(r"^/s/([A-Za-z0-9_-]{4,32})$")
REFERENCE_PATH = re.compile(r"^/api/reference/([a-z0-9-]{1,64})$")
MAX_JSON_BODY = 64 * 1024
LOGIN_FAILURE_LIMIT = 8
LOGIN_FAILURE_WINDOW_SECONDS = 300.0
GLOBAL_LOGIN_RATE_PER_SECOND = 1.0
GLOBAL_LOGIN_BURST = 8
PASSWORD_VERIFY_CONCURRENCY = 2


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Session:
    token_hash: str
    csrf_token: str
    expires_at: int


@dataclass
class _ClientLoginState:
    failures: deque[float] = field(default_factory=deque)
    in_flight: int = 0


class _LoginLimiter:
    """Atomic per-client failures plus a short-lived global CPU budget."""

    def __init__(
        self,
        *,
        failure_limit: int = LOGIN_FAILURE_LIMIT,
        failure_window: float = LOGIN_FAILURE_WINDOW_SECONDS,
        global_rate: float = GLOBAL_LOGIN_RATE_PER_SECOND,
        global_burst: int = GLOBAL_LOGIN_BURST,
        clock: Any = time.monotonic,
    ):
        self.failure_limit = failure_limit
        self.failure_window = failure_window
        self.global_rate = global_rate
        self.global_burst = float(global_burst)
        self.clock = clock
        self.lock = threading.Lock()
        self.clients: dict[str, _ClientLoginState] = {}
        self.global_tokens = float(global_burst)
        self.last_refill = clock()
        self.last_client_cleanup = self.last_refill

    def begin(self, client: str | None) -> bool:
        now = self.clock()
        with self.lock:
            if now - self.last_client_cleanup >= min(60.0, self.failure_window):
                cutoff = now - self.failure_window
                self.clients = {
                    key: state
                    for key, state in self.clients.items()
                    if state.in_flight or (state.failures and state.failures[-1] >= cutoff)
                }
                self.last_client_cleanup = now
            elapsed = max(0.0, now - self.last_refill)
            self.global_tokens = min(
                self.global_burst,
                self.global_tokens + elapsed * self.global_rate,
            )
            self.last_refill = now

            state: _ClientLoginState | None = None
            if client is not None:
                state = self.clients.get(client, _ClientLoginState())
                cutoff = now - self.failure_window
                while state.failures and state.failures[0] < cutoff:
                    state.failures.popleft()
                # Include in-flight verifications so concurrent requests cannot
                # all pass the check before their failures are recorded.
                if len(state.failures) + state.in_flight >= self.failure_limit:
                    return False

            if self.global_tokens < 1.0:
                return False
            self.global_tokens -= 1.0
            if state is not None:
                self.clients[client] = state
                state.in_flight += 1
            return True

    def finish(self, client: str | None, *, succeeded: bool) -> None:
        if client is None:
            return
        now = self.clock()
        with self.lock:
            state = self.clients.get(client)
            if state is None:
                return
            state.in_flight = max(0, state.in_flight - 1)
            if succeeded:
                state.failures.clear()
            else:
                state.failures.append(now)
            if not state.failures and state.in_flight == 0:
                self.clients.pop(client, None)


def _rate_limit_client(
    peer_address: str,
    forwarded_for: str | None,
    *,
    trust_proxy: bool,
) -> str | None:
    """Return an attributable client IP, or None for a shared loopback hop.

    X-Forwarded-For is accepted only from a loopback peer and only when proxy
    trust was explicitly enabled. The rightmost value represents the client
    supplied by a single trusted hop and cannot be replaced by a spoofed
    leftmost value appended by that hop.
    """

    try:
        peer = ipaddress.ip_address(peer_address.split("%", 1)[0])
    except ValueError:
        return peer_address[:64]

    if peer.is_loopback:
        if trust_proxy and forwarded_for:
            candidate = forwarded_for.rsplit(",", 1)[-1].strip()
            try:
                forwarded = ipaddress.ip_address(candidate.split("%", 1)[0])
            except ValueError:
                return None
            if not (forwarded.is_unspecified or forwarded.is_multicast):
                return forwarded.compressed
        # In the TrustTunnel topology every browser shares this peer address.
        # Rely on the short global CPU budget instead of a five-minute IP ban.
        return None
    return peer.compressed


class PortalApplication:
    def __init__(self, config: Config):
        self.config = config
        self.database = Database(config.database_path, config.migrations_dir)
        self.database.initialize()
        self.stop_event = threading.Event()
        self.background_threads: list[threading.Thread] = []
        self.password_binding = session_password_binding(config.password_hash)
        self.login_limiter = _LoginLimiter()
        self.password_slots = threading.BoundedSemaphore(PASSWORD_VERIFY_CONCURRENCY)

    def get_session(self, cookie_header: str | None) -> Session | None:
        if not cookie_header or len(cookie_header) > 8192:
            return None
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except Exception:
            return None
        morsel = cookies.get(SESSION_COOKIE)
        if morsel is None:
            return None
        raw_token = morsel.value
        if not raw_token or len(raw_token) > 256:
            return None
        now = int(time.time())
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT token_hash, csrf_token, expires_at FROM sessions "
                "WHERE token_hash = ? AND password_binding = ? AND expires_at > ?",
                (token_hash(raw_token), self.password_binding, now),
            ).fetchone()
        if row is None:
            return None
        return Session(row["token_hash"], row["csrf_token"], row["expires_at"])

    def create_session(self) -> tuple[str, Session]:
        raw_token = random_token()
        csrf_token = random_token()
        now = int(time.time())
        session = Session(token_hash(raw_token), csrf_token, now + self.config.session_ttl)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO sessions"
                "(token_hash, csrf_token, created_at, expires_at, password_binding) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session.token_hash,
                    csrf_token,
                    now,
                    session.expires_at,
                    self.password_binding,
                ),
            )
        return raw_token, session

    def delete_session(self, session: Session) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (session.token_hash,)
            )

    def start_background_tasks(self) -> None:
        jobs = [
            ("status-monitor", self.config.monitor_interval, monitor.check_all),
            ("feed-refresh", self.config.feed_refresh_interval, rss_reader.refresh_all),
        ]
        for name, interval, function in jobs:
            if interval <= 0:
                continue
            thread = threading.Thread(
                target=self._background_loop,
                args=(name, interval, function),
                daemon=True,
                name=name,
            )
            thread.start()
            self.background_threads.append(thread)

    def _background_loop(self, name: str, interval: int, function: Any) -> None:
        while not self.stop_event.wait(interval):
            try:
                function(self.database, self.config)
                self.database.cleanup_sessions()
            except Exception:
                LOG.exception("background job %s failed", name)

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.background_threads:
            thread.join(timeout=2)

    def list_references(self) -> list[dict[str, Any]]:
        articles: list[dict[str, Any]] = []
        if not self.config.reference_dir.is_dir():
            return articles
        for path in sorted(self.config.reference_dir.glob("*.md")):
            if not re.fullmatch(r"[a-z0-9-]{1,64}", path.stem):
                continue
            content = path.read_text(encoding="utf-8")
            title = path.stem.replace("-", " ").title()
            summary = ""
            tags: list[str] = []
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("# "):
                    title = stripped[2:].strip()[:120]
                elif stripped.startswith("<!-- tags:") and stripped.endswith("-->"):
                    tags = [
                        tag.strip()[:30]
                        for tag in stripped[len("<!-- tags:") : -len("-->")].split(",")
                        if tag.strip()
                    ][:10]
                elif stripped and not stripped.startswith(("#", "<!--", "```", "- ")):
                    summary = stripped[:240]
                    break
            articles.append(
                {"slug": path.stem, "title": title, "summary": summary, "tags": tags}
            )
        return articles

    def get_reference(self, slug: str) -> dict[str, Any] | None:
        path = (self.config.reference_dir / f"{slug}.md").resolve()
        try:
            path.relative_to(self.config.reference_dir.resolve())
        except ValueError:
            return None
        if not path.is_file():
            return None
        content = path.read_text(encoding="utf-8")
        article = next(
            (item for item in self.list_references() if item["slug"] == slug), None
        )
        if article is None:
            return None
        return {**article, "content": content}


class PortalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], app: PortalApplication):
        self.app = app
        super().__init__(server_address, PortalRequestHandler)


class PortalRequestHandler(BaseHTTPRequestHandler):
    server: PortalHTTPServer
    server_version = "Portal"
    sys_version = ""

    def version_string(self) -> str:
        return "Portal"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.client_address[0], fmt % args)

    @property
    def app(self) -> PortalApplication:
        return self.server.app

    def _security_headers(self, *, html: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self.app.config.public_url.startswith("https://"):
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if html:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            )

    def _json(self, status: int, payload: Any, *, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, error: APIError) -> None:
        self._json(
            error.status,
            {"error": {"code": error.code, "message": error.message}},
        )

    def _body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise APIError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content_type", "expected application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise APIError(HTTPStatus.LENGTH_REQUIRED, "content_length", "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise APIError(HTTPStatus.BAD_REQUEST, "content_length", "invalid Content-Length") from exc
        if length < 0 or length > MAX_JSON_BODY:
            raise APIError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", "request body is too large")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise APIError(HTTPStatus.BAD_REQUEST, "invalid_json", "request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise APIError(HTTPStatus.BAD_REQUEST, "invalid_json", "request body must be a JSON object")
        return payload

    def _session(self) -> Session | None:
        return self.app.get_session(self.headers.get("Cookie"))

    def _require_session(self, *, csrf: bool = False) -> Session:
        session = self._session()
        if session is None:
            raise APIError(HTTPStatus.UNAUTHORIZED, "authentication_required", "authentication required")
        if csrf:
            supplied = self.headers.get("X-CSRF-Token", "")
            if not supplied or not secrets_compare(supplied, session.csrf_token):
                raise APIError(HTTPStatus.FORBIDDEN, "csrf", "invalid CSRF token")
        return session

    def _same_origin(self) -> None:
        origin = self.headers.get("Origin")
        if not origin:
            return
        expected = url_origin(self.app.config.public_url)
        if url_origin(origin) != expected:
            raise APIError(HTTPStatus.FORBIDDEN, "origin", "cross-origin request rejected")

    def _client_key(self) -> str | None:
        client = _rate_limit_client(
            self.client_address[0],
            self.headers.get("X-Forwarded-For"),
            trust_proxy=self.app.config.trust_proxy,
        )
        return client

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": {"code": "method", "message": "method not allowed"}}, headers={"Allow": "GET, HEAD, POST, DELETE"})

    def _dispatch(self) -> None:
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            if path.startswith("/api/"):
                self._dispatch_api(path, parse_qs(parsed.query))
                return
            short_match = SHORT_LINK_PATH.fullmatch(path)
            if short_match and self.command in {"GET", "HEAD"}:
                self._redirect_short_link(short_match.group(1))
                return
            if self.command not in {"GET", "HEAD"}:
                raise APIError(HTTPStatus.METHOD_NOT_ALLOWED, "method", "method not allowed")
            self._serve_static(path)
        except APIError as exc:
            self._error(exc)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            LOG.exception("unhandled request error")
            self._error(APIError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal", "internal server error"))

    def _dispatch_api(self, path: str, query: dict[str, list[str]]) -> None:
        method = self.command
        if path == "/api/health" and method in {"GET", "HEAD"}:
            healthy = self.app.database.healthy()
            self._json(
                HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                {"status": "ok" if healthy else "error", "version": __version__},
            )
            return
        if path == "/api/auth/session" and method in {"GET", "HEAD"}:
            session = self._session()
            payload: dict[str, Any] = {"authenticated": session is not None}
            if session is not None:
                payload.update({"csrf_token": session.csrf_token, "expires_at": session.expires_at})
            self._json(HTTPStatus.OK, payload)
            return
        if path == "/api/auth/login" and method == "POST":
            self._login()
            return
        if path == "/api/auth/logout" and method == "POST":
            session = self._require_session(csrf=True)
            self._same_origin()
            self.app.delete_session(session)
            self._json(HTTPStatus.OK, {"authenticated": False}, headers={"Set-Cookie": self._expired_cookie()})
            return
        if path == "/api/status" and method in {"GET", "HEAD"}:
            authenticated = self._session() is not None
            services = monitor.list_services(self.app.database, include_url=authenticated)
            self._json(HTTPStatus.OK, {"services": services, "summary": monitor.status_summary(services)})
            return
        if path == "/api/status/services" and method in {"GET", "HEAD"}:
            self._require_session()
            self._json(HTTPStatus.OK, {"services": monitor.list_services(self.app.database)})
            return
        if path == "/api/status/services" and method == "POST":
            self._require_session(csrf=True)
            self._same_origin()
            body = self._body()
            try:
                service = monitor.add_service(self.app.database, self.app.config, body.get("name", ""), body.get("url", ""))
            except (ValueError, UnsafeURLError) as exc:
                raise APIError(HTTPStatus.BAD_REQUEST, "invalid_service", str(exc)) from exc
            self._json(HTTPStatus.CREATED, {"service": service})
            return
        service_match = SERVICE_ID_PATH.fullmatch(path)
        if service_match and method == "DELETE":
            self._require_session(csrf=True)
            self._same_origin()
            if not monitor.delete_service(self.app.database, int(service_match.group(1))):
                raise APIError(HTTPStatus.NOT_FOUND, "not_found", "service not found")
            self._json(HTTPStatus.OK, {"deleted": True})
            return
        if path == "/api/status/check" and method == "POST":
            self._require_session(csrf=True)
            self._same_origin()
            body = self._body()
            try:
                if body.get("id") is None:
                    services = monitor.check_all(self.app.database, self.app.config)
                else:
                    services = [monitor.check_service(self.app.database, self.app.config, int(body["id"]))]
            except (ValueError, TypeError):
                raise APIError(HTTPStatus.BAD_REQUEST, "invalid_id", "id must be an integer")
            except KeyError:
                raise APIError(HTTPStatus.NOT_FOUND, "not_found", "service not found")
            self._json(HTTPStatus.OK, {"services": services})
            return
        if path == "/api/feeds/items" and method in {"GET", "HEAD"}:
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                raise APIError(HTTPStatus.BAD_REQUEST, "invalid_limit", "limit must be an integer")
            self._json(HTTPStatus.OK, {"items": rss_reader.list_items(self.app.database, limit)})
            return
        if path == "/api/feeds" and method in {"GET", "HEAD"}:
            self._require_session()
            self._json(HTTPStatus.OK, {"feeds": rss_reader.list_feeds(self.app.database)})
            return
        if path == "/api/feeds" and method == "POST":
            self._require_session(csrf=True)
            self._same_origin()
            body = self._body()
            try:
                feed = rss_reader.add_feed(self.app.database, self.app.config, body.get("title", ""), body.get("url", ""))
            except (ValueError, UnsafeURLError) as exc:
                raise APIError(HTTPStatus.BAD_REQUEST, "invalid_feed", str(exc)) from exc
            self._json(HTTPStatus.CREATED, {"feed": feed})
            return
        feed_match = FEED_ID_PATH.fullmatch(path)
        if feed_match and method == "DELETE":
            self._require_session(csrf=True)
            self._same_origin()
            if not rss_reader.delete_feed(self.app.database, int(feed_match.group(1))):
                raise APIError(HTTPStatus.NOT_FOUND, "not_found", "feed not found")
            self._json(HTTPStatus.OK, {"deleted": True})
            return
        if path == "/api/feeds/refresh" and method == "POST":
            self._require_session(csrf=True)
            self._same_origin()
            body = self._body()
            try:
                if body.get("id") is None:
                    results = rss_reader.refresh_all(self.app.database, self.app.config)
                else:
                    results = [rss_reader.refresh_feed(self.app.database, self.app.config, int(body["id"]))]
            except (ValueError, TypeError):
                raise APIError(HTTPStatus.BAD_REQUEST, "invalid_id", "id must be an integer")
            except KeyError:
                raise APIError(HTTPStatus.NOT_FOUND, "not_found", "feed not found")
            except (rss_reader.FeedParseError, UnsafeURLError, RuntimeError) as exc:
                raise APIError(HTTPStatus.BAD_GATEWAY, "feed_refresh", str(exc)) from exc
            self._json(HTTPStatus.OK, {"results": results})
            return
        if path == "/api/links" and method in {"GET", "HEAD"}:
            self._require_session()
            self._json(HTTPStatus.OK, {"links": self._list_links()})
            return
        if path == "/api/links" and method == "POST":
            self._require_session(csrf=True)
            self._same_origin()
            self._create_link()
            return
        link_match = LINK_CODE_PATH.fullmatch(path)
        if link_match and method == "DELETE":
            self._require_session(csrf=True)
            self._same_origin()
            with self.app.database.connect() as connection:
                cursor = connection.execute("DELETE FROM short_links WHERE code = ?", (link_match.group(1),))
            if cursor.rowcount == 0:
                raise APIError(HTTPStatus.NOT_FOUND, "not_found", "short link not found")
            self._json(HTTPStatus.OK, {"deleted": True})
            return
        if path == "/api/reference" and method in {"GET", "HEAD"}:
            self._json(HTTPStatus.OK, {"articles": self.app.list_references()})
            return
        reference_match = REFERENCE_PATH.fullmatch(path)
        if reference_match and method in {"GET", "HEAD"}:
            article = self.app.get_reference(reference_match.group(1))
            if article is None:
                raise APIError(HTTPStatus.NOT_FOUND, "not_found", "reference article not found")
            self._json(HTTPStatus.OK, article)
            return
        raise APIError(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")

    def _login(self) -> None:
        self._same_origin()
        body = self._body()
        password = body.get("password")
        if not isinstance(password, str) or not password or len(password) > 1024:
            raise APIError(HTTPStatus.BAD_REQUEST, "invalid_password", "password is required")
        client = self._client_key()
        if not self.app.password_slots.acquire(blocking=False):
            raise APIError(HTTPStatus.TOO_MANY_REQUESTS, "rate_limit", "too many login attempts")
        reserved = False
        valid = False
        try:
            if not self.app.login_limiter.begin(client):
                raise APIError(HTTPStatus.TOO_MANY_REQUESTS, "rate_limit", "too many login attempts")
            reserved = True
            valid = verify_password(password, self.app.config.password_hash)
        finally:
            if reserved:
                self.app.login_limiter.finish(client, succeeded=valid)
            self.app.password_slots.release()
        if not valid:
            raise APIError(HTTPStatus.UNAUTHORIZED, "invalid_credentials", "invalid credentials")
        existing = self._session()
        if existing is not None:
            self.app.delete_session(existing)
        raw_token, session = self.app.create_session()
        self._json(
            HTTPStatus.OK,
            {"authenticated": True, "csrf_token": session.csrf_token, "expires_at": session.expires_at},
            headers={"Set-Cookie": self._session_cookie(raw_token)},
        )

    def _session_cookie(self, token: str) -> str:
        parts = [
            f"{SESSION_COOKIE}={token}",
            "Path=/",
            f"Max-Age={self.app.config.session_ttl}",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if self.app.config.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _expired_cookie(self) -> str:
        parts = [
            f"{SESSION_COOKIE}=",
            "Path=/",
            "Max-Age=0",
            "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if self.app.config.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _list_links(self) -> list[dict[str, Any]]:
        with self.app.database.connect() as connection:
            rows = connection.execute(
                "SELECT code, target_url, created_at, clicks, last_accessed_at "
                "FROM short_links ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                **dict(row),
                "short_url": f"{self.app.config.public_url}/s/{row['code']}",
            }
            for row in rows
        ]

    def _create_link(self) -> None:
        body = self._body()
        try:
            target_url = validate_redirect_target(body.get("target_url", ""))
        except ValueError as exc:
            raise APIError(HTTPStatus.BAD_REQUEST, "invalid_target", str(exc)) from exc
        requested_code = body.get("code")
        if requested_code is not None and (
            not isinstance(requested_code, str) or not SAFE_CODE.fullmatch(requested_code)
        ):
            raise APIError(HTTPStatus.BAD_REQUEST, "invalid_code", "code must be 4-32 URL-safe characters")
        created_at = int(time.time())
        attempts = 1 if requested_code else 8
        for _ in range(attempts):
            code = requested_code or random_short_code()
            try:
                with self.app.database.connect() as connection:
                    connection.execute(
                        "INSERT INTO short_links(code, target_url, created_at) VALUES (?, ?, ?)",
                        (code, target_url, created_at),
                    )
            except sqlite3.IntegrityError:
                if requested_code:
                    raise APIError(HTTPStatus.CONFLICT, "code_exists", "short code already exists")
                continue
            self._json(
                HTTPStatus.CREATED,
                {
                    "code": code,
                    "target_url": target_url,
                    "short_url": f"{self.app.config.public_url}/s/{code}",
                    "created_at": created_at,
                },
            )
            return
        raise APIError(HTTPStatus.SERVICE_UNAVAILABLE, "code_generation", "could not allocate a short code")

    def _redirect_short_link(self, code: str) -> None:
        with self.app.database.connect() as connection:
            row = connection.execute(
                "SELECT target_url FROM short_links WHERE code = ?", (code,)
            ).fetchone()
            if row is not None and self.command != "HEAD":
                connection.execute(
                    "UPDATE short_links SET clicks = clicks + 1, last_accessed_at = ? WHERE code = ?",
                    (int(time.time()), code),
                )
        if row is None:
            raise APIError(HTTPStatus.NOT_FOUND, "not_found", "short link not found")
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", row["target_url"])
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def _serve_static(self, raw_path: str) -> None:
        try:
            decoded = unquote(raw_path, errors="strict")
        except UnicodeDecodeError:
            raise APIError(HTTPStatus.BAD_REQUEST, "path", "invalid path encoding")
        if "\x00" in decoded or "\\" in decoded:
            raise APIError(HTTPStatus.BAD_REQUEST, "path", "invalid path")
        if decoded == "/":
            relative = "index.html"
        elif decoded.startswith("/static/"):
            relative = decoded[len("/static/") :]
        else:
            relative = decoded.lstrip("/")
        root = self.app.config.static_dir.resolve()
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise APIError(HTTPStatus.NOT_FOUND, "not_found", "file not found")
        if not path.is_file() or any(part.startswith(".") for part in Path(relative).parts):
            raise APIError(HTTPStatus.NOT_FOUND, "not_found", "file not found")
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if path.name == "index.html":
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=3600")
        self._security_headers(html=path.suffix.lower() == ".html")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def secrets_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def url_origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    default_port = 443 if parsed.scheme == "https" else 80
    try:
        port = parsed.port or default_port
    except ValueError:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    host = f"[{host}]" if ":" in host else host
    suffix = "" if port == default_port else f":{port}"
    return f"{parsed.scheme.lower()}://{host}{suffix}"


def serve(config: Config) -> None:
    app = PortalApplication(config)
    server = PortalHTTPServer((config.host, config.port), app)
    server.timeout = 1
    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    app.start_background_tasks()
    LOG.info("portal listening on http://%s:%d", config.host, config.port)
    try:
        while not stop.is_set():
            server.handle_request()
    finally:
        server.server_close()
        app.stop()


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ffknd utility portal")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="run the HTTP server")
    hash_parser = subparsers.add_parser("hash-password", help="generate PORTAL_PASSWORD_HASH")
    hash_parser.add_argument("--iterations", type=int, default=600_000)
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "hash-password":
        password = getpass.getpass("Portal password: ")
        confirmation = getpass.getpass("Repeat password: ")
        if password != confirmation:
            parser.error("passwords do not match")
        print(hash_password(password, iterations=args.iterations))
        return 0
    config = Config.from_env()
    serve(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
