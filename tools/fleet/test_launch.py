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
        titles = [t.title for t in launch.candidates()]
        self.assertEqual(titles, ["alpha", "zeta"])

    def test_title_is_the_folder_name(self):
        self.project("myrtle")
        self.assertEqual([t.title for t in launch.candidates()], ["myrtle"])

    def test_home_and_temp_folders_are_skipped(self):
        for cwd in (self.home, self.systemp / "x"):
            (cwd / "thoughts" / "shared" / "handoffs").mkdir(parents=True)
            (cwd / "thoughts" / "shared" / "handoffs" / "h.yaml").write_text("x\n")
            folder = self.projects / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
            folder.mkdir()
            (folder / "a.jsonl").write_text(
                json.dumps({"cwd": str(cwd)}) + "\n", encoding="utf-8"
            )
        self.assertEqual(launch.candidates(), [])

    def test_renamed_folder_with_renamed_transcript_folder_is_listed(self):
        old = self.project("ccv47-readme-fix", local=True)
        new = old.parent / "epoch_harness"
        old.rename(new)
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(old))
        (self.projects / slug).rename(
            self.projects / re.sub(r"[^A-Za-z0-9]", "-", str(new))
        )
        (found,) = launch.candidates()
        self.assertEqual((found.title, found.cwd), ("epoch_harness", str(new)))

    def test_renamed_folder_with_old_transcript_folder_is_skipped(self):
        old = self.project("ccv47-readme-fix", local=True)
        old.rename(old.parent / "epoch_harness")
        self.assertEqual(launch.candidates(), [])

    def test_transcript_without_cwd_is_skipped(self):
        folder = self.projects / "x"
        folder.mkdir()
        (folder / "a.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
        self.assertEqual(launch.candidates(), [])

    def test_newest_stub_transcript_without_cwd_falls_back_to_older(self):
        cwd = self.project("amber", mtime=1_000)
        folder = self.projects / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
        stub = folder / "stub.jsonl"
        stub.write_text('{"type":"ai-title","aiTitle":"t"}\n', encoding="utf-8")
        os.utime(stub, (2_000, 2_000))
        (found,) = launch.candidates()
        self.assertEqual((found.title, found.last), ("amber", 2_000))


class SelectTests(LaunchHome):
    def setUp(self):
        super().setUp()
        self.project("amber")
        self.project("myrtle")
        self.found = launch.candidates()

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

    def test_profile_on_every_tab(self):
        targets = [launch.Target("a", r"C:\p\a"), launch.Target("b", r"C:\p\b")]
        args = launch.wt_args(targets, profile="epoch harness")
        self.assertEqual(args.count("-p"), 2)
        tab = args[args.index(";") + 1 :]
        self.assertEqual(tab[:3], ["new-tab", "-p", "epoch harness"])
        self.assertEqual(tab[-4:], ["pwsh", "-NoExit", "-Command", launch.TAB_PROMPT])
        self.assertNotIn("-p", launch.wt_args(targets))

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
            (command,) = launch.launch([launch.Target("a", "C:\\a")])
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


class StaggerTests(unittest.TestCase):
    targets = [
        launch.Target("a", r"C:\p\a"),
        launch.Target("b b", r"C:\p\b"),
        launch.Target("c", r"C:\p\c"),
    ]

    @staticmethod
    def tabs(args):
        """wt_args split into per-tab argument lists (without the leading -w new)."""
        tabs, tab = [], []
        for arg in args[2:]:
            if arg == ";":
                tabs.append(tab)
                tab = []
            else:
                tab.append(arg)
        return [*tabs, tab]

    def test_stagger_zero_keeps_the_single_chained_command(self):
        calls = launch.wt_calls(self.targets, profile="p", stagger=0)
        self.assertEqual(calls, [(0.0, launch.wt_args(self.targets, profile="p"))])

    def test_first_call_names_the_window_later_calls_target_it_after_a_delay(self):
        calls = launch.wt_calls(
            self.targets, "powershell", "p", stagger=3, window="fleet-7"
        )
        self.assertEqual([d for d, _ in calls], [0.0, 3.0, 3.0])
        expected = self.tabs(launch.wt_args(self.targets, "powershell", "p"))
        for (_, args), tab in zip(calls, expected, strict=True):
            self.assertEqual(args[:2], ["-w", "fleet-7"])
            self.assertEqual(args[2:], tab)
            self.assertNotIn(";", args)

    def test_window_name_is_a_unique_named_window_not_a_reserved_id(self):
        name = launch.window_name()
        self.assertRegex(name, r"^fleet-\d+$")
        self.assertNotIn(name, ("new", "last", "-1", "0"))
        calls = launch.wt_calls(self.targets, stagger=1)
        self.assertEqual(len({tuple(args[:2]) for _, args in calls}), 1)
        self.assertRegex(calls[0][1][1], r"^fleet-\d+$")

    def run_launch(self, targets, **kw):
        order = mock.Mock()
        with (
            mock.patch.dict(os.environ, {"CLAUDE_CODE_CHILD_SESSION": "1"}),
            mock.patch.object(launch.shutil, "which", return_value="C:\\wt.exe"),
            mock.patch.object(launch.subprocess, "Popen", order.popen),
        ):
            commands = launch.launch(targets, sleep=order.sleep, **kw)
        return commands, order

    def test_launch_staggered_runs_one_wt_call_per_tab_with_sleeps_between(self):
        commands, order = self.run_launch(self.targets, stagger=2.5, window="fleet-9")
        self.assertEqual(
            [c[0] for c in order.mock_calls],
            ["popen", "sleep", "popen", "sleep", "popen"],
        )
        self.assertEqual(order.sleep.call_args_list, [mock.call(2.5)] * 2)
        self.assertEqual(len(commands), 3)
        for call, command in zip(order.popen.call_args_list, commands, strict=True):
            self.assertEqual(call.args[0], command)
            self.assertEqual(command[:3], ["C:\\wt.exe", "-w", "fleet-9"])
            self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", call.kwargs["env"])

    def test_launch_staggered_single_tab_never_sleeps(self):
        commands, order = self.run_launch(self.targets[:1], stagger=3)
        order.sleep.assert_not_called()
        self.assertEqual(len(commands), 1)

    def test_launch_default_sleep_is_time_sleep(self):
        with (
            mock.patch.object(launch.shutil, "which", return_value="C:\\wt.exe"),
            mock.patch.object(launch.subprocess, "Popen") as popen,
            mock.patch.object(launch.time, "sleep") as sleep,
        ):
            launch.launch(self.targets[:2], stagger=4)
        self.assertEqual(popen.call_count, 2)
        sleep.assert_called_once_with(4)

    def test_launch_stagger_zero_is_one_popen(self):
        commands, order = self.run_launch(self.targets, stagger=0)
        order.popen.assert_called_once()
        order.sleep.assert_not_called()
        self.assertEqual(commands[0][1:3], ["-w", "new"])


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
        self.assertRegex(out, r"^wt -w fleet-\d+ new-tab --title amber")
        self.assertIn("\"claude '/resume-handoff'\"", out)

    def test_dry_run_prints_every_staggered_call_with_its_delay(self):
        self.project("amber")
        self.project("myrtle")
        with mock.patch.object(launch.subprocess, "Popen") as popen:
            code, out, _ = self.run_cli("--all", "--yes", "--dry-run")
        self.assertEqual(code, 0)
        popen.assert_not_called()
        first, second = out.strip().splitlines()
        name = re.match(r"wt -w (fleet-\d+) new-tab --title amber ", first).group(1)
        self.assertTrue(
            second.startswith(f"(after 3 s) wt -w {name} new-tab --title myrtle ")
        )
        self.assertNotIn(" ; ", out)

    def test_stagger_option_sets_the_delay_and_zero_keeps_one_command(self):
        self.project("amber")
        self.project("myrtle")
        _, out, _ = self.run_cli("--all", "--yes", "--dry-run", "--stagger", "1.5")
        self.assertIn("(after 1.5 s) wt -w fleet-", out)
        _, out, _ = self.run_cli("--all", "--yes", "--dry-run", "--stagger", "0")
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertTrue(out.startswith("wt -w new new-tab --title amber"))
        self.assertIn(" ; new-tab --title myrtle", out)

    def test_negative_stagger_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as exit_:
            self.run_cli("amber", "--stagger", "-1")
        self.assertEqual(exit_.exception.code, 2)

    def test_all_yes_opens_every_project(self):
        self.project("amber")
        self.project("myrtle")
        with (
            mock.patch.object(launch.shutil, "which", return_value="C:\\wt.exe"),
            mock.patch.object(launch.subprocess, "Popen") as popen,
            mock.patch.object(launch.time, "sleep") as sleep,
        ):
            code, out, _ = self.run_cli("--all", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "opened 2 tab(s): amber, myrtle")
        self.assertEqual(popen.call_count, 2)
        sleep.assert_called_once_with(3.0)
        for call in popen.call_args_list:
            self.assertEqual(call.args[0].count("new-tab"), 1)

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
