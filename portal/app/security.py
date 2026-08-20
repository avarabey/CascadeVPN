from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets


PASSWORD_ALGORITHM = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 600_000
MIN_PASSWORD_ITERATIONS = 100_000
MAX_PASSWORD_ITERATIONS = 10_000_000
SESSION_COOKIE = "portal_session"
SAFE_CODE = re.compile(r"^[A-Za-z0-9_-]{4,32}$")
SESSION_PASSWORD_BINDING_CONTEXT = b"ffknd-portal-session-password-v1\0"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _parse_password_hash(encoded: str) -> tuple[int, bytes, bytes] | None:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded.split(":", 3)
        iterations = int(raw_iterations)
        salt = _b64decode(raw_salt)
        digest = _b64decode(raw_digest)
    except (AttributeError, ValueError, TypeError, base64.binascii.Error):
        return None
    if (
        algorithm != PASSWORD_ALGORITHM
        or not MIN_PASSWORD_ITERATIONS <= iterations <= MAX_PASSWORD_ITERATIONS
    ):
        return None
    if len(salt) < 16 or len(digest) != hashlib.sha256().digest_size:
        return None
    return iterations, salt, digest


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    if not password:
        raise ValueError("password cannot be empty")
    if not MIN_PASSWORD_ITERATIONS <= iterations <= MAX_PASSWORD_ITERATIONS:
        raise ValueError(
            "iterations must be between "
            f"{MIN_PASSWORD_ITERATIONS} and {MAX_PASSWORD_ITERATIONS}"
        )
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{PASSWORD_ALGORITHM}:{iterations}:{_b64encode(salt)}:{_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    parsed = _parse_password_hash(encoded)
    if parsed is None:
        return False
    iterations, salt, expected = parsed
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def password_hash_is_valid(encoded: str) -> bool:
    """Validate a stored password hash without performing the expensive KDF."""
    return _parse_password_hash(encoded) is not None


def random_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def session_password_binding(encoded_password_hash: str) -> str:
    """Return a non-secret identifier that changes whenever credentials rotate."""
    return hashlib.sha256(
        SESSION_PASSWORD_BINDING_CONTEXT + encoded_password_hash.encode("utf-8")
    ).hexdigest()


def random_short_code(length: int = 7) -> str:
    alphabet = "23456789abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ"
    return "".join(secrets.choice(alphabet) for _ in range(length))
