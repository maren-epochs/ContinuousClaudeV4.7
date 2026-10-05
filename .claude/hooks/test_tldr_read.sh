#!/usr/bin/env bash
# Tests for .claude/hooks/tldr-read.mjs
#   VAL-001: nav-map cache keyed on path+mtime+size (warm hit <400ms, byte-identical, mtime invalidates)
#   VAL-002: BYPASS_PATTERNS match Windows backslash paths
# Self-contained; run from anywhere: bash .claude/hooks/test_tldr_read.sh
set -u

HOOK="$(cd "$(dirname "$0")" && pwd)/tldr-read.mjs"
FIXTURE="~/.claude/tools/ouros_harness.py"
BS_FIXTURE='~\\.claude\\hooks\\status.mjs'  # real, >1500B, backslashes
CACHE_DIR="$(node -e "console.log(require('os').tmpdir().replace(/\\\\/g,'/'))")/tldr-read-cache"

PASS=0
FAIL=0

# run_hook <payload> [threshold_ms] — sets OUT (stdout) and MS (wall ms).
# With a threshold: best-of-5, MS = minimum. Windows node process start spikes
# to ~500-1000ms under load (Defender scans fresh spawns); a genuine tldr spawn
# is >2000ms on EVERY attempt, so taking the minimum cannot mask a missing
# bypass or cache. Output is deterministic across attempts.
run_hook() {
  local start end attempt best=
  for attempt in 1 2 3 4 5; do
    start=$(date +%s%N)
    OUT=$(printf '%s' "$1" | node "$HOOK")
    end=$(date +%s%N)
    MS=$(( (end - start) / 1000000 ))
    [ -z "${2:-}" ] && return                        # no threshold: single shot
    [ -z "$best" ] || [ "$MS" -lt "$best" ] && best=$MS
    if [ "$best" -lt "$2" ]; then MS=$best; return; fi  # under threshold: done
    sleep 0.2                                        # let AV/scheduler settle
  done
  MS=$best
}

check() { # <name> <condition result: 0 ok>
  if [ "$2" -eq 0 ]; then PASS=$((PASS+1)); echo "PASS: $1";
  else FAIL=$((FAIL+1)); echo "FAIL: $1"; fi
}

payload() { # <file_path already JSON-escaped>
  printf '{"tool_name":"Read","tool_input":{"file_path":"%s"}}' "$1"
}

# Warm node/OS caches so timing assertions measure the hook, not first-touch I/O.
printf '{}' | node "$HOOK" > /dev/null

# --- VAL-002a: backslash path under .claude\hooks\ must bypass -> {} fast ---
run_hook "$(payload "$BS_FIXTURE")" 500
[ "$OUT" = "{}" ]; check "VAL-002a backslash .claude\\hooks path returns {} (got: ${OUT:0:60})" $?
[ "$MS" -lt 500 ]; check "VAL-002a backslash bypass is fast (<500ms, got ${MS}ms)" $?

# --- VAL-002b: nonexistent backslash path bypasses before statSync, fast {} ---
run_hook "$(payload 'C:\\Users\\x\\.claude\\hooks\\fake.mjs')" 500
[ "$OUT" = "{}" ]; check "VAL-002b nonexistent backslash hook path returns {}" $?
[ "$MS" -lt 500 ]; check "VAL-002b fast (<500ms, got ${MS}ms)" $?

# --- VAL-002c: forward-slash equivalent still bypasses ---
run_hook "$(payload '~/.claude/hooks/status.mjs')"
[ "$OUT" = "{}" ]; check "VAL-002c forward-slash .claude/hooks path returns {}" $?

# --- VAL-002d: test-file pattern still bypasses (backslash path) ---
run_hook "$(payload 'C:\\Users\\x\\proj\\test_foo.py')"
[ "$OUT" = "{}" ]; check "VAL-002d test_*.py bypass unaffected" $?

# --- VAL-001a: cold run on large .py emits Nav Map ---
rm -rf "$CACHE_DIR"
run_hook "$(payload "$FIXTURE")"
COLD_OUT="$OUT"; COLD_MS="$MS"
case "$COLD_OUT" in *"Nav Map"*) r=0;; *) r=1;; esac
check "VAL-001a cold run emits Nav Map (${COLD_MS}ms)" $r

# --- VAL-001b: warm run byte-identical and <400ms ---
run_hook "$(payload "$FIXTURE")" 400
WARM_MS="$MS"
[ "$OUT" = "$COLD_OUT" ]; check "VAL-001b warm output byte-identical to cold" $?
[ "$WARM_MS" -lt 400 ]; check "VAL-001b warm run <400ms (got ${WARM_MS}ms; cold was ${COLD_MS}ms)" $?

# --- VAL-001c: touching the file invalidates the cache (re-spawns tldr, >1s) ---
TMPWIN="$(node -e "console.log(require('os').tmpdir().replace(/\\\\/g,'/'))")"
TMP_PY="$TMPWIN/tldr_cache_probe_$$.py"  # name must not hit test-file bypass patterns
cp "$FIXTURE" "$TMP_PY"
run_hook "$(payload "$TMP_PY")"   # cold: populates cache
touch "$TMP_PY"                   # new mtime -> cache must miss
run_hook "$(payload "$TMP_PY")"
[ "$MS" -gt 1000 ]; check "VAL-001c mtime change invalidates cache (re-run took ${MS}ms, want >1000ms)" $?
rm -f "$TMP_PY"

# --- VAL-201: persistent tldr-mcp shim (tldr-shim.mjs) — additive assertions ---
# Shim is opt-in (explicit start / TLDR_READ_SHIM_AUTOSTART=1), so the
# assertions above always run against the spawnSync path. These verify:
# shim-on cold read is fast and byte-identical; fallback survives a stale
# port file; stopping the shim restores today's behavior.
SHIM="$(cd "$(dirname "$0")" && pwd)/tldr-shim.mjs"
SHIM_PORT_FILE="$TMPWIN/tldr-shim.json"

node "$SHIM" stop > /dev/null 2>&1   # clean slate
node "$SHIM" start > /dev/null 2>&1
[ -f "$SHIM_PORT_FILE" ]; check "VAL-201a shim start writes port file" $?

# --- VAL-201b/c: cold read via shim: byte-identical to spawnSync cold, <500ms ---
rm -rf "$CACHE_DIR"
run_hook "$(payload "$FIXTURE")" 500
[ "$OUT" = "$COLD_OUT" ]; check "VAL-201b shim cold output byte-identical to spawnSync cold" $?
[ "$MS" -lt 500 ]; check "VAL-201c shim cold read <500ms (got ${MS}ms)" $?

# --- VAL-201d: stale port file (no listener) falls back to spawnSync, still works ---
node "$SHIM" stop > /dev/null 2>&1
printf '{"port":1,"pid":0}' > "$SHIM_PORT_FILE"
rm -rf "$CACHE_DIR"
run_hook "$(payload "$FIXTURE")"
[ "$OUT" = "$COLD_OUT" ]; check "VAL-201d stale port file falls back to spawnSync, identical output (${MS}ms)" $?
rm -f "$SHIM_PORT_FILE"

# --- VAL-201e: TLDR_READ_SHIM=0 ignores a live shim (spawnSync path, >1s) ---
node "$SHIM" start > /dev/null 2>&1
rm -rf "$CACHE_DIR"
start=$(date +%s%N)
OUT=$(printf '%s' "$(payload "$FIXTURE")" | TLDR_READ_SHIM=0 node "$HOOK")
end=$(date +%s%N); MS=$(( (end - start) / 1000000 ))
[ "$OUT" = "$COLD_OUT" ] && [ "$MS" -gt 1000 ]
check "VAL-201e TLDR_READ_SHIM=0 bypasses live shim (${MS}ms, want >1000ms)" $?
node "$SHIM" stop > /dev/null 2>&1

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
