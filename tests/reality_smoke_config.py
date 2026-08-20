#!/usr/bin/env python3
"""Build a private, ephemeral Xray client config for the Reality smoke test.

Only the generated JSON file contains client credentials.  The CLI accepts
paths and public connection details; secrets are never command-line arguments
or stdout.  X25519 public-key derivation is implemented with Python's standard
library so the server private key does not have to be passed to ``xray x25519
-i`` (where it would be visible in the process list).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import stat
import sys
import time
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """A safe-to-display validation error that never contains config values."""


def _as_dict(value: Any, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(message)
    return value


def _decode_x25519_key(value: Any, *, private: bool) -> bytes:
    kind = "private" if private else "public"
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ConfigError(f"Reality {kind} key is missing or malformed")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"Reality {kind} key is malformed") from exc
    if len(raw) != 32:
        raise ConfigError(f"Reality {kind} key has an invalid length")
    return raw


def _encode_x25519_key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def derive_x25519_public(private_key: str) -> str:
    """Return X25519(private_key, basepoint=9), encoded as base64url.

    This is the RFC 7748 Montgomery ladder.  Python integers are not intended
    to provide constant-time cryptography, but this one-shot local derivation
    runs as root with no attacker-controlled timing channel.  Its purpose is
    specifically to keep the private key out of argv and logs.
    """

    scalar_bytes = bytearray(_decode_x25519_key(private_key, private=True))
    scalar_bytes[0] &= 248
    scalar_bytes[31] &= 127
    scalar_bytes[31] |= 64
    scalar = int.from_bytes(scalar_bytes, "little")

    prime = 2**255 - 19
    x_1 = 9
    x_2, z_2 = 1, 0
    x_3, z_3 = x_1, 1
    swap = 0

    for bit_index in range(254, -1, -1):
        bit = (scalar >> bit_index) & 1
        swap ^= bit
        if swap:
            x_2, x_3 = x_3, x_2
            z_2, z_3 = z_3, z_2
        swap = bit

        a = (x_2 + z_2) % prime
        aa = (a * a) % prime
        b = (x_2 - z_2) % prime
        bb = (b * b) % prime
        e = (aa - bb) % prime
        c = (x_3 + z_3) % prime
        d = (x_3 - z_3) % prime
        da = (d * a) % prime
        cb = (c * b) % prime
        x_3 = ((da + cb) ** 2) % prime
        z_3 = (x_1 * ((da - cb) ** 2)) % prime
        x_2 = (aa * bb) % prime
        z_2 = (e * (aa + 121665 * e)) % prime

    if swap:
        x_2, x_3 = x_3, x_2
        z_2, z_3 = z_3, z_2

    public = (x_2 * pow(z_2, prime - 2, prime)) % prime
    return _encode_x25519_key(public.to_bytes(32, "little"))


def _valid_text(value: Any, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= maximum
        and not any(character.isspace() for character in value)
        and "\x00" not in value
    )


def _select_reality_inbound(
    config: dict[str, Any], inbound_tag: str | None
) -> dict[str, Any]:
    inbounds = config.get("inbounds")
    if not isinstance(inbounds, list):
        raise ConfigError("Xray config has no inbound list")

    candidates: list[dict[str, Any]] = []
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            continue
        stream = inbound.get("streamSettings")
        if (
            str(inbound.get("protocol", "")).lower() == "vless"
            and isinstance(stream, dict)
            and str(stream.get("security", "")).lower() == "reality"
        ):
            candidates.append(inbound)

    if inbound_tag:
        candidates = [item for item in candidates if item.get("tag") == inbound_tag]
        if len(candidates) != 1:
            raise ConfigError("the requested Reality inbound tag is not unique")
    elif len(candidates) != 1:
        raise ConfigError(
            "expected exactly one VLESS Reality inbound; set REALITY_SMOKE_INBOUND_TAG"
        )
    return candidates[0]


def _first_enabled_client(inbound: dict[str, Any]) -> dict[str, Any]:
    settings = _as_dict(inbound.get("settings"), "Reality inbound settings are missing")
    clients = settings.get("clients")
    if not isinstance(clients, list):
        raise ConfigError("Reality inbound has no client list")
    for client in clients:
        if (
            isinstance(client, dict)
            and client.get("enable") is not False
            and _valid_text(client.get("id"), maximum=256)
        ):
            return client
    raise ConfigError("Reality inbound has no usable enabled client")


def _reality_client_settings(
    inbound: dict[str, Any], public_server: str
) -> tuple[dict[str, Any], str]:
    stream = _as_dict(inbound.get("streamSettings"), "stream settings are missing")
    network = str(stream.get("network", "tcp")).lower()
    if network not in {"tcp", "raw"}:
        raise ConfigError("the live smoke supports only Reality TCP/RAW transport")

    transport_name = "tcpSettings" if network == "tcp" else "rawSettings"
    transport = stream.get(transport_name, {})
    if transport is not None and not isinstance(transport, dict):
        raise ConfigError("Reality transport settings are malformed")
    header = (transport or {}).get("header", {})
    if header is not None and (
        not isinstance(header, dict) or header.get("type", "none") != "none"
    ):
        raise ConfigError("the live smoke supports only an unwrapped TCP/RAW header")

    reality = _as_dict(stream.get("realitySettings"), "Reality settings are missing")
    client_defaults = reality.get("settings", {})
    if client_defaults is None:
        client_defaults = {}
    client_defaults = _as_dict(client_defaults, "Reality client defaults are malformed")

    server_names = reality.get("serverNames")
    if not isinstance(server_names, list) or not server_names:
        raise ConfigError("Reality serverNames is empty")
    server_name = server_names[0]
    if not _valid_text(server_name, maximum=253):
        raise ConfigError("the first Reality serverName is malformed")
    if server_name.rstrip(".").casefold() == public_server.rstrip(".").casefold():
        raise ConfigError("Reality SNI collides with the portal SNI route")

    short_ids = reality.get("shortIds")
    if not isinstance(short_ids, list) or not short_ids:
        raise ConfigError("Reality shortIds is empty")
    short_id = short_ids[0]
    if (
        not isinstance(short_id, str)
        or len(short_id) > 16
        or len(short_id) % 2 != 0
        or re.fullmatch(r"[0-9a-fA-F]*", short_id) is None
    ):
        raise ConfigError("the first Reality shortId is malformed")

    public_key = reality.get("publicKey") or client_defaults.get("publicKey")
    if public_key:
        public_key = _encode_x25519_key(
            _decode_x25519_key(public_key, private=False)
        )
    else:
        private_key = reality.get("privateKey")
        if not private_key:
            raise ConfigError("Reality has neither a public nor private key")
        public_key = derive_x25519_public(private_key)

    fingerprint = client_defaults.get("fingerprint") or reality.get("fingerprint") or "chrome"
    if not _valid_text(fingerprint, maximum=64):
        raise ConfigError("Reality fingerprint is malformed")
    spider_x = client_defaults.get("spiderX") or reality.get("spiderX") or "/"
    if (
        not isinstance(spider_x, str)
        or not spider_x.startswith("/")
        or len(spider_x) > 2048
        or "\x00" in spider_x
        or "\r" in spider_x
        or "\n" in spider_x
    ):
        raise ConfigError("Reality spiderX is malformed")

    result: dict[str, Any] = {
        "show": False,
        "fingerprint": fingerprint,
        "serverName": server_name,
        "publicKey": public_key,
        "shortId": short_id,
        "spiderX": spider_x,
    }
    mldsa_verify = client_defaults.get("mldsa65Verify")
    if mldsa_verify:
        if not _valid_text(mldsa_verify, maximum=8192):
            raise ConfigError("Reality ML-DSA verify key is malformed")
        result["mldsa65Verify"] = mldsa_verify
    return result, network


def build_client_config(
    source: Path,
    *,
    public_server: str,
    public_port: int,
    socks_port: int,
    inbound_tag: str | None = None,
) -> dict[str, Any]:
    if not _valid_text(public_server, maximum=253):
        raise ConfigError("public Reality endpoint is malformed")
    if not 1 <= public_port <= 65535 or not 1024 <= socks_port <= 65535:
        raise ConfigError("a network port is outside the allowed range")
    try:
        with source.open("r", encoding="utf-8") as handle:
            raw_config = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("unable to read the active Xray JSON config") from exc
    config = _as_dict(raw_config, "the active Xray config is not an object")

    inbound = _select_reality_inbound(config, inbound_tag)
    client = _first_enabled_client(inbound)
    reality_settings, network = _reality_client_settings(inbound, public_server)

    user: dict[str, Any] = {"id": client["id"], "encryption": "none"}
    flow = client.get("flow")
    if flow:
        if not _valid_text(flow, maximum=128):
            raise ConfigError("VLESS client flow is malformed")
        user["flow"] = flow

    stream_settings: dict[str, Any] = {
        "network": network,
        "security": "reality",
        "realitySettings": reality_settings,
    }
    stream_settings["tcpSettings" if network == "tcp" else "rawSettings"] = {
        "header": {"type": "none"}
    }

    return {
        "log": {"loglevel": "none"},
        "inbounds": [
            {
                "tag": "reality-smoke-socks",
                "listen": "127.0.0.1",
                "port": socks_port,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": False},
            }
        ],
        "outbounds": [
            {
                "tag": "reality-under-test",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": public_server,
                            "port": public_port,
                            "users": [user],
                        }
                    ]
                },
                "streamSettings": stream_settings,
            }
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {
                    "type": "field",
                    "inboundTag": ["reality-smoke-socks"],
                    "outboundTag": "reality-under-test",
                }
            ],
        },
    }


def write_private_json(destination: Path, value: dict[str, Any]) -> None:
    parent = destination.parent
    parent_stat = parent.stat()
    if parent_stat.st_uid != os.geteuid() or stat.S_IMODE(parent_stat.st_mode) != 0o700:
        raise ConfigError("temporary directory must be owned by the caller and mode 0700")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(destination, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), ensure_ascii=True)
            handle.write("\n")
    except OSError as exc:
        raise ConfigError("unable to create the private client config") from exc


def choose_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_loopback_port(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def validate_cloudflare_trace(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    fields = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key and value:
            fields[key] = value
    return bool(fields.get("ip") and fields.get("colo") and fields.get("tls"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="private Reality smoke config helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--source", required=True, type=Path)
    build.add_argument("--destination", required=True, type=Path)
    build.add_argument("--server", required=True)
    build.add_argument("--server-port", required=True, type=int)
    build.add_argument("--socks-port", required=True, type=int)
    build.add_argument("--inbound-tag")

    subparsers.add_parser("free-port")
    wait = subparsers.add_parser("wait-port")
    wait.add_argument("--port", required=True, type=int)
    wait.add_argument("--timeout", type=float, default=10.0)
    trace = subparsers.add_parser("validate-trace")
    trace.add_argument("--path", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "build":
            client_config = build_client_config(
                arguments.source,
                public_server=arguments.server,
                public_port=arguments.server_port,
                socks_port=arguments.socks_port,
                inbound_tag=arguments.inbound_tag,
            )
            write_private_json(arguments.destination, client_config)
            return 0
        if arguments.command == "free-port":
            print(choose_free_loopback_port())
            return 0
        if arguments.command == "wait-port":
            return 0 if wait_for_loopback_port(arguments.port, arguments.timeout) else 1
        if arguments.command == "validate-trace":
            return 0 if validate_cloudflare_trace(arguments.path) else 1
    except ConfigError as exc:
        print(f"Reality smoke configuration error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
