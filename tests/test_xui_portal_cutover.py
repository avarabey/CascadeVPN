from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/xui_portal_cutover.py"
SPEC = importlib.util.spec_from_file_location("xui_portal_cutover", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
cutover = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cutover)


PRIVATE_KEY = "server-private-key-must-not-leak"


def inbound() -> dict[str, object]:
    return {
        "id": 6,
        "nodeId": None,
        "total": 0,
        "remark": "Reality",
        "subSortIndex": 1,
        "enable": True,
        "expiryTime": 0,
        "trafficReset": "never",
        "trafficResetDay": 1,
        "listen": "",
        "port": 443,
        "protocol": "vless",
        "settings": json.dumps(
            {
                "clients": [
                    {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "email": "private-client",
                    }
                ],
                "decryption": "none",
            }
        ),
        "streamSettings": json.dumps(
            {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "target": "cloud.ru:443",
                    "serverNames": ["cloud.ru"],
                    "privateKey": PRIVATE_KEY,
                    "shortIds": ["0011223344556677"],
                },
                "externalProxy": [
                    {
                        "forceTls": "same",
                        "dest": "existing.example",
                        "port": 8443,
                        "remark": "existing",
                    }
                ],
            }
        ),
        "tag": "in-443-tcp-2",
        "sniffing": json.dumps({"enabled": True, "destOverride": ["http", "tls"]}),
        "shareAddrStrategy": "node",
        "shareAddr": "",
    }


def exact_host() -> dict[str, object]:
    value = copy.deepcopy(cutover.HOST_CREATE_PAYLOAD)
    value["hosts"] = ["ffknd.ru:443"]
    return value


class XuiPortalCutoverTests(unittest.TestCase):
    def test_apply_payload_changes_only_audited_fields(self) -> None:
        source = inbound()
        payload = cutover.build_apply_payload(source)

        self.assertEqual(set(payload), set(cutover.WRITABLE_INBOUND_FIELDS))
        self.assertEqual(payload["listen"], "127.0.0.1")
        self.assertEqual(payload["port"], 10443)
        self.assertEqual(payload["shareAddrStrategy"], "custom")
        self.assertEqual(payload["shareAddr"], "ffknd.ru")
        self.assertEqual(payload["tag"], source["tag"])
        self.assertEqual(payload["settings"], source["settings"])
        self.assertEqual(payload["sniffing"], source["sniffing"])

        old_stream = json.loads(str(source["streamSettings"]))
        new_stream = payload["streamSettings"]
        self.assertEqual(new_stream["realitySettings"], old_stream["realitySettings"])
        self.assertEqual(new_stream["externalProxy"][0], old_stream["externalProxy"][0])
        self.assertEqual(new_stream["externalProxy"][1], cutover.EXTERNAL_PROXY)

    def test_reserved_external_proxy_conflict_aborts(self) -> None:
        source = inbound()
        stream = json.loads(str(source["streamSettings"]))
        stream["externalProxy"].append(
            {"remark": cutover.HOST_GROUP_ID, "dest": "attacker.example", "port": 443}
        )
        source["streamSettings"] = stream
        with self.assertRaises(cutover.CutoverError):
            cutover.build_apply_payload(source)

    def test_writable_compare_ignores_json_formatting_but_not_client_change(self) -> None:
        left = inbound()
        right = copy.deepcopy(left)
        right["settings"] = json.loads(str(right["settings"]))
        right["streamSettings"] = json.loads(str(right["streamSettings"]))
        right["sniffing"] = json.loads(str(right["sniffing"]))
        self.assertTrue(cutover.writable_equal(left, right))

        right["settings"]["clients"][0]["email"] = "changed-client"
        self.assertFalse(cutover.writable_equal(left, right))

    def test_exact_managed_host_is_reused(self) -> None:
        host = exact_host()
        self.assertTrue(cutover.host_matches(host))
        self.assertFalse(cutover.validate_hosts([host], [host]))

    def test_host_defaults_are_part_of_conflict_check(self) -> None:
        host = exact_host()
        host["allowInsecure"] = True
        self.assertFalse(cutover.host_matches(host))
        with self.assertRaises(cutover.CutoverError):
            cutover.validate_hosts([host], [host])

    def test_reserved_host_group_on_other_inbound_aborts(self) -> None:
        host = exact_host()
        host["inboundIds"] = [7]
        with self.assertRaises(cutover.CutoverError):
            cutover.validate_hosts([], [host])

    def test_reality_validation_rejects_node_or_wrong_target(self) -> None:
        source = inbound()
        cutover.validate_inbound(source, applied=False)

        remote = copy.deepcopy(source)
        remote["nodeId"] = 1
        with self.assertRaises(cutover.CutoverError):
            cutover.validate_inbound(remote)

        wrong = copy.deepcopy(source)
        stream = json.loads(str(wrong["streamSettings"]))
        stream["realitySettings"]["target"] = "example.org:443"
        wrong["streamSettings"] = stream
        with self.assertRaises(cutover.CutoverError):
            cutover.validate_inbound(wrong)

    def test_redacted_status_contains_no_client_or_reality_secret(self) -> None:
        rendered = json.dumps(cutover.redacted(inbound(), [], "test"))
        self.assertNotIn(PRIVATE_KEY, rendered)
        self.assertNotIn("private-client", rendered)
        self.assertNotIn("00000000-0000-0000-0000-000000000001", rendered)
        self.assertIn("cloud.ru:443", rendered)

    def test_online_backup_uses_consistent_sqlite_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "x-ui.db"
            state = root / "state"
            state.mkdir()
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE inbounds (id INTEGER PRIMARY KEY, port INTEGER)")
                db.execute("INSERT INTO inbounds VALUES (6, 443)")
            backup = cutover.online_backup(source, state)
            with sqlite3.connect(backup) as db:
                self.assertEqual(db.execute("SELECT port FROM inbounds WHERE id=6").fetchone(), (443,))
                self.assertEqual(db.execute("PRAGMA quick_check").fetchone(), ("ok",))


if __name__ == "__main__":
    unittest.main()
