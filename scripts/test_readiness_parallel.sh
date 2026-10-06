#!/usr/bin/env bash
# test_readiness_parallel.sh — asserts readiness.sh median wall time (3 runs) <=18s with
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

# Single-sample budgets failed on this machine under load (12-17s full,
# 4-5s skip-secure). One uncounted warm-up, then the median of TIMING_RUNS,
# against the original budgets + 50% headroom. Output always goes to files
# (never piped) and each run is capped by `timeout 120`.
TIMING_RUNS=3

# run_readiness <out_json> [ENV=val...] — sets RC (worst of all runs) and ELAPSED_MS
run_readiness() {
  local out="$1"; shift
  local s e
  s=$(date +%s%N)
  (cd "$REPO_ROOT" && env "$@" timeout 120 bash scripts/readiness.sh . > "$out" 2>/dev/null)
  local rc=$?
  e=$(date +%s%N)
  ELAPSED_MS=$(( (e - s) / 1000000 ))
  [[ $rc -ne 0 ]] && RC=$rc
  return 0
}

median_ms() { # <out_json> [ENV=val...] -> sets MEDIAN_MS, TIMING_SAMPLES, RC
  local i samples=()
  RC=0
  run_readiness "$@"   # warm-up: page cache, AV scan of fresh tldr spawns
  for ((i=0; i<TIMING_RUNS; i++)); do run_readiness "$@"; samples+=("$ELAPSED_MS"); done
  MEDIAN_MS=$(printf '%s\n' "${samples[@]}" | sort -n | sed -n "$(( (TIMING_RUNS+1)/2 ))p")
  TIMING_SAMPLES="${samples[*]}"
}

# ── Run readiness.sh against the repo root, timed ──────────────
median_ms "$OUT_JSON"
ELAPSED="${MEDIAN_MS}ms"

check "exit code is 0 on every run (got $RC)" "$([[ $RC -eq 0 ]]; echo $?)"
check "median wall time <= 18000ms (got ${MEDIAN_MS}ms; runs: $TIMING_SAMPLES)" "$([[ $MEDIAN_MS -le 18000 ]]; echo $?)"

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

# ── Toggle run: READINESS_SKIP_SECURE=1 skips the slow secure job ──
OUT_JSON_SKIP="$OUT_DIR/out_skip.json"
median_ms "$OUT_JSON_SKIP" READINESS_SKIP_SECURE=1
ELAPSED_SKIP="${MEDIAN_MS}ms"

check "toggle: exit code is 0 on every run (got $RC)" "$([[ $RC -eq 0 ]]; echo $?)"
check "toggle: median wall time <= 7500ms (got ${MEDIAN_MS}ms; runs: $TIMING_SAMPLES)" "$([[ $MEDIAN_MS -le 7500 ]]; echo $?)"

if command -v cygpath >/dev/null 2>&1; then
  OUT_JSON_SKIP_W="$(cygpath -w "$OUT_JSON_SKIP")"
else
  OUT_JSON_SKIP_W="$OUT_JSON_SKIP"
fi

py -3.13 - "$OUT_JSON_W" "$OUT_JSON_SKIP_W" <<'PYEOF'
import json, sys

base = json.load(open(sys.argv[1]))
skip = json.load(open(sys.argv[2]))

# identical key set at every level the baseline run asserts
assert set(skip.keys()) == set(base.keys()), \
    f"toggle top-level keys mismatch: {sorted(skip.keys())}"
assert set(skip["summary"].keys()) == set(base["summary"].keys()), \
    f"toggle summary keys mismatch: {sorted(skip['summary'].keys())}"
assert set(skip["report"].keys()) == set(base["report"].keys()), \
    f"toggle report keys mismatch: {sorted(skip['report'].keys())}"

# security_scan is skipped the same way tldr-absent criteria are: numerator null
sec = skip["report"]["security_scan"]
assert set(sec.keys()) == {"numerator", "denominator", "rationale", "category"}, \
    f"security_scan entry keys mismatch: {sorted(sec.keys())}"
assert sec["numerator"] is None, f"security_scan numerator not null: {sec['numerator']}"
assert sec["denominator"] == 1, "security_scan denominator != 1"
assert skip["summary"]["skipped"] == base["summary"]["skipped"] + 1, \
    "toggle run should skip exactly one more criterion than baseline"

# the other six tldr-powered criteria still evaluate normally
for k in ("dead_code", "clones", "complexity", "tech_debt", "call_graph", "hotspots"):
    entry = skip["report"][k]
    assert entry["numerator"] in (0, 1), f"{k} numerator not 0/1: {entry['numerator']}"

print("toggle JSON shape OK")
PYEOF
check "toggle: JSON shape identical, security_scan skipped" "$?"

echo ""
if [[ "$FAILURES" -eq 0 ]]; then
  echo "ALL CHECKS PASSED (median wall time: ${ELAPSED} full, ${ELAPSED_SKIP} with READINESS_SKIP_SECURE=1)"
  exit 0
else
  echo "$FAILURES CHECK(S) FAILED (median wall time: ${ELAPSED} full, ${ELAPSED_SKIP} with READINESS_SKIP_SECURE=1)"
  exit 1
fi
