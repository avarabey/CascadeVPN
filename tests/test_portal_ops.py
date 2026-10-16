from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PortalOperationsContractTests(unittest.TestCase):
    def test_trusttunnel_does_not_pull_in_optional_portal(self):
        drop_in = (
            ROOT / "systemd/trusttunnel.service.d/10-ttx-overlay.conf"
        ).read_text(encoding="utf-8")

        self.assertNotIn(
            "ffknd-portal",
            drop_in,
            "TrustTunnel must remain startable when the optional portal is disabled",
        )

    def test_xui_restart_pulls_local_dependents_back_in(self):
        drop_in = (
            ROOT / "systemd/x-ui.service.d/20-ttx-dependents.conf"
        ).read_text(encoding="utf-8")
        installer = (ROOT / "install/ttx-install.sh").read_text(encoding="utf-8")

        self.assertIn("Wants=ttx-bridge.service trusttunnel.service", drop_in)
        self.assertIn("Before=ttx-bridge.service trusttunnel.service", drop_in)
        self.assertNotIn("Requires=", drop_in)
        self.assertIn("20-ttx-dependents.conf", installer)

    def test_installer_deploys_all_recovery_and_smoke_tools(self):
        installer = (ROOT / "install/ttx-install.sh").read_text(encoding="utf-8")

        self.assertIn('"$TTX_DIR/recovery"', installer)
        for name in (
            "xui_portal_cutover.py",
            "xui_subscription_recovery.py",
            "xui_outbound_client_recovery.py",
            "portal-health-smoke.sh",
            "reality-e2e-smoke.sh",
            "reality_smoke_config.py",
        ):
            self.assertIn(name, installer)

    def test_remote_deploy_has_one_command_production_update(self):
        deploy = (ROOT / "deploy/deploy.sh").read_text(encoding="utf-8")

        self.assertIn("--production-update", deploy)
        self.assertIn("install/ttx-install.sh", deploy)
        self.assertIn("systemctl start x-ui.service", deploy)
        self.assertIn("/opt/ttx/tests/portal-health-smoke.sh", deploy)
        self.assertIn("ttx doctor", deploy)
        self.assertIn("ttx reconcile --dry-run", deploy)
        self.assertIn("/opt/ttx/tests/e2e-smoke.sh", deploy)
        self.assertIn("/opt/ttx/tests/reality-e2e-smoke.sh", deploy)

    def test_bare_metal_env_uses_systemd_writable_state_directory(self):
        environment = (ROOT / "portal/portal.env.example").read_text(encoding="utf-8")

        self.assertIn(
            "PORTAL_DB=/var/lib/ffknd-portal/portal.db",
            environment,
        )
        self.assertNotIn("PORTAL_DB=/data/portal.db", environment)

    def test_installer_adds_managed_block_but_does_not_start_portal(self):
        installer = (ROOT / "install/ttx-install.sh").read_text(encoding="utf-8")
        portal_step = installer.split("step_portal() {", 1)[1].split(
            "\n}\n\nstep_cli()", 1
        )[0]

        self.assertRegex(portal_step, r'configure_reverse_proxy\.py"\s+add')
        self.assertIn("ffknd-portal-config", portal_step)
        self.assertNotIn("systemctl enable", portal_step)
        self.assertNotIn("systemctl start", portal_step)
        self.assertIn("systemctl disable --now ffknd-portal", portal_step)

    def test_bootstrap_stops_portal_until_hash_is_configured(self):
        bootstrap = (ROOT / "deploy/bootstrap-ubuntu.sh").read_text(encoding="utf-8")

        self.assertIn("if portal_hash_configured; then", bootstrap)
        self.assertIn("systemctl disable --now ffknd-portal", bootstrap)

    def test_qr_svg_resets_global_icon_stroke(self):
        stylesheet = (ROOT / "portal/app/static/css/portal.css").read_text(
            encoding="utf-8"
        )
        global_svg_rule = stylesheet.index("svg { display: block;")
        qr_rule_start = stylesheet.index(".qr-output svg {")
        qr_rule_end = stylesheet.index("}", qr_rule_start)
        qr_rule = stylesheet[qr_rule_start:qr_rule_end]

        self.assertGreater(qr_rule_start, global_svg_rule)
        self.assertIn("stroke: none", qr_rule)
        self.assertIn("stroke-width: 0", qr_rule)
        self.assertIn("shape-rendering: crispEdges", qr_rule)

    def test_static_entry_assets_are_busted_by_portal_version(self):
        package = (ROOT / "portal/app/__init__.py").read_text(encoding="utf-8")
        index = (ROOT / "portal/app/static/index.html").read_text(encoding="utf-8")
        version_match = re.search(r'__version__ = "([^"]+)"', package)

        self.assertIsNotNone(version_match)
        version = version_match.group(1)
        self.assertIn(f'/static/css/portal.css?v={version}', index)
        self.assertIn(f'/static/js/app.js?v={version}', index)


if __name__ == "__main__":
    unittest.main()
