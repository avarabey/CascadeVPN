#!/usr/bin/env python3
"""Safely add or remove the portal reverse-proxy table in an operator config."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import tempfile
from pathlib import Path


BRIDGE_MARKER = "# --- managed by ttx-bridge: do not edit below this line ---"
PORTAL_BLOCK_START = "# --- managed by ffknd-portal: reverse_proxy start ---"
PORTAL_BLOCK_END = "# --- managed by ffknd-portal: reverse_proxy end ---"
REVERSE_PROXY_TABLE = re.compile(
    r"^\s*\[reverse_proxy\]\s*(?:#.*)?$", re.MULTILINE
)
PORTAL_BLOCK_BODY = """# Обычные HTTP-запросы; VPN authority-form CONNECT не совпадает с '/'.
[reverse_proxy]
server_address = "127.0.0.1:8080"
path_mask = "/"
h3_backward_compatibility = false
"""
PORTAL_BLOCK = (
    f"{PORTAL_BLOCK_START}\n"
    f"{PORTAL_BLOCK_BODY}"
    f"{PORTAL_BLOCK_END}\n"
)
# The leading newline is owned by the portal too. Appending this exact byte
# sequence lets removal restore every pre-existing byte, even when the source
# file did not end with a newline.
PORTAL_INSERTION = "\n" + PORTAL_BLOCK

# Versions before the explicit start/end markers used this exact generated
# comment as their ownership marker. Supporting only the byte-exact legacy
# block lets rollback remain safe without treating an operator table as ours.
LEGACY_PORTAL_BLOCK = PORTAL_BLOCK_BODY


class ManagedBlockError(ValueError):
    """The managed markers exist, but the block is not safe to edit."""


def _write_atomic(path: Path, content: str) -> None:
    metadata = path.stat()
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.portal-"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, metadata.st_mode & 0o777)
        try:
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        except PermissionError:
            pass
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def ensure_reverse_proxy(path: Path) -> bool:
    """Append the loopback origin unless the operator already owns this table."""

    text = path.read_text(encoding="utf-8")
    has_start = PORTAL_BLOCK_START in text
    has_end = PORTAL_BLOCK_END in text
    if has_start or has_end:
        if (
            text.count(PORTAL_BLOCK) == 1
            and text.count(PORTAL_BLOCK_START) == 1
            and text.count(PORTAL_BLOCK_END) == 1
        ):
            return False
        raise ManagedBlockError(
            "ffknd portal reverse_proxy markers are malformed; refusing to edit"
        )
    if REVERSE_PROXY_TABLE.search(text):
        return False

    before_marker, marker, managed = text.partition(BRIDGE_MARKER)
    updated = before_marker + PORTAL_INSERTION
    if marker:
        updated += marker + managed

    backup = path.with_suffix(path.suffix + ".portal-bak")
    if not backup.exists():
        shutil.copy2(path, backup)

    _write_atomic(path, updated)
    return True


def remove_reverse_proxy(path: Path) -> bool:
    """Remove only the exact reverse-proxy block owned by ffknd portal."""

    text = path.read_text(encoding="utf-8")
    has_start = PORTAL_BLOCK_START in text
    has_end = PORTAL_BLOCK_END in text

    if has_start or has_end:
        if text.count(PORTAL_BLOCK_START) != 1 or text.count(PORTAL_BLOCK_END) != 1:
            raise ManagedBlockError(
                "ffknd portal reverse_proxy markers are duplicated or incomplete; "
                "refusing to edit"
            )
        if text.count(PORTAL_BLOCK) != 1:
            raise ManagedBlockError(
                "ffknd portal reverse_proxy block was modified; refusing to remove it"
            )
        owned = PORTAL_INSERTION if text.count(PORTAL_INSERTION) == 1 else PORTAL_BLOCK
        updated = text.replace(owned, "", 1)
    else:
        legacy_count = text.count(LEGACY_PORTAL_BLOCK)
        if legacy_count == 0:
            return False
        if legacy_count != 1:
            raise ManagedBlockError(
                "legacy ffknd portal reverse_proxy block is duplicated; refusing to edit"
            )
        updated = text.replace(LEGACY_PORTAL_BLOCK, "", 1)

    _write_atomic(path, updated)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="manage ffknd portal reverse_proxy without replacing operator settings"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("add", "remove"):
        command = subparsers.add_parser(action)
        command.add_argument("path", type=Path, help="vpn.base.toml path")
    args = parser.parse_args(argv)

    try:
        if args.action == "add":
            changed = ensure_reverse_proxy(args.path)
            print("reverse_proxy added" if changed else "existing reverse_proxy preserved")
        else:
            changed = remove_reverse_proxy(args.path)
            print("managed reverse_proxy removed" if changed else "no managed reverse_proxy found")
    except ManagedBlockError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
