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
    "tests": {
        "added": [{"file": "t.py", "name": "retries", "verifies": "VAL-001"}],
        "command": "pytest t.py",
        "exit_code": 0,
    },
    "checks": [
        {"command": "ruff check .", "exit_code": 0},
        {"action": "ran CLI", "observed": "3 attempts logged"},
    ],
    "bloks_used": [
        {"card": "httpx-retry", "helpful": True},
        {"card": "old-card", "helpful": False, "reason": "API renamed"},
    ],
    "corrections": [{"block": "old-card", "issue": "API renamed"}],
    "discoveries": [
        {
            "lib": "httpx",
            "finding": "Client is not thread-safe",
            "bloks_cmd": 'bloks new rule "httpx: Client is not thread-safe" --tags httpx,python',
        }
    ],
    "issues": [{"severity": "non-blocking", "description": "flaky test t.py:4"}],
    "conventions": ["single quotes"],
}

CONTRACT = {
    "task": "retry",
    "complexity": "feature",
    "milestones": [{"name": "m1", "status": "pending", "assertions": ["VAL-001"]}],
    "assertions": [
        {
            "id": "VAL-001",
            "type": "invariant",
            "text": "fetch retries",
            "milestone": "m1",
            "status": "pending",
            "depends": [],
            "worker": None,
            "evidence": None,
        }
    ],
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
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
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
        code, out = self._run(
            self._report(
                lambda r: r["discoveries"][0].update(
                    bloks_cmd="bloks new rule --lib httpx --text 'x'"
                )
            )
        )
        self.assertEqual(code, 1)
        self.assertIn("invented flag", out)

    def test_space_separated_tags_fail(self):
        # clap value_delimiter=',' — a second bare word is an unexpected argument
        code, out = self._run(
            self._report(
                lambda r: r["discoveries"][0].update(
                    bloks_cmd='bloks new rule "x" --tags httpx python'
                )
            )
        )
        self.assertEqual(code, 1)
        self.assertIn("$.discoveries[0].bloks_cmd", out)

    def test_null_exit_code_needs_empty_command(self):
        code, out = self._run(self._report(lambda r: r["tests"].update(exit_code=None)))
        self.assertEqual(code, 1)
        self.assertIn("null but tests.command is set", out)
        code, out = self._run(
            self._report(lambda r: r["tests"].update(command="", exit_code=None))
        )
        self.assertEqual(code, 0, out)

    def test_blocked_early_exit_accepted(self):
        code, out = self._run(
            self._write("r.json", {"result": "blocked", "reason": "no creds"})
        )
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
        code, out = self._run(
            self._report(lambda r: r.update(assertion="VAL-999")), "--contract", c
        )
        self.assertEqual(code, 1)
        self.assertIn("VAL-999 not in contract", out)

    def test_contract_enum_and_dangling_refs_fail(self):
        bad = copy.deepcopy(CONTRACT)
        bad["assertions"][0].update(type="unit", status="done", depends=["VAL-404"])
        bad["milestones"][0]["assertions"].append("VAL-002")
        code, out = self._run("--contract", self._write("contract.json", bad))
        self.assertEqual(code, 1)
        for frag in (
            "$.assertions[0].type",
            "$.assertions[0].status",
            "depends[0]: unknown assertion id 'VAL-404'",
            "$.milestones[0].assertions[1]: unknown assertion id 'VAL-002'",
        ):
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


def _load_validator():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "validate_report_under_test", VALIDATOR
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_DROP = object()


def _with(base, **changes):
    obj = copy.deepcopy(base)
    for k, v in changes.items():
        if v is _DROP:
            obj.pop(k, None)
        else:
            obj[k] = v
    return obj


# Characterization (VAL-612): exact findings, in order, captured on HEAD 2aff883
# before validate_report/validate_contract/main were split into helpers.
REPORT_CASES = {
    "valid": VALID,
    "not-object": [1, 2],
    "blocked-early-exit": {"result": "blocked", "reason": 5, "extra": 1},
    "blocked-with-implemented": _with(VALID, result="blocked", reason="r"),
    "result-not-str": _with(VALID, result=1),
    "multi-assertion": _with(VALID, assertion="VAL-001, VAL-002"),
    "bad-assertion-ids": _with(VALID, assertion="VAL-1, nope"),
    "missing-scalars": _with(VALID, implemented=_DROP, task=3, remaining=None),
    "tests-not-object": _with(VALID, tests=[]),
    "tests-missing-exit": _with(VALID, tests={"added": [], "command": ""}),
    "tests-null-exit-with-command": _with(
        VALID, tests={"added": [], "command": "pytest", "exit_code": None}
    ),
    "tests-null-exit-no-command": _with(
        VALID, tests={"added": [], "command": "", "exit_code": None}
    ),
    "tests-bad-shapes": _with(
        VALID,
        tests={
            "added": [1, {"file": "f", "name": 2, "x": 1}],
            "command": 5,
            "exit_code": True,
            "z": 1,
        },
    ),
    "tests-added-not-list": _with(VALID, tests={"added": "x", "exit_code": "0"}),
    "checks-bad-shapes": _with(
        VALID,
        checks=[
            1,
            {"command": 5, "exit_code": "0"},
            {"command": "c", "exit_code": False, "observed": "o", "x": 1},
            {"foo": 1},
            {"action": "a", "observed": "o", "extra": 1},
        ],
    ),
    "checks-not-list": _with(VALID, checks={}),
    "bloks-used-shapes": _with(
        VALID,
        bloks_used=[
            {"card": "c", "helpful": "yes"},
            {"card": "c"},
            {"card": "c", "helpful": False},
            {"helpful": True, "x": 1},
            3,
        ],
    ),
    "lists-not-lists": _with(VALID, corrections="x", discoveries=None, issues=_DROP),
    "discoveries-shapes": _with(
        VALID,
        discoveries=[
            {"lib": "l", "finding": "f", "bloks_cmd": ""},
            {"lib": "l", "finding": "f", "bloks_cmd": "bloks new rule x"},
            {"lib": "l", "finding": "f", "bloks_cmd": 'bloks new rule "x" --lib y'},
            {"lib": "l", "finding": "f", "bloks_cmd": 7},
        ],
    ),
    "issues-shapes": _with(
        VALID,
        issues=[
            {"severity": "low", "description": "d"},
            {"severity": 1, "description": "d"},
            {"description": "d"},
        ],
    ),
    "conventions-shapes": _with(VALID, conventions=["ok", 3, None]),
    "conventions-not-list": _with(VALID, conventions="x"),
    "unknown-top-level": _with(VALID, zzz=1, reason="allowed"),
}

_A = CONTRACT["assertions"][0]
CONTRACT_CASES = {
    "valid": CONTRACT,
    "not-object": "x",
    "kitchen-sink": {
        "task": 1,
        "complexity": "huge",
        "assertions": [
            5,
            {
                "id": "VAL-1",
                "type": "bad",
                "text": "t",
                "milestone": "mX",
                "status": "weird",
                "depends": ["VAL-9"],
                "worker": 3,
            },
            dict(_A, id="VAL-1", status="passed", milestone="m1", extra=1),
            dict(_A, id="VAL-2", status="passed", evidence="e.txt", depends=["VAL-1"]),
            dict(_A, id=7, milestone=None),
        ],
        "milestones": [
            7,
            {"name": "m1", "status": "bad", "assertions": ["VAL-1", "VAL-404"], "x": 1},
            {"name": "", "status": "pending", "assertions": "notalist"},
        ],
        "other": 1,
    },
    "patch-null-milestone": dict(
        CONTRACT,
        complexity="patch",
        milestones=[],
        assertions=[dict(_A, milestone=None)],
    ),
    "feature-null-milestone": dict(CONTRACT, assertions=[dict(_A, milestone=None)]),
    "no-milestones": {
        "task": "t",
        "complexity": "feature",
        "assertions": [dict(_A, milestone="nowhere")],
    },
    "assertions-not-list": dict(CONTRACT, assertions={}, milestones="x"),
    "depends-null": dict(CONTRACT, assertions=[dict(_A, depends=None)]),
}

REPORT_GOLDEN = {
    "valid": [],
    "not-object": [["ERROR", "$", "expected object, got list"]],
    "blocked-early-exit": [
        ["ERROR", "$.reason", "expected str, got int"],
        ["WARN", "$.extra", "unknown field"],
    ],
    "blocked-with-implemented": [],
    "result-not-str": [["ERROR", "$.result", "expected str, got int"]],
    "multi-assertion": [
        [
            "WARN",
            "$.assertion",
            "multiple assertions — plan decomposes one task = one assertion",
        ]
    ],
    "bad-assertion-ids": [["ERROR", "$.assertion", "not an assertion id: ['nope']"]],
    "missing-scalars": [
        ["ERROR", "$.task", "expected str, got int"],
        ["ERROR", "$.implemented", "missing required field"],
        ["ERROR", "$.remaining", "expected str, got null"],
    ],
    "tests-not-object": [["ERROR", "$.tests", "expected dict, got list"]],
    "tests-missing-exit": [["ERROR", "$.tests.exit_code", "missing required field"]],
    "tests-null-exit-with-command": [
        [
            "ERROR",
            "$.tests.exit_code",
            "null but tests.command is set — record the exit code",
        ]
    ],
    "tests-null-exit-no-command": [],
    "tests-bad-shapes": [
        [
            "ERROR",
            "$.tests.added[0]",
            "expected object {file, name, verifies}, got int",
        ],
        ["ERROR", "$.tests.added[1].name", "expected str, got int"],
        ["ERROR", "$.tests.added[1].verifies", "missing required field"],
        ["WARN", "$.tests.added[1].x", "unknown field"],
        ["ERROR", "$.tests.command", "expected str, got int"],
        ["ERROR", "$.tests.exit_code", "expected int, got bool"],
        ["WARN", "$.tests.z", "unknown field"],
    ],
    "tests-added-not-list": [
        ["ERROR", "$.tests.added", "expected list, got str"],
        ["ERROR", "$.tests.command", "missing required field"],
        ["ERROR", "$.tests.exit_code", "expected int, got str"],
    ],
    "checks-bad-shapes": [
        [
            "ERROR",
            "$.checks[0]",
            "expected object {command, exit_code} | {action, observed}, got int",
        ],
        ["ERROR", "$.checks[1].command", "expected str, got int"],
        ["ERROR", "$.checks[1].exit_code", "expected int, got str"],
        ["ERROR", "$.checks[2].exit_code", "expected int, got bool"],
        ["WARN", "$.checks[2].x", "unknown field"],
        ["ERROR", "$.checks[3]", "expected {command, exit_code} or {action, observed}"],
        ["WARN", "$.checks[4].extra", "unknown field"],
    ],
    "checks-not-list": [["ERROR", "$.checks", "expected list, got dict"]],
    "bloks-used-shapes": [
        ["ERROR", "$.bloks_used[0].helpful", "expected bool, got str"],
        ["ERROR", "$.bloks_used[1].helpful", "missing required field"],
        [
            "WARN",
            "$.bloks_used[2].reason",
            "helpful: false without a reason — EVOLVE can't act on it",
        ],
        ["ERROR", "$.bloks_used[3].card", "missing required field"],
        ["WARN", "$.bloks_used[3].x", "unknown field"],
        ["ERROR", "$.bloks_used[4]", "expected object {card}, got int"],
    ],
    "lists-not-lists": [
        ["ERROR", "$.corrections", "expected list, got str"],
        ["ERROR", "$.discoveries", "expected list, got null"],
        ["ERROR", "$.issues", "missing required field"],
    ],
    "discoveries-shapes": [
        [
            "ERROR",
            "$.discoveries[1].bloks_cmd",
            'expected: bloks new rule "<text>" [--tags a,b]',
        ],
        [
            "ERROR",
            "$.discoveries[2].bloks_cmd",
            'invented flag (--lib/--title/--body/--text); syntax: bloks new rule "<text>" --tags a,b',
        ],
        ["ERROR", "$.discoveries[3].bloks_cmd", "expected str, got int"],
    ],
    "issues-shapes": [
        ["ERROR", "$.issues[0].severity", "'low' not in ('blocking', 'non-blocking')"],
        ["ERROR", "$.issues[1].severity", "expected str, got int"],
        ["ERROR", "$.issues[2].severity", "missing required field"],
    ],
    "conventions-shapes": [
        ["ERROR", "$.conventions[1]", "expected str, got int"],
        ["ERROR", "$.conventions[2]", "expected str, got null"],
    ],
    "conventions-not-list": [["ERROR", "$.conventions", "expected list, got str"]],
    "unknown-top-level": [["WARN", "$.zzz", "unknown field"]],
}
CONTRACT_GOLDEN = {
    "valid": ([], ["VAL-001"]),
    "not-object": ([["ERROR", "$", "expected object, got str"]], []),
    "kitchen-sink": (
        [
            ["ERROR", "$.task", "expected str, got int"],
            [
                "ERROR",
                "$.complexity",
                "'huge' not in ('patch', 'feature', 'multi-feature', 'greenfield')",
            ],
            ["ERROR", "$.assertions[0]", "expected object, got int"],
            [
                "ERROR",
                "$.assertions[1].type",
                "'bad' not in ('invariant', 'behavioral', 'contract', 'property', 'fuzz', 'approval')",
            ],
            [
                "ERROR",
                "$.assertions[1].status",
                "'weird' not in ('pending', 'passed', 'failed')",
            ],
            ["ERROR", "$.assertions[1].worker", "expected str|null, got int"],
            [
                "ERROR",
                "$.assertions[1].evidence",
                "missing required field (null until set)",
            ],
            ["ERROR", "$.assertions[2].id", "duplicate id VAL-1"],
            ["ERROR", "$.assertions[2].evidence", "status passed without evidence"],
            ["WARN", "$.assertions[2].extra", "unknown field"],
            ["ERROR", "$.assertions[4].id", "expected str, got int"],
            ["ERROR", "$.assertions[4].milestone", "expected str, got null"],
            ["ERROR", "$.milestones[0]", "expected object, got int"],
            [
                "ERROR",
                "$.milestones[1].status",
                "'bad' not in ('pending', 'passed', 'failed')",
            ],
            [
                "ERROR",
                "$.milestones[1].assertions[1]",
                "unknown assertion id 'VAL-404'",
            ],
            ["WARN", "$.milestones[1].x", "unknown field"],
            ["ERROR", "$.milestones[2].assertions", "expected list, got str"],
            ["ERROR", "$.assertions[1].depends[0]", "unknown assertion id 'VAL-9'"],
            ["ERROR", "$.assertions[1].milestone", "unknown milestone 'mX'"],
            ["WARN", "$.other", "unknown field"],
        ],
        ["VAL-1", "VAL-2"],
    ),
    "patch-null-milestone": ([], ["VAL-001"]),
    "feature-null-milestone": (
        [["ERROR", "$.assertions[0].milestone", "expected str, got null"]],
        ["VAL-001"],
    ),
    "no-milestones": (
        [["ERROR", "$.milestones", "missing required field"]],
        ["VAL-001"],
    ),
    "assertions-not-list": (
        [
            ["ERROR", "$.assertions", "expected list, got dict"],
            ["ERROR", "$.milestones", "expected list, got str"],
        ],
        [],
    ),
    "depends-null": (
        [["ERROR", "$.assertions[0].depends", "expected list, got null"]],
        ["VAL-001"],
    ),
}


class FindingsCharacterization(unittest.TestCase):
    """Pins every finding (level, path, message) and the contract id set."""

    @classmethod
    def setUpClass(cls):
        cls.vr = _load_validator()

    def test_report_findings_pinned(self):
        self.assertEqual(sorted(REPORT_GOLDEN), sorted(REPORT_CASES))
        for case, obj in REPORT_CASES.items():
            with self.subTest(case=case):
                got = self.vr.validate_report(copy.deepcopy(obj)).items
                self.assertEqual([list(i) for i in got], REPORT_GOLDEN[case])

    def test_contract_findings_pinned(self):
        self.assertEqual(sorted(CONTRACT_GOLDEN), sorted(CONTRACT_CASES))
        for case, obj in CONTRACT_CASES.items():
            with self.subTest(case=case):
                f, ids = self.vr.validate_contract(copy.deepcopy(obj))
                want_items, want_ids = CONTRACT_GOLDEN[case]
                self.assertEqual([list(i) for i in f.items], want_items)
                self.assertEqual(sorted(ids), want_ids)


class CliCharacterization(unittest.TestCase):
    """main() branches the CLI tests above do not reach."""

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
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_bad_flag_exits_2_and_help_exits_0(self):
        self.assertEqual(self._run("--bogus")[0], 2)
        code, out, _ = self._run("--help")
        self.assertEqual(code, 0)
        self.assertIn("--contract", out)

    def test_contract_only(self):
        c = self._write("c.json", CONTRACT)
        code, out, err = self._run("--contract", c)
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err.strip(), "OK: 1 file(s) checked")

    def test_malformed_contract_still_checks_reports(self):
        c = self._write("c.json", "{nope")
        r = self._write("r.json", VALID)
        code, out, err = self._run(r, "--contract", c)
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(f"{c}: ERROR $: malformed JSON: "))
        self.assertEqual(err.strip(), "FAIL: 2 file(s) checked")

    def test_cross_check_skips_bad_ids_and_non_dict_reports(self):
        c = self._write("c.json", CONTRACT)
        r1 = self._write("r1.json", _with(VALID, assertion="VAL-001, VAL-777, junk"))
        r2 = self._write("r2.json", [1])
        code, out, err = self._run(r1, r2, "--contract", c)
        self.assertEqual(code, 1)
        self.assertEqual(
            out.splitlines(),
            [
                f"{r1}: ERROR $.assertion: not an assertion id: ['junk']",
                f"{r1}: ERROR $.assertion: VAL-777 not in contract",
                f"{r2}: ERROR $: expected object, got list",
            ],
        )
        self.assertEqual(err.strip(), "FAIL: 3 file(s) checked")

    def test_missing_files_listed_exit_2(self):
        code, out, err = self._run("nope1.json", "--contract", "nope2.json")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(
            err.splitlines(),
            ["nope1.json: file not found", "nope2.json: file not found"],
        )


if __name__ == "__main__":
    unittest.main()
