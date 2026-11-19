"""Tests for tools/fleet/screenshot.py (PowerShell mocked; nothing is captured)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.fleet import screenshot


class ParseTests(unittest.TestCase):
    def test_lines_become_shots_and_noise_is_skipped(self):
        out = (
            "C:\\t\\screen0.png|\\\\.\\DISPLAY1|0,0,1920,1200|True\n"
            "warning: something\n"
            "C:\\t\\screen1.png|\\\\.\\DISPLAY2|-2880,-131,2880,1620|False\n"
            "bad|line|x,y,w,h|True\n"
        )
        shots = screenshot.parse(out)
        self.assertEqual(len(shots), 2)
        self.assertEqual(shots[0].bounds, (0, 0, 1920, 1200))
        self.assertTrue(shots[0].primary)
        self.assertEqual(shots[1].bounds, (-2880, -131, 2880, 1620))
        self.assertFalse(shots[1].primary)

    def test_report_names_every_png(self):
        shots = screenshot.parse(
            "C:\\a.png|D1|0,0,10,20|True\nC:\\b.png|D2|-10,0,5,5|False\n"
        )
        text = screenshot.report(shots)
        self.assertIn("2 screen(s) captured", text)
        self.assertIn("C:\\a.png  D1 10x20 at 0,0 primary", text)
        self.assertIn("C:\\b.png  D2 5x5 at -10,0", text)


@unittest.skipUnless(os.name == "nt", "Windows only")
class CaptureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name) / "shots"

    def test_runs_powershell_with_the_folder_and_returns_shots(self):
        done = subprocess.CompletedProcess([], 0, "C:\\x.png|D1|0,0,1,1|True\n", "")
        with mock.patch.object(screenshot.subprocess, "run", return_value=done) as run:
            shots = screenshot.capture(self.out)
        self.assertTrue(self.out.is_dir())
        self.assertEqual(run.call_args.kwargs["env"]["FLEET_SHOT_DIR"], str(self.out))
        self.assertEqual(run.call_args.args[0][0], "powershell.exe")
        self.assertEqual([s.path for s in shots], ["C:\\x.png"])

    def test_failure_raises_with_the_last_error_line(self):
        done = subprocess.CompletedProcess([], 1, "", "line one\nAccess denied\n")
        with (
            mock.patch.object(screenshot.subprocess, "run", return_value=done),
            self.assertRaisesRegex(screenshot.ScreenshotError, "Access denied"),
        ):
            screenshot.capture(self.out)

    def test_no_screens_is_a_failure(self):
        done = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(screenshot.subprocess, "run", return_value=done),
            self.assertRaises(screenshot.ScreenshotError),
        ):
            screenshot.capture(self.out)


if __name__ == "__main__":
    unittest.main()
