#!/usr/bin/env python3
"""Transactionally sync one Xray template outbound UUID with a local client."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any

from xui_portal_cutover import (
    CutoverError,
    DEFAULT_DB_FILE,
    atomic_json,
    ensure_state_dir,
    online_backup,
)


STATE_VERSION = 1
DEFAULT_STATE_DIR = Path("/var/lib/ffknd-outbound-client-recovery")
DEFAULT_ACTIVE_CONFIG = Path("/usr/local/x-ui/bin/config.json")


def secret_fp(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def read_values(db: Path, inbound_id: int, email: str, outbound_tag: str) -> tuple[str, str, str, str]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15) as connection:
        row = connection.execute(
            "SELECT settings FROM inbounds WHERE id = ?", (inbound_id,)
        ).fetchone()
        template_row = connection.execute(
            "SELECT value FROM settings WHERE key = 'xrayTemplateConfig'"
        ).fetchone()
    if row is None or template_row is None:
        raise CutoverError("inbound or xrayTemplateConfig is missing")
    inbound_settings = json.loads(row[0])
    matches = [
        client for client in inbound_settings.get("clients", [])
        if isinstance(client, dict) and client.get("email") == email
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
        raise CutoverError("source client is not unique or has no UUID")
    template = json.loads(template_row[0])
    outbounds = template.get("outbounds", [])
    targets = [item for item in outbounds if item.get("tag") == outbound_tag]
    if len(targets) != 1 or targets[0].get("protocol") != "vless":
        raise CutoverError("target VLESS outbound is not unique")
    old_uuid = targets[0].get("settings", {}).get("id")
    if not isinstance(old_uuid, str) or not old_uuid:
        raise CutoverError("target outbound UUID is absent")
    targets[0]["settings"]["id"] = matches[0]["id"]
    desired_flow = matches[0].get("flow") or ""
    targets[0]["settings"]["flow"] = desired_flow
    applied = json.dumps(template, ensure_ascii=False, separators=(",", ":"))
    return template_row[0], applied, matches[0]["id"], desired_flow


def replace_value(db: Path, expected: str, replacement: str) -> None:
    with sqlite3.connect(db, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE settings SET value = ? WHERE key = 'xrayTemplateConfig' AND value = ?",
            (replacement, expected),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise CutoverError("xrayTemplateConfig changed concurrently")
        connection.commit()


def active_credentials(config: Path, outbound_tag: str) -> tuple[str, str]:
    payload = json.loads(config.read_text(encoding="utf-8"))
    targets = [item for item in payload.get("outbounds", []) if item.get("tag") == outbound_tag]
    if len(targets) != 1:
        raise CutoverError("active target outbound is not unique")
    value = targets[0].get("settings", {}).get("id")
    if not isinstance(value, str) or not value:
        raise CutoverError("active target outbound UUID is absent")
    return value, targets[0].get("settings", {}).get("flow") or ""


def template_credentials(raw: str, outbound_tag: str) -> tuple[str, str]:
    payload = json.loads(raw)
    targets = [item for item in payload.get("outbounds", []) if item.get("tag") == outbound_tag]
    if len(targets) != 1:
        raise CutoverError("template target outbound is not unique")
    value = targets[0].get("settings", {}).get("id")
    if not isinstance(value, str) or not value:
        raise CutoverError("template target outbound UUID is absent")
    return value, targets[0].get("settings", {}).get("flow") or ""


def restart_and_verify(
    config: Path, outbound_tag: str, expected_uuid: str, expected_flow: str
) -> None:
    subprocess.run(["systemctl", "restart", "x-ui.service"], check=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if active_credentials(config, outbound_tag) == (expected_uuid, expected_flow):
                return
        except (OSError, ValueError, KeyError, json.JSONDecodeError, CutoverError):
            pass
        time.sleep(0.25)
    raise CutoverError("active Xray config did not receive the expected UUID")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("dry-run", "apply", "rollback", "status"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_FILE)
    parser.add_argument("--active-config", type=Path, default=DEFAULT_ACTIVE_CONFIG)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--inbound-id", type=int, default=2)
    parser.add_argument("--email")
    parser.add_argument("--outbound-tag", default="Poland TCP")
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        print("error: run as root", file=sys.stderr)
        return 1
    try:
        state_path = args.state_dir / "state.json"
        if args.command == "status":
            result = json.loads((args.state_dir / "status.json").read_text())
        elif args.command == "rollback":
            state = json.loads(state_path.read_text())
            if state.get("version") != STATE_VERSION or state.get("stage") != "applied":
                raise CutoverError("rollback state is not applied")
            current, _unused, _uuid, _flow = read_values(
                args.db, state["inboundId"], state["email"], state["outboundTag"]
            )
            if current != state["appliedTemplate"]:
                raise CutoverError("xrayTemplateConfig changed since apply")
            replace_value(args.db, current, state["originalTemplate"])
            original_uuid = state["originalUuid"]
            restart_and_verify(
                args.active_config, state["outboundTag"],
                original_uuid, state["originalFlow"],
            )
            state["stage"] = "rolled-back"
            atomic_json(state_path, state)
            result = {"stage": "rolled-back", "outboundTag": state["outboundTag"]}
            atomic_json(args.state_dir / "status.json", result)
        else:
            if not args.email:
                raise CutoverError("email is required")
            original, applied, desired_uuid, desired_flow = read_values(
                args.db, args.inbound_id, args.email, args.outbound_tag
            )
            original_uuid, original_flow = active_credentials(
                args.active_config, args.outbound_tag
            )
            if template_credentials(original, args.outbound_tag) != (
                original_uuid, original_flow
            ):
                raise CutoverError("active config and database template credentials differ")
            result: dict[str, Any] = {
                "stage": "dry-run",
                "outboundTag": args.outbound_tag,
                "oldUuidFingerprint": secret_fp(original_uuid),
                "newUuidFingerprint": secret_fp(desired_uuid),
                "flowChanged": original_flow != desired_flow,
            }
            if args.command == "apply":
                ensure_state_dir(args.state_dir, create=True)
                if state_path.exists():
                    raise CutoverError("state already exists; rollback or archive it first")
                backup = online_backup(args.db, args.state_dir)
                state = {
                    "version": STATE_VERSION,
                    "stage": "prepared",
                    "backupPath": str(backup),
                    "inboundId": args.inbound_id,
                    "email": args.email,
                    "outboundTag": args.outbound_tag,
                    "originalUuid": original_uuid,
                    "originalFlow": original_flow,
                    "originalTemplate": original,
                    "appliedTemplate": applied,
                }
                atomic_json(state_path, state)
                changed = False
                try:
                    replace_value(args.db, original, applied)
                    changed = True
                    restart_and_verify(
                        args.active_config, args.outbound_tag, desired_uuid, desired_flow
                    )
                except Exception:
                    if changed:
                        replace_value(args.db, applied, original)
                        restart_and_verify(
                            args.active_config, args.outbound_tag,
                            original_uuid, original_flow,
                        )
                    raise
                state["stage"] = "applied"
                atomic_json(state_path, state)
                result.update(stage="applied", backupPath=str(backup))
                atomic_json(args.state_dir / "status.json", result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (CutoverError, OSError, ValueError, KeyError, json.JSONDecodeError,
            sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
