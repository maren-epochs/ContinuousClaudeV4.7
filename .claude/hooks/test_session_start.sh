#!/usr/bin/env bash
# Tests for .claude/hooks/session-start.mjs
#   startup/clear inject bloks context (capped); compact injects latest handoff from the
#   handoff root (project thoughts/ else ~/.claude/handoffs/<basename>); resume/fork and
#   failures emit '{}'.
# Self-contained; run from anywhere: bash .claude/hooks/test_session_start.sh
set -u

HOOK="$(cd "$(dirname "$0")" && pwd)/session-start.mjs"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export HOME="$WORK/home" USERPROFILE="$WORK/home"
mkdir -p "$HOME"

PASS=0
FAIL=0
check() { # <name> <condition result: 0 ok>
  if [ "$2" -eq 0 ]; then PASS=$((PASS+1)); echo "PASS: $1";
  else FAIL=$((FAIL+1)); echo "FAIL: $1"; fi
}
win() { cygpath -m "$1" 2>/dev/null || printf '%s' "$1"; }

# Fake bloks: prints a marker plus the cwd it was given
FAKE="$WORK/fakebloks.mjs"
printf '%s\n' "console.log('RULES\\n  fake-rule-marker for ' + process.argv[3]); " > "$FAKE"
BIN="$(win "$FAKE")"
run() { # <json> [env...] -> OUT, RC
  OUT=$(printf '%s' "$1" | env "${@:2}" node "$HOOK" 2>/dev/null); RC=$?
}
ctx() { printf '%s' "$OUT" | node -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{try{const j=JSON.parse(d);process.stdout.write((j.hookSpecificOutput&&j.hookSpecificOutput.additionalContext)||'')}catch{process.stdout.write('BADJSON')}})"; }

PROJ="$WORK/proj"; mkdir -p "$PROJ"
P="$(win "$PROJ")"

# --- S1: startup injects bloks context for cwd ---
run "{\"source\":\"startup\",\"cwd\":\"$P\"}" BLOKS_BIN="$BIN"
C=$(ctx)
case "$C" in *fake-rule-marker*) r=0;; *) r=1;; esac
check "S1a startup injects bloks context (got: ${C:0:80})" $r
case "$C" in *"$P"*) r=0;; *) r=1;; esac
check "S1b bloks called with the session cwd" $r
[ "$RC" -eq 0 ]; check "S1c exit 0" $?

# --- S2: clear behaves like startup ---
run "{\"source\":\"clear\",\"cwd\":\"$P\"}" BLOKS_BIN="$BIN"
case "$(ctx)" in *fake-rule-marker*) r=0;; *) r=1;; esac
check "S2 clear injects bloks context" $r

# --- S3: cap ---
run "{\"source\":\"startup\",\"cwd\":\"$P\"}" BLOKS_BIN="$BIN" SESSION_START_CAP=10
case "$(ctx)" in *"truncated at 10 chars"*) r=0;; *) r=1;; esac
check "S3 SESSION_START_CAP truncates" $r

# --- S4: resume / fork inject nothing ---
for s in resume fork; do
  run "{\"source\":\"$s\",\"cwd\":\"$P\"}" BLOKS_BIN="$BIN"
  [ "$OUT" = "{}" ]; check "S4 $s -> {} (got: ${OUT:0:40})" $?
done

# --- S5: compact, project without thoughts/ -> newest handoff under ~/.claude/handoffs/<basename> ---
H="$HOME/.claude/handoffs/proj/general"; mkdir -p "$H"
printf 'goal: old\n' > "$H/2026-01-01_00-00_old.yaml"; touch -d '2 hours ago' "$H/2026-01-01_00-00_old.yaml"
printf '# Auto-Handoff\nnewest-handoff-marker\n' > "$H/auto-handoff-x.md"
run "{\"source\":\"compact\",\"cwd\":\"$P\"}" BLOKS_BIN="$BIN"
C=$(ctx)
case "$C" in *newest-handoff-marker*) r=0;; *) r=1;; esac
check "S5a compact injects newest handoff by mtime (got: ${C:0:80})" $r
case "$C" in *fake-rule-marker*) r=1;; *) r=0;; esac
check "S5b compact does not inject bloks context" $r

# --- S6: compact, project WITH thoughts/shared/handoffs -> project root wins ---
mkdir -p "$PROJ/thoughts/shared/handoffs/s1"
printf 'goal: local\nlocal-handoff-marker\n' > "$PROJ/thoughts/shared/handoffs/s1/2026-02-02_00-00_l.yaml"
run "{\"source\":\"compact\",\"cwd\":\"$P\"}" BLOKS_BIN="$BIN"
case "$(ctx)" in *local-handoff-marker*) r=0;; *) r=1;; esac
check "S6 project handoff root preferred over ~/.claude/handoffs" $r

# --- S7: failures fail open ---
run "{\"source\":\"startup\",\"cwd\":\"$P\"}" BLOKS_BIN="$WORK/does-not-exist"
[ "$OUT" = "{}" ] && [ "$RC" -eq 0 ]; check "S7a missing bloks -> {} exit 0" $?
OUT=$(printf 'garbage' | node "$HOOK" 2>/dev/null); RC=$?
[ "$RC" -eq 0 ] && printf '%s' "$OUT" | node -e "JSON.parse(require('fs').readFileSync(0,'utf-8'))"
check "S7b garbage stdin -> valid JSON, exit 0" $?
OUT=$(printf 'null' | node "$HOOK" 2>/dev/null); RC=$?
[ "$RC" -eq 0 ]; check "S7c null stdin -> exit 0" $?
run "{\"source\":\"compact\",\"cwd\":\"$(win "$WORK/nohandoffs")\"}" BLOKS_BIN="$BIN"
[ "$OUT" = "{}" ]; check "S7d compact with no handoffs -> {}" $?

node --check "$HOOK"; check "syntax node --check passes" $?

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
