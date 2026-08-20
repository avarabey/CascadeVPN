from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


PORTAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PORTAL_ROOT))

from configure_reverse_proxy import (  # noqa: E402
    BRIDGE_MARKER,
    LEGACY_PORTAL_BLOCK,
    PORTAL_BLOCK,
    PORTAL_BLOCK_END,
    PORTAL_BLOCK_START,
    ManagedBlockError,
    ensure_reverse_proxy,
    main,
    remove_reverse_proxy,
)


class ReverseProxyConfigTests(unittest.TestCase):
    def test_adds_before_bridge_marker_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vpn.base.toml"
            original = (
                'listen_address = "0.0.0.0:443"\n\n'
                f"{BRIDGE_MARKER}\n"
                "[forward_protocol.socks5]\n"
                'address = "127.0.0.1:10800"\n'
            )
            path.write_text(original, encoding="utf-8")

            self.assertTrue(ensure_reverse_proxy(path))
            updated = path.read_text(encoding="utf-8")
            self.assertLess(updated.index("[reverse_proxy]"), updated.index(BRIDGE_MARKER))
            self.assertEqual(updated.count("[reverse_proxy]"), 1)
            self.assertEqual(updated.count(PORTAL_BLOCK_START), 1)
            self.assertEqual(updated.count(PORTAL_BLOCK_END), 1)
            self.assertEqual(
                path.with_suffix(".toml.portal-bak").read_text(encoding="utf-8"),
                original,
            )

            # A later operator edit must survive portal rollback; restoring the
            # old .portal-bak snapshot would lose this line.
            updated = updated.replace(
                BRIDGE_MARKER,
                'operator_setting = "keep-me"\n\n' + BRIDGE_MARKER,
            )
            path.write_text(updated, encoding="utf-8")

            self.assertTrue(remove_reverse_proxy(path))
            rolled_back = path.read_text(encoding="utf-8")
            self.assertIn('operator_setting = "keep-me"', rolled_back)
            self.assertIn(BRIDGE_MARKER, rolled_back)
            self.assertIn("[forward_protocol.socks5]", rolled_back)
            self.assertNotIn("[reverse_proxy]", rolled_back)
            self.assertNotIn(PORTAL_BLOCK_START, rolled_back)

    def test_add_then_remove_restores_source_bytes_exactly(self):
        for original in (
            'listen_address = "0.0.0.0:443"',
            'listen_address = "0.0.0.0:443"\n',
            'listen_address = "0.0.0.0:443"\n\n',
            f'listen_address = "0.0.0.0:443"\n{BRIDGE_MARKER}\nmanaged\n',
        ):
            with self.subTest(original=repr(original)):
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "vpn.base.toml"
                    path.write_text(original, encoding="utf-8")

                    self.assertTrue(ensure_reverse_proxy(path))
                    self.assertTrue(remove_reverse_proxy(path))
                    self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_add_and_remove_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vpn.base.toml"
            path.write_text('listen_address = "0.0.0.0:443"\n', encoding="utf-8")

            self.assertTrue(ensure_reverse_proxy(path))
            added = path.read_text(encoding="utf-8")
            self.assertFalse(ensure_reverse_proxy(path))
            self.assertEqual(path.read_text(encoding="utf-8"), added)

            self.assertTrue(remove_reverse_proxy(path))
            removed = path.read_text(encoding="utf-8")
            self.assertFalse(remove_reverse_proxy(path))
            self.assertEqual(path.read_text(encoding="utf-8"), removed)

    def test_add_and_remove_preserve_file_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vpn.base.toml"
            path.write_text('listen_address = "0.0.0.0:443"\n', encoding="utf-8")
            path.chmod(0o640)

            ensure_reverse_proxy(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            remove_reverse_proxy(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_preserves_operator_reverse_proxy(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vpn.base.toml"
            original = (
                '[reverse_proxy]\nserver_address = "127.0.0.1:9000"\n'
                'path_mask = "/custom"\n'
            )
            path.write_text(original, encoding="utf-8")

            self.assertFalse(ensure_reverse_proxy(path))
            self.assertFalse(remove_reverse_proxy(path))
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertFalse(path.with_suffix(".toml.portal-bak").exists())

    def test_removes_only_byte_exact_legacy_managed_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vpn.base.toml"
            original = (
                'listen_address = "0.0.0.0:443"\n\n'
                f"{LEGACY_PORTAL_BLOCK}\n"
                'operator_setting = "keep-me"\n'
            )
            path.write_text(original, encoding="utf-8")

            self.assertTrue(remove_reverse_proxy(path))
            updated = path.read_text(encoding="utf-8")
            self.assertNotIn("[reverse_proxy]", updated)
            self.assertIn('operator_setting = "keep-me"', updated)

    def test_refuses_to_remove_modified_managed_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vpn.base.toml"
            modified = PORTAL_BLOCK.replace('path_mask = "/"', 'path_mask = "/custom"')
            path.write_text(modified, encoding="utf-8")

            with self.assertRaises(ManagedBlockError):
                remove_reverse_proxy(path)
            self.assertEqual(path.read_text(encoding="utf-8"), modified)

    def test_refuses_incomplete_managed_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vpn.base.toml"
            malformed = f"{PORTAL_BLOCK_START}\n{LEGACY_PORTAL_BLOCK}"
            path.write_text(malformed, encoding="utf-8")

            with self.assertRaises(ManagedBlockError):
                remove_reverse_proxy(path)
            self.assertEqual(path.read_text(encoding="utf-8"), malformed)

    def test_preserves_similar_unmarked_operator_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vpn.base.toml"
            operator_block = LEGACY_PORTAL_BLOCK.replace(
                "# Обычные HTTP-запросы",
                "# Настроено оператором: обычные HTTP-запросы",
            )
            path.write_text(operator_block, encoding="utf-8")

            self.assertFalse(remove_reverse_proxy(path))
            self.assertEqual(path.read_text(encoding="utf-8"), operator_block)

    def test_remove_subcommand(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vpn.base.toml"
            path.write_text('listen_address = "0.0.0.0:443"\n', encoding="utf-8")
            ensure_reverse_proxy(path)

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["remove", str(path)]), 0)
            self.assertIn("managed reverse_proxy removed", output.getvalue())
            self.assertNotIn("[reverse_proxy]", path.read_text(encoding="utf-8"))

    def test_add_subcommand(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vpn.base.toml"
            path.write_text('listen_address = "0.0.0.0:443"\n', encoding="utf-8")

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["add", str(path)]), 0)
            self.assertIn("reverse_proxy added", output.getvalue())
            self.assertEqual(path.read_text(encoding="utf-8").count(PORTAL_BLOCK), 1)


if __name__ == "__main__":
    unittest.main()
