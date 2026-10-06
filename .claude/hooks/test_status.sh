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

node --check "$HOOK"
check "syntax node --check passes" $?

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
