"""Tests for install/build_zips.py (built from the repo's own HEAD)."""

from __future__ import annotations

import contextlib
import importlib
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
bz = importlib.import_module("build_zips")


class BuildZips(unittest.TestCase):
    def test_each_zip_has_its_installer_only(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            mac, win = bz.build("HEAD", Path(tmp))
            mac_names = set(zipfile.ZipFile(mac).namelist())
            win_names = set(zipfile.ZipFile(win).namelist())
            for names in (mac_names, win_names):
                self.assertIn("epoch-harness/install/sync_global.py", names)
                self.assertNotIn("epoch-harness/Install-Epoch-Harness.cmd", names)
                self.assertFalse(
                    any(n.startswith("epoch-harness/continuum/") for n in names)
                )
            self.assertIn("epoch-harness/Install-Mac.command", mac_names)
            self.assertNotIn("epoch-harness/Install-Windows.cmd", mac_names)
            self.assertIn("epoch-harness/Install-Windows.cmd", win_names)
            self.assertNotIn("epoch-harness/Install-Mac.command", win_names)
            info = zipfile.ZipFile(mac).getinfo("epoch-harness/Install-Mac.command")
            self.assertTrue((info.external_attr >> 16) & 0o111, "executable bit kept")


if __name__ == "__main__":
    unittest.main()
