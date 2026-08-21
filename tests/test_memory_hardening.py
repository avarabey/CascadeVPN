from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MemoryHardeningContractTests(unittest.TestCase):
    def test_slice_matches_specification(self) -> None:
        text = (ROOT / "systemd/user-0.slice.d/50-ffknd-memory.conf").read_text()
        self.assertIn("Managed by CascadeVPN REPO 0.2.2", text)
        for setting in (
            "MemoryHigh=384M",
            "MemoryMax=512M",
            "MemorySwapMax=512M",
            "TasksMax=256",
        ):
            self.assertEqual(text.count(setting), 1)

    def test_sysctl_is_low_pressure_fallback(self) -> None:
        text = (ROOT / "sysctl/90-ffknd-memory.conf").read_text()
        self.assertIn("Managed by CascadeVPN REPO 0.2.2", text)
        self.assertRegex(text, r"(?m)^vm\.swappiness\s*=\s*10$")

    def test_apply_and_rollback_are_scoped(self) -> None:
        text = (ROOT / "deploy/harden-memory.sh").read_text()
        for command in ("dry-run", "apply", "status", "rollback"):
            self.assertIn(command, text)
        self.assertIn('STATE_DIR="/var/lib/ffknd-memory"', text)
        self.assertIn('FSTAB_MARKER="# ffknd-memory-guard-v1"', text)
        self.assertIn("refusing to overwrite unmanaged", text)
        self.assertIn("flock -x 9", text)
        self.assertNotIn("rm -rf", text)
        self.assertIsNone(re.search(r"rm\s+-[^\n]*\s/(?:\s|$)", text))


if __name__ == "__main__":
    unittest.main()
