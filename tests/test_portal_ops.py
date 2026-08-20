from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
