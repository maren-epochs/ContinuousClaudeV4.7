#!/usr/bin/env bash
# Tests for .claude/hooks/status.mjs
#   VAL-701: (a) used_percentage preferred over computed value
#            (b) current_usage / context_window_size fallback, no added overhead
#            (c) null current_usage with context_window writes pct 0 over a prior high value
#            (d) no context_window -> no pct file written
#            (e) 1M context_window_size math
#            (f) handoff goal/now from <project>/thoughts/shared/handoffs, else
#                ~/.claude/handoffs/<basename>/ (HOME + USERPROFILE overridden)
#            (g) garbage stdin never throws
# Self-contained; run from anywhere: bash .claude/hooks/test_status.sh
# STATUS_HOOK=<path> overrides the hook under test (used for mutation checks).
set -u

HOOK="${STATUS_HOOK:-$(cd "$(dirname "$0")" && pwd)/status.mjs}"
TMPWIN="$(node -e "console.log(require('os').tmpdir().split(String.fromCharCode(92)).join('/'))")"
ROOT="$TMPWIN/sttst_status_$$"
ERRFILE="$ROOT/err.txt"
FAKEHOME="$ROOT/home"

# Every session id is 'sttst' + 3 chars so pct files never collide with real ones.
SIDS="sttstA1x sttstB2x sttstC3x sttstD4x sttstD5x sttstE5x sttstF6x sttstGzz sttstNaN"
pctfile() { printf '%s/claude-context-pct-%s.txt' "$TMPWIN" "$1"; }
cleanup() {
  for s in $SIDS; do rm -f "$(pctfile "$s")"; done
  rm -rf "$ROOT"
}
trap cleanup EXIT
cleanup
mkdir -p "$FAKEHOME/.claude"

# Isolate homedir(): node uses USERPROFILE on Windows, HOME elsewhere.
export HOME="$FAKEHOME"
export USERPROFILE="$FAKEHOME"
unset CLAUDE_SESSION_ID

PASS=0
FAIL=0
check() { # <name> <condition result: 0 ok>
  if [ "$2" -eq 0 ]; then PASS=$((PASS+1)); echo "PASS: $1";
  else FAIL=$((FAIL+1)); echo "FAIL: $1"; fi
}

mkproj() { # <dir> — git repo so findProjectRoot stops here
  mkdir -p "$1" && git init -q "$1" >/dev/null 2>&1
}

# run_hook <stdin payload> — sets OUT (stdout), ERR (stderr), RC (exit code)
run_hook() {
  OUT=$(printf '%s' "$1" | CLAUDE_PROJECT_DIR="$PROJ0" node "$HOOK" 2>"$ERRFILE"); RC=$?
  ERR=$(cat "$ERRFILE" 2>/dev/null || true)
}
readpct() { cat "$(pctfile "$1")" 2>/dev/null || printf 'MISSING'; }
payload() { # <sid8> <context_window json or empty> [current_dir]
  local cw="" dir="${3:-$PROJ0}"
  [ -n "$2" ] && cw=",\"context_window\":$2"
  printf '{"session_id":"%s-0000-4000-8000-tail","workspace":{"current_dir":"%s"}%s}' "$1" "$dir" "$cw"
}

PROJ0="$ROOT/proj0"
mkproj "$PROJ0"

# --- (a) used_percentage preferred: native 42.7 vs computed 90 ---
run_hook "$(payload sttstA1x '{"used_percentage":42.7,"context_window_size":200000,"current_usage":{"input_tokens":10000,"cache_read_input_tokens":170000,"cache_creation_input_tokens":0}}')"
v=$(readpct sttstA1x); [ "$v" = "42" ]; check "a1 used_percentage 42.7 -> pct file 42, not computed 90 (got: $v)" $?
case "$OUT" in *' 42%'*) r=0;; *) r=1;; esac
check "a2 statusline shows 42% (got: ${OUT:0:60})" $r
[ "$RC" -eq 0 ]; check "a3 exit 0" $?

# --- (b) current_usage / context_window_size fallback, NO overhead added ---
# 5000 + 40000 + 5000 = 50000 of 200000 -> 25%. Old +45K overhead would give 47%.
run_hook "$(payload sttstB2x '{"context_window_size":200000,"current_usage":{"input_tokens":5000,"cache_read_input_tokens":40000,"cache_creation_input_tokens":5000}}')"
v=$(readpct sttstB2x); [ "$v" = "25" ]; check "b1 fallback 50K/200K -> 25, no +45K overhead (got: $v)" $?
case "$OUT" in *'50.0K 25%'*) r=0;; *) r=1;; esac
check "b2 statusline shows 50.0K 25% (got: ${OUT:0:60})" $r
# missing context_window_size defaults to 200000
run_hook "$(payload sttstB2x '{"current_usage":{"input_tokens":100000}}')"
v=$(readpct sttstB2x); [ "$v" = "50" ]; check "b3 missing context_window_size defaults to 200K -> 50 (got: $v)" $?

# --- (c) null current_usage (pre-first-call / post-/compact) writes 0 over prior 90 ---
printf '90' > "$(pctfile sttstC3x)"
rm -f "$FAKEHOME/.claude/autocompact.log"
run_hook "$(payload sttstC3x '{"used_percentage":null,"context_window_size":200000,"current_usage":null}')"
v=$(readpct sttstC3x); [ "$v" = "0" ]; check "c1 null current_usage overwrites prior 90 with 0 (got: $v)" $?
[ "$RC" -eq 0 ]; check "c2 exit 0" $?
grep -q 'sttstC3x | 90% -> 0%' "$FAKEHOME/.claude/autocompact.log" 2>/dev/null
check "c3 drop logged to overridden homedir autocompact.log" $?

# --- (d) no context_window -> no pct file written, existing file untouched ---
rm -f "$(pctfile sttstD4x)"
run_hook "$(payload sttstD4x '')"
[ ! -e "$(pctfile sttstD4x)" ]; check "d1 no context_window -> no pct file created" $?
[ "$RC" -eq 0 ]; check "d2 exit 0" $?
printf '77' > "$(pctfile sttstD5x)"
run_hook "$(payload sttstD5x '')"
v=$(readpct sttstD5x); [ "$v" = "77" ]; check "d3 no context_window -> existing pct 77 left untouched (got: $v)" $?

# --- (e) 1M window math: 250000 / 1000000 -> 25 (200K assumption would cap at 100) ---
run_hook "$(payload sttstE5x '{"context_window_size":1000000,"current_usage":{"input_tokens":50000,"cache_read_input_tokens":150000,"cache_creation_input_tokens":50000}}')"
v=$(readpct sttstE5x); [ "$v" = "25" ]; check "e1 250K of 1M window -> 25 (got: $v)" $?
case "$OUT" in *'250.0K 25%'*) r=0;; *) r=1;; esac
check "e2 statusline shows 250.0K 25% (got: ${OUT:0:60})" $r
run_hook "$(payload sttstE5x '{"context_window_size":1000000,"current_usage":{"input_tokens":850000}}')"
v=$(readpct sttstE5x); [ "$v" = "85" ]; check "e3 850K of 1M -> 85 (got: $v)" $?

# --- (f) handoff goal/now resolution ---
mkhandoff() { # <dir> <file> <goal> <now>
  mkdir -p "$1" && printf 'goal: %s\nnow: %s\n' "$3" "$4" > "$1/$2"
}
# f1: project-local thoughts/shared/handoffs wins over home
PROJL="$ROOT/sttstprojl"
mkproj "$PROJL"
mkhandoff "$PROJL/thoughts/shared/handoffs/s1" 2026-10-01_10-00_x.yaml "Local goal" "Local now"
mkhandoff "$FAKEHOME/.claude/handoffs/sttstprojl" 2026-10-02_10-00_x.yaml "Home goal" "Home now"
run_hook "$(payload sttstF6x '' "$PROJL")"
case "$OUT" in *'Local goal -> Local now'*) r=0;; *) r=1;; esac
check "f1 project-local handoff goal/now used (got: ${OUT:0:120})" $r
case "$OUT" in *'Home goal'*) r=1;; *) r=0;; esac
check "f2 home handoff ignored when project-local dir exists" $r
# f3: no project-local dir -> ~/.claude/handoffs/<basename>/
PROJH="$ROOT/sttstprojh"
mkproj "$PROJH"
mkhandoff "$FAKEHOME/.claude/handoffs/sttstprojh/s2" 2026-10-01_09-00_old.yaml "Old goal" "Old now"
mkhandoff "$FAKEHOME/.claude/handoffs/sttstprojh/s2" 2026-10-03_09-00_new.yaml "Home goal" "Home now"
run_hook "$(payload sttstF6x '' "$PROJH")"
case "$OUT" in *'Home goal -> Home now'*) r=0;; *) r=1;; esac
check "f3 home fallback ~/.claude/handoffs/<basename>/ via HOME/USERPROFILE, latest file (got: ${OUT:0:120})" $r
# f4: neither -> no continuity segment
PROJN="$ROOT/sttstprojn"
mkproj "$PROJN"
run_hook "$(payload sttstF6x '' "$PROJN")"
case "$OUT" in *' -> '*|*'goal'*) r=1;; *) r=0;; esac
check "f4 no handoffs anywhere -> no goal/now segment (got: ${OUT:0:120})" $r
[ "$RC" -eq 0 ]; check "f5 exit 0" $?
# f6: session cd'd into a subdir of a non-git project: handoffs still key on workspace.project_dir
PROJX="$ROOT/sttstprojx"; mkdir -p "$PROJX/sub"
mkhandoff "$FAKEHOME/.claude/handoffs/sttstprojx/s3" 2026-10-04_09-00_x.yaml "Launch goal" "Launch now"
run_hook "{\"session_id\":\"sttstF6x-f6\",\"workspace\":{\"current_dir\":\"$PROJX/sub\",\"project_dir\":\"$PROJX\"}}"
case "$OUT" in *'Launch goal -> Launch now'*) r=0;; *) r=1;; esac
check "f6 cd'd subdir still resolves handoffs via workspace.project_dir (got: ${OUT:0:120})" $r

# --- (g) garbage stdin never throws (exit 0, no stderr) ---
# CLAUDE_SESSION_ID pins the fallback key so any write lands on a test-owned file.
export CLAUDE_SESSION_ID=sttstGzz
gi=0
while IFS= read -r g; do
  gi=$((gi+1))
  run_hook "$g"
  [ "$RC" -eq 0 ] && [ -z "$ERR" ]
  check "g$gi garbage stdin never throws: [${g:0:60}] (rc=$RC err=${ERR:0:80})" $?
done <<'EOF'
not-json {{{

[]
null
"str"
42
{"context_window":"x"}
{"context_window":{"current_usage":"x","context_window_size":"y"}}
{"context_window":{"used_percentage":"50","current_usage":{"input_tokens":"lots"}}}
{"workspace":null,"context_window":null}
{"session_id":123,"context_window":{}}
{"workspace":{"current_dir":42}}
EOF
OUT=$(printf '\377\376\000\001garbage' | CLAUDE_PROJECT_DIR="$PROJ0" node "$HOOK" 2>"$ERRFILE"); RC=$?
ERR=$(cat "$ERRFILE" 2>/dev/null || true)
[ "$RC" -eq 0 ] && [ -z "$ERR" ]; check "g-bin binary stdin never throws (rc=$RC)" $?
unset CLAUDE_SESSION_ID

# g13: wrong-typed context_window_size must never put a non-integer (NaN) in the pct file.
# session_id sttstNaN0 -> key sttstNaN (first 8 chars). Absent file is acceptable; non-integer is not.
rm -f "$(pctfile sttstNaN)"
run_hook '{"session_id":"sttstNaN0","context_window":{"context_window_size":"x","current_usage":{"input_tokens":1000}}}'
v=$(readpct sttstNaN)
case "$v" in MISSING) r=0;; ''|*[!0-9]*) r=1;; *) r=0;; esac
[ "$RC" -eq 0 ] || r=1
check "g13 wrong-typed context_window_size -> pct file integer or absent (got: $v, rc=$RC)" $r

# g14: bad size/percentage fall back to 200K math: no "NaN" in the statusline, and a stale
# prior pct is overwritten (1000/200K -> 0), not left in place.
printf '90' > "$(pctfile sttstNaN)"
run_hook '{"session_id":"sttstNaN0","context_window":{"context_window_size":"x","used_percentage":"y","current_usage":{"input_tokens":1000}}}'
v=$(readpct sttstNaN)
case "$OUT" in *NaN*) r=1;; *) r=0;; esac
[ "$v" = "0" ] || r=1
check "g14 bad size/pct -> no NaN in statusline, stale 90 reset to 0 (got: pct=$v out=${OUT:0:40})" $r

# --- (h) VAL-809 fleet segment: reads ~/.claude/fleet/state.json only ---
FLEETDIR="$FAKEHOME/.claude/fleet"
STATE="$FLEETDIR/state.json"
iso_ago() { # <minutes> -> ISO UTC string that many minutes ago
  node -e "console.log(new Date(Date.now()-Number(process.argv[1])*60000).toISOString().replace(/\.\d+Z$/,'Z'))" "$1"
}
mkstate() { # <generated_at json value> <alive count> <dead count> <inbox> <alerts on alive> [alerts on dead]
  node -e '
    const [g, live, dead, inbox, al, dl] = process.argv.slice(1);
    const alerts = (n) => Array.from({ length: Number(n) }, (_, i) => ({ kind: "stuck", severity: "warn", session: "sess-" + i }));
    const sess = (alive, i, n) => ({ pid: 4000 + i, session_id: "sess-" + i, project: "project-A", alive, alerts: alerts(n) });
    const sessions = [];
    for (let i = 0; i < Number(live); i++) sessions.push(sess(true, i, i === 0 ? al : 0));
    for (let i = 0; i < Number(dead); i++) sessions.push(sess(false, 100 + i, i === 0 ? (dl || 0) : 0));
    require("fs").writeFileSync(process.argv[7], JSON.stringify({ schema_version: 1, generated_at: JSON.parse(g),
      harness: {}, sessions, inbox_count: Number(inbox), audit_recent: [], collisions: [], machine: {} }));
  ' "$1" "$2" "$3" "$4" "$5" "${6:-0}" "$STATE"
}
strip_fleet() { printf '%s' "$1" | sed -E 's/ \| fleet\??( [0-9]+ (live|inbox|alert)( \|)?)*$//'; }
PROJF="$ROOT/sttstprojf"
mkproj "$PROJF"
fpay() { payload sttstH7x '{"used_percentage":12,"context_window_size":200000}' "$PROJF"; }
SIDS="$SIDS sttstH7x"

rm -rf "$FLEETDIR"
run_hook "$(fpay)"; BASE="$OUT"
case "$OUT" in *fleet*) r=1;; *) r=0;; esac
[ "$RC" -eq 0 ] && [ -z "$ERR" ] || r=1
check "h1 no state.json -> no fleet segment, exit 0, no stderr (got: ${OUT:0:120})" $r

mkdir -p "$FLEETDIR"
mkstate "\"$(iso_ago 1)\"" 3 1 2 1 4
run_hook "$(fpay)"
case "$OUT" in *' | fleet 3 live | 2 inbox | 1 alert') r=0;; *) r=1;; esac
check "h2 fresh state -> ends with 'fleet 3 live | 2 inbox | 1 alert' (dead sessions and their alerts not counted) (got: ${OUT:0:160})" $r
[ "$(strip_fleet "$OUT")" = "$BASE" ]; check "h3 rest of the statusline unchanged by the segment" $?

mkstate "\"$(iso_ago 1)\"" 0 2 2 0 3
run_hook "$(fpay)"
case "$OUT" in *' | fleet 2 inbox') r=0;; *) r=1;; esac
check "h4 only non-zero parts: 'fleet 2 inbox' (got: ${OUT:0:160})" $r

mkstate "\"$(iso_ago 1)\"" 0 2 0 0 3
run_hook "$(fpay)"
[ "$OUT" = "$BASE" ]; check "h5 all-zero fresh state -> no fleet segment (got: ${OUT:0:160})" $?

mkstate "\"$(iso_ago 20)\"" 3 0 0 1
run_hook "$(fpay)"
case "$OUT" in *' | fleet? 3 live | 1 alert') r=0;; *) r=1;; esac
check "h6 generated_at 20 min old -> 'fleet? 3 live | 1 alert' (got: ${OUT:0:160})" $r

mkstate "\"$(iso_ago 14)\"" 1 0 0 0
run_hook "$(fpay)"
case "$OUT" in *' | fleet 1 live') r=0;; *) r=1;; esac
check "h7 generated_at 14 min old is fresh (got: ${OUT:0:160})" $r

mkstate "\"$(iso_ago 30)\"" 0 0 0 0
run_hook "$(fpay)"
[ "$OUT" = "$BASE" ]; check "h8 stale all-zero state -> no segment ('?' only marks shown counts) (got: ${OUT:0:160})" $?

for g in null '"not-a-date"' 42; do
  mkstate "$g" 1 0 0 0
  run_hook "$(fpay)"
  case "$OUT" in *' | fleet? 1 live') r=0;; *) r=1;; esac
  check "h9 generated_at $g -> unknown age is stale (got: ${OUT:0:160})" $r
done

hi=0
while IFS= read -r body; do
  hi=$((hi+1))
  printf '%s' "$body" > "$STATE"
  run_hook "$(fpay)"
  [ "$RC" -eq 0 ] && [ -z "$ERR" ] && [ "$OUT" = "$BASE" ]
  check "h10.$hi unreadable/garbage state.json -> no segment, statusline unchanged: [${body:0:50}] (rc=$RC err=${ERR:0:60} out=${OUT:0:100})" $?
done <<'EOF'
not json {{{

null
[]
"str"
{"sessions":"x","inbox_count":"2","generated_at":{}}
{"sessions":[null,1,"x",{"alive":"yes","alerts":"x"}],"inbox_count":-3}
{"sessions":[{"alive":1,"alerts":[1,2]}],"inbox_count":true}
EOF
printf '{"sessions":[{"alive":true,"alerts":"x"},{"alive":true,"alerts":[{},{"severity":"warn"}]}],"inbox_count":2.5,"generated_at":"%s"}' "$(iso_ago 1)" > "$STATE"
run_hook "$(fpay)"
case "$OUT" in *' | fleet 2 live | 1 alert') r=0;; *) r=1;; esac
[ "$RC" -eq 0 ] && [ -z "$ERR" ] || r=1
check "h11 wrong-typed fields tolerated, non-integer inbox ignored (got: ${OUT:0:160})" $r

# only warn/error alerts count; info (drift after a sync, question turns) and unknown don't
printf '{"sessions":[{"alive":true,"alerts":[{"severity":"info"},{"severity":"warn"},{"severity":"error"},{"severity":"INFO"},{"severity":"critical"},{"severity":null}]},{"alive":true,"alerts":[{"severity":"info"},{"severity":"info"}]}],"generated_at":"%s"}' "$(iso_ago 1)" > "$STATE"
run_hook "$(fpay)"
case "$OUT" in *' | fleet 2 live | 2 alert') r=0;; *) r=1;; esac
check "h14 alert count = warn + error only, info/other severities ignored (got: ${OUT:0:160})" $r
printf '{"sessions":[{"alive":true,"alerts":[{"severity":"info"}]}],"generated_at":"%s"}' "$(iso_ago 1)" > "$STATE"
run_hook "$(fpay)"
case "$OUT" in *' | fleet 1 live') r=0;; *) r=1;; esac
check "h15 info-only alerts -> no alert part (got: ${OUT:0:160})" $r

rm -f "$STATE"; mkdir -p "$STATE"
run_hook "$(fpay)"
[ "$RC" -eq 0 ] && [ -z "$ERR" ] && [ "$OUT" = "$BASE" ]; check "h12 state.json is a directory -> no segment, no throw" $?
rm -rf "$STATE"

# statusline never collects: nothing but state.json appears under ~/.claude/fleet
rm -rf "$FLEETDIR"; mkdir -p "$FLEETDIR"
mkstate "\"$(iso_ago 30)\"" 1 0 0 0
run_hook "$(fpay)"
[ "$(ls -A "$FLEETDIR")" = "state.json" ]; r=$?; check "h13 statusline writes/locks/spawns nothing under ~/.claude/fleet (got: $(ls -A "$FLEETDIR" | tr '\n' ' '))" $r

# --- (i) VAL-809 refresh path: auto-handoff-stop.mjs starts a detached, locked collect ---
STOPHOOK="${STOP_HOOK:-$(dirname "$HOOK")/auto-handoff-stop.mjs}"
FAKECOL="$ROOT/fake-fleet.mjs"
RUNLOG="$FLEETDIR/fake-runs.log"
cat > "$FAKECOL" <<'EOF'
import { appendFileSync } from 'fs';
import { join } from 'path';
import { homedir } from 'os';
appendFileSync(join(homedir(), '.claude', 'fleet', 'fake-runs.log'), JSON.stringify({
  argv: process.argv.slice(2), cwd: process.cwd(), nobytecode: process.env.PYTHONDONTWRITEBYTECODE || '',
}) + '\n');
const ms = Number(process.env.FAKE_COLLECT_MS || 0);
if (ms) setTimeout(() => {}, ms);
EOF
stop_run() { # <payload> — runs the Stop hook from inside the project dir
  local t0 t1
  t0=$(date +%s%N)
  OUT=$(cd "$PROJF" && printf '%s' "$1" | FLEET_PYTHON="${FLEET_PYTHON_T-node}" FLEET_PY="${FLEET_PY_T-$FAKECOL}" node "$STOPHOOK" 2>"$ERRFILE"); RC=$?
  t1=$(date +%s%N)
  ELAPSED_MS=$(( (t1 - t0) / 1000000 ))
  ERR=$(cat "$ERRFILE" 2>/dev/null || true)
}
runs() { if [ -f "$RUNLOG" ]; then grep -c . "$RUNLOG"; else echo 0; fi; }
wait_runs() { # <n> — up to 10 s for the fake collector to log n runs
  local i=0
  while [ "$(runs)" -lt "$1" ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done
}
spay() { printf '{"session_id":"sttstI8x-0000-4000-8000-tail","stop_hook_active":false}'; }
SIDS="$SIDS sttstI8x"
age_file() { # <file> <seconds ago>
  node -e "const t=(Date.now()-Number(process.argv[2])*1000)/1000; require('fs').utimesSync(process.argv[1], t, t)" "$1" "$2"
}

rm -rf "$FLEETDIR"
stop_run "$(spay)"
[ "$OUT" = "{}" ] && [ "$RC" -eq 0 ]; check "i1 stop hook output unchanged ({}), exit 0 (got: $OUT rc=$RC)" $?
wait_runs 1
[ "$(runs)" -eq 1 ]; r=$?; check "i2 one background collect started (runs: $(runs))" $r
grep -q '"argv":\["collect"\]' "$RUNLOG" 2>/dev/null; r=$?; check "i3 collector invoked as 'fleet.py collect' (log: $(head -c 200 "$RUNLOG" 2>/dev/null))" $r
CWD_SEEN=$(node -e "const l=require('fs').readFileSync(process.argv[1],'utf-8').trim().split('\n')[0];console.log(JSON.parse(l).cwd.split(String.fromCharCode(92)).join('/'))" "$RUNLOG" 2>/dev/null)
case "$CWD_SEEN" in */.claude/fleet) r=0;; *) r=1;; esac
check "i4 collect runs with cwd ~/.claude/fleet, not the project (got: $CWD_SEEN)" $r
grep -q '"nobytecode":"1"' "$RUNLOG" 2>/dev/null; check "i5 PYTHONDONTWRITEBYTECODE=1 (no __pycache__ beside a repo-layout fleet.py)" $?
[ -f "$FLEETDIR/collect.lock" ]; check "i6 lock file ~/.claude/fleet/collect.lock created" $?

stop_run "$(spay)"; sleep 1
[ "$(runs)" -eq 1 ]; r=$?; check "i7 second Stop within 2 min -> no second collect (runs: $(runs))" $r

age_file "$FLEETDIR/collect.lock" 180
stop_run "$(spay)"; wait_runs 2
[ "$(runs)" -eq 2 ]; r=$?; check "i8 lock older than 2 min -> collect runs again (runs: $(runs))" $r

rm -f "$FLEETDIR/collect.lock"; printf '{}' > "$STATE"
stop_run "$(spay)"; sleep 1
[ "$(runs)" -eq 2 ]; r=$?; check "i9 state.json written < 2 min ago (e.g. manual collect) -> skip (runs: $(runs))" $r
age_file "$STATE" 180
stop_run "$(spay)"; wait_runs 3
[ "$(runs)" -eq 3 ]; r=$?; check "i10 state.json older than 2 min, no lock -> collect (runs: $(runs))" $r

# future mtimes (clock skew, restored backup) are stale, never 'recent' until the clock catches up
rm -f "$FLEETDIR/collect.lock"; printf '{}' > "$STATE"; age_file "$STATE" -3600
stop_run "$(spay)"; wait_runs 4
[ "$(runs)" -eq 4 ]; r=$?; check "i19 state.json mtime in the future -> not recent, collect runs (runs: $(runs))" $r
age_file "$STATE" 180; age_file "$FLEETDIR/collect.lock" -3600
stop_run "$(spay)"; wait_runs 5
[ "$(runs)" -eq 5 ]; r=$?; check "i20 collect.lock mtime in the future -> stale, reclaimed, collect runs (runs: $(runs))" $r
age_file "$FLEETDIR/collect.lock" 119
stop_run "$(spay)"; sleep 1
[ "$(runs)" -eq 5 ]; r=$?; check "i21 lock 119 s old -> still held, no collect (runs: $(runs))" $r

rm -f "$FLEETDIR/collect.lock" "$STATE"
FAKE_COLLECT_MS=3000 stop_run "$(spay)"
[ "$ELAPSED_MS" -lt 2000 ] && [ "$OUT" = "{}" ]; check "i11 a 3 s collect never blocks the hook (hook took ${ELAPSED_MS} ms)" $?
wait_runs 6
sleep 3 # let it exit: on Windows its cwd pins ~/.claude/fleet against deletion

rm -f "$FLEETDIR/collect.lock"
printf '95' > "$(pctfile sttstI8x)"
stop_run "$(spay)"
case "$OUT" in *'"decision":"block"'*'95%'*) r=0;; *) r=1;; esac
check "i12 context block decision unaffected by the refresh (got: ${OUT:0:80})" $r
rm -f "$(pctfile sttstI8x)"
wait_runs 7

rm -f "$FLEETDIR/collect.lock"
FLEET_PYTHON_T="$ROOT/no-such-python.exe" stop_run "$(spay)"; sleep 0.5
[ "$OUT" = "{}" ] && [ "$RC" -eq 0 ]; r=$?
case "$ERR" in *rror*|*ENOENT*) r=1;; esac
check "i13 missing interpreter -> fail-open, no error output (rc=$RC err=${ERR:0:120})" $r

rm -f "$FLEETDIR/collect.lock"
FLEET_PY_T="$ROOT/no-such-fleet.py" stop_run "$(spay)"; sleep 0.5
[ ! -f "$FLEETDIR/collect.lock" ] && [ "$OUT" = "{}" ]; check "i14 missing fleet.py -> nothing started, no lock" $?

rm -rf "$FLEETDIR"; printf 'x' > "$FLEETDIR"
stop_run "$(spay)"
[ "$OUT" = "{}" ] && [ "$RC" -eq 0 ]; r=$?
case "$ERR" in *rror*|*EEXIST*|*ENOTDIR*) r=1;; esac
check "i15 ~/.claude/fleet unusable (a file) -> fail-open (rc=$RC err=${ERR:0:120})" $r
rm -f "$FLEETDIR"; mkdir -p "$FLEETDIR"

FLEET_COLLECT=0 stop_run "$(spay)"; sleep 0.5
[ ! -f "$FLEETDIR/collect.lock" ]; check "i16 FLEET_COLLECT=0 disables the refresh" $?

[ -z "$(cd "$PROJF" && git status --porcelain --ignored)" ]; r=$?; check "i17 nothing written inside the project folder (status: $(cd "$PROJF" && git status --porcelain --ignored | head -3))" $r

# i18: end to end with the real tools/fleet/fleet.py when a Python is available. A fixture
# sessions/<pid>.key must never reach state.json (collector reads only <digits>.json).
if [ "$(uname -s | cut -c1-5)" = "MINGW" ] || [ "$(uname -s | cut -c1-4)" = "MSYS" ]; then PYCHK="py -3.13"; else PYCHK="python3"; fi
if $PYCHK -c "import sys; sys.exit(sys.version_info < (3, 10))" >/dev/null 2>&1; then
  rm -rf "$FLEETDIR"
  mkdir -p "$FAKEHOME/.claude/sessions" "$FAKEHOME/.claude/harness-inbox"
  printf 'SECRETKEYBYTES-sttst' > "$FAKEHOME/.claude/sessions/999991.sttst.key"
  printf '{"pid":999991,"sessionId":"sess-e2e","cwd":"%s","status":"idle","kind":"interactive"}' "$PROJF" > "$FAKEHOME/.claude/sessions/999991.json"
  printf '{"schema_version":1,"id":"20261007T120000Z-0000abcd","kind":"edit","status":"pending"}' > "$FAKEHOME/.claude/harness-inbox/20261007T120000Z-0000abcd.json"
  OUT=$(cd "$PROJF" && printf '%s' "$(spay)" | node "$STOPHOOK" 2>"$ERRFILE"); RC=$?
  i=0; while [ ! -s "$STATE" ] && [ $i -lt 300 ]; do sleep 0.1; i=$((i+1)); done
  sleep 1
  [ -s "$STATE" ]; check "i18a real fleet.py collect wrote ~/.claude/fleet/state.json (temp HOME)" $?
  ! grep -q 'SECRETKEYBYTES' "$STATE" 2>/dev/null; check "i18b *.key bytes never reach state.json" $?
  run_hook "$(fpay)"
  case "$OUT" in *' | fleet 1 inbox') r=0;; *) r=1;; esac
  check "i18c statusline reads the collected state: 'fleet 1 inbox' (got: ${OUT:0:160})" $r
  [ -z "$(cd "$PROJF" && git status --porcelain --ignored)" ]; check "i18d real collect writes nothing inside the project folder" $?
  rm -rf "$FAKEHOME/.claude/sessions" "$FAKEHOME/.claude/harness-inbox"
else
  echo "SKIP: i18 end-to-end collect (no Python >= 3.10 as '$PYCHK')"
fi

node --check "$HOOK"
check "syntax node --check passes" $?
node --check "$STOPHOOK"
check "syntax node --check passes (stop hook)" $?

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
