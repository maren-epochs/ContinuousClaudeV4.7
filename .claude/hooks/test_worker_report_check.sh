#!/usr/bin/env bash
# Tests for .claude/hooks/worker-report-check.mjs (worker frontmatter Stop -> SubagentStop)
#   block on invalid report; allow on valid; allow when stop_hook_active;
#   allow when no report path in transcript; block when output path has no file;
#   fail-open when python missing; output field beats a later sibling Write;
#   loop guard caps blocks per agent_id.
# Self-contained; run from anywhere: bash .claude/hooks/test_worker_report_check.sh
set -u

HOOK="$(cd "$(dirname "$0")" && pwd)/worker-report-check.mjs"
TMPWIN="$(node -e "console.log(require('os').tmpdir().replace(/\\\\/g,'/'))")"
WORK="$TMPWIN/wrc_test_$$"
ERRFILE="$WORK/err.txt"
mkdir -p "$WORK/continuum/autonomous/t1/reports"
trap 'rm -rf "$WORK"; rm -f "$TMPWIN"/worker-report-check-wrc-*.txt' EXIT

PASS=0
FAIL=0
check() { # <name> <condition result: 0 ok>
  if [ "$2" -eq 0 ]; then PASS=$((PASS+1)); echo "PASS: $1";
  else FAIL=$((FAIL+1)); echo "FAIL: $1"; fi
}

# run_hook <stdin payload> [env...] — sets OUT, ERR, RC
run_hook() {
  local p="$1"; shift
  OUT=$(printf '%s' "$p" | env "$@" node "$HOOK" 2>"$ERRFILE"); RC=$?
  ERR=$(cat "$ERRFILE" 2>/dev/null || true)
}

REL="continuum/autonomous/t1/reports/worker-1.json"
REPORT="$WORK/$REL"
SIB="$WORK/continuum/autonomous/t1/reports/worker-2.json"

# transcript <file> <output-path-or-empty> [extra Write path] — first user msg carries the prompt JSON
transcript() {
  local f="$1" out="$2" extra="${3:-}"
  local prompt='{\"role\":\"implement\"}'
  [ -n "$out" ] && prompt="{\\\"role\\\":\\\"implement\\\",\\\"output\\\":\\\"$out\\\"}"
  printf '{"type":"user","message":{"role":"user","content":"%s"}}\n' "$prompt" > "$f"
  printf '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"Write","input":{"file_path":"%s"}}]}}\n' "$REPORT" >> "$f"
  [ -n "$extra" ] && printf '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"Write","input":{"file_path":"%s"}}]}}\n' "$extra" >> "$f"
  return 0
}
payload() { # <transcript> <agent_id> [stop_hook_active]
  printf '{"hook_event_name":"SubagentStop","agent_id":"%s","agent_type":"worker","stop_hook_active":%s,"agent_transcript_path":"%s","cwd":"%s"}' \
    "$2" "${3:-false}" "$1" "$WORK"
}

VALID='{"task":"t","assertion":"VAL-001","result":"success","implemented":"x","remaining":"","tests":{"added":[{"file":"t.py","name":"n","verifies":"VAL-001"}],"command":"pytest","exit_code":0},"checks":[{"command":"ruff check .","exit_code":0}],"bloks_used":[],"corrections":[],"discoveries":[],"issues":[],"conventions":[]}'
INVALID='{"task":"t","assertion":"VAL-001","result":"pass","implemented":"x","remaining":"","tests":{"added":[],"command":"pytest","exit_code":0},"checks":["ran tests"],"bloks_used":[],"corrections":[],"discoveries":[],"issues":["oops"],"conventions":[]}'

T="$WORK/agent.jsonl"
transcript "$T" "$REL"

# 1. invalid report -> block with ERROR lines
printf '%s' "$INVALID" > "$REPORT"
run_hook "$(payload "$T" wrc-a1)"
echo "$OUT" | grep -q '"decision":"block"'; check "invalid report blocks" $?
echo "$OUT" | grep -q 'ERROR'; check "block reason carries ERROR lines" $?

# 2. valid report -> allow
printf '%s' "$VALID" > "$REPORT"
run_hook "$(payload "$T" wrc-a2)"
[ "$OUT" = "{}" ] && [ $RC -eq 0 ]; check "valid report allows" $?

# 3. stop_hook_active -> allow even when invalid
printf '%s' "$INVALID" > "$REPORT"
run_hook "$(payload "$T" wrc-a3 true)"
[ "$OUT" = "{}" ]; check "stop_hook_active allows" $?

# 4. no report path anywhere in transcript -> allow + note
T2="$WORK/agent2.jsonl"
printf '{"type":"user","message":{"role":"user","content":"hello"}}\n' > "$T2"
run_hook "$(payload "$T2" wrc-a4)"
[ "$OUT" = "{}" ] && echo "$ERR" | grep -q 'no report path'; check "no report path allows with note" $?

# 5. output path known but file missing -> block
T3="$WORK/agent3.jsonl"
printf '{"type":"user","message":{"role":"user","content":"{\\"output\\":\\"continuum/autonomous/t1/reports/missing.json\\"}"}}\n' > "$T3"
run_hook "$(payload "$T3" wrc-a5)"
echo "$OUT" | grep -q '"decision":"block"' && echo "$OUT" | grep -q 'No report'; check "missing report file blocks" $?

# 6. python missing -> fail-open
run_hook "$(payload "$T" wrc-a6)" WORKER_REPORT_PYTHON=definitely-not-a-python-xyz
[ "$OUT" = "{}" ] && echo "$ERR" | grep -q 'validator did not run'; check "python missing fails open" $?

# 7. output field wins over a later Write to a sibling worker's report
printf '%s' "$VALID" > "$REPORT"; printf '%s' "$INVALID" > "$SIB"
T4="$WORK/agent4.jsonl"
transcript "$T4" "$REL" "$SIB"
run_hook "$(payload "$T4" wrc-a7)"
[ "$OUT" = "{}" ]; check "prompt output path beats sibling Write" $?

# 8. loop guard: third block for the same agent_id allows
printf '%s' "$INVALID" > "$REPORT"
run_hook "$(payload "$T" wrc-a8)"; B1="$OUT"
run_hook "$(payload "$T" wrc-a8)"; B2="$OUT"
run_hook "$(payload "$T" wrc-a8)"; B3="$OUT"
echo "$B1" | grep -q '"decision":"block"' && echo "$B2" | grep -q '"decision":"block"' && [ "$B3" = "{}" ]; check "loop guard caps blocks at 2" $?

# 9. garbage stdin never throws
OUT=$(printf 'not json' | node "$HOOK" 2>/dev/null); RC=$?
[ "$OUT" = "{}" ] && [ $RC -eq 0 ]; check "unparseable input allows" $?

echo ""
echo "RESULT: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
