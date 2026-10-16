#!/usr/bin/env python3
"""Transactionally restore one 3x-ui subscription id from a trusted backup."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any
import urllib.parse

from xui_portal_cutover import (
    CutoverError,
    DEFAULT_DB_FILE,
    DEFAULT_ENV_FILE,
    PanelAPI,
    _json_object,
    atomic_json,
    ensure_state_dir,
    online_backup,
    read_install_env,
    writable_equal,
    writable_original,
)


STATE_VERSION = 2
DEFAULT_STATE_DIR = Path("/var/lib/ffknd-subscription-recovery")


def parse_ids(value: str) -> list[int]:
    try:
        result = [int(part) for part in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("inbound ids must be comma-separated integers") from exc
    if not result or len(result) != len(set(result)) or any(item < 1 for item in result):
        raise argparse.ArgumentTypeError("inbound ids must be unique positive integers")
    return result


def secret_fp(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def clients_from(inbound: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    settings = _json_object(inbound.get("settings"), "settings")
    clients = settings.get("clients")
    if not isinstance(clients, list) or not all(isinstance(item, dict) for item in clients):
        raise CutoverError("inbound clients have an unexpected shape")
    return settings, clients


def target_client(inbound: dict[str, Any], email: str) -> dict[str, Any]:
    _settings, clients = clients_from(inbound)
    matches = [item for item in clients if item.get("email") == email]
    if len(matches) != 1:
        raise CutoverError(f"target client is not unique in inbound {inbound.get('id')}")
    return copy.deepcopy(matches[0])


def client_equal_except_update_time(
    expected: dict[str, Any], current: dict[str, Any], expected_sub_id: str
) -> bool:
    left = copy.deepcopy(expected)
    right = copy.deepcopy(current)
    left["subId"] = expected_sub_id
    for item in (left, right):
        item.pop("updated_at", None)
        item.pop("updatedAt", None)
        # 3x-ui stores the disabled weekly-reset default as either omitted,
        # null, or zero depending on the inbound's creation version.
        item["resetWeekday"] = int(item.get("resetWeekday") or 0)
    return left == right


def inbound_equal_after_client_update(
    original: dict[str, Any], current: dict[str, Any], email: str, expected_sub_id: str
) -> bool:
    if not isinstance(current, dict):
        return False
    left = writable_original(original)
    right = writable_original(current)
    left_settings, left_clients = clients_from(left)
    right_settings, right_clients = clients_from(right)
    left_matches = [i for i, item in enumerate(left_clients) if item.get("email") == email]
    right_matches = [i for i, item in enumerate(right_clients) if item.get("email") == email]
    if len(left_matches) != 1 or left_matches != right_matches:
        return False
    index = left_matches[0]
    if not client_equal_except_update_time(
        left_clients[index], right_clients[index], expected_sub_id
    ):
        return False
    right_clients[index] = copy.deepcopy(left_clients[index])
    right_settings["clients"] = right_clients
    left_settings["clients"] = left_clients
    left["settings"] = left_settings
    right["settings"] = right_settings
    return writable_equal(left, right)


def source_sub_id(db_path: Path, inbound_ids: list[int], email: str) -> str:
    if not db_path.is_file() or db_path.is_symlink():
        raise CutoverError("source database is missing or is a symlink")
    source_uri = f"file:{db_path}?mode=ro"
    found: list[str] = []
    try:
        with sqlite3.connect(source_uri, uri=True, timeout=15) as connection:
            for inbound_id in inbound_ids:
                row = connection.execute(
                    "SELECT settings FROM inbounds WHERE id = ?", (inbound_id,)
                ).fetchone()
                if row is None:
                    raise CutoverError(f"source inbound {inbound_id} is absent")
                settings = _json_object(row[0], "source settings")
                matches = [
                    item for item in settings.get("clients", [])
                    if isinstance(item, dict) and item.get("email") == email
                ]
                if len(matches) != 1 or not isinstance(matches[0].get("subId"), str):
                    raise CutoverError(f"source client is not unique in inbound {inbound_id}")
                found.append(matches[0]["subId"])
    except sqlite3.Error as exc:
        raise CutoverError("cannot read source database") from exc
    if len(set(found)) != 1 or not found[0]:
        raise CutoverError("source subId is empty or inconsistent across inbounds")
    return found[0]


def build_payload(inbound: dict[str, Any], email: str, sub_id: str) -> tuple[dict[str, Any], str]:
    payload = target_client(inbound, email)
    old_sub_id = payload.get("subId")
    if not isinstance(old_sub_id, str) or not old_sub_id:
        raise CutoverError(f"target subId is absent in inbound {inbound.get('id')}")
    payload["subId"] = sub_id
    return payload, old_sub_id


def prepare(api: PanelAPI, inbound_ids: list[int], email: str, sub_id: str) -> tuple[list[dict], dict, dict, dict, list[dict]]:
    originals: list[dict] = []
    status: list[dict] = []
    encoded_email = urllib.parse.quote(email, safe="")
    client_snapshot = api.call("GET", f"clients/get/{encoded_email}")
    if not isinstance(client_snapshot, dict) or not isinstance(client_snapshot.get("client"), dict):
        raise CutoverError("client API response is invalid")
    attached = client_snapshot.get("inboundIds")
    if not isinstance(attached, list) or sorted(attached) != sorted(inbound_ids):
        raise CutoverError("target client inbound attachments do not match requested ids")
    original_payload: dict[str, Any] | None = None
    applied_payload: dict[str, Any] | None = None
    for inbound_id in inbound_ids:
        inbound = api.call("GET", f"inbounds/get/{inbound_id}")
        if not isinstance(inbound, dict) or inbound.get("id") != inbound_id:
            raise CutoverError(f"inbound {inbound_id} API response is invalid")
        payload, old_sub_id = build_payload(inbound, email, sub_id)
        if original_payload is None:
            original_payload = target_client(inbound, email)
            applied_payload = payload
        elif not client_equal_except_update_time(
            original_payload, target_client(inbound, email), old_sub_id
        ):
            raise CutoverError("target client differs between attached inbounds")
        originals.append(inbound)
        status.append({
            "id": inbound_id,
            "remark": inbound.get("remark"),
            "email": email,
            "clients": len(clients_from(inbound)[1]),
            "oldSubIdFingerprint": secret_fp(old_sub_id),
            "newSubIdFingerprint": secret_fp(sub_id),
        })
    if original_payload is None or applied_payload is None:
        raise CutoverError("no target inbounds were prepared")
    return originals, client_snapshot, original_payload, applied_payload, status


def verify_inbounds(
    api: PanelAPI, originals: list[dict], email: str, expected_sub_id: str
) -> None:
    for original in originals:
        inbound_id = int(original["id"])
        current = api.call("GET", f"inbounds/get/{inbound_id}")
        if not inbound_equal_after_client_update(original, current, email, expected_sub_id):
            raise CutoverError(f"post-update verification failed for inbound {inbound_id}")


def verify_client_record(
    api: PanelAPI, snapshot: dict[str, Any], email: str, expected_sub_id: str
) -> None:
    encoded_email = urllib.parse.quote(email, safe="")
    current = api.call("GET", f"clients/get/{encoded_email}")
    if not isinstance(current, dict) or current.get("inboundIds") != snapshot.get("inboundIds"):
        raise CutoverError("client attachment verification failed")
    before = snapshot.get("client")
    after = current.get("client")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise CutoverError("client record verification failed")
    if not client_equal_except_update_time(before, after, expected_sub_id):
        raise CutoverError("client record changed beyond subId and update time")


def apply(api: PanelAPI, db: Path, state_dir: Path, originals: list[dict], client_snapshot: dict, original_client: dict, applied_client: dict, status: list[dict]) -> dict:
    ensure_state_dir(state_dir, create=True)
    state_path = state_dir / "state.json"
    if state_path.exists():
        raise CutoverError("state already exists; rollback or archive it first")
    backup = online_backup(db, state_dir)
    state = {
        "version": STATE_VERSION,
        "stage": "prepared",
        "backupPath": str(backup),
        "originals": originals,
        "clientSnapshot": client_snapshot,
        "originalClient": original_client,
        "appliedClient": applied_client,
    }
    atomic_json(state_path, state)
    email = original_client["email"]
    encoded_email = urllib.parse.quote(email, safe="")
    attempted = False
    try:
        attempted = True
        api.call("POST", f"clients/update/{encoded_email}", applied_client)
        verify_client_record(api, client_snapshot, email, applied_client["subId"])
        verify_inbounds(api, originals, email, applied_client["subId"])
    except Exception:
        if attempted:
            api.call("POST", f"clients/update/{encoded_email}", original_client)
            verify_client_record(api, client_snapshot, email, original_client["subId"])
            verify_inbounds(api, originals, email, original_client["subId"])
        raise
    state["stage"] = "applied"
    atomic_json(state_path, state)
    result = {"stage": "applied", "backupPath": str(backup), "inbounds": status}
    atomic_json(state_dir / "status.json", result)
    return result


def rollback(api: PanelAPI, state_dir: Path) -> dict:
    state_path = state_dir / "state.json"
    st = state_path.lstat()
    if not state_path.is_file() or state_path.is_symlink() or st.st_mode & 0o077:
        raise CutoverError("rollback state must be a regular private file")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("version") != STATE_VERSION or state.get("stage") != "applied":
        raise CutoverError("rollback state is not applied")
    originals = state["originals"]
    original_client = state["originalClient"]
    applied_client = state["appliedClient"]
    client_snapshot = state["clientSnapshot"]
    email = original_client["email"]
    verify_client_record(api, client_snapshot, email, applied_client["subId"])
    verify_inbounds(api, originals, email, applied_client["subId"])
    encoded_email = urllib.parse.quote(email, safe="")
    api.call("POST", f"clients/update/{encoded_email}", original_client)
    verify_client_record(api, client_snapshot, email, original_client["subId"])
    verify_inbounds(api, originals, email, original_client["subId"])
    state["stage"] = "rolled-back"
    atomic_json(state_path, state)
    result = {"stage": "rolled-back", "inboundIds": [item["id"] for item in originals]}
    atomic_json(state_dir / "status.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("dry-run", "apply", "rollback", "status"))
    parser.add_argument("--source-db", type=Path)
    parser.add_argument("--source-email")
    parser.add_argument("--target-email")
    parser.add_argument("--inbound-ids", type=parse_ids, default=[2, 5, 6])
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_FILE)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        print("error: run as root", file=sys.stderr)
        return 1
    try:
        if args.command == "status":
            result = json.loads((args.state_dir / "status.json").read_text(encoding="utf-8"))
        else:
            api = PanelAPI(read_install_env(args.env_file))
            if args.command == "rollback":
                result = rollback(api, args.state_dir)
            else:
                if args.source_db is None or not args.source_email or not args.target_email:
                    raise CutoverError("source-db, source-email and target-email are required")
                sub_id = source_sub_id(args.source_db, args.inbound_ids, args.source_email)
                originals, client_snapshot, original_client, applied_client, status = prepare(
                    api, args.inbound_ids, args.target_email, sub_id
                )
                result = {"stage": "dry-run", "inbounds": status}
                if args.command == "apply":
                    result = apply(
                        api, args.db, args.state_dir, originals,
                        client_snapshot, original_client, applied_client, status,
                    )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (CutoverError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
