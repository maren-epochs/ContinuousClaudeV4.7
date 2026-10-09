"""Tests for tools/fleet/launch.py and ``fleet.py open``.

Run from the repo root:  py -3.13 -m pytest -q tools/fleet
Each test uses a temp HOME/USERPROFILE; the real ~/.claude is never touched and no
terminal is started (subprocess.Popen and shutil.which are mocked).
"""

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.fleet import fleet, launch
from tools.fleet.model import FleetState, Session


class LaunchHome(unittest.TestCase):
    """Temp HOME with ~/.claude/projects and project folders under it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.projects = self.home / ".claude" / "projects"
        self.projects.mkdir(parents=True)
        env = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        )
        env.start()
        self.addCleanup(env.stop)
        self.systemp = self.root / "systemp"
        temp = mock.patch.object(
            launch.tempfile, "gettempdir", return_value=str(self.systemp)
        )
        temp.start()
        self.addCleanup(temp.stop)

    def project(self, name, handoff=True, local=False, mtime=None):
        """A project folder with one transcript; its handoff lives where asked."""
        cwd = self.root / "docs" / name
        cwd.mkdir(parents=True)
        if handoff:
            base = (
                cwd / "thoughts" / "shared" / "handoffs"
                if local
                else self.home / ".claude" / "handoffs" / name
            )
            base.mkdir(parents=True)
            (base / "h.yaml").write_text("goal: x\n", encoding="utf-8")
        folder = self.projects / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
        folder.mkdir(exist_ok=True)
        transcript = folder / f"{name}-id.jsonl"
        transcript.write_text(
            json.dumps({"type": "user", "cwd": str(cwd)}) + "\n", encoding="utf-8"
        )
        if mtime is not None:
            os.utime(transcript, (mtime, mtime))
        return cwd


class CandidateTests(LaunchHome):
    def test_lists_projects_with_folder_and_handoff_sorted_by_title(self):
        self.project("zeta")
        self.project("alpha", local=True)
        self.project("nohandoff", handoff=False)
        shutil.rmtree(self.project("gone"))
        titles = [t.title for t in launch.candidates(None)]
        self.assertEqual(titles, ["alpha", "zeta"])

    def test_live_session_name_becomes_the_title(self):
        cwd = self.project("myrtle")
        state = FleetState(
            sessions=[Session(cwd=str(cwd), name="myrtle budget", alive=True)]
        )
        self.assertEqual([t.title for t in launch.candidates(state)], ["myrtle budget"])

    def test_ended_session_name_is_not_used(self):
        cwd = self.project("myrtle")
        state = FleetState(sessions=[Session(cwd=str(cwd), name="old", alive=False)])
        self.assertEqual([t.title for t in launch.candidates(state)], ["myrtle"])

    def test_home_and_temp_folders_are_skipped(self):
        for cwd in (self.home, self.systemp / "x"):
            (cwd / "thoughts" / "shared" / "handoffs").mkdir(parents=True)
            (cwd / "thoughts" / "shared" / "handoffs" / "h.yaml").write_text("x\n")
            folder = self.projects / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
            folder.mkdir()
            (folder / "a.jsonl").write_text(
                json.dumps({"cwd": str(cwd)}) + "\n", encoding="utf-8"
            )
        self.assertEqual(launch.candidates(None), [])

    def test_renamed_folder_with_renamed_transcript_folder_is_listed(self):
        old = self.project("ccv47-readme-fix", local=True)
        new = old.parent / "epoch_harness"
        old.rename(new)
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(old))
        (self.projects / slug).rename(
            self.projects / re.sub(r"[^A-Za-z0-9]", "-", str(new))
        )
        (found,) = launch.candidates(None)
        self.assertEqual((found.title, found.cwd), ("epoch_harness", str(new)))

    def test_renamed_folder_with_old_transcript_folder_is_skipped(self):
        old = self.project("ccv47-readme-fix", local=True)
        old.rename(old.parent / "epoch_harness")
        self.assertEqual(launch.candidates(None), [])

    def test_transcript_without_cwd_is_skipped(self):
        folder = self.projects / "x"
        folder.mkdir()
        (folder / "a.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
        self.assertEqual(launch.candidates(None), [])


class SelectTests(LaunchHome):
    def setUp(self):
        super().setUp()
        self.project("amber")
        self.project("myrtle")
        self.found = launch.candidates(None)

    def test_number_name_and_case_insensitive_name(self):
        picked = launch.select(self.found, ["2", "AMBER"])
        self.assertEqual([t.title for t in picked], ["myrtle", "amber"])

    def test_duplicates_collapse(self):
        picked = launch.select(self.found, ["1", "amber"])
        self.assertEqual([t.title for t in picked], ["amber"])

    def test_directory_pick_opens_an_unlisted_folder(self):
        extra = self.root / "docs" / "epoch_harness"
        extra.mkdir()
        (picked,) = launch.select(self.found, [str(extra)])
        self.assertEqual((picked.title, picked.cwd), ("epoch_harness", str(extra)))

    def test_unknown_name_and_missing_directory_refuse(self):
        with self.assertRaises(launch.LaunchError):
            launch.select(self.found, ["nope"])
        with self.assertRaises(launch.LaunchError):
            launch.select(self.found, [str(self.root / "missing")])
        with self.assertRaises(launch.LaunchError):
            launch.select(self.found, ["9"])


class WtArgsTests(unittest.TestCase):
    def test_one_window_titled_tabs_separated(self):
        targets = [launch.Target("a", r"C:\p\a"), launch.Target("b b", r"C:\p\b")]
        args = launch.wt_args(targets)
        self.assertEqual(args[:2], ["-w", "new"])
        self.assertEqual(args.count("new-tab"), 2)
        self.assertEqual(args.count(";"), 1)
        self.assertEqual(args.count("--suppressApplicationTitle"), 2)
        tab = args[args.index(";") + 1 :]
        self.assertEqual(
            tab,
            [
                "new-tab",
                "--title",
                "b b",
                "--suppressApplicationTitle",
                "-d",
                r"C:\p\b",
                "pwsh",
                "-NoExit",
                "-Command",
                launch.TAB_PROMPT,
            ],
        )

    def test_launch_refuses_without_wt_or_targets(self):
        with self.assertRaises(launch.LaunchError):
            launch.launch([])
        with (
            mock.patch.object(launch.shutil, "which", return_value=None),
            self.assertRaises(launch.LaunchError),
        ):
            launch.launch([launch.Target("a", "C:\\a")])

    def test_launch_falls_back_to_windows_powershell(self):
        which = {"wt": "C:\\wt.exe", "pwsh": None}.get
        with (
            mock.patch.object(launch.shutil, "which", side_effect=which),
            mock.patch.object(launch.subprocess, "Popen") as popen,
        ):
            command = launch.launch([launch.Target("a", "C:\\a")])
        popen.assert_called_once()
        self.assertEqual(command[0], "C:\\wt.exe")
        self.assertIn("powershell", command)
        self.assertNotIn("pwsh", command)

    def test_launch_drops_the_calling_sessions_markers(self):
        markers = {
            "CLAUDECODE": "1",
            "CLAUDE_CODE_CHILD_SESSION": "1",
            "CLAUDE_CODE_SESSION_ID": "x",
            "CLAUDE_PID": "1",
            "CLAUDE_EFFORT": "medium",
        }
        kept = {
            "PATH": os.environ.get("PATH", ""),
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "90",
        }
        with (
            mock.patch.dict(os.environ, {**markers, **kept}),
            mock.patch.object(launch.shutil, "which", return_value="C:\\wt.exe"),
            mock.patch.object(launch.subprocess, "Popen") as popen,
        ):
            launch.launch([launch.Target("a", "C:\\a")])
        env = popen.call_args.kwargs["env"]
        self.assertFalse(set(markers) & {k.upper() for k in env})
        self.assertEqual(env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "90")
        self.assertIn("PATH", {k.upper() for k in env})


class OpenCommandTests(LaunchHome):
    def run_cli(self, *args, interactive=False):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(fleet, "_interactive", return_value=interactive),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = fleet.main(["open", *args])
        return code, out.getvalue(), err.getvalue()

    def test_no_picks_lists_numbered_projects(self):
        self.project("amber")
        self.project("myrtle")
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("projects (2):", out)
        self.assertRegex(out, r"\n\s+1\s+amber ")
        self.assertRegex(out, r"\n\s+2\s+myrtle ")

    def test_empty_list_says_so(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("no projects to open", out)

    def test_dry_run_prints_wt_command_and_starts_nothing(self):
        self.project("amber")
        with mock.patch.object(launch.subprocess, "Popen") as popen:
            code, out, _ = self.run_cli("amber", "--dry-run")
        self.assertEqual(code, 0)
        popen.assert_not_called()
        self.assertTrue(out.startswith("wt -w new new-tab --title amber"))
        self.assertIn("\"claude '/resume-handoff'\"", out)

    def test_all_yes_opens_every_project(self):
        self.project("amber")
        self.project("myrtle")
        with (
            mock.patch.object(launch.shutil, "which", return_value="C:\\wt.exe"),
            mock.patch.object(launch.subprocess, "Popen") as popen,
        ):
            code, out, _ = self.run_cli("--all", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "opened 2 tab(s): amber, myrtle")
        self.assertEqual(popen.call_args.args[0].count("new-tab"), 2)

    def test_all_with_a_directory_pick_adds_it(self):
        self.project("amber")
        extra = self.root / "docs" / "epoch_harness"
        extra.mkdir()
        code, out, _ = self.run_cli("--all", "--yes", str(extra), "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("new-tab"), 2)
        self.assertIn("--title epoch_harness", out)

    def test_all_without_a_terminal_lists_and_opens_nothing(self):
        self.project("amber")
        self.project("myrtle")
        with mock.patch.object(launch.subprocess, "Popen") as popen:
            code, out, _ = self.run_cli("--all")
        self.assertEqual(code, 0)
        popen.assert_not_called()
        self.assertIn("projects (2):", out)
        self.assertIn("nothing opened", out)

    def run_interactive(self, answer, *args):
        with mock.patch("builtins.input", side_effect=[answer]) as ask:
            result = self.run_cli(*args, interactive=True)
        return (*result, ask)

    def test_all_in_a_terminal_asks_and_opens_only_the_chosen(self):
        for name in ("amber", "massave", "myrtle", "privacy"):
            self.project(name)
        code, out, _, ask = self.run_interactive("1 3-4", "--all", "--dry-run")
        self.assertEqual(code, 0)
        ask.assert_called_once()
        self.assertIn("projects (4):", out)
        self.assertEqual(out.count("new-tab"), 3)
        self.assertNotIn("--title massave", out)

    def test_all_in_a_terminal_accepts_names_and_a_for_all(self):
        self.project("amber")
        self.project("myrtle")
        _, out, _, _ = self.run_interactive("myrtle", "--all", "--dry-run")
        self.assertEqual(out.count("new-tab"), 1)
        self.assertIn("--title myrtle", out)
        _, out, _, _ = self.run_interactive("a", "--all", "--dry-run")
        self.assertEqual(out.count("new-tab"), 2)

    def test_all_in_a_terminal_lists_an_extra_directory_pick(self):
        self.project("amber")
        extra = self.root / "docs" / "epoch_harness"
        extra.mkdir()
        _, out, _, _ = self.run_interactive("2", "--all", str(extra), "--dry-run")
        self.assertRegex(out, r"\n\s+2\s+epoch_harness ")
        self.assertIn("--title epoch_harness", out)
        self.assertNotIn("--title amber", out)

    def test_all_in_a_terminal_enter_cancels(self):
        self.project("amber")
        with mock.patch.object(launch.subprocess, "Popen") as popen:
            code, out, _, _ = self.run_interactive("", "--all")
        self.assertEqual(code, 0)
        popen.assert_not_called()
        self.assertIn("cancelled: nothing opened", out)

    def test_all_in_a_terminal_bad_answer_refuses(self):
        self.project("amber")
        code, _, err, _ = self.run_interactive("5-9", "--all", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("range out of 1-1: 5-9", err)

    def test_yes_skips_the_question(self):
        self.project("amber")
        self.project("myrtle")
        with mock.patch("builtins.input") as ask:
            code, out, _ = self.run_cli("--all", "--yes", "--dry-run", interactive=True)
        self.assertEqual(code, 0)
        ask.assert_not_called()
        self.assertEqual(out.count("new-tab"), 2)

    def test_unknown_pick_refuses_with_exit_1(self):
        self.project("amber")
        code, _, err = self.run_cli("nope")
        self.assertEqual(code, 1)
        self.assertIn("refused: no project named 'nope'", err)


if __name__ == "__main__":
    unittest.main()
