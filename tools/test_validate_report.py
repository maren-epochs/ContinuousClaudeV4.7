"""Tests for tools/validate_report.py.

Covers:
  (a) a fully valid report passes, exit 0
  (b) missing required field / bad enum / wrong type -> exit 1 with JSON path
  (c) unknown fields warn but don't fail
  (d) invented bloks flags and space-separated --tags are errors
  (e) blocked early-exit report shape is accepted
  (f) malformed JSON -> exit 1; missing file / no args -> exit 2
  (g) --contract: enums, dangling ids, report assertion must exist in contract

Run: py -3.13 tools/test_validate_report.py

Tests go through the CLI via subprocess — same path the VALIDATE phase uses.
"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
VALIDATOR = PROJECT / "tools" / "validate_report.py"

VALID = {
    "task": "add retry to fetch",
    "assertion": "VAL-001",
    "result": "success",
    "implemented": "wrapped fetch in retry(3)",
    "remaining": "",
    "tests": {"added": [{"file": "t.py", "name": "retries", "verifies": "VAL-001"}],
              "command": "pytest t.py", "exit_code": 0},
    "checks": [{"command": "ruff check .", "exit_code": 0},
               {"action": "ran CLI", "observed": "3 attempts logged"}],
    "bloks_used": [{"card": "httpx-retry", "helpful": True},
                   {"card": "old-card", "helpful": False, "reason": "API renamed"}],
    "corrections": [{"block": "old-card", "issue": "API renamed"}],
    "discoveries": [{"lib": "httpx", "finding": "Client is not thread-safe",
                     "bloks_cmd": 'bloks new rule "httpx: Client is not thread-safe" --tags httpx,python'}],
    "issues": [{"severity": "non-blocking", "description": "flaky test t.py:4"}],
    "conventions": ["single quotes"],
}

CONTRACT = {
    "task": "retry",
    "complexity": "feature",
    "milestones": [{"name": "m1", "status": "pending", "assertions": ["VAL-001"]}],
    "assertions": [{"id": "VAL-001", "type": "invariant", "text": "fetch retries", "milestone": "m1",
                    "status": "pending", "depends": [], "worker": None, "evidence": None}],
}


class ValidateReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, obj):
        p = self.dir / name
        p.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
        return str(p)

    def _run(self, *args):
        proc = subprocess.run([sys.executable, str(VALIDATOR), *args],
                              capture_output=True, text=True, encoding="utf-8", check=False)
        return proc.returncode, proc.stdout

    def _report(self, mutate):
        r = copy.deepcopy(VALID)
        mutate(r)
        return self._write("r.json", r)

    def test_valid_report_passes(self):
        code, out = self._run(self._write("r.json", VALID))
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "")

    def test_missing_field_fails_with_path(self):
        code, out = self._run(self._report(lambda r: r.pop("remaining")))
        self.assertEqual(code, 1)
        self.assertIn("ERROR $.remaining: missing required field", out)

    def test_bad_result_enum_fails(self):
        code, out = self._run(self._report(lambda r: r.update(result="pass")))
        self.assertEqual(code, 1)
        self.assertIn("ERROR $.result: 'pass' not in", out)

    def test_bad_issue_severity_and_string_checks_fail(self):
        def m(r):
            r["issues"] = [{"severity": "minor", "description": "x"}]
            r["checks"] = ["node --check: pass"]
        code, out = self._run(self._report(m))
        self.assertEqual(code, 1)
        self.assertIn("ERROR $.issues[0].severity", out)
        self.assertIn("ERROR $.checks[0]: expected object", out)

    def test_wrong_type_fails(self):
        code, out = self._run(self._report(lambda r: r.update(implemented=["a", "b"])))
        self.assertEqual(code, 1)
        self.assertIn("ERROR $.implemented: expected str, got list", out)

    def test_extra_field_warns_only(self):
        def m(r):
            r["notes"] = "x"
            r["tests"]["red"] = "3 failed"
        code, out = self._run(self._report(m))
        self.assertEqual(code, 0, out)
        self.assertIn("WARN $.notes: unknown field", out)
        self.assertIn("WARN $.tests.red: unknown field", out)

    def test_invented_bloks_flags_fail(self):
        code, out = self._run(self._report(lambda r: r["discoveries"][0].update(
            bloks_cmd="bloks new rule --lib httpx --text 'x'")))
        self.assertEqual(code, 1)
        self.assertIn("invented flag", out)

    def test_space_separated_tags_fail(self):
        # clap value_delimiter=',' — a second bare word is an unexpected argument
        code, out = self._run(self._report(lambda r: r["discoveries"][0].update(
            bloks_cmd='bloks new rule "x" --tags httpx python')))
        self.assertEqual(code, 1)
        self.assertIn("$.discoveries[0].bloks_cmd", out)

    def test_null_exit_code_needs_empty_command(self):
        code, out = self._run(self._report(lambda r: r["tests"].update(exit_code=None)))
        self.assertEqual(code, 1)
        self.assertIn("null but tests.command is set", out)
        code, out = self._run(self._report(lambda r: r["tests"].update(command="", exit_code=None)))
        self.assertEqual(code, 0, out)

    def test_blocked_early_exit_accepted(self):
        code, out = self._run(self._write("r.json", {"result": "blocked", "reason": "no creds"}))
        self.assertEqual(code, 0, out)

    def test_malformed_json_fails(self):
        code, out = self._run(self._write("r.json", '{"task": "x",'))
        self.assertEqual(code, 1)
        self.assertIn("malformed JSON", out)

    def test_usage_errors_exit_2(self):
        self.assertEqual(self._run()[0], 2)
        self.assertEqual(self._run(str(self.dir / "nope.json"))[0], 2)

    def test_contract_valid_and_cross_check(self):
        c = self._write("contract.json", CONTRACT)
        code, out = self._run(self._write("r.json", VALID), "--contract", c)
        self.assertEqual(code, 0, out)
        code, out = self._run(self._report(lambda r: r.update(assertion="VAL-999")), "--contract", c)
        self.assertEqual(code, 1)
        self.assertIn("VAL-999 not in contract", out)

    def test_contract_enum_and_dangling_refs_fail(self):
        bad = copy.deepcopy(CONTRACT)
        bad["assertions"][0].update(type="unit", status="done", depends=["VAL-404"])
        bad["milestones"][0]["assertions"].append("VAL-002")
        code, out = self._run("--contract", self._write("contract.json", bad))
        self.assertEqual(code, 1)
        for frag in ("$.assertions[0].type", "$.assertions[0].status",
                     "depends[0]: unknown assertion id 'VAL-404'",
                     "$.milestones[0].assertions[1]: unknown assertion id 'VAL-002'"):
            self.assertIn(frag, out)

    def test_contract_passed_requires_evidence(self):
        bad = copy.deepcopy(CONTRACT)
        bad["assertions"][0]["status"] = "passed"
        code, out = self._run("--contract", self._write("contract.json", bad))
        self.assertEqual(code, 1)
        self.assertIn("status passed without evidence", out)

    def test_patch_contract_allows_null_milestone_only_for_patch(self):
        patch = copy.deepcopy(CONTRACT)
        patch.update(complexity="patch", milestones=[])
        patch["assertions"][0]["milestone"] = None
        code, out = self._run("--contract", self._write("contract.json", patch))
        self.assertEqual(code, 0, out)
        feature = copy.deepcopy(patch)
        feature["complexity"] = "feature"
        code, out = self._run("--contract", self._write("contract.json", feature))
        self.assertEqual(code, 1)
        self.assertIn("$.assertions[0].milestone: expected str, got null", out)
        missing = copy.deepcopy(patch)
        del missing["assertions"][0]["milestone"]
        code, out = self._run("--contract", self._write("contract.json", missing))
        self.assertEqual(code, 1)
        self.assertIn("$.assertions[0].milestone", out)


if __name__ == "__main__":
    unittest.main()
