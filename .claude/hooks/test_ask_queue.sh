#!/usr/bin/env bash
# Tests for .claude/hooks/ask-queue.mjs
#   VAL-1001: with ~/.claude/fleet/away present, every AskUserQuestion is denied and queued per session in
#             ~/.claude/fleet/questions/<session_id>.json; relay-marked and
#             FLEET_QUESTIONS=0 calls open the box; bad input fails open; an unreadable
#             queue or a held <queue>.json.lock writes nothing and opens the box.
# Self-contained; run from anywhere: bash .claude/hooks/test_ask_queue.sh
set -u

HOOK="$(cd "$(dirname "$0")" && pwd)/ask-queue.mjs"
WORK="$(mktemp -d)"
WORK="$(cygpath -m "$WORK" 2>/dev/null || echo "$WORK")"  # Windows node cannot resolve /tmp POSIX paths
trap 'rm -rf "$WORK"' EXIT
export HOME="$WORK/home" USERPROFILE="$WORK/home"
unset FLEET_QUESTIONS
QDIR="$HOME/.claude/fleet/questions"

PASS=0
FAIL=0
check() { # <name> <condition result: 0 ok>
  if [ "$2" -eq 0 ]; then PASS=$((PASS+1)); echo "PASS: $1";
  else FAIL=$((FAIL+1)); echo "FAIL: $1"; fi
}
jget() { node -e "const d=JSON.parse(require('fs').readFileSync(process.argv[1],'utf8'));console.log(eval(process.argv[2]))" "$@"; }

node --check "$HOOK"
check "VAL-1001 node --check passes" $?

Q1='{"question":"Which release date?","header":"date","multiSelect":false,"options":[{"label":"A. Monday (Recommended)","description":"why"},{"label":"B. Friday","description":"other"}]}'
Q2='{"question":"Ship now?","header":"ship","options":[{"label":"A. Yes","description":""},{"label":"B. No","description":""}]}'
payload() { # <session_id or empty> <questions json array> -> stdin JSON
  local sid=""; [ -n "$1" ] && sid="\"session_id\":\"$1\","
  printf '{%s"cwd":"C:/work/proj-alpha","tool_name":"AskUserQuestion","tool_input":{"questions":%s}}' "$sid" "$2"
}

# --- VAL-1001z: not away (no flag file) -> box opens, nothing queued ---
OUT=$(payload sessZ "[$Q1]" | node "$HOOK")
[ -z "$OUT" ] && [ ! -e "$QDIR/sessZ.json" ]
check "VAL-1001z user present (no away flag): clickable box allowed" $?
mkdir -p "$HOME/.claude/fleet" && : > "$HOME/.claude/fleet/away"

# --- VAL-1001a: deny + queued with ids, options, project ---
OUT=$(payload sessA "[$Q1,$Q2]" | node "$HOOK")
printf '%s' "$OUT" | grep -q '"permissionDecision":"deny"'
check "VAL-1001a question box denied (got: ${OUT:0:80})" $?
F="$QDIR/sessA.json"
[ -f "$F" ] && [ "$(jget "$F" 'd.questions.length')" = 2 ]
check "VAL-1001a both questions queued in the session's own file" $?
[ "$(jget "$F" 'd.project')" = proj-alpha ] && [ "$(jget "$F" 'd.questions[0].status')" = pending ] \
  && [ "$(jget "$F" 'd.questions[0].options[0].label')" = "A. Monday (Recommended)" ]
check "VAL-1001a project, status and options recorded" $?
ID1=$(jget "$F" 'd.questions[0].id')
printf '%s' "$OUT" | grep -q "Queued as $ID1" && printf '%s' "$OUT" | grep -q 'same format' \
  && printf '%s' "$OUT" | grep -q 'fleet.py answer'
check "VAL-1001a reason names the ids, the A-D listing and the answer command" $?
case "$ID1" in q-????????) r=0;; *) r=1;; esac
check "VAL-1001a id shape q-<8 hex> (got $ID1)" $r

# --- VAL-1001b: second call appends to the same session queue; other session separate ---
payload sessA "[$Q2]" | node "$HOOK" > /dev/null
[ "$(jget "$F" 'd.questions.length')" = 3 ]
check "VAL-1001b later question appended to the same queue" $?
payload sessB "[$Q1]" | node "$HOOK" > /dev/null
[ -f "$QDIR/sessB.json" ] && [ "$(jget "$F" 'd.questions.length')" = 3 ]
check "VAL-1001b another session gets its own queue file" $?

# --- VAL-1001c: relay-marked box (/fleet questions) opens ---
RELAY='{"question":"[fleet q-0123abcd] proj-alpha: Which release date?","header":"date","options":[{"label":"A","description":""},{"label":"B","description":""}]}'
OUT=$(payload sessC "[$RELAY]" | node "$HOOK")
[ -z "$OUT" ] && [ ! -e "$QDIR/sessC.json" ]
check "VAL-1001c relay-marked question allowed, nothing queued" $?
OUT=$(payload sessC "[$RELAY,$Q2]" | node "$HOOK")
printf '%s' "$OUT" | grep -q '"deny"'
check "VAL-1001c a mixed call (one unmarked question) is still queued" $?

# --- VAL-1001d: FLEET_QUESTIONS=0 opens the box ---
OUT=$(payload sessD "[$Q1]" | FLEET_QUESTIONS=0 node "$HOOK")
[ -z "$OUT" ] && [ ! -e "$QDIR/sessD.json" ]
check "VAL-1001d FLEET_QUESTIONS=0 allows the box" $?

# --- VAL-1001e: fail open ---
OUT=$(payload "" "[$Q1]" | node "$HOOK")
[ -z "$OUT" ] && grep -q 'ask-queue missing or unsafe session_id' "$HOME/.claude/fleet/guard-errors.log"
check "VAL-1001e no session_id -> box allowed and logged" $?
OUT=$(payload '../evil' "[$Q1]" | node "$HOOK")
[ -z "$OUT" ] && [ ! -e "$HOME/.claude/fleet/evil.json" ]
check "VAL-1001e path-like session_id -> box allowed, nothing written outside the queue" $?
OUT=$(printf 'not json' | node "$HOOK"); RC=$?
[ -z "$OUT" ] && [ "$RC" -eq 0 ]
check "VAL-1001e malformed stdin -> allow, exit 0" $?
OUT=$(printf '{"tool_name":"Bash","session_id":"sessE","tool_input":{"command":"ls"}}' | node "$HOOK")
[ -z "$OUT" ]
check "VAL-1001e other tools ignored" $?

# --- VAL-1001f: only a missing queue starts a new one; anything unreadable is left untouched ---
LOG="$HOME/.claude/fleet/guard-errors.log"
F="$QDIR/sessF.json"
printf '{"schema_version":1,"session_id":"sessF","questions":[{"id":"q-aaaaaaa1","status":"answered","answer":"A"}' > "$F"
cp "$F" "$WORK/sessF.before"
OUT=$(payload sessF "[$Q1]" | node "$HOOK")
[ -z "$OUT" ] && cmp -s "$F" "$WORK/sessF.before" && grep -q 'ask-queue enqueue failed' "$LOG"
check "VAL-1001f partial/corrupt queue -> box allowed, file unchanged, logged" $?
printf '{"schema_version":1}' > "$F"; cp "$F" "$WORK/sessF.before"
OUT=$(payload sessF "[$Q1]" | node "$HOOK")
[ -z "$OUT" ] && cmp -s "$F" "$WORK/sessF.before" && grep -q 'not a question queue' "$LOG"
check "VAL-1001f JSON that is not a queue -> box allowed, file unchanged" $?
if command -v py >/dev/null 2>&1; then
  printf '{"schema_version":1,"session_id":"sessF","questions":[{"id":"q-aaaaaaa1","status":"answered","answer":"A"}]}' > "$F"
  cp "$F" "$WORK/sessF.before"
  # Windows byte-range lock = sharing violation on read (EBUSY/EPERM), as a concurrent writer causes
  OUT=$(py -3.13 - "$F" "$HOOK" "$(payload sessF "[$Q1]")" <<'PYEOF2'
import msvcrt, subprocess, sys
f = open(sys.argv[1], "r+b")
msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 10)
p = subprocess.run(["node", sys.argv[2]], input=sys.argv[3].encode(), capture_output=True)
f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 10); f.close()
sys.stdout.write(p.stdout.decode())
PYEOF2
)
  [ -z "$OUT" ] && cmp -s "$F" "$WORK/sessF.before" && [ ! -e "$F.lock" ]
  check "VAL-1001f sharing violation on read -> box allowed, queue and answer kept, lock released" $?
fi

# --- VAL-1001g: writer lock <queue>.json.lock shared with tools/fleet/questions.py ---
F="$QDIR/sessG.json"
payload sessG "[$Q1]" | node "$HOOK" > /dev/null
[ -f "$F" ] && [ ! -e "$F.lock" ]
check "VAL-1001g lock released after a normal enqueue" $?
printf '1 held\n' > "$F.lock"; cp "$F" "$WORK/sessG.before"
START=$(date +%s)
OUT=$(payload sessG "[$Q2]" | node "$HOOK")
ELAPSED=$(( $(date +%s) - START ))
[ -z "$OUT" ] && cmp -s "$F" "$WORK/sessG.before" && [ -e "$F.lock" ] && grep -q 'queue busy' "$LOG" \
  && [ "$ELAPSED" -ge 1 ] && [ "$ELAPSED" -le 5 ]
check "VAL-1001g held lock -> waits ~2 s, then box allowed, nothing written, lock left (${ELAPSED}s)" $?
touch -d '1 minute ago' "$F.lock"
OUT=$(payload sessG "[$Q2]" | node "$HOOK")
printf '%s' "$OUT" | grep -q '"deny"' && [ "$(jget "$F" 'd.questions.length')" = 2 ] && [ ! -e "$F.lock" ]
check "VAL-1001g stale lock (older than 10 s) broken, question queued" $?

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
