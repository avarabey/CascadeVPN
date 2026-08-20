#!/usr/bin/env python3
"""Read-only, redacted probe for the local 3x-ui API.

The script is intended to run as root on the server.  It reads the installer
environment without echoing credentials and prints only the fields needed for
the ffknd.ru cutover preflight.
"""

from __future__ import annotations

import argparse
import json
import shlex
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


def load_shell_environment(path: str) -> dict[str, str]:
    command = f"set -a; . {shlex.quote(path)}; env -0"
    completed = subprocess.run(
        ["/bin/bash", "-c", command],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result: dict[str, str] = {}
    for item in completed.stdout.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        result[key.decode()] = value.decode()
    return result


class XUIAPI:
    def __init__(self, environment: dict[str, str]) -> None:
        token = environment.get("XUI_API_TOKEN", "")
        port = environment.get("XUI_PANEL_PORT", "")
        if not token or not port.isdigit():
            raise RuntimeError("XUI_API_TOKEN/XUI_PANEL_PORT are missing")

        access = urllib.parse.urlsplit(environment.get("XUI_ACCESS_URL", ""))
        scheme = access.scheme if access.scheme in {"http", "https"} else "http"
        base_path = environment.get("XUI_WEB_BASE_PATH", "/").strip("/")
        suffix = f"/{base_path}/" if base_path else "/"
        self.base_url = f"{scheme}://127.0.0.1:{int(port)}{suffix}"
        self.token = token
        self.ssl_context = ssl._create_unverified_context() if scheme == "https" else None

    def get(self, path: str) -> object:
        url = urllib.parse.urljoin(self.base_url, path.lstrip("/"))
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=10, context=self.ssl_context
            ) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"3x-ui API returned HTTP {exc.code}") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise RuntimeError("3x-ui API returned an unsuccessful envelope")
        return payload.get("obj")


def reality_summary(inbound: dict[str, object]) -> dict[str, object]:
    stream = inbound.get("streamSettings")
    reality = stream.get("realitySettings", {}) if isinstance(stream, dict) else {}
    server_names = reality.get("serverNames", []) if isinstance(reality, dict) else []
    target = reality.get("target", "") if isinstance(reality, dict) else ""
    return {
        "id": inbound.get("id"),
        "nodeId": inbound.get("nodeId"),
        "listen": inbound.get("listen"),
        "port": inbound.get("port"),
        "protocol": inbound.get("protocol"),
        "tag": inbound.get("tag"),
        "shareAddrStrategy": inbound.get("shareAddrStrategy"),
        "shareAddr": inbound.get("shareAddr"),
        "realityTarget": target,
        "realityServerNames": server_names,
        "hasPrivateKey": bool(reality.get("privateKey"))
        if isinstance(reality, dict)
        else False,
        "clientCount": len(inbound.get("settings", {}).get("clients", []))
        if isinstance(inbound.get("settings"), dict)
        else 0,
    }


def host_summary(host: object) -> dict[str, object]:
    if not isinstance(host, dict):
        return {"invalid": True}
    return {
        "groupId": host.get("groupId"),
        "hosts": host.get("hosts"),
        "port": host.get("port"),
        "security": host.get("security"),
        "sni": host.get("sni"),
        "isDisabled": host.get("isDisabled"),
        "isHidden": host.get("isHidden"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="/etc/x-ui/install-result.env")
    parser.add_argument("--inbound-id", type=int, default=6)
    args = parser.parse_args()

    api = XUIAPI(load_shell_environment(args.env))
    inbound = api.get(f"panel/api/inbounds/get/{args.inbound_id}")
    hosts = api.get(f"panel/api/hosts/byInbound/{args.inbound_id}")
    if not isinstance(inbound, dict):
        raise RuntimeError(
            f"unexpected inbound response type: {type(inbound).__name__}"
        )
    if hosts is None:
        hosts = []
    if not isinstance(hosts, list):
        raise RuntimeError(f"unexpected hosts response type: {type(hosts).__name__}")

    print(
        json.dumps(
            {
                "api": "ok",
                "inbound": reality_summary(inbound),
                "hosts": [host_summary(host) for host in hosts],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
