#!/usr/bin/env bash
# Tests for .claude/hooks/auto-handoff-stop.mjs
#   VAL-102: stale pct files (mtime > 2h) ignored; missing pct file degrades with
#            exactly one stderr note; unguarded JSON.parse wrapped (never throws);
#            session-id keying symmetric with status.mjs (session_id.slice(0,8),
#            env/ppid fallback); guard fails OPEN on missing/corrupt data.
# Self-contained; run from anywhere: bash .claude/hooks/test_auto_handoff_stop.sh
set -u

HOOK="$(cd "$(dirname "$0")" && pwd)/auto-handoff-stop.mjs"
TMPWIN="$(node -e "console.log(require('os').tmpdir().replace(/\\\\/g,'/'))")"
ERRFILE="$TMPWIN/stopguard_test_err_$$.txt"

PASS=0
FAIL=0
check() { # <name> <condition result: 0 ok>
  if [ "$2" -eq 0 ]; then PASS=$((PASS+1)); echo "PASS: $1";
  else FAIL=$((FAIL+1)); echo "FAIL: $1"; fi
}

# run_hook <stdin payload> — sets OUT (stdout), ERR (stderr), RC (exit code)
run_hook() {
  OUT=$(printf '%s' "$1" | node "$HOOK" 2>"$ERRFILE"); RC=$?
  ERR=$(cat "$ERRFILE" 2>/dev/null || true)
}

pctfile() { printf '%s/claude-context-pct-%s.txt' "$TMPWIN" "$1"; }
payload() { # <sid8> — full session_id whose slice(0,8) is sid8, matching status.mjs keying
  printf '{"session_id":"%s-0000-4000-8000-tail","stop_hook_active":false}' "$1"
}
set_mtime_hours_ago() { # <file> <hours>
  node -e "const t=(Date.now()-Number(process.argv[2])*3600*1000)/1000; require('fs').utimesSync(process.argv[1], t, t)" "$1" "$2"
}

SIDS="sgtstA1x sgtstB2x sgtstC3x sgtstD4x sgtstE5x sgtstF6x sgtstG7x sgtstH8x"
for s in $SIDS; do rm -f "$(pctfile "$s")"; done

# --- T1: fresh pct >= 85 blocks, reason carries the pct ---
printf '95' > "$(pctfile sgtstA1x)"
run_hook "$(payload sgtstA1x)"
case "$OUT" in *'"decision":"block"'*) r=0;; *) r=1;; esac
check "T1a fresh 95% blocks (got: ${OUT:0:80})" $r
case "$OUT" in *'95%'*) r=0;; *) r=1;; esac
check "T1b block reason carries pct" $r
[ "$RC" -eq 0 ]; check "T1c exit 0" $?
case "$OUT" in *'/create-handoff'*) r=0;; *) r=1;; esac
check "T1d reason names real skill /create-handoff (hyphen)" $r
case "$OUT" in *'/create_handoff'*) r=1;; *) r=0;; esac
check "T1e no stale /create_handoff (underscore) reference" $r

# --- T2: keying symmetric with status.mjs — key is session_id.slice(0,8), so a
#     file written under a DIFFERENT 8-char prefix must not be picked up ---
printf '95' > "$(pctfile sgtstH8x)"
run_hook "$(payload sgtstB2x)"   # no file for sgtstB2x; sgtstH8x's 95 must not leak
case "$OUT" in '{}'*) r=0;; *) r=1;; esac
check "T2 key derived from own stdin session_id only (got: ${OUT:0:60})" $r
rm -f "$(pctfile sgtstH8x)"

# --- T3: fresh pct < 85 allows ---
printf '50' > "$(pctfile sgtstB2x)"
run_hook "$(payload sgtstB2x)"
[ "$OUT" = "{}" ]; check "T3 fresh 50% allows (got: ${OUT:0:60})" $?

# --- T4: STALE pct file (mtime > 2h) ignored -> allow, exit 0 (fail OPEN) ---
printf '95' > "$(pctfile sgtstC3x)"
set_mtime_hours_ago "$(pctfile sgtstC3x)" 3
run_hook "$(payload sgtstC3x)"
[ "$OUT" = "{}" ]; check "T4a stale (3h) 95% file ignored -> allow (got: ${OUT:0:60})" $?
[ "$RC" -eq 0 ]; check "T4b exit 0 on stale file" $?

# --- T5: 1h-old file is NOT stale -> still blocks ---
printf '95' > "$(pctfile sgtstD4x)"
set_mtime_hours_ago "$(pctfile sgtstD4x)" 1
run_hook "$(payload sgtstD4x)"
case "$OUT" in *'"decision":"block"'*) r=0;; *) r=1;; esac
check "T5 1h-old 95% file still blocks (got: ${OUT:0:60})" $r

# --- T6: MISSING pct file -> allow, exit 0, exactly ONE stderr note ---
rm -f "$(pctfile sgtstE5x)"
run_hook "$(payload sgtstE5x)"
[ "$OUT" = "{}" ]; check "T6a missing pct file -> allow (fail open) (got: ${OUT:0:60})" $?
[ "$RC" -eq 0 ]; check "T6b exit 0 on missing file" $?
[ -n "$ERR" ]; check "T6c stderr note present when pct file missing (got: '${ERR:0:80}')" $?
NLINES=$(printf '%s' "$ERR" | grep -c . || true)
[ "$NLINES" -eq 1 ]; check "T6d exactly one stderr line (got $NLINES)" $?

# --- T7: fresh/present file -> NO stderr noise ---
printf '95' > "$(pctfile sgtstA1x)"
run_hook "$(payload sgtstA1x)"
[ -z "$ERR" ]; check "T7 no stderr when pct file present (got: '${ERR:0:80}')" $?

# --- T8: corrupt stdin (invalid JSON) -> never throw, allow, exit 0 ---
run_hook 'not-json {{{'
[ "$RC" -eq 0 ]; check "T8a corrupt stdin -> exit 0 (JSON.parse guarded)" $?
[ "$OUT" = "{}" ]; check "T8b corrupt stdin -> allow (fail open) (got: ${OUT:0:60})" $?

# --- T9: empty stdin -> never throw, allow ---
run_hook ''
[ "$RC" -eq 0 ]; check "T9a empty stdin -> exit 0" $?
[ "$OUT" = "{}" ]; check "T9b empty stdin -> allow" $?

# --- T10: corrupt pct file contents -> allow (fail open), never 'NaN%' block ---
printf 'garbage' > "$(pctfile sgtstF6x)"
run_hook "$(payload sgtstF6x)"
[ "$OUT" = "{}" ]; check "T10a corrupt pct contents -> allow (got: ${OUT:0:60})" $?
[ "$RC" -eq 0 ]; check "T10b exit 0 on corrupt pct contents" $?

# --- T11: stop_hook_active short-circuits even at 95% ---
printf '95' > "$(pctfile sgtstG7x)"
run_hook '{"session_id":"sgtstG7x-0000-4000-8000-tail","stop_hook_active":true}'
[ "$OUT" = "{}" ]; check "T11 stop_hook_active -> allow despite 95%" $?

# --- T12: env fallback keying (no session_id -> CLAUDE_SESSION_ID), same as status.mjs ---
printf '95' > "$(pctfile sgtstE5x)"
OUT=$(printf '{"stop_hook_active":false}' | CLAUDE_SESSION_ID=sgtstE5x node "$HOOK" 2>"$ERRFILE"); RC=$?
case "$OUT" in *'"decision":"block"'*) r=0;; *) r=1;; esac
check "T12 CLAUDE_SESSION_ID fallback keying blocks at 95% (got: ${OUT:0:60})" $r

# --- T13: node --check passes on the hook ---
node --check "$HOOK"
check "T13 node --check passes" $?

for s in $SIDS; do rm -f "$(pctfile "$s")"; done
rm -f "$ERRFILE"

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
