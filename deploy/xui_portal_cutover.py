#!/usr/bin/env python3
"""Safely move one local 3x-ui Reality inbound behind the ffknd SNI router.

The helper deliberately uses only Python's standard library.  It reads the
root-only install result written by 3x-ui, authenticates with its Bearer token,
and never prints API response bodies or inbound payloads.  Full rollback state
is stored mode 0600; the human-readable status is redacted.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import socket
import sqlite3
import ssl
import stat
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


INBOUND_ID = 6
PUBLIC_HOST = "ffknd.ru"
PUBLIC_PORT = 443
XRAY_LISTEN = "127.0.0.1"
XRAY_PORT = 10443
REALITY_SNI = "cloud.ru"
REALITY_TARGET = "cloud.ru:443"
HOST_GROUP_ID = "ffknd-public-443"
STATE_VERSION = 1
MAX_API_RESPONSE = 128 * 1024 * 1024

DEFAULT_ENV_FILE = Path("/etc/x-ui/install-result.env")
DEFAULT_DB_FILE = Path("/etc/x-ui/x-ui.db")
DEFAULT_STATE_DIR = Path("/var/lib/ffknd-xui-cutover")

WRITABLE_INBOUND_FIELDS = (
    "total",
    "remark",
    "subSortIndex",
    "enable",
    "expiryTime",
    "trafficReset",
    "trafficResetDay",
    "listen",
    "port",
    "protocol",
    "settings",
    "streamSettings",
    "tag",
    "sniffing",
    "shareAddrStrategy",
    "shareAddr",
)

HOST_CREATE_PAYLOAD: dict[str, Any] = {
    "groupId": HOST_GROUP_ID,
    "inboundIds": [INBOUND_ID],
    "hosts": [PUBLIC_HOST],
    "port": PUBLIC_PORT,
    "remark": HOST_GROUP_ID,
    "serverDescription": "",
    "sortOrder": 0,
    "isDisabled": False,
    "isHidden": False,
    "security": "reality",
    "sni": REALITY_SNI,
    "overrideSniFromAddress": False,
    "keepSniBlank": False,
    "allowInsecure": False,
    "tags": ["public-443"],
    "alpn": [],
    "fingerprint": "",
    "hostHeader": "",
    "path": "",
    "pinnedPeerCertSha256": [],
    "verifyPeerCertByName": "",
    "echConfigList": "",
    "muxParams": "",
    "sockoptParams": "",
    "finalMask": "",
    "vlessRoute": "",
    "excludeFromSubTypes": [],
    "mihomoIpVersion": "",
    "mihomoX25519": False,
    "shuffleHost": False,
    "nodeGuids": [],
}

EXTERNAL_PROXY: dict[str, Any] = {
    "forceTls": "same",
    "dest": PUBLIC_HOST,
    "port": PUBLIC_PORT,
    "remark": HOST_GROUP_ID,
    "sni": REALITY_SNI,
}


class CutoverError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never let the Bearer-bearing request leave the loopback endpoint."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _decode_shell_value(raw: str) -> str:
    """Decode ordinary printf-%q output without ever invoking a shell."""
    if raw == "":
        return ""
    if raw.startswith("$'"):
        raise CutoverError("ANSI-C quoted install-result values are unsupported")
    try:
        parts = shlex.split(raw, comments=False, posix=True)
    except ValueError as exc:
        raise CutoverError("invalid quoting in install-result.env") from exc
    if len(parts) != 1:
        raise CutoverError("invalid value in install-result.env")
    return parts[0]


def read_install_env(path: Path) -> dict[str, str]:
    try:
        st = path.lstat()
    except OSError as exc:
        raise CutoverError(f"cannot read install result: {path}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise CutoverError("install-result.env must be a regular file, not a symlink")
    if st.st_uid != 0 or st.st_mode & 0o077:
        raise CutoverError("install-result.env must be owned by root and mode 0600")

    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CutoverError("invalid line in install-result.env")
        key, raw_value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise CutoverError("invalid key in install-result.env")
        result[key] = _decode_shell_value(raw_value)

    token = result.get("XUI_API_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9]{32,128}", token):
        raise CutoverError("XUI_API_TOKEN is absent or has an unexpected format")
    port = result.get("XUI_PANEL_PORT", "")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise CutoverError("XUI_PANEL_PORT is invalid")
    base_path = result.get("XUI_WEB_BASE_PATH", "")
    if base_path and not re.fullmatch(r"/?[A-Za-z0-9_-]+/?", base_path):
        raise CutoverError("XUI_WEB_BASE_PATH has an unexpected format")
    return result


class PanelAPI:
    def __init__(self, env: dict[str, str], timeout: float = 15.0) -> None:
        access = urllib.parse.urlsplit(env.get("XUI_ACCESS_URL", ""))
        scheme = access.scheme.lower()
        if scheme not in {"http", "https"}:
            raise CutoverError("XUI_ACCESS_URL must specify http or https")
        port = int(env["XUI_PANEL_PORT"])
        base_path = "/" + env.get("XUI_WEB_BASE_PATH", "").strip("/")
        if base_path == "/":
            base_path = ""
        self.base = f"{scheme}://127.0.0.1:{port}{base_path}/panel/api"
        self.token = env["XUI_API_TOKEN"]
        self.timeout = timeout
        # Certificate verification is disabled only for a loopback connection.
        # The Bearer token never traverses an external interface.
        self.context = ssl._create_unverified_context() if scheme == "https" else None
        handlers: list[Any] = [urllib.request.ProxyHandler({}), _NoRedirect()]
        if self.context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=self.context))
        self.opener = urllib.request.build_opener(*handlers)

    def call(self, method: str, path: str, payload: Any | None = None) -> Any:
        body = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{self.base}/{path.lstrip('/')}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read(MAX_API_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            raise CutoverError(f"3x-ui API returned HTTP {exc.code} for {path}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CutoverError(f"3x-ui API request failed for {path}") from exc
        if len(raw) > MAX_API_RESPONSE:
            raise CutoverError("3x-ui API response is unexpectedly large")
        try:
            envelope = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CutoverError("3x-ui API returned invalid JSON") from exc
        if not isinstance(envelope, dict) or envelope.get("success") is not True:
            # Do not include the server message: validation errors can quote
            # portions of the submitted (secret-bearing) inbound JSON.
            raise CutoverError(f"3x-ui API rejected {path}")
        return envelope.get("obj")

    def get_inbound(self) -> dict[str, Any]:
        obj = self.call("GET", f"inbounds/get/{INBOUND_ID}")
        if not isinstance(obj, dict):
            raise CutoverError("inbound API response has an unexpected shape")
        return obj

    def get_hosts(self) -> list[dict[str, Any]]:
        obj = self.call("GET", f"hosts/byInbound/{INBOUND_ID}")
        if obj is None:
            return []
        if not isinstance(obj, list) or not all(isinstance(x, dict) for x in obj):
            raise CutoverError("hosts API response has an unexpected shape")
        return obj

    def get_all_hosts(self) -> list[dict[str, Any]]:
        obj = self.call("GET", "hosts/list")
        if obj is None:
            return []
        if not isinstance(obj, list) or not all(isinstance(x, dict) for x in obj):
            raise CutoverError("global hosts API response has an unexpected shape")
        return obj


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CutoverError(f"{field} is not valid JSON") from exc
        if isinstance(decoded, dict):
            return decoded
    raise CutoverError(f"{field} is not a JSON object")


def reality_summary(inbound: dict[str, Any]) -> dict[str, Any]:
    stream = _json_object(inbound.get("streamSettings"), "streamSettings")
    reality = stream.get("realitySettings")
    if not isinstance(reality, dict):
        raise CutoverError("inbound does not contain Reality settings")
    target = reality.get("target", reality.get("dest"))
    names = reality.get("serverNames")
    if not isinstance(names, list):
        names = []
    return {
        "network": stream.get("network"),
        "security": stream.get("security"),
        "target": target,
        "serverNames": [x for x in names if isinstance(x, str)],
    }


def validate_inbound(inbound: dict[str, Any], *, applied: bool | None = None) -> None:
    if inbound.get("id") != INBOUND_ID:
        raise CutoverError("unexpected inbound id")
    if inbound.get("nodeId") is not None:
        raise CutoverError("inbound 6 is node-managed, not local")
    if inbound.get("protocol") != "vless":
        raise CutoverError("inbound 6 is not VLESS")
    summary = reality_summary(inbound)
    if summary["security"] != "reality":
        raise CutoverError("inbound 6 is not Reality")
    if summary["target"] != REALITY_TARGET or REALITY_SNI not in summary["serverNames"]:
        raise CutoverError("Reality target/serverNames do not match the audited topology")
    if summary["network"] not in {"tcp", "raw"}:
        raise CutoverError("inbound 6 is not a TCP/RAW Reality listener")
    if applied is False and inbound.get("port") != PUBLIC_PORT:
        raise CutoverError("inbound 6 is not on the expected pre-cutover port")
    if applied is True and (
        inbound.get("listen") != XRAY_LISTEN or inbound.get("port") != XRAY_PORT
    ):
        raise CutoverError("inbound 6 is not on the expected loopback endpoint")


def host_matches(group: dict[str, Any]) -> bool:
    host_values = group.get("hosts")
    if not (
        group.get("groupId") == HOST_GROUP_ID
        and group.get("inboundIds") == [INBOUND_ID]
        and isinstance(host_values, list)
        and host_values in ([PUBLIC_HOST], [f"{PUBLIC_HOST}:{PUBLIC_PORT}"])
    ):
        return False
    for key, expected in HOST_CREATE_PAYLOAD.items():
        if key in {"groupId", "inboundIds", "hosts"}:
            continue
        if group.get(key) != expected:
            return False
    return True


def validate_hosts(
    hosts: list[dict[str, Any]], all_hosts: list[dict[str, Any]] | None = None
) -> bool:
    """Return True when the managed group must be created."""
    reserved = [] if all_hosts is None else [
        group for group in all_hosts if group.get("groupId") == HOST_GROUP_ID
    ]
    if not hosts:
        if reserved:
            raise CutoverError("the reserved Host group id is already used elsewhere")
        return True
    if len(hosts) == 1 and host_matches(hosts[0]):
        if all_hosts is not None and (len(reserved) != 1 or not host_matches(reserved[0])):
            raise CutoverError("the reserved Host group has an inconsistent global scope")
        return False
    raise CutoverError("inbound 6 already has a conflicting managed Host group")


def _external_proxy_is_exact(value: dict[str, Any]) -> bool:
    return all(value.get(k) == v for k, v in EXTERNAL_PROXY.items())


def build_apply_payload(inbound: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key in WRITABLE_INBOUND_FIELDS if key not in inbound]
    if missing:
        raise CutoverError("inbound response is missing writable fields")
    payload = {key: copy.deepcopy(inbound[key]) for key in WRITABLE_INBOUND_FIELDS}
    stream = _json_object(payload["streamSettings"], "streamSettings")
    existing = stream.get("externalProxy", [])
    if not isinstance(existing, list) or not all(isinstance(x, dict) for x in existing):
        raise CutoverError("streamSettings.externalProxy has an unexpected shape")
    kept: list[dict[str, Any]] = []
    for entry in existing:
        if entry.get("remark") == HOST_GROUP_ID:
            if not _external_proxy_is_exact(entry):
                raise CutoverError("the reserved externalProxy remark is already in use")
            continue
        kept.append(copy.deepcopy(entry))
    kept.append(copy.deepcopy(EXTERNAL_PROXY))
    stream["externalProxy"] = kept
    payload.update(
        {
            "listen": XRAY_LISTEN,
            "port": XRAY_PORT,
            "shareAddrStrategy": "custom",
            "shareAddr": PUBLIC_HOST,
            "streamSettings": stream,
        }
    )
    return payload


def writable_original(inbound: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key in WRITABLE_INBOUND_FIELDS if key not in inbound]
    if missing:
        raise CutoverError("stored inbound is missing writable fields")
    return {key: copy.deepcopy(inbound[key]) for key in WRITABLE_INBOUND_FIELDS}


def writable_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare the update contract while ignoring harmless JSON formatting."""
    for key in WRITABLE_INBOUND_FIELDS:
        if key not in left or key not in right:
            return False
        if key in {"settings", "streamSettings", "sniffing"}:
            try:
                if _json_object(left[key], key) != _json_object(right[key], key):
                    return False
            except CutoverError:
                return False
        elif left[key] != right[key]:
            return False
    return True


def port_is_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        # Match the listener semantics used by Nginx and avoid false failures
        # from recently closed accepted sockets lingering in TIME_WAIT.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def port_is_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_is_open(host, port):
            return
        time.sleep(0.2)
    raise CutoverError(f"expected local listener {host}:{port} did not become ready")


def ensure_state_dir(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        st = path.lstat()
    except OSError as exc:
        raise CutoverError("state directory does not exist") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise CutoverError("state directory is not a real directory")
    if st.st_uid != 0 or st.st_mode & 0o077:
        raise CutoverError("state directory must be root-owned and mode 0700")


def atomic_json(path: Path, value: Any, mode: int = 0o600) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def online_backup(db_path: Path, state_dir: Path) -> Path:
    if not db_path.is_file() or db_path.is_symlink():
        raise CutoverError("3x-ui SQLite database is missing or is a symlink")
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = state_dir / f"x-ui-before-{stamp}.db"
    if destination.exists():
        raise CutoverError("backup destination already exists")
    source_uri = f"file:{urllib.parse.quote(str(db_path))}?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True, timeout=15) as source:
            with sqlite3.connect(destination, timeout=15) as target:
                source.backup(target)
                check = target.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise CutoverError("online SQLite backup failed integrity check")
    except sqlite3.Error as exc:
        raise CutoverError("online SQLite backup failed") from exc
    os.chmod(destination, 0o600)
    return destination


def redacted(inbound: dict[str, Any], hosts: list[dict[str, Any]], stage: str) -> dict[str, Any]:
    summary = reality_summary(inbound)
    return {
        "stage": stage,
        "inbound": {
            "id": inbound.get("id"),
            "tag": inbound.get("tag"),
            "listen": inbound.get("listen"),
            "port": inbound.get("port"),
            "protocol": inbound.get("protocol"),
            "nodeManaged": inbound.get("nodeId") is not None,
            "shareAddrStrategy": inbound.get("shareAddrStrategy"),
            "shareAddr": inbound.get("shareAddr"),
            **summary,
        },
        "hostGroups": [
            {
                "groupId": h.get("groupId"),
                "inboundIds": h.get("inboundIds"),
                "hosts": h.get("hosts"),
                "port": h.get("port"),
                "security": h.get("security"),
                "sni": h.get("sni"),
                "disabled": h.get("isDisabled"),
            }
            for h in hosts
        ],
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updatedAt"] = dt.datetime.now(dt.UTC).isoformat()
    atomic_json(path, state)


def load_state(path: Path) -> dict[str, Any]:
    try:
        st = path.lstat()
    except OSError as exc:
        raise CutoverError("rollback state does not exist") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise CutoverError("rollback state must be a regular mode-0600 file")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CutoverError("rollback state is invalid") from exc
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise CutoverError("unsupported rollback state version")
    return state


def verify_applied(original: dict[str, Any], current: dict[str, Any]) -> None:
    validate_inbound(current, applied=True)
    old_stream = _json_object(original["streamSettings"], "original streamSettings")
    new_stream = _json_object(current["streamSettings"], "current streamSettings")
    if old_stream.get("realitySettings") != new_stream.get("realitySettings"):
        raise CutoverError("Reality settings changed during cutover")
    entries = new_stream.get("externalProxy", [])
    matches = [x for x in entries if isinstance(x, dict) and x.get("remark") == HOST_GROUP_ID]
    if len(matches) != 1 or not _external_proxy_is_exact(matches[0]):
        raise CutoverError("public externalProxy verification failed")
    if current.get("shareAddrStrategy") != "custom" or current.get("shareAddr") != PUBLIC_HOST:
        raise CutoverError("share address verification failed")


def command_dry_run(api: PanelAPI) -> dict[str, Any]:
    inbound = api.get_inbound()
    validate_inbound(inbound, applied=None)
    hosts = api.get_hosts()
    must_create = validate_hosts(hosts, api.get_all_hosts())
    if inbound.get("port") == PUBLIC_PORT and not port_is_free(XRAY_LISTEN, XRAY_PORT):
        raise CutoverError("127.0.0.1:10443 is already occupied")
    result = redacted(inbound, hosts, "dry-run")
    result["plannedHostAction"] = "create" if must_create else "keep-exact"
    result["plannedEndpoint"] = f"{XRAY_LISTEN}:{XRAY_PORT}"
    return result


def command_apply(api: PanelAPI, db_path: Path, state_dir: Path) -> dict[str, Any]:
    state_path = state_dir / "state.json"
    if state_path.exists():
        raise CutoverError("state.json already exists; rollback or archive it before another apply")
    inbound = api.get_inbound()
    validate_inbound(inbound, applied=False)
    hosts = api.get_hosts()
    create_host = validate_hosts(hosts, api.get_all_hosts())
    if not port_is_free(XRAY_LISTEN, XRAY_PORT):
        raise CutoverError("127.0.0.1:10443 is already occupied")
    payload = build_apply_payload(inbound)
    backup = online_backup(db_path, state_dir)
    state: dict[str, Any] = {
        "version": STATE_VERSION,
        "stage": "prepared",
        "inboundId": INBOUND_ID,
        "originalInbound": inbound,
        "originalHosts": hosts,
        # This ownership bit is committed before the API call.  Rollback can
        # therefore clean up an exact managed group even if the process dies
        # immediately after hosts/add succeeds.
        "hostOwnedByApply": create_host,
        "backupPath": str(backup),
        "createdAt": dt.datetime.now(dt.UTC).isoformat(),
    }
    save_state(state_path, state)

    if create_host:
        api.call("POST", "hosts/add", HOST_CREATE_PAYLOAD)
        state["stage"] = "host-created"
        save_state(state_path, state)
    current_hosts = api.get_hosts()
    if len(current_hosts) != 1 or not host_matches(current_hosts[0]):
        raise CutoverError("managed Host verification failed before inbound update")
    if not writable_equal(api.get_inbound(), inbound):
        raise CutoverError("inbound changed concurrently after backup; refusing to overwrite it")

    api.call("POST", f"inbounds/update/{INBOUND_ID}", payload)
    state["stage"] = "inbound-updated"
    save_state(state_path, state)
    api.call("POST", "server/restartXrayService")
    state["stage"] = "xray-restarted"
    save_state(state_path, state)

    wait_for_port(XRAY_LISTEN, XRAY_PORT)
    if not port_is_free("0.0.0.0", PUBLIC_PORT):
        raise CutoverError("public port 443 was not released for the Nginx router")
    current = api.get_inbound()
    verify_applied(inbound, current)
    current_hosts = api.get_hosts()
    if len(current_hosts) != 1 or not host_matches(current_hosts[0]):
        raise CutoverError("managed Host verification failed after restart")
    state["stage"] = "applied"
    state["appliedTag"] = current.get("tag")
    state["appliedInbound"] = current
    save_state(state_path, state)
    status = redacted(current, current_hosts, "applied")
    status["backupPath"] = str(backup)
    atomic_json(state_dir / "status.json", status)
    return status


def command_rollback(api: PanelAPI, state_dir: Path) -> dict[str, Any]:
    state_path = state_dir / "state.json"
    state = load_state(state_path)
    if state.get("stage") == "rolled-back":
        raise CutoverError("this state has already been rolled back")
    original = state.get("originalInbound")
    original_hosts = state.get("originalHosts")
    if not isinstance(original, dict) or not isinstance(original_hosts, list):
        raise CutoverError("rollback state is incomplete")
    validate_inbound(original, applied=False)

    current = api.get_inbound()
    already_original = writable_equal(current, original)
    applied_snapshot = state.get("appliedInbound")
    if (
        not already_original
        and isinstance(applied_snapshot, dict)
        and not writable_equal(current, applied_snapshot)
    ):
        raise CutoverError("inbound changed since apply; refusing to overwrite it during rollback")
    if current.get("port") != original.get("port"):
        old_listen = original.get("listen") or "0.0.0.0"
        bind_host = "0.0.0.0" if old_listen in {"", "0.0.0.0", "::"} else old_listen
        if bind_host == "::":
            bind_host = "0.0.0.0"
        if not port_is_free(bind_host, int(original["port"])):
            raise CutoverError("original Xray port is occupied; stop the Nginx 443 listener first")

    current_hosts = api.get_hosts()
    owned_host_present = False
    if state.get("hostOwnedByApply"):
        if current_hosts and (len(current_hosts) != 1 or not host_matches(current_hosts[0])):
            raise CutoverError("managed Host changed since apply; refusing destructive rollback")
        owned_host_present = len(current_hosts) == 1

    if not already_original:
        api.call("POST", f"inbounds/update/{INBOUND_ID}", writable_original(original))
        api.call("POST", "server/restartXrayService")
        original_listen = original.get("listen")
        if original_listen in {None, "", "0.0.0.0"}:
            probe_host = "127.0.0.1"
        elif original_listen == "::":
            probe_host = "::1"
        else:
            probe_host = str(original_listen)
        wait_for_port(probe_host, int(original["port"]))
    if owned_host_present:
        api.call("POST", f"hosts/del/{HOST_GROUP_ID}")

    restored = api.get_inbound()
    validate_inbound(restored, applied=False)
    old_stream = _json_object(original["streamSettings"], "original streamSettings")
    restored_stream = _json_object(restored["streamSettings"], "restored streamSettings")
    if old_stream.get("realitySettings") != restored_stream.get("realitySettings"):
        raise CutoverError("Reality settings did not restore exactly")
    if old_stream.get("externalProxy", []) != restored_stream.get("externalProxy", []):
        raise CutoverError("externalProxy settings did not restore exactly")
    for key in ("listen", "port", "tag", "shareAddrStrategy", "shareAddr"):
        if restored.get(key) != original.get(key):
            raise CutoverError(f"inbound {key} did not restore exactly")
    restored_hosts = api.get_hosts()
    if state.get("hostOwnedByApply") and restored_hosts:
        raise CutoverError("created Host group was not removed")
    if not state.get("hostOwnedByApply") and (
        len(restored_hosts) != 1 or not host_matches(restored_hosts[0])
    ):
        raise CutoverError("pre-existing managed Host group did not remain exact")
    state["stage"] = "rolled-back"
    state["rolledBackAt"] = dt.datetime.now(dt.UTC).isoformat()
    save_state(state_path, state)
    status = redacted(restored, restored_hosts, "rolled-back")
    atomic_json(state_dir / "status.json", status)
    return status


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("dry-run", "apply", "rollback", "status"))
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    p.add_argument("--db", type=Path, default=DEFAULT_DB_FILE)
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    return p


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    if os.geteuid() != 0:
        print("error: run as root", file=sys.stderr)
        return 1
    try:
        if args.command != "dry-run":
            ensure_state_dir(args.state_dir, create=args.command == "apply")
        if args.command == "status":
            status_path = args.state_dir / "status.json"
            if not status_path.is_file() or status_path.is_symlink():
                raise CutoverError("redacted status does not exist")
            status = json.loads(status_path.read_text(encoding="utf-8"))
        else:
            env = read_install_env(args.env_file)
            if env.get("XUI_DB_TYPE", "sqlite").lower() != "sqlite":
                raise CutoverError("this helper supports only the local SQLite panel")
            api = PanelAPI(env)
            if args.command == "dry-run":
                status = command_dry_run(api)
            elif args.command == "apply":
                status = command_apply(api, args.db, args.state_dir)
            else:
                status = command_rollback(api, args.state_dir)
        print(json.dumps(status, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (CutoverError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
