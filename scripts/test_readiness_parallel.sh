#!/usr/bin/env bash
# test_readiness_parallel.sh — asserts readiness.sh completes within 12s with
# unchanged exit behavior and unchanged JSON report shape.
#
# Usage: bash scripts/test_readiness_parallel.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OUT_DIR="$(mktemp -d)"
trap 'rm -rf "$OUT_DIR"' EXIT
OUT_JSON="$OUT_DIR/out.json"

FAILURES=0
check() {
  local label="$1" ok="$2"
  if [[ "$ok" == "0" ]]; then
    echo "PASS: $label"
  else
    echo "FAIL: $label"
    FAILURES=$((FAILURES + 1))
  fi
}

# ── Run readiness.sh against the repo root, timed ──────────────
START=$(date +%s)
(cd "$REPO_ROOT" && bash scripts/readiness.sh . > "$OUT_JSON" 2>/dev/null)
RC=$?
END=$(date +%s)
ELAPSED=$((END - START))

check "exit code is 0 (got $RC)" "$([[ $RC -eq 0 ]]; echo $?)"
check "wall time <= 12s (got ${ELAPSED}s)" "$([[ $ELAPSED -le 12 ]]; echo $?)"

# ── JSON shape assertions ──────────────────────────────────────
# py needs a Windows path on Git Bash
if command -v cygpath >/dev/null 2>&1; then
  OUT_JSON_W="$(cygpath -w "$OUT_JSON")"
else
  OUT_JSON_W="$OUT_JSON"
fi

py -3.13 - "$OUT_JSON_W" <<'PYEOF'
import json, sys

d = json.load(open(sys.argv[1]))

expected_top = {"target", "language", "evaluatedAt", "level", "passRate",
                "errorSurface", "summary", "report"}
assert set(d.keys()) == expected_top, f"top-level keys mismatch: {sorted(d.keys())}"

expected_summary = {"total", "evaluated", "passing", "failing", "skipped",
                    "deterministicConstraints"}
assert set(d["summary"].keys()) == expected_summary, \
    f"summary keys mismatch: {sorted(d['summary'].keys())}"

expected_report = {
    "agents_md", "build_cmd_doc", "call_graph", "clones", "codeowners",
    "complexity", "coverage", "dead_code", "dep_update_auto", "deps_pinned",
    "env_template", "formatter", "gitignore", "hotspots", "integration_tests",
    "issue_templates", "lint_config", "pr_templates", "pre_commit_hooks",
    "readme", "security_scan", "single_cmd_setup", "skills", "tech_debt",
    "test_config", "type_check", "unit_tests",
}
assert set(d["report"].keys()) == expected_report, \
    f"report keys mismatch: {sorted(d['report'].keys())}"

# the seven tldr-powered criteria keep their numerator/denominator shape
for k in ("dead_code", "clones", "complexity", "tech_debt",
          "security_scan", "call_graph", "hotspots"):
    entry = d["report"][k]
    assert set(entry.keys()) == {"numerator", "denominator", "rationale", "category"}, \
        f"{k} entry keys mismatch: {sorted(entry.keys())}"
    assert entry["denominator"] == 1, f"{k} denominator != 1"
    assert entry["numerator"] in (0, 1), f"{k} numerator not 0/1: {entry['numerator']}"

print("JSON shape OK")
PYEOF
check "JSON parses with expected keys and shape" "$?"

echo ""
if [[ "$FAILURES" -eq 0 ]]; then
  echo "ALL CHECKS PASSED (wall time: ${ELAPSED}s)"
  exit 0
else
  echo "$FAILURES CHECK(S) FAILED (wall time: ${ELAPSED}s)"
  exit 1
fi
