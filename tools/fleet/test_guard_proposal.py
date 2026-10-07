"""Proposals written by .claude/hooks/harness-guard.mjs load with tools/fleet/model.py.

Run from the repo root:  py -3.13 -m pytest -q tools/fleet
Runs the hook under a temp HOME/USERPROFILE with a fake manifest; skipped without node.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.fleet import model
from tools.fleet.model import Proposal

HOOK = Path(REPO_ROOT, ".claude", "hooks", "harness-guard.mjs")
ID_FORMAT = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")
NODE = shutil.which("node")


@unittest.skipIf(NODE is None, "node not on PATH")
class GuardProposal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)
        self.env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)}
        patcher = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        claude = model.claude_dir()
        (claude / "hooks").mkdir(parents=True)
        self.installed = claude / "hooks" / "status.mjs"
        self.installed.write_text("old\n", encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "repo": str(self.home / "repo"),
            "files": {
                "hooks/status.mjs": {
                    "repo_path": ".claude/hooks/status.mjs",
                    "sha256": None,
                    "kept": False,
                }
            },
        }
        model.manifest_path().write_text(json.dumps(manifest), encoding="utf-8")

    def run_guard(self, tool: str, tool_input: dict) -> tuple[dict, Proposal, dict]:
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "sess-a1",
            "cwd": str(self.home / "project-A"),
            "tool_name": tool,
            "tool_input": tool_input,
        }
        proc = subprocess.run(
            [str(NODE), str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        files = list(model.inbox_dir().glob("*.json"))
        self.assertEqual(len(files), 1, files)
        proposal_id = files[0].stem
        self.assertRegex(proposal_id, ID_FORMAT)
        self.assertEqual(model.proposal_path(proposal_id), files[0])
        self.assertIn(f"inbox id {proposal_id}", out["permissionDecisionReason"])
        self.assertIn(".claude/hooks/status.mjs", out["permissionDecisionReason"])
        raw = json.loads(files[0].read_text(encoding="utf-8"))
        loaded = model.load_proposal(proposal_id)
        assert loaded is not None
        self.assertEqual(loaded.to_dict(), raw)
        self.assertEqual(Proposal.from_dict(raw).to_dict(), raw)
        return out, loaded, raw

    def test_write_proposal_loads_with_model(self):
        _, p, _ = self.run_guard(
            "Write", {"file_path": str(self.installed), "content": "new body\n"}
        )
        self.assertEqual(p.extra, {})
        self.assertEqual(p.schema_version, model.SCHEMA_VERSION)
        self.assertEqual((p.kind, p.status), ("edit", "pending"))
        self.assertEqual(p.change.tool, "Write")
        self.assertEqual(p.change.content, "new body\n")
        self.assertIsNone(p.change.command)
        self.assertEqual(p.target.repo_path, ".claude/hooks/status.mjs")
        self.assertEqual(
            os.path.normcase(p.target.installed_path or ""),
            os.path.normcase(str(self.installed)),
        )
        self.assertEqual(
            (p.source.project, p.source.session_id), ("project-A", "sess-a1")
        )
        self.assertRegex(p.created_at or "", r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.installed.read_text(encoding="utf-8"), "old\n")

    def test_multiedit_edits_round_trip_in_change_extra(self):
        edits = [
            {"old_string": "old", "new_string": "new"},
            {"old_string": "a", "new_string": "b"},
        ]
        _, p, raw = self.run_guard(
            "MultiEdit", {"file_path": str(self.installed), "edits": edits}
        )
        self.assertEqual(p.change.tool, "MultiEdit")
        self.assertEqual(p.change.extra, {"edits": edits})
        self.assertEqual(raw["change"]["edits"], edits)

    def test_notebook_cell_data_round_trips_in_change_extra(self):
        _, p, _ = self.run_guard(
            "NotebookEdit",
            {
                "notebook_path": str(self.installed),
                "new_source": "x = 1",
                "cell_id": "c1",
            },
        )
        self.assertEqual(p.change.content, "x = 1")
        self.assertEqual(
            p.change.extra,
            {"notebook": {"cell_id": "c1", "cell_type": None, "edit_mode": None}},
        )


if __name__ == "__main__":
    unittest.main()
