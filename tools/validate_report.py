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
    """Accumulates (level, path, message) validation findings for one file."""

    def __init__(self):
        self.items = []  # (level, path, message)

    def error(self, path, msg):
        """Record an ERROR finding at the given JSON path."""
        self.items.append(("ERROR", path, msg))

    def warn(self, path, msg):
        """Record a WARN finding at the given JSON path."""
        self.items.append(("WARN", path, msg))

    @property
    def errors(self):
        """Only the ERROR-level findings, in insertion order."""
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


def _check_assertion_ref(f, assertion):
    """$.assertion: comma-separated ids; more than one is a WARN."""
    ids = [a.strip() for a in assertion.split(",")]
    bad = [a for a in ids if not ASSERTION_ID.match(a)]
    if bad:
        f.error("$.assertion", f"not an assertion id: {bad}")
    elif len(ids) > 1:
        f.warn(
            "$.assertion",
            "multiple assertions — plan decomposes one task = one assertion",
        )


def _check_tests_added(f, added):
    for i, t in enumerate(added or []):
        p = f"$.tests.added[{i}]"
        if not isinstance(t, dict):
            f.error(p, f"expected object {{file, name, verifies}}, got {_type_name(t)}")
            continue
        for k in ("file", "name", "verifies"):
            _expect(f, t, k, str, p)
        _extra(f, t, ("file", "name", "verifies"), p)


def _check_tests_exit_code(f, tests, command):
    if "exit_code" not in tests:
        f.error("$.tests.exit_code", "missing required field")
        return
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


def _check_tests(f, report):
    tests = _expect(f, report, "tests", dict, "$")
    if tests is None:
        return
    _check_tests_added(f, _expect(f, tests, "added", list, "$.tests"))
    command = _expect(f, tests, "command", str, "$.tests")
    _check_tests_exit_code(f, tests, command)
    _extra(f, tests, ("added", "command", "exit_code"), "$.tests")


def _check_checks(f, report):
    checks = _expect(f, report, "checks", list, "$")
    for i, c in enumerate(checks or []):
        p = f"$.checks[{i}]"
        if isinstance(c, dict):
            _check_check(f, c, p)
        else:
            f.error(
                p,
                f"expected object {{command, exit_code}} | {{action, observed}}, got {_type_name(c)}",
            )


def _check_conventions(f, report):
    conventions = _expect(f, report, "conventions", list, "$")
    for i, c in enumerate(conventions or []):
        if not isinstance(c, str):
            f.error(f"$.conventions[{i}]", f"expected str, got {_type_name(c)}")


# key -> (allowed fields, required str fields, per-item check)
REPORT_LISTS = (
    ("bloks_used", ("card", "helpful", "reason"), ("card",), _check_bloks_used),
    ("corrections", ("block", "issue"), ("block", "issue"), None),
    (
        "discoveries",
        ("lib", "finding", "bloks_cmd"),
        ("lib", "finding", "bloks_cmd"),
        _check_discovery,
    ),
    ("issues", ("severity", "description"), ("severity", "description"), _check_issue),
)


def _is_blocked_early_exit(result, report):
    """worker.md allows {"result": "blocked", "reason": "..."} with nothing else."""
    return result == "blocked" and "reason" in report and "implemented" not in report


def validate_report(report):
    """Return Findings for one parsed report object."""
    f = Findings()
    if not isinstance(report, dict):
        f.error("$", f"expected object, got {_type_name(report)}")
        return f

    result = _expect(f, report, "result", str, "$")
    if result is not None and result not in RESULTS:
        f.error("$.result", f"'{result}' not in {RESULTS}")

    if _is_blocked_early_exit(result, report):
        _expect(f, report, "reason", str, "$")
        _extra(f, report, ("result", "reason", "task", "assertion"), "$")
        return f

    _expect(f, report, "task", str, "$")
    assertion = _expect(f, report, "assertion", str, "$")
    if assertion is not None:
        _check_assertion_ref(f, assertion)
    _expect(f, report, "implemented", str, "$")
    _expect(f, report, "remaining", str, "$")
    _check_tests(f, report)
    _check_checks(f, report)
    for key, allowed, required, check in REPORT_LISTS:
        _list_of_objects(f, report, key, allowed, required, check)
    _check_conventions(f, report)
    _extra(f, report, REPORT_FIELDS + ("reason",), "$")
    return f


ASSERTION_FIELDS = (
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
)
MILESTONE_FIELDS = ("name", "status", "assertions")
CONTRACT_FIELDS = (
    "task",
    "complexity",
    "milestones",
    "assertions",
    "baseline",
    "premortem",
    "post_validation",
    "meta_goal",
)


def _check_enum(f, obj, key, enum, path):
    """obj[key] is a str in enum (missing / wrong type reported by _expect)."""
    v = _expect(f, obj, key, str, path)
    if v is not None and v not in enum:
        f.error(f"{path}.{key}", f"'{v}' not in {enum}")
    return v


def _check_nullable_str(f, a, key, p):
    if key not in a:
        f.error(f"{p}.{key}", "missing required field (null until set)")
    elif a[key] is not None and not isinstance(a[key], str):
        f.error(f"{p}.{key}", f"expected str|null, got {_type_name(a[key])}")


def _check_contract_assertion(f, a, p, cx, ids):
    """One assertion object; records its id in ids."""
    _record_id(f, _expect(f, a, "id", str, p), p, ids)
    _expect(f, a, "text", str, p)
    if not _null_patch_milestone(a, cx):
        _expect(f, a, "milestone", str, p)
    _check_enum(f, a, "type", ASSERTION_TYPES, p)
    _check_enum(f, a, "status", STATUSES, p)
    _expect(f, a, "depends", list, p)
    for k in ("worker", "evidence"):
        _check_nullable_str(f, a, k, p)
    if a.get("status") == "passed" and not a.get("evidence"):
        f.error(f"{p}.evidence", "status passed without evidence")
    _extra(f, a, ASSERTION_FIELDS, p)


def _record_id(f, aid, p, ids):
    """Add a (non-None) assertion id to ids; a repeat is a duplicate-id error."""
    if aid is None:
        return
    if aid in ids:
        f.error(f"{p}.id", f"duplicate id {aid}")
    ids.add(aid)


def _null_patch_milestone(a, cx):
    """Patch class has no milestones (SKILL.md PATCH FAST PATH): milestone may be null."""
    return cx == "patch" and "milestone" in a and a["milestone"] is None


def _check_milestone(f, m, p, ids, names):
    """One milestone object; records its name in names."""
    name = _expect(f, m, "name", str, p)
    if name:
        names.add(name)
    _check_enum(f, m, "status", STATUSES, p)
    for j, ref in enumerate(_expect(f, m, "assertions", list, p) or []):
        if ref not in ids:
            f.error(f"{p}.assertions[{j}]", f"unknown assertion id {ref!r}")
    _extra(f, m, MILESTONE_FIELDS, p)


def _each_object(f, items, key):
    """Yield (path, item) for dict items of $.key[]; non-dicts are errors."""
    for i, item in enumerate(items):
        p = f"$.{key}[{i}]"
        if isinstance(item, dict):
            yield p, item
        else:
            f.error(p, f"expected object, got {_type_name(item)}")


def _check_assertion_refs(f, a, i, ids, names):
    """An assertion's depends[] and milestone point at things that exist.

    names is None when the contract has no milestones (milestone not checked).
    """
    for j, dep in enumerate(a.get("depends") or []):
        if dep not in ids:
            f.error(f"$.assertions[{i}].depends[{j}]", f"unknown assertion id {dep!r}")
    ms = a.get("milestone")
    if names is not None and isinstance(ms, str) and ms not in names:
        f.error(f"$.assertions[{i}].milestone", f"unknown milestone {ms!r}")


def _check_milestones(f, contract, ids):
    """Check $.milestones[]; their names, or None when there are no milestones."""
    milestones = _expect(f, contract, "milestones", list, "$") or []
    names = set()
    for p, m in _each_object(f, milestones, "milestones"):
        _check_milestone(f, m, p, ids, names)
    return names if milestones else None


def validate_contract(contract):
    """Return (Findings, set of assertion ids) for a parsed contract.json."""
    f = Findings()
    ids = set()
    if not isinstance(contract, dict):
        f.error("$", f"expected object, got {_type_name(contract)}")
        return f, ids

    _expect(f, contract, "task", str, "$")
    cx = _check_enum(f, contract, "complexity", COMPLEXITIES, "$")

    assertions = _expect(f, contract, "assertions", list, "$") or []
    for p, a in _each_object(f, assertions, "assertions"):
        _check_contract_assertion(f, a, p, cx, ids)

    names = _check_milestones(f, contract, ids)
    for i, a in enumerate(assertions):
        if isinstance(a, dict):
            _check_assertion_refs(f, a, i, ids, names)

    _extra(f, contract, CONTRACT_FIELDS, "$")
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


def _build_parser():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("reports", nargs="*", help="worker report JSON files")
    ap.add_argument(
        "--contract",
        help="contract.json: validate it and cross-check report assertion ids",
    )
    return ap


def _report_missing(paths):
    """Print 'file not found' for each missing path; True if any."""
    missing = [p for p in paths if not Path(p).is_file()]
    for p in missing:
        print(f"{p}: file not found", file=sys.stderr)
    return bool(missing)


def _check_contract_file(path):
    """Validate contract.json. Returns (failed, contract ids or None)."""
    obj, err = _load(path)
    if err:
        print(f"{path}: ERROR $: {err}")
        return True, None
    cf, ids = validate_contract(obj)
    _emit(path, cf)
    return bool(cf.errors), ids


def _cross_check(rf, obj, contract_ids):
    """Every well-formed assertion id of the report must exist in the contract."""
    if contract_ids is None or not isinstance(obj, dict):
        return
    if not isinstance(obj.get("assertion"), str):
        return
    for aid in (a.strip() for a in obj["assertion"].split(",")):
        if ASSERTION_ID.match(aid) and aid not in contract_ids:
            rf.error("$.assertion", f"{aid} not in contract")


def _check_report_file(path, contract_ids):
    """Validate one report file. Returns True if it has errors."""
    obj, err = _load(path)
    if err:
        print(f"{path}: ERROR $: {err}")
        return True
    rf = validate_report(obj)
    _cross_check(rf, obj, contract_ids)
    _emit(path, rf)
    return bool(rf.errors)


def main(argv=None):
    """CLI entry: validate reports/contract; exit 0 ok, 1 findings, 2 usage or missing file."""
    ap = _build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if not args.reports and not args.contract:
        ap.print_usage(sys.stderr)
        return 2
    return _check_files(args)


def _check_files(args):
    """Check the contract (ids feed the reports' cross-check), then each report."""
    paths = args.reports + ([args.contract] if args.contract else [])
    if _report_missing(paths):
        return 2

    failed = False
    contract_ids = None
    if args.contract:
        failed, contract_ids = _check_contract_file(args.contract)
    for path in args.reports:
        failed |= _check_report_file(path, contract_ids)

    print(
        f"{'FAIL' if failed else 'OK'}: {len(paths)} file(s) checked", file=sys.stderr
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
