from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


PORTAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PORTAL_ROOT))

from app import entrypoint  # noqa: E402


class EntrypointTests(unittest.TestCase):
    def test_restrictive_umask_is_set_before_exec(self):
        with (
            mock.patch("app.entrypoint.os.umask") as umask,
            mock.patch("app.entrypoint.os.geteuid", return_value=1000),
            mock.patch("app.entrypoint.os.execvp") as execvp,
            mock.patch.object(sys, "argv", ["entrypoint", "serve"]),
        ):
            entrypoint.main()
        umask.assert_called_once_with(0o077)
        execvp.assert_called_once_with("python3", ["python3", "-m", "app", "serve"])


if __name__ == "__main__":
    unittest.main()
