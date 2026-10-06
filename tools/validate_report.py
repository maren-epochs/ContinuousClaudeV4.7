#!/usr/bin/env python3
"""Validate autonomous-pipeline worker reports (and optionally contract.json).

Contract source: harness/agents/worker.md (Output) + phases/execute.md (Report),
phases/plan.md (contract.json). Stdlib only.

Usage:
    py -3.13 tools/validate_report.py reports/*.json
    py -3.13 tools/validate_report.py reports/*.json --contract contract.json

One line per finding: "<file>: ERROR|WARN <json.path>: <message>".
Unknown fields WARN (never fail); missing required fields, wrong types and
out-of-enum values ERROR. With --contract, the contract itself is validated
and every report's assertion id must exist in it.

Exit: 0 valid (warnings allowed), 1 any error, 2 usage error (bad args,
missing file).
"""

import argparse
import json
import re
import sys
from pathlib import Path

RESULTS = ("success", "partial", "blocked")
SEVERITIES = ("blocking", "non-blocking")
ASSERTION_TYPES = (
    "invariant",
    "behavioral",
    "contract",
    "property",
    "fuzz",
    "approval",
)
STATUSES = ("pending", "passed", "failed")
COMPLEXITIES = ("patch", "feature", "multi-feature", "greenfield")

REPORT_FIELDS = (
    "task",
    "assertion",
    "result",
    "implemented",
    "remaining",
    "tests",
    "checks",
    "bloks_used",
    "corrections",
    "discoveries",
    "issues",
    "conventions",
)
# Real bloks CLI: positional text, options after. --lib/--title/--body/--text don't exist.
BLOKS_CMD = re.compile(r'^bloks new rule "(?:[^"\\]|\\.)+"(?: --tags [\w.:/,+-]+)?$')
BAD_BLOKS_FLAGS = re.compile(r"--(?:lib|title|body|text)\b")
ASSERTION_ID = re.compile(r"^VAL-[\w.-]+$")


class Findings:
    def __init__(self):
        self.items = []  # (level, path, message)

    def error(self, path, msg):
        self.items.append(("ERROR", path, msg))

    def warn(self, path, msg):
        self.items.append(("WARN", path, msg))

    @property
    def errors(self):
        return [i for i in self.items if i[0] == "ERROR"]


def _type_name(v):
    return "null" if v is None else type(v).__name__


def _expect(f, obj, key, kind, path, required=True):
    """Check obj[key] is of `kind` (type or tuple). Returns the value or None."""
    if key not in obj:
        if required:
            f.error(f"{path}.{key}", "missing required field")
        return None
    v = obj[key]
    # bool is an int subclass — never accept it where an int is meant
    if kind is int and isinstance(v, bool) or not isinstance(v, kind):
        names = "|".join(
            k.__name__ for k in (kind if isinstance(kind, tuple) else (kind,))
        )
        f.error(f"{path}.{key}", f"expected {names}, got {_type_name(v)}")
        return None
    return v


def _extra(f, obj, allowed, path):
    for k in obj:
        if k not in allowed:
            f.warn(f"{path}.{k}", "unknown field")


def _list_of_objects(f, report, key, allowed, required, check=None):
    items = _expect(f, report, key, list, "$")
    if items is None:
        return
    for i, item in enumerate(items):
        p = f"$.{key}[{i}]"
        if not isinstance(item, dict):
            f.error(
                p, f"expected object {{{', '.join(required)}}}, got {_type_name(item)}"
            )
            continue
        for k in required:
            _expect(f, item, k, str, p)
        _extra(f, item, allowed, p)
        if check:
            check(f, item, p)


def _check_bloks_used(f, item, p):
    if "helpful" in item and not isinstance(item["helpful"], bool):
        f.error(f"{p}.helpful", f"expected bool, got {_type_name(item['helpful'])}")
    elif "helpful" not in item:
        f.error(f"{p}.helpful", "missing required field")
    if item.get("helpful") is False and not item.get("reason"):
        f.warn(
            f"{p}.reason", "helpful: false without a reason — EVOLVE can't act on it"
        )


def _check_discovery(f, item, p):
    cmd = item.get("bloks_cmd")
    if not isinstance(cmd, str) or cmd == "":
        return
    if BAD_BLOKS_FLAGS.search(cmd):
        f.error(
            f"{p}.bloks_cmd",
            'invented flag (--lib/--title/--body/--text); syntax: bloks new rule "<text>" --tags a,b',
        )
    elif not BLOKS_CMD.match(cmd):
        f.error(f"{p}.bloks_cmd", 'expected: bloks new rule "<text>" [--tags a,b]')


def _check_issue(f, item, p):
    sev = item.get("severity")
    if isinstance(sev, str) and sev not in SEVERITIES:
        f.error(f"{p}.severity", f"'{sev}' not in {SEVERITIES}")


def _check_check(f, item, p):
    keys = set(item)
    if {"command", "exit_code"} <= keys:
        if not isinstance(item["command"], str):
            f.error(f"{p}.command", f"expected str, got {_type_name(item['command'])}")
        if not isinstance(item["exit_code"], int) or isinstance(
            item["exit_code"], bool
        ):
            f.error(
                f"{p}.exit_code", f"expected int, got {_type_name(item['exit_code'])}"
            )
        _extra(f, item, ("command", "exit_code", "observed"), p)
    elif {"action", "observed"} <= keys:
        _extra(f, item, ("action", "observed"), p)
    else:
        f.error(p, "expected {command, exit_code} or {action, observed}")


def validate_report(report):
    """Return Findings for one parsed report object."""
    f = Findings()
    if not isinstance(report, dict):
        f.error("$", f"expected object, got {_type_name(report)}")
        return f

    result = _expect(f, report, "result", str, "$")
    if result is not None and result not in RESULTS:
        f.error("$.result", f"'{result}' not in {RESULTS}")

    # Early exit allowed by worker.md: {"result": "blocked", "reason": "..."}
    if result == "blocked" and "reason" in report and "implemented" not in report:
        _expect(f, report, "reason", str, "$")
        _extra(f, report, ("result", "reason", "task", "assertion"), "$")
        return f

    _expect(f, report, "task", str, "$")
    assertion = _expect(f, report, "assertion", str, "$")
    if assertion is not None:
        ids = [a.strip() for a in assertion.split(",")]
        bad = [a for a in ids if not ASSERTION_ID.match(a)]
        if bad:
            f.error("$.assertion", f"not an assertion id: {bad}")
        elif len(ids) > 1:
            f.warn(
                "$.assertion",
                "multiple assertions — plan decomposes one task = one assertion",
            )
    _expect(f, report, "implemented", str, "$")
    _expect(f, report, "remaining", str, "$")

    tests = _expect(f, report, "tests", dict, "$")
    if tests is not None:
        added = _expect(f, tests, "added", list, "$.tests")
        for i, t in enumerate(added or []):
            p = f"$.tests.added[{i}]"
            if not isinstance(t, dict):
                f.error(
                    p, f"expected object {{file, name, verifies}}, got {_type_name(t)}"
                )
                continue
            for k in ("file", "name", "verifies"):
                _expect(f, t, k, str, p)
            _extra(f, t, ("file", "name", "verifies"), p)
        command = _expect(f, tests, "command", str, "$.tests")
        if "exit_code" not in tests:
            f.error("$.tests.exit_code", "missing required field")
        else:
            code = tests["exit_code"]
            # null only when no test command ran (docs-only work)
            if code is None:
                if command:
                    f.error(
                        "$.tests.exit_code",
                        "null but tests.command is set — record the exit code",
                    )
            elif not isinstance(code, int) or isinstance(code, bool):
                f.error("$.tests.exit_code", f"expected int, got {_type_name(code)}")
        _extra(f, tests, ("added", "command", "exit_code"), "$.tests")

    checks = _expect(f, report, "checks", list, "$")
    for i, c in enumerate(checks or []):
        p = f"$.checks[{i}]"
        if not isinstance(c, dict):
            f.error(
                p,
                f"expected object {{command, exit_code}} | {{action, observed}}, got {_type_name(c)}",
            )
        else:
            _check_check(f, c, p)

    _list_of_objects(
        f,
        report,
        "bloks_used",
        ("card", "helpful", "reason"),
        ("card",),
        _check_bloks_used,
    )
    _list_of_objects(f, report, "corrections", ("block", "issue"), ("block", "issue"))
    _list_of_objects(
        f,
        report,
        "discoveries",
        ("lib", "finding", "bloks_cmd"),
        ("lib", "finding", "bloks_cmd"),
        _check_discovery,
    )
    _list_of_objects(
        f,
        report,
        "issues",
        ("severity", "description"),
        ("severity", "description"),
        _check_issue,
    )

    conventions = _expect(f, report, "conventions", list, "$")
    for i, c in enumerate(conventions or []):
        if not isinstance(c, str):
            f.error(f"$.conventions[{i}]", f"expected str, got {_type_name(c)}")

    _extra(f, report, REPORT_FIELDS + ("reason",), "$")
    return f


def validate_contract(contract):
    """Return (Findings, set of assertion ids) for a parsed contract.json."""
    f = Findings()
    ids = set()
    if not isinstance(contract, dict):
        f.error("$", f"expected object, got {_type_name(contract)}")
        return f, ids

    _expect(f, contract, "task", str, "$")
    cx = _expect(f, contract, "complexity", str, "$")
    if cx is not None and cx not in COMPLEXITIES:
        f.error("$.complexity", f"'{cx}' not in {COMPLEXITIES}")

    assertions = _expect(f, contract, "assertions", list, "$") or []
    for i, a in enumerate(assertions):
        p = f"$.assertions[{i}]"
        if not isinstance(a, dict):
            f.error(p, f"expected object, got {_type_name(a)}")
            continue
        aid = _expect(f, a, "id", str, p)
        if aid is not None:
            if aid in ids:
                f.error(f"{p}.id", f"duplicate id {aid}")
            ids.add(aid)
        _expect(f, a, "text", str, p)
        # Patch class has no milestones (SKILL.md PATCH FAST PATH): milestone may be null.
        if not (cx == "patch" and "milestone" in a and a["milestone"] is None):
            _expect(f, a, "milestone", str, p)
        for k, enum in (("type", ASSERTION_TYPES), ("status", STATUSES)):
            v = _expect(f, a, k, str, p)
            if v is not None and v not in enum:
                f.error(f"{p}.{k}", f"'{v}' not in {enum}")
        _expect(f, a, "depends", list, p)
        for k in ("worker", "evidence"):
            if k not in a:
                f.error(f"{p}.{k}", "missing required field (null until set)")
            elif a[k] is not None and not isinstance(a[k], str):
                f.error(f"{p}.{k}", f"expected str|null, got {_type_name(a[k])}")
        if a.get("status") == "passed" and not a.get("evidence"):
            f.error(f"{p}.evidence", "status passed without evidence")
        _extra(
            f,
            a,
            (
                "id",
                "type",
                "text",
                "milestone",
                "status",
                "depends",
                "worker",
                "evidence",
                "presentation",
                "variants",
                "medium",
            ),
            p,
        )

    milestones = _expect(f, contract, "milestones", list, "$") or []
    names = set()
    for i, m in enumerate(milestones):
        p = f"$.milestones[{i}]"
        if not isinstance(m, dict):
            f.error(p, f"expected object, got {_type_name(m)}")
            continue
        name = _expect(f, m, "name", str, p)
        if name:
            names.add(name)
        st = _expect(f, m, "status", str, p)
        if st is not None and st not in STATUSES:
            f.error(f"{p}.status", f"'{st}' not in {STATUSES}")
        for j, ref in enumerate(_expect(f, m, "assertions", list, p) or []):
            if ref not in ids:
                f.error(f"{p}.assertions[{j}]", f"unknown assertion id {ref!r}")
        _extra(f, m, ("name", "status", "assertions"), p)

    for i, a in enumerate(assertions):
        if not isinstance(a, dict):
            continue
        for j, dep in enumerate(a.get("depends") or []):
            if dep not in ids:
                f.error(
                    f"$.assertions[{i}].depends[{j}]", f"unknown assertion id {dep!r}"
                )
        ms = a.get("milestone")
        if milestones and isinstance(ms, str) and ms not in names:
            f.error(f"$.assertions[{i}].milestone", f"unknown milestone {ms!r}")

    _extra(
        f,
        contract,
        (
            "task",
            "complexity",
            "milestones",
            "assertions",
            "baseline",
            "premortem",
            "post_validation",
            "meta_goal",
        ),
        "$",
    )
    return f, ids


def _load(path):
    """Parse a JSON file. Returns (obj, None) or (None, error message)."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig")), None
    except json.JSONDecodeError as e:
        return None, f"malformed JSON: {e}"


def _emit(path, findings):
    for level, p, msg in findings.items:
        print(f"{path}: {level} {p}: {msg}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("reports", nargs="*", help="worker report JSON files")
    ap.add_argument(
        "--contract",
        help="contract.json: validate it and cross-check report assertion ids",
    )
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if not args.reports and not args.contract:
        ap.print_usage(sys.stderr)
        return 2
    missing = [
        p
        for p in args.reports + ([args.contract] if args.contract else [])
        if not Path(p).is_file()
    ]
    if missing:
        for p in missing:
            print(f"{p}: file not found", file=sys.stderr)
        return 2

    failed = False
    contract_ids = None
    if args.contract:
        obj, err = _load(args.contract)
        if err:
            print(f"{args.contract}: ERROR $: {err}")
            failed = True
        else:
            cf, contract_ids = validate_contract(obj)
            _emit(args.contract, cf)
            failed |= bool(cf.errors)

    for path in args.reports:
        obj, err = _load(path)
        if err:
            print(f"{path}: ERROR $: {err}")
            failed = True
            continue
        rf = validate_report(obj)
        if (
            contract_ids is not None
            and isinstance(obj, dict)
            and isinstance(obj.get("assertion"), str)
        ):
            for aid in (a.strip() for a in obj["assertion"].split(",")):
                if ASSERTION_ID.match(aid) and aid not in contract_ids:
                    rf.error("$.assertion", f"{aid} not in contract")
        _emit(path, rf)
        failed |= bool(rf.errors)

    n = len(args.reports) + (1 if args.contract else 0)
    print(f"{'FAIL' if failed else 'OK'}: {n} file(s) checked", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
