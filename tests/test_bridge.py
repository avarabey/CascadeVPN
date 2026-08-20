import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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

    def test_render_preserves_reverse_proxy_when_generating_socks5(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "vpn.base.toml"
            target = root / "vpn.toml"
            reverse_proxy = (
                '[reverse_proxy]\n'
                'server_address = "127.0.0.1:8080"\n'
                'path_mask = "/"\n'
                'h3_backward_compatibility = false\n'
            )
            base.write_text(
                'listen_address = "0.0.0.0:443"\n'
                '[forward_protocol]\n'
                'direct = {}\n'
                + reverse_proxy
                + '[metrics]\n'
                'address = "127.0.0.1:1987"\n',
                encoding="utf-8",
            )
            cfg = config(base, target)

            rendered = ttx.render_vpn_config(cfg)

            self.assertEqual(rendered.count("[reverse_proxy]"), 1)
            self.assertIn(reverse_proxy, rendered)
            self.assertNotIn("direct = {}", rendered)
            self.assertEqual(rendered.count("[forward_protocol.socks5]"), 1)
            self.assertIn('address = "127.0.0.1:10800"', rendered)

            # Re-rendering an already managed config must preserve both
            # independent tables without duplicating either of them.
            base.write_text(rendered, encoding="utf-8")
            rerendered = ttx.render_vpn_config(cfg)
            self.assertEqual(rerendered, rendered)
            self.assertEqual(rerendered.count("[reverse_proxy]"), 1)
            self.assertEqual(rerendered.count("[forward_protocol.socks5]"), 1)

    def test_guard_loop_rejects_same_port(self):
        cfg = config(Path("base"), Path("target"))
        with self.assertRaises(SystemExit):
            ttx.guard_loop(cfg, 'listen_address = "0.0.0.0:10800"\n')

    def test_json_field_supports_legacy_and_current_api(self):
        value = {"udp": True}
        self.assertEqual(ttx.json_field(json.dumps(value)), value)
        self.assertEqual(ttx.json_field(value), value)
        self.assertTrue(ttx.contains_fields({"udp": True, "newDefault": 1}, value))

    def test_invalid_toml_is_rejected_before_target_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "vpn.base.toml"
            target = root / "vpn.toml"
            target.write_text('listen_address = "0.0.0.0:443"\n', encoding="utf-8")
            cfg = config(base, target)
            cfg.tt_restart = True

            with mock.patch.object(ttx.subprocess, "run") as run:
                with self.assertRaises(ttx.TrustTunnelApplyError):
                    ttx.apply_trusttunnel_config(
                        cfg, 'listen_address = "unterminated\n', dry_run=False)

            self.assertEqual(
                target.read_text(encoding="utf-8"),
                'listen_address = "0.0.0.0:443"\n',
            )
            self.assertFalse(target.with_suffix(".toml.ttx-bak").exists())
            run.assert_not_called()

    def test_successful_apply_restarts_and_health_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "vpn.base.toml"
            target = root / "vpn.toml"
            old = 'listen_address = "0.0.0.0:443"\n'
            new = old + '[metrics]\naddress = "127.0.0.1:1987"\n'
            target.write_text(old, encoding="utf-8")
            target.chmod(0o640)
            cfg = config(base, target)
            cfg.tt_restart = True

            with mock.patch.object(ttx.subprocess, "run") as run:
                changed = ttx.apply_trusttunnel_config(cfg, new, dry_run=False)

            self.assertTrue(changed)
            self.assertEqual(target.read_text(encoding="utf-8"), new)
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertEqual(target.with_suffix(".toml.ttx-bak").read_text(), old)
            self.assertEqual(
                [call.args[0][1] for call in run.call_args_list],
                ["restart", "is-active"],
            )
            for call in run.call_args_list:
                self.assertEqual(call.kwargs["timeout"], 30.0)

    def test_restart_failure_rolls_back_and_restarts_previous_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "vpn.base.toml"
            target = root / "vpn.toml"
            old = 'listen_address = "0.0.0.0:443"\n'
            new = 'listen_address = "0.0.0.0:8443"\n'
            target.write_text(old, encoding="utf-8")
            cfg = config(base, target)
            cfg.tt_restart = True
            failure = ttx.subprocess.CalledProcessError(1, ["systemctl", "restart"])

            with mock.patch.object(
                    ttx.subprocess, "run",
                    side_effect=[failure, mock.DEFAULT, mock.DEFAULT]) as run:
                with self.assertRaises(ttx.TrustTunnelApplyError):
                    ttx.apply_trusttunnel_config(cfg, new, dry_run=False)

            self.assertEqual(target.read_text(encoding="utf-8"), old)
            self.assertEqual(
                [call.args[0][1] for call in run.call_args_list],
                ["restart", "restart", "is-active"],
            )

    def test_health_failure_rolls_back_and_restarts_previous_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "vpn.base.toml"
            target = root / "vpn.toml"
            old = 'listen_address = "0.0.0.0:443"\n'
            new = 'listen_address = "0.0.0.0:8443"\n'
            target.write_text(old, encoding="utf-8")
            cfg = config(base, target)
            cfg.tt_restart = True
            failure = ttx.subprocess.CalledProcessError(3, ["systemctl", "is-active"])

            with mock.patch.object(
                    ttx.subprocess, "run",
                    side_effect=[mock.DEFAULT, failure, mock.DEFAULT, mock.DEFAULT]) as run:
                with self.assertRaises(ttx.TrustTunnelApplyError):
                    ttx.apply_trusttunnel_config(cfg, new, dry_run=False)

            self.assertEqual(target.read_text(encoding="utf-8"), old)
            self.assertEqual(
                [call.args[0][1] for call in run.call_args_list],
                ["restart", "is-active", "restart", "is-active"],
            )

    def test_rollback_removes_target_that_did_not_exist_before_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "vpn.base.toml"
            target = root / "vpn.toml"
            cfg = config(base, target)
            cfg.tt_restart = True
            failure = ttx.subprocess.CalledProcessError(1, ["systemctl", "restart"])

            with mock.patch.object(
                    ttx.subprocess, "run", side_effect=[failure, failure]) as run:
                with self.assertRaises(ttx.TrustTunnelApplyError):
                    ttx.apply_trusttunnel_config(
                        cfg, 'listen_address = "0.0.0.0:443"\n', dry_run=False)

            self.assertFalse(target.exists())
            self.assertEqual(
                [call.args[0][1] for call in run.call_args_list],
                ["restart", "restart"],
            )

    def test_reconcile_returns_error_after_failed_apply_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "vpn.base.toml"
            target = root / "vpn.toml"
            old = 'listen_address = "0.0.0.0:443"\n'
            base.write_text('listen_address = "0.0.0.0:8443"\n', encoding="utf-8")
            target.write_text(old, encoding="utf-8")
            cfg = config(base, target)
            cfg.tt_restart = True
            failure = ttx.subprocess.CalledProcessError(1, ["systemctl", "restart"])

            with mock.patch.object(ttx, "PanelClient"), \
                    mock.patch.object(ttx, "reconcile_ingress", return_value={}), \
                    mock.patch.object(
                        ttx.subprocess, "run",
                        side_effect=[failure, mock.DEFAULT, mock.DEFAULT]):
                result = ttx.cmd_reconcile(cfg, dry_run=False)

            self.assertEqual(result, 4)
            self.assertEqual(target.read_text(encoding="utf-8"), old)

    def test_systemd_dropin_does_not_mutate_config_during_exec_start_pre(self):
        dropin = ROOT / "systemd" / "trusttunnel.service.d" / "10-ttx-overlay.conf"
        executable_lines = {
            line.strip() for line in dropin.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertFalse(any(line.startswith("ExecStartPre=") for line in executable_lines))


if __name__ == "__main__":
    unittest.main()
