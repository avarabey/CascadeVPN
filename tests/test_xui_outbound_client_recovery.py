from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import xui_outbound_client_recovery as recovery  # noqa: E402


class OutboundClientRecoveryTests(unittest.TestCase):
    def test_read_values_changes_only_selected_outbound_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "x-ui.db"
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE inbounds (id INTEGER PRIMARY KEY, settings TEXT)")
                connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
                connection.execute(
                    "INSERT INTO inbounds VALUES (?, ?)",
                    (2, json.dumps({"clients": [{
                        "email": "client", "id": "new-uuid", "flow": ""
                    }]})),
                )
                template = {"outbounds": [
                    {"tag": "Poland TCP", "protocol": "vless", "settings": {
                        "id": "old-uuid", "flow": "xtls-rprx-vision"
                    }},
                    {"tag": "direct", "protocol": "freedom", "settings": {}},
                ]}
                connection.execute(
                    "INSERT INTO settings VALUES (?, ?)",
                    ("xrayTemplateConfig", json.dumps(template)),
                )
            original, applied, desired, flow = recovery.read_values(
                db, 2, "client", "Poland TCP"
            )
            changed = json.loads(applied)
            self.assertEqual(desired, "new-uuid")
            self.assertEqual(flow, "")
            self.assertEqual(changed["outbounds"][0]["settings"]["id"], "new-uuid")
            self.assertEqual(changed["outbounds"][0]["settings"]["flow"], "")
            changed["outbounds"][0]["settings"]["id"] = "old-uuid"
            changed["outbounds"][0]["settings"]["flow"] = "xtls-rprx-vision"
            self.assertEqual(json.loads(original), changed)

    def test_replace_value_is_compare_and_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "x-ui.db"
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
                connection.execute("INSERT INTO settings VALUES ('xrayTemplateConfig', 'old')")
            recovery.replace_value(db, "old", "new")
            with self.assertRaises(recovery.CutoverError):
                recovery.replace_value(db, "old", "other")


if __name__ == "__main__":
    unittest.main()
