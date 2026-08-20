"""Container entrypoint: prepare the named volume, then permanently drop root."""

from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path


def main() -> None:
    # Database, WAL and session data must never be group/world-readable.
    os.umask(0o077)
    arguments = sys.argv[1:] or ["serve"]
    if os.geteuid() == 0:
        account = pwd.getpwnam("portal")
        data_dir = Path("/data")
        data_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        os.chown(data_dir, account.pw_uid, account.pw_gid)
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    os.execvp("python3", ["python3", "-m", "app", *arguments])


if __name__ == "__main__":
    main()
