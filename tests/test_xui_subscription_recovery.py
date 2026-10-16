from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import xui_subscription_recovery as recovery  # noqa: E402


def inbound(sub_id: str = "new-token") -> dict:
    return {
        "id": 2,
        "total": 10,
        "remark": "France TCP",
        "subSortIndex": 0,
        "enable": True,
        "expiryTime": 0,
        "trafficReset": "never",
        "trafficResetDay": 1,
        "listen": "",
        "port": 443,
        "protocol": "vless",
        "settings": json.dumps({
            "clients": [{
                "id": "00000000-0000-0000-0000-000000000001",
                "email": "current-client",
                "subId": sub_id,
            }],
            "decryption": "none",
        }),
        "streamSettings": json.dumps({"network": "tcp", "security": "none"}),
        "tag": "in-443-tcp",
        "sniffing": json.dumps({"enabled": True}),
        "shareAddrStrategy": "node",
        "shareAddr": "",
    }


class SubscriptionRecoveryTests(unittest.TestCase):
    def test_source_sub_id_requires_same_value_on_every_inbound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.db"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE inbounds (id INTEGER PRIMARY KEY, settings TEXT)"
                )
                for inbound_id in (2, 5, 6):
                    connection.execute(
                        "INSERT INTO inbounds VALUES (?, ?)",
                        (inbound_id, json.dumps({"clients": [{
                            "email": "old-client", "subId": "restored-token"
                        }]})),
                    )
            self.assertEqual(
                recovery.source_sub_id(path, [2, 5, 6], "old-client"),
                "restored-token",
            )

    def test_build_payload_changes_only_sub_id(self):
        original = inbound()
        payload, old_sub_id = recovery.build_payload(
            original, "current-client", "restored-token"
        )
        self.assertEqual(old_sub_id, "new-token")
        self.assertEqual(payload["subId"], "restored-token")
        payload["subId"] = old_sub_id
        self.assertEqual(payload, recovery.target_client(original, "current-client"))

    def test_inbound_verification_allows_only_sub_id_and_update_time(self):
        original = inbound()
        current = inbound("restored-token")
        original_client = recovery.target_client(original, "current-client")
        current_client = recovery.target_client(current, "current-client")
        original_client["updated_at"] = 1
        original_client["resetWeekday"] = None
        current_client["updated_at"] = 2
        current_client["resetWeekday"] = 0
        original_settings, original_clients = recovery.clients_from(original)
        current_settings, current_clients = recovery.clients_from(current)
        original_clients[0] = original_client
        current_clients[0] = current_client
        original_settings["clients"] = original_clients
        current_settings["clients"] = current_clients
        original["settings"] = original_settings
        current["settings"] = current_settings
        self.assertTrue(recovery.inbound_equal_after_client_update(
            original, current, "current-client", "restored-token"
        ))
        current["port"] = 444
        self.assertFalse(recovery.inbound_equal_after_client_update(
            original, current, "current-client", "restored-token"
        ))

    def test_parse_ids_rejects_duplicates(self):
        with self.assertRaises(Exception):
            recovery.parse_ids("2,2,6")


if __name__ == "__main__":
    unittest.main()
