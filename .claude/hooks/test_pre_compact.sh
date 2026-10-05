#!/usr/bin/env bash
# Tests for .claude/hooks/pre-compact.mjs
#   VAL-101: extracts real state from actual Claude Code transcript JSONL
#            (message.content[] blocks); guarded IO; never throws.
# Fixture is synthesized from the real transcript SCHEMA (structure copied,
# content replaced with placeholder text). Self-contained; run from anywhere:
#   bash .claude/hooks/test_pre_compact.sh
set -u

HOOK="$(cd "$(dirname "$0")" && pwd)/pre-compact.mjs"
WORK="$(mktemp -d)"
WORK="$(cygpath -m "$WORK" 2>/dev/null || echo "$WORK")"  # Windows node cannot resolve /tmp POSIX paths
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
check() { # <name> <condition result: 0 ok>
  if [ "$2" -eq 0 ]; then PASS=$((PASS+1)); echo "PASS: $1";
  else FAIL=$((FAIL+1)); echo "FAIL: $1"; fi
}

# --- syntax gate ---
node --check "$HOOK"
check "VAL-101a node --check passes" $?

# --- skill name: /create-handoff (hyphen), never /create_handoff ---
! grep -q 'create_handoff' "$HOOK"
check "VAL-101a hook references /create-handoff, not /create_handoff" $?

# --- fixture: real Claude Code transcript schema, placeholder content ---
FIXTURE="$WORK/fixture-session.jsonl"
cat > "$FIXTURE" <<'EOF'
{"type":"user","isSidechain":false,"uuid":"u01","timestamp":"2026-10-05T00:00:01Z","sessionId":"fixture","message":{"role":"user","content":"Placeholder goal one: refactor the alpha module retry logic"}}
{"type":"assistant","isSidechain":false,"uuid":"a01","timestamp":"2026-10-05T00:00:02Z","sessionId":"fixture","message":{"role":"assistant","content":[{"type":"thinking","thinking":"placeholder thinking"},{"type":"text","text":"Placeholder plan text."}]}}
{"type":"assistant","isSidechain":false,"uuid":"a02","timestamp":"2026-10-05T00:00:03Z","sessionId":"fixture","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_01","name":"Edit","input":{"file_path":"C:/fixture/project/src/alpha-module.mjs","old_string":"placeholder old","new_string":"placeholder new","replace_all":false}}]}}
{"type":"user","isSidechain":false,"uuid":"u02","timestamp":"2026-10-05T00:00:04Z","sessionId":"fixture","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_01","is_error":false,"content":"placeholder edit ok"}]}}
{"type":"assistant","isSidechain":false,"uuid":"a03","timestamp":"2026-10-05T00:00:05Z","sessionId":"fixture","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_02","name":"Bash","input":{"command":"node --check src/alpha-module.mjs","description":"placeholder"}}]}}
{"type":"user","isSidechain":false,"uuid":"u03","timestamp":"2026-10-05T00:00:06Z","sessionId":"fixture","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_02","is_error":false,"content":""}]}}
{"type":"assistant","isSidechain":false,"uuid":"a04","timestamp":"2026-10-05T00:00:07Z","sessionId":"fixture","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_03","name":"Bash","input":{"command":"bash run_placeholder_tests.sh","description":"placeholder"}}]}}
{"type":"user","isSidechain":false,"uuid":"u04","timestamp":"2026-10-05T00:00:08Z","sessionId":"fixture","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_03","is_error":true,"content":"Exit code 1\nplaceholder test failure detail"}]}}
{"type":"assistant","isSidechain":false,"uuid":"a05","timestamp":"2026-10-05T00:00:09Z","sessionId":"fixture","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_04","name":"Edit","input":{"file_path":"C:/fixture/project/src/alpha-module.mjs","old_string":"x","new_string":"y","replace_all":false}}]}}
{"type":"user","isSidechain":false,"uuid":"u05","timestamp":"2026-10-05T00:00:10Z","sessionId":"fixture","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_04","is_error":false,"content":"placeholder edit ok"}]}}
{"type":"assistant","isSidechain":false,"uuid":"a06","timestamp":"2026-10-05T00:00:11Z","sessionId":"fixture","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_05","name":"Bash","input":{"command":"bash run_placeholder_tests.sh","description":"placeholder rerun"}}]}}
{"type":"user","isSidechain":false,"uuid":"u06","timestamp":"2026-10-05T00:00:12Z","sessionId":"fixture","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_05","is_error":false,"content":[{"type":"text","text":"all placeholder tests passed"}]}]}}
{"type":"assistant","isSidechain":false,"uuid":"a07","timestamp":"2026-10-05T00:00:13Z","sessionId":"fixture","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_06","name":"Write","input":{"file_path":"C:/fixture/project/docs/beta-notes.md","content":"placeholder doc body"}}]}}
{"type":"user","isSidechain":false,"uuid":"u07","timestamp":"2026-10-05T00:00:14Z","sessionId":"fixture","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_06","is_error":false,"content":"placeholder write ok"}]}}
{"type":"assistant","isSidechain":false,"uuid":"a08","timestamp":"2026-10-05T00:00:15Z","sessionId":"fixture","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_07","name":"TodoWrite","input":{"todos":[{"id":"1","content":"placeholder done item","status":"completed"},{"id":"2","content":"placeholder open item","status":"pending"}]}}]}}
{"type":"user","isSidechain":false,"uuid":"u08","timestamp":"2026-10-05T00:00:16Z","sessionId":"fixture","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_07","is_error":false,"content":"placeholder todos ok"}]}}
{"type":"user","isSidechain":false,"uuid":"u09","timestamp":"2026-10-05T00:00:17Z","sessionId":"fixture","message":{"role":"user","content":"Placeholder goal two: wire the beta notes into the gamma pipeline"}}
{"type":"user","isSidechain":true,"uuid":"u10","timestamp":"2026-10-05T00:00:18Z","sessionId":"fixture","message":{"role":"user","content":"Sidechain placeholder noise must not become the goal"}}
{"type":"assistant","isSidechain":false,"uuid":"a09","timestamp":"2026-10-05T00:00:19Z","sessionId":"fixture","message":{"role":"assistant","content":[{"type":"text","text":"Placeholder final assistant summary."}]}}
{"type":"attachment","uuid":"x01","timestamp":"2026-10-05T00:00:20Z","attachment":{"placeholder":true}}
{"type":"file-history-snapshot","uuid":"x02","timestamp":"2026-10-05T00:00:21Z"}
EOF

FAKEHOME="$WORK/home"; mkdir -p "$FAKEHOME"
export HOME="$FAKEHOME" USERPROFILE="$FAKEHOME"   # non-opted-in projects write under ~/.claude/handoffs
run_hook() { # <transcript_path> <project_dir> -> OUT, RC
  OUT=$(printf '{"trigger":"auto","session_id":"fixture-session-0001","transcript_path":"%s"}' "$1" \
        | CLAUDE_PROJECT_DIR="$2" node "$HOOK")
  RC=$?
}

# --- VAL-101b: real-schema fixture -> handoff with real content ---
PROJ="$WORK/proj"
mkdir -p "$PROJ/thoughts/shared/handoffs"   # project opted into the convention
run_hook "$FIXTURE" "$PROJ"
check "VAL-101b hook exit 0" $RC
printf '%s' "$OUT" | node -e "const d=JSON.parse(require('fs').readFileSync(0,'utf-8')); if(d.continue!==true)process.exit(1)"
check "VAL-101b stdout is valid hook JSON with continue:true (got: ${OUT:0:80})" $?

HANDOFF=$(find "$PROJ/thoughts/shared/handoffs" -name 'auto-handoff-*.md' 2>/dev/null | head -1)
[ -n "$HANDOFF" ] && [ -f "$HANDOFF" ]
check "VAL-101b handoff file written under thoughts/shared/handoffs/" $?

if [ -n "$HANDOFF" ]; then
  grep -q "Placeholder goal two: wire the beta notes" "$HANDOFF"
  check "VAL-101c goal = last non-tool user text" $?
  ! grep -q "Sidechain placeholder noise" "$HANDOFF"
  check "VAL-101c sidechain user text excluded from goal" $?
  grep -q "alpha-module.mjs" "$HANDOFF"
  check "VAL-101d Edit tool_use file path extracted" $?
  grep -q "beta-notes.md" "$HANDOFF"
  check "VAL-101d Write tool_use file path extracted" $?
  grep -q "run_placeholder_tests.sh" "$HANDOFF"
  check "VAL-101e Bash tool_use command extracted" $?
  grep -q "placeholder test failure detail" "$HANDOFF"
  check "VAL-101f tool_result is_error captured as error" $?
  grep -q "placeholder open item" "$HANDOFF"
  check "VAL-101g TodoWrite todos extracted" $?
  grep -q "Placeholder final assistant summary" "$HANDOFF"
  check "VAL-101h last assistant text block extracted" $?
else
  for name in "goal" "sidechain" "edit path" "write path" "command" "error" "todos" "assistant text"; do
    check "VAL-101 content ($name) — no handoff file produced" 1
  done
fi

# --- VAL-101i: malformed transcript lines -> graceful, valid JSON, exit 0 ---
BAD="$WORK/malformed.jsonl"
cat > "$BAD" <<'EOF'
this is not json at all
{"type":"user","message":
{"type":"user","message":null}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use"}]}}
{}
EOF
PROJ2="$WORK/proj2"
mkdir -p "$PROJ2"
run_hook "$BAD" "$PROJ2"
check "VAL-101i malformed transcript: exit 0" $RC
printf '%s' "$OUT" | node -e "JSON.parse(require('fs').readFileSync(0,'utf-8'))"
check "VAL-101i malformed transcript: stdout still valid JSON (got: ${OUT:0:80})" $?

# --- VAL-101j: garbage stdin -> no throw, valid JSON, exit 0 ---
OUT=$(printf 'not json' | CLAUDE_PROJECT_DIR="$WORK/proj3" node "$HOOK" 2>/dev/null)
RC=$?
check "VAL-101j garbage stdin: exit 0" $RC
printf '%s' "$OUT" | node -e "JSON.parse(require('fs').readFileSync(0,'utf-8'))"
check "VAL-101j garbage stdin: stdout valid JSON (got: ${OUT:0:80})" $?

# --- VAL-101k: missing transcript path -> graceful message, exit 0 ---
OUT=$(printf '{"trigger":"auto","session_id":"s","transcript_path":"%s"}' "$WORK/nope.jsonl" \
      | CLAUDE_PROJECT_DIR="$WORK/proj4" node "$HOOK")
RC=$?
check "VAL-101k missing transcript: exit 0" $RC
printf '%s' "$OUT" | node -e "const d=JSON.parse(require('fs').readFileSync(0,'utf-8')); if(d.continue!==true)process.exit(1)"
check "VAL-101k missing transcript: continue:true" $?

# --- VAL-101l: project without thoughts/shared/handoffs -> ~/.claude/handoffs, repo untouched ---
PROJ5="$WORK/userrepo"
mkdir -p "$PROJ5"
run_hook "$FIXTURE" "$PROJ5"
[ ! -e "$PROJ5/thoughts" ]
check "VAL-101l non-opted-in project: no thoughts/ written into the repo" $?
H5=$(find "$FAKEHOME/.claude/handoffs/userrepo" -name 'auto-handoff-*.md' 2>/dev/null | head -1)
[ -n "$H5" ] && grep -q "Placeholder goal two" "$H5"
check "VAL-101l handoff written under ~/.claude/handoffs/<project>/" $?
printf '%s' "$OUT" | grep -q "handoffs/userrepo/"
check "VAL-101l systemMessage names the real location (got: ${OUT:0:120})" $?

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
