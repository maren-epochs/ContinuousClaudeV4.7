#!/usr/bin/env bash
# Tests for post-edit-diagnostics.mjs (VAL-003: direct ruff fast path for Python).
# Self-contained: creates temp .py fixtures, invokes the hook as Claude Code
# would (JSON payload on stdin), asserts output shape and wall time.
set -u

HOOK="$(cd "$(dirname "$0")" && pwd)/post-edit-diagnostics.mjs"
FAIL=0
PASS=0

note() { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); note "PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); note "FAIL: $1"; }

now_ms() { date +%s%3N; }

# --- fixtures -------------------------------------------------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ped-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

printf 'import os\n' > "$WORK/dirty.py"
printf 'x = 1\nprint(x)\n' > "$WORK/clean.py"
printf 'export const x = 1;\nconsole.log(x);\n' > "$WORK/sample.mjs"

# Windows-usable (mixed) paths for embedding in JSON payloads
if command -v cygpath >/dev/null 2>&1; then
  DIRTY="$(cygpath -m "$WORK/dirty.py")"
  CLEAN="$(cygpath -m "$WORK/clean.py")"
  MJS="$(cygpath -m "$WORK/sample.mjs")"
else
  DIRTY="$WORK/dirty.py"; CLEAN="$WORK/clean.py"; MJS="$WORK/sample.mjs"
fi

payload() { printf '{"tool_name":"Edit","tool_input":{"file_path":"%s"}}' "$1"; }

run_hook() { # $1=file_path -> sets OUT, ELAPSED_MS, RC
  local t0 t1
  t0=$(now_ms)
  OUT=$(payload "$1" | node "$HOOK" 2>/dev/null)
  RC=$?
  t1=$(now_ms)
  ELAPSED_MS=$((t1 - t0))
}

# Wall-time budget uses the median of N runs after one discarded warm-up.
# Single samples are dominated by host noise on Windows: the hook is ~150-250ms
# steady state (always the ruff path), but bare `node -e 0` startup alone swings
# 70ms->140ms under concurrent load and isolated runs spiked to 0.5-2s, hitting
# dirty and clean fixtures together — environment, not a code path.
TIMING_RUNS=5
median_ms() { # $1=file_path -> sets MEDIAN_MS
  local i samples=()
  run_hook "$1"   # warm-up: page cache, AV scan of fresh fixture, ruff mmap
  for ((i=0; i<TIMING_RUNS; i++)); do run_hook "$1"; samples+=("$ELAPSED_MS"); done
  MEDIAN_MS=$(printf '%s\n' "${samples[@]}" | sort -n | sed -n "$(( (TIMING_RUNS+1)/2 ))p")
  TIMING_SAMPLES="${samples[*]}"
}

json_valid() { printf '%s' "$1" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{try{JSON.parse(s);process.exit(0)}catch{process.exit(1)}})'; }

# --- (a) dirty .py: Diagnostics + F401, <400ms ---------------------------
run_hook "$DIRTY"
if [ $RC -ne 0 ]; then bad "(a) hook exited $RC on dirty.py"; fi
case "$OUT" in
  *"Diagnostics:"*) ok "(a) dirty.py output contains Diagnostics:";;
  *) bad "(a) dirty.py output missing Diagnostics: — got: $OUT";;
esac
case "$OUT" in
  *F401*) ok "(a) dirty.py output contains F401";;
  *) bad "(a) dirty.py output missing F401 — got: $OUT";;
esac
median_ms "$DIRTY"
if [ "$MEDIAN_MS" -lt 400 ]; then
  ok "(a) dirty.py median wall time ${MEDIAN_MS}ms < 400ms (runs: $TIMING_SAMPLES)"
else
  bad "(a) dirty.py median wall time ${MEDIAN_MS}ms >= 400ms (runs: $TIMING_SAMPLES)"
fi

# --- (b) clean .py: {} output, <400ms -------------------------------------
run_hook "$CLEAN"
if [ "$OUT" = "{}" ]; then
  ok "(b) clean.py output is {}"
else
  bad "(b) clean.py output is not {} — got: $OUT"
fi
median_ms "$CLEAN"
if [ "$MEDIAN_MS" -lt 400 ]; then
  ok "(b) clean.py median wall time ${MEDIAN_MS}ms < 400ms (runs: $TIMING_SAMPLES)"
else
  bad "(b) clean.py median wall time ${MEDIAN_MS}ms >= 400ms (runs: $TIMING_SAMPLES)"
fi

# --- (c) .mjs file: tldr path, valid JSON, no time bound -------------------
run_hook "$MJS"
if [ $RC -eq 0 ] && json_valid "$OUT"; then
  ok "(c) .mjs output is valid JSON (tldr path, ${ELAPSED_MS}ms)"
else
  bad "(c) .mjs output invalid JSON or nonzero exit ($RC) — got: $OUT"
fi

# --- (d) ruff-absent fallback ---------------------------------------------
# Structural: the hook must resolve ruff via a candidates loop and fall back
# to the tldr path when resolution/spawn/parse fails.
if grep -q "RUFF_CANDIDATES" "$HOOK" && grep -q "tldrDiagnostics" "$HOOK"; then
  ok "(d1) structural: ruff candidates loop + tldr fallback branch present"
else
  bad "(d1) structural: missing RUFF_CANDIDATES or tldrDiagnostics in hook"
fi
# Executed: fake HOME so the hardcoded Scripts candidate misses, strip every
# PATH dir that contains a ruff executable so bare 'ruff' spawn fails too.
# The hook must not throw and must still print valid JSON (tldr fallback).
STRIPPED=""
IFS=':' read -ra DIRS <<< "$PATH"
for d in "${DIRS[@]}"; do
  [ -x "$d/ruff" ] || [ -x "$d/ruff.exe" ] && continue
  STRIPPED="${STRIPPED:+$STRIPPED:}$d"
done
t0=$(now_ms)
OUT=$(payload "$DIRTY" | env PATH="$STRIPPED" USERPROFILE="$(cygpath -w "$WORK" 2>/dev/null || echo "$WORK")" HOME="$WORK" node "$HOOK" 2>/dev/null)
RC=$?
t1=$(now_ms)
if [ $RC -eq 0 ] && json_valid "$OUT"; then
  ok "(d2) ruff-absent: hook did not throw, printed valid JSON ($((t1-t0))ms)"
else
  bad "(d2) ruff-absent: exit $RC or invalid JSON — got: $OUT"
fi

# --- summary ---------------------------------------------------------------
note "----"
note "PASS=$PASS FAIL=$FAIL"
[ $FAIL -eq 0 ] || exit 1
exit 0
