#!/usr/bin/env bash
# Tests for .claude/hooks/handover-check.mjs (Stop hook: blocks replies that hand the
# user a task Claude could do). Self-contained: bash .claude/hooks/test_handover_check.sh
set -u

HOOK="$(cd "$(dirname "$0")" && pwd)/handover-check.mjs"
TEST_TMP="$(mktemp -d)"
TMPWIN="$(cd "$TEST_TMP" && { pwd -W 2>/dev/null || pwd; })"
trap 'rm -rf "$TEST_TMP"' EXIT
unset HANDOVER_CHECK

PASS=0
FAIL=0
check() { # <name> <0 ok>
  if [ "$2" -eq 0 ]; then PASS=$((PASS+1)); echo "PASS: $1";
  else FAIL=$((FAIL+1)); echo "FAIL: $1"; fi
}

# payload <message> [stop_hook_active] - JSON built by node so quoting is exact
payload() {
  node -e 'process.stdout.write(JSON.stringify({session_id:"hc-test",stop_hook_active:process.argv[2]==="1",last_assistant_message:process.argv[1]}))' "$1" "${2:-0}"
}
run() { OUT=$(printf '%s' "$1" | node "$HOOK" 2>/dev/null); RC=$?; }
blocks() { case "$OUT" in *'"decision":"block"'*) return 0;; *) return 1;; esac; }

for msg in \
  "Next steps: 1. Look at a screenshot of the tab, if you send one, and say what it shows" \
  "Can you run the installer and tell me the result?" \
  "Send me a screenshot of the error." \
  "You'll need to restart the terminal for this to work." \
  "Please run fleet.py grant apply." \
  "Try it yourself and let me know what it says." \
  "Open the app and tell me what you see." \
  "Check the tab, then tell me what it shows." \
  "I need you to take a screenshot." \
  "I'll look at it. Take a screenshot and paste it."; do
  run "$(payload "$msg")"
  blocks; check "blocks: ${msg:0:50}" $?
  [ "$RC" -eq 0 ]; check "exit 0: ${msg:0:30}" $?
done

run "$(payload "If you send one, I will look.")"
case "$OUT" in *'If you send'*) r=0;; *) r=1;; esac
check "reason quotes the matched phrase" $r
case "$OUT" in *'fleet.py screenshot'*) r=0;; *) r=1;; esac
check "reason names the screenshot command" $r

for msg in \
  "I took a screenshot of both screens and read them." \
  "I'll run the tests, then push and install." \
  "3. Check the stopwatch, I'll take a screenshot and compare it with the table." \
  "Next I will take a screenshot of both screens." \
  "Let me take a screenshot first." \
  "We'll then take a screenshot after opening." \
  'You said "send me a screenshot" earlier; I took one myself.' \
  'The rule `if you send` is now checked.' \
  "Done. Tests pass (169), committed and pushed." \
  ""; do
  run "$(payload "$msg")"
  blocks; [ $? -ne 0 ]; check "allows: ${msg:0:50}" $?
done

# The allowed hand-over: an actual denial plus one ! line
run "$(payload "$(printf 'The settings write was denied twice.\n! py -3.13 fleet.py grant apply\nPlease run it.')")"
blocks; [ $? -ne 0 ]; check "allows a named denial with a ! line" $?

# The retry after a block always passes (no loop)
run "$(payload "Can you run it?" 1)"
[ "$OUT" = "{}" ]; check "stop_hook_active passes" $?

# Disabled by env
OUT=$(payload "Can you run it?" | HANDOVER_CHECK=0 node "$HOOK" 2>/dev/null)
[ "$OUT" = "{}" ]; check "HANDOVER_CHECK=0 disables" $?

# Fails open on garbage input
run "not json"
[ "$OUT" = "{}" ] && [ "$RC" -eq 0 ]; check "garbage input fails open" $?

# Transcript fallback: last assistant text after the last user prompt
T="$TMPWIN/t.jsonl"
node -e '
const fs=require("fs"); const L=[
 {type:"user",message:{content:"old prompt"}},
 {type:"assistant",message:{content:[{type:"text",text:"Can you run the old thing?"}]}},
 {type:"user",message:{content:"new prompt"}},
 {type:"assistant",message:{content:[{type:"tool_use",name:"Bash"}]}},
 {type:"user",message:{content:[{type:"tool_result",content:"ok"}]}},
 {type:"assistant",message:{content:[{type:"text",text:process.argv[2]}]}}];
fs.writeFileSync(process.argv[1], L.map(x=>JSON.stringify(x)).join("\n")+"\n");' "$T" "If you paste the log I can read it."
run "$(node -e 'process.stdout.write(JSON.stringify({transcript_path:process.argv[1]}))' "$T")"
blocks; check "transcript fallback finds the final reply" $?
node -e '
const fs=require("fs"); const L=[
 {type:"user",message:{content:"old prompt"}},
 {type:"assistant",message:{content:[{type:"text",text:"Can you run the old thing?"}]}},
 {type:"user",message:{content:"new prompt"}},
 {type:"assistant",message:{content:[{type:"text",text:"Done, all tests pass."}]}}];
fs.writeFileSync(process.argv[1], L.map(x=>JSON.stringify(x)).join("\n")+"\n");' "$T"
run "$(node -e 'process.stdout.write(JSON.stringify({transcript_path:process.argv[1]}))' "$T")"
[ "$OUT" = "{}" ]; check "transcript fallback ignores replies to earlier prompts" $?

echo "RESULT: $PASS passed, $FAIL failed (handover-check)"
[ "$FAIL" -eq 0 ]
