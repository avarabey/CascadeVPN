import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ttx_bridge", ROOT / "bridge" / "ttx_bridge.py")
ttx = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ttx)


def config(base: Path, target: Path) -> "ttx.Config":
    return ttx.Config({
        "panel": {"api_token": "test-token"},
        "ingress": {"port": 10800, "listen": "127.0.0.1"},
        "trusttunnel": {
            "base_config": str(base),
            "target_config": str(target),
            "restart_on_change": False,
        },
    })


class BridgeTests(unittest.TestCase):
    def test_config_requires_panel_auth(self):
        with self.assertRaises(ValueError):
            ttx.Config({"panel": {}})

    def test_config_rejects_same_base_and_target(self):
        with self.assertRaises(ValueError):
            config(Path("same"), Path("same"))

    def test_ipv6_socks_address_is_bracketed(self):
        cfg = config(Path("base"), Path("target"))
        cfg.ing_listen = "::1"
        self.assertEqual(cfg.socks_address, "[::1]:10800")

    def test_find_ingress_uses_owned_remark_only(self):
        cfg = config(Path("base"), Path("target"))
        unrelated = {"remark": "operator-owned", "port": 10800, "protocol": "socks"}
        self.assertIsNone(ttx.find_ingress([unrelated], cfg))

    def test_render_replaces_forward_protocol_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "vpn.base.toml"
            target = root / "vpn.toml"
            base.write_text(
                'listen_address = "0.0.0.0:443"\n'
                '[forward_protocol]\n'
                'direct = {}\n'
                '[metrics]\n'
                'address = "127.0.0.1:1987"\n',
                encoding="utf-8",
            )
            cfg = config(base, target)
            rendered = ttx.render_vpn_config(cfg)
            self.assertEqual(rendered.count(ttx.MARKER), 1)
            self.assertEqual(rendered.count("[forward_protocol.socks5]"), 1)
            self.assertNotIn("direct = {}", rendered)
            target.write_text(rendered, encoding="utf-8")
            base.write_text(rendered, encoding="utf-8")
            self.assertEqual(ttx.render_vpn_config(cfg), rendered)

    def test_guard_loop_rejects_same_port(self):
        cfg = config(Path("base"), Path("target"))
        with self.assertRaises(SystemExit):
            ttx.guard_loop(cfg, 'listen_address = "0.0.0.0:10800"\n')

    def test_json_field_supports_legacy_and_current_api(self):
        value = {"udp": True}
        self.assertEqual(ttx.json_field(json.dumps(value)), value)
        self.assertEqual(ttx.json_field(value), value)
        self.assertTrue(ttx.contains_fields({"udp": True, "newDefault": 1}, value))


if __name__ == "__main__":
    unittest.main()
