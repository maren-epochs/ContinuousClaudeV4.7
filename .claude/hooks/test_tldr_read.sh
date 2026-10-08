#!/usr/bin/env bash
# Tests for .claude/hooks/tldr-read.mjs
#   VAL-001: nav-map cache keyed on path+mtime+size (warm hit <400ms, byte-identical, mtime invalidates)
#   VAL-002: BYPASS_PATTERNS match Windows backslash paths
# Self-contained; run from anywhere: bash .claude/hooks/test_tldr_read.sh
set -u

# Isolate from user env: with TLDR_READ_SHIM_AUTOSTART=1 a live shim would serve
# reads and break the >1s mtime-invalidation proxy.
unset TLDR_READ_SHIM_AUTOSTART
# Private os.tmpdir() for every node process here, so the cache wipes and shim
# start/stop below never touch the live tldr-read-cache or tldr-shim.json that
# running sessions use. Windows node reads TEMP/TMP, POSIX node reads TMPDIR.
TEST_TMP="$(mktemp -d)"
TEST_TMP_NATIVE="$(cd "$TEST_TMP" && { pwd -W 2>/dev/null || pwd; })"
export TMPDIR="$TEST_TMP_NATIVE" TEMP="$TEST_TMP_NATIVE" TMP="$TEST_TMP_NATIVE"
cleanup() {
  node "$(cd "$(dirname "$0")" && pwd)/tldr-shim.mjs" stop > /dev/null 2>&1
  rm -rf "$TEST_TMP"
}
trap cleanup EXIT

HOOK="$(cd "$(dirname "$0")" && pwd)/tldr-read.mjs"
# Home dir derived at runtime (no username in the repo): forward-slash form, and
# the JSON-escaped backslash form for the real ~/.claude/hooks/status.mjs.
HOME_FWD="$(node -e "console.log(require('os').homedir().replace(/\\\\/g,'/'))")"
# Repo root in the form node expects (Git Bash `pwd -W` gives C:/...; POSIX pwd elsewhere).
REPO_FWD="$(cd "$(dirname "$0")/../.." && { pwd -W 2>/dev/null || pwd; })"
FIXTURE_DIR="$REPO_FWD/tests/fixtures/tldr"
FIXTURE="$FIXTURE_DIR/nav_fixture.py"  # in-repo, >1500B, not a bypass pattern
PLATFORM="$(node -p process.platform)"
BS_FIXTURE="$(node -e "console.log(JSON.stringify(require('path').win32.join(require('os').homedir(),'.claude','hooks','status.mjs')).slice(1,-1))")"  # real, >1500B, backslashes
CACHE_DIR="$(node -e "console.log(require('os').tmpdir().replace(/\\\\/g,'/'))")/tldr-read-cache"
TMPWIN="${CACHE_DIR%/tldr-read-cache}"

# The nav-map, shim and launch-dir cases need the tldr binary (same lookup as the
# hook: ~/.cargo/bin/tldr, else PATH; CI installs it) and read the in-repo fixture.
# Without tldr they are SKIPPED, not failed; the bypass and .ipynb cases are pure
# JS and always run.
TLDR_OK=0; TLDR_SKIP=""
if [ -x "$HOME_FWD/.cargo/bin/tldr" ] || [ -x "$HOME_FWD/.cargo/bin/tldr.exe" ] \
   || command -v tldr >/dev/null 2>&1; then
  if [ -f "$FIXTURE" ]; then TLDR_OK=1; else TLDR_SKIP="fixture missing: $FIXTURE"; fi
else
  TLDR_SKIP="tldr not installed"
fi

# no_tldr <cmd...>: run with tldr unreachable (empty home, so no ~/.cargo/bin/tldr,
# and PATH = node's dir only). The hook then yields {} unless the output comes
# from its cache or a live shim, which makes cache misses and shim use observable
# without timing thresholds (a Linux tldr spawn can be well under 1s).
NODE_BIN="$(command -v node)"
NO_TLDR_HOME="$(mktemp -d)"
no_tldr() {
  env HOME="$NO_TLDR_HOME" USERPROFILE="$(cd "$NO_TLDR_HOME" && { pwd -W 2>/dev/null || pwd; })" \
    PATH="$(dirname "$NODE_BIN")" "$@"
}
# Guard: tldr must really be unreachable there (it could share node's dir).
if no_tldr "$NODE_BIN" -e "process.exit(require('child_process').spawnSync('tldr',['--version']).error ? 0 : 1)"; then
  NO_TLDR_OK=1
else
  NO_TLDR_OK=0
fi
# hook_no_tldr <payload> [extra env...] — sets OUT
hook_no_tldr() { local p="$1"; shift; OUT=$(printf '%s' "$p" | no_tldr "$@" "$NODE_BIN" "$HOOK"); }
SKIPPED=0
PASS=0
FAIL=0
# CCV_REQUIRE_TLDR=1 (set in CI, which installs tldr): a group that would be
# skipped FAILS instead, so CI cannot silently lose tldr coverage.
skip_group() {
  if [ "${CCV_REQUIRE_TLDR:-}" = 1 ]; then
    echo "FAIL: $1 not run ($TLDR_SKIP) but CCV_REQUIRE_TLDR=1"; FAIL=$((FAIL+1))
  else
    echo "SKIP: $1 ($TLDR_SKIP)"; SKIPPED=$((SKIPPED+1))
  fi
}
skip_check() { # <name>: a check that cannot run here (same CCV_REQUIRE_TLDR rule)
  if [ "${CCV_REQUIRE_TLDR:-}" = 1 ]; then echo "FAIL: $1 not run"; FAIL=$((FAIL+1));
  else echo "SKIP: $1"; fi
}

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
run_hook "$(payload "$HOME_FWD/.claude/hooks/status.mjs")"
[ "$OUT" = "{}" ]; check "VAL-002c forward-slash .claude/hooks path returns {}" $?

# --- VAL-002d: test-file pattern still bypasses (backslash path) ---
run_hook "$(payload 'C:\\Users\\x\\proj\\test_foo.py')"
[ "$OUT" = "{}" ]; check "VAL-002d test_*.py bypass unaffected" $?

if [ "$TLDR_OK" -eq 1 ]; then
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

# --- VAL-001c: touching the file invalidates the cache ---
# With tldr unreachable, a cache hit still returns the Nav Map and a miss returns {}.
TMP_PY="$TMPWIN/tldr_cache_probe_$$.py"  # name must not hit test-file bypass patterns
cp "$FIXTURE" "$TMP_PY"
run_hook "$(payload "$TMP_PY")"   # cold: populates cache
PROBE_OUT="$OUT"
if [ "$NO_TLDR_OK" -eq 1 ]; then
  hook_no_tldr "$(payload "$TMP_PY")"
  [ "$OUT" = "$PROBE_OUT" ] && [ "$OUT" != "{}" ]
  check "VAL-001c unchanged file is served from cache without tldr" $?
  touch -d '+5 seconds' "$TMP_PY" 2>/dev/null || { sleep 1; touch "$TMP_PY"; }  # new mtime -> cache must miss
  hook_no_tldr "$(payload "$TMP_PY")"
  [ "$OUT" = "{}" ]; check "VAL-001c mtime change invalidates cache (miss without tldr -> {}, got: ${OUT:0:40})" $?
  run_hook "$(payload "$TMP_PY")"
  [ "$OUT" = "$PROBE_OUT" ]; check "VAL-001c re-extract after mtime change matches original output" $?
else
  skip_check "VAL-001c (tldr reachable from node's dir; cannot hide it)"
fi
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

# --- VAL-201e: TLDR_READ_SHIM=0 ignores a live shim (spawnSync path) ---
# tldr hidden from the hook: the live shim still serves the Nav Map (proves the
# shim is the source), and with TLDR_READ_SHIM=0 the hook must not use it -> {}.
node "$SHIM" start > /dev/null 2>&1
if [ "$NO_TLDR_OK" -eq 1 ]; then
  rm -rf "$CACHE_DIR"
  hook_no_tldr "$(payload "$FIXTURE")"
  [ "$OUT" = "$COLD_OUT" ]; check "VAL-201e live shim serves the read with tldr hidden from the hook" $?
  rm -rf "$CACHE_DIR"
  hook_no_tldr "$(payload "$FIXTURE")" TLDR_READ_SHIM=0
  [ "$OUT" = "{}" ]; check "VAL-201e TLDR_READ_SHIM=0 bypasses live shim (got: ${OUT:0:40})" $?
else
  skip_check "VAL-201e (tldr reachable from node's dir; cannot hide it)"
fi
rm -rf "$CACHE_DIR"
OUT=$(printf '%s' "$(payload "$FIXTURE")" | TLDR_READ_SHIM=0 node "$HOOK")
[ "$OUT" = "$COLD_OUT" ]; check "VAL-201e TLDR_READ_SHIM=0 spawnSync output identical to cold" $?
node "$SHIM" stop > /dev/null 2>&1
else
  skip_group "VAL-001 nav-map cache + VAL-201 shim"
fi

# --- VAL-404: .ipynb reads get a cell nav map; base64 outputs never injected ---
# Fixture: real nbformat-4 JSON (indent=1, multi-line) with a ~100KB fake
# base64 image/png in cell 0's output. Pure-JS path — no tldr involved.
NB_FIXTURE="$TMPWIN/tldr_nb_probe_$$.ipynb"
NB_SMALL="$TMPWIN/tldr_nb_small_$$.ipynb"
node -e "
const b64 = 'iVBORw0KGgo' + 'A'.repeat(100000) + '==';
const nb = { cells: [
  { cell_type:'code', execution_count:1, metadata:{},
    source:['import matplotlib.pyplot as plt\n','plt.plot([1,2,3])\n'],
    outputs:[{ output_type:'display_data', metadata:{}, data:{ 'image/png': b64 } }] },
  { cell_type:'markdown', metadata:{}, source:['# Analysis\n','Notes here\n'] },
  { cell_type:'code', execution_count:2, metadata:{},
    source:['print(42)\n'], outputs:[{ output_type:'stream', name:'stdout', text:['42\n'] }] }
], metadata:{ kernelspec:{ name:'python3', display_name:'Python 3' } }, nbformat:4, nbformat_minor:5 };
require('fs').writeFileSync(process.argv[1], JSON.stringify(nb, null, 1));
const small = { cells:[{ cell_type:'code', execution_count:null, metadata:{}, source:['print(1)\n'], outputs:[] }],
  metadata:{}, nbformat:4, nbformat_minor:5 };
require('fs').writeFileSync(process.argv[2], JSON.stringify(small, null, 1));
" "$NB_FIXTURE" "$NB_SMALL"

run_hook "$(payload "$NB_FIXTURE")"
NB_OUT="$OUT"
case "$NB_OUT" in *"Notebook Map"*) r=0;; *) r=1;; esac
check "VAL-404a large .ipynb emits Notebook Map (got: ${NB_OUT:0:60})" $r
case "$NB_OUT" in *"3 cells"*) r=0;; *) r=1;; esac
check "VAL-404b map carries total cell count" $r
case "$NB_OUT" in *"output: image"*) r=0;; *) r=1;; esac
check "VAL-404c map marks image output kind" $r
if printf '%s' "$NB_OUT" | grep -qE '[A-Za-z0-9+/=]{101}'; then r=1; else r=0; fi
check "VAL-404d no base64 run >100 chars in hook output" $r

# VAL-404e: updatedInput.limit truncates the raw read BEFORE the first
# base64-carrying line — the injected read window stays base64-free.
printf '%s' "$NB_OUT" | node -e "
let buf=''; process.stdin.on('data', d => buf += d).on('end', () => {
  let out; try { out = JSON.parse(buf); } catch { process.exit(1); }
  const lim = out && out.hookSpecificOutput && out.hookSpecificOutput.updatedInput
    && out.hookSpecificOutput.updatedInput.limit;
  if (!Number.isInteger(lim) || lim < 1) process.exit(1);
  const lines = require('fs').readFileSync(process.argv[1], 'utf-8').split('\n').slice(0, lim);
  process.exit(lines.some(l => /[A-Za-z0-9+\/=]{101}/.test(l)) ? 1 : 0);
});" "$NB_FIXTURE"
check "VAL-404e limit set; truncated read window contains no base64" $?

# VAL-404f: small notebook below SIZE_THRESHOLD passes through untouched
run_hook "$(payload "$NB_SMALL")"
[ "$OUT" = "{}" ]; check "VAL-404f small .ipynb passes through as {}" $?

rm -f "$NB_FIXTURE" "$NB_SMALL"

# --- VAL-501: auto-approval scoped to the launch dir (decision D1) ---
# allow inside CLAUDE_PROJECT_DIR (case/separator-insensitive on Windows), ask outside it,
# including a sibling dir whose name is a prefix of the file's dir.
if [ "$TLDR_OK" -eq 1 ]; then
if [ "$PLATFORM" = win32 ]; then NOWHERE="C:/nowhere"; else NOWHERE="/nowhere"; fi
DP="{\"tool_name\":\"Read\",\"cwd\":\"$NOWHERE\",\"tool_input\":{\"file_path\":\"$FIXTURE\"}}"
decision() { printf '%s' "$DP" | env -u CLAUDE_PROJECT_DIR "$@" node "$HOOK" 2>/dev/null | grep -o '"permissionDecision":"[a-z]*"'; }
[ "$(decision CLAUDE_PROJECT_DIR="$REPO_FWD")" = '"permissionDecision":"allow"' ]
check "VAL-501a inside launch dir -> allow" $?
if [ "$PLATFORM" = win32 ]; then
  # Windows paths are case-insensitive: lowercased, backslashed, trailing separator.
  REPO_LC_BS="$(node -e "console.log(process.argv[1].replace(/\//g,'\\\\').toLowerCase())" "$REPO_FWD")"
  [ "$(decision CLAUDE_PROJECT_DIR="$REPO_LC_BS\\")" = '"permissionDecision":"allow"' ]
  check "VAL-501b case/backslash variant of launch dir -> allow" $?
else
  # POSIX paths are case-sensitive: a trailing slash still matches, a case variant does not.
  [ "$(decision CLAUDE_PROJECT_DIR="$REPO_FWD/")" = '"permissionDecision":"allow"' ]
  check "VAL-501b trailing-slash variant of launch dir -> allow" $?
  REPO_UC="$(node -e "console.log(process.argv[1].toUpperCase())" "$REPO_FWD")"
  [ "$(decision CLAUDE_PROJECT_DIR="$REPO_UC")" = '"permissionDecision":"ask"' ]
  check "VAL-501b case variant of launch dir on POSIX -> ask" $?
fi
[ "$(decision CLAUDE_PROJECT_DIR="$REPO_FWD/install")" = '"permissionDecision":"ask"' ]
check "VAL-501c outside launch dir -> ask" $?
[ "$(decision CLAUDE_PROJECT_DIR="${FIXTURE_DIR%?}")" = '"permissionDecision":"ask"' ]
check "VAL-501d prefix-named sibling dir -> ask" $?
[ "$(decision)" = '"permissionDecision":"ask"' ]
check "VAL-501e no CLAUDE_PROJECT_DIR, cwd elsewhere -> ask" $?
else
  skip_group "VAL-501 launch-dir approval"
fi
rm -rf "$NO_TLDR_HOME"

echo
[ "$SKIPPED" -gt 0 ] && echo "SKIPPED: $SKIPPED group(s): $TLDR_SKIP"
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
