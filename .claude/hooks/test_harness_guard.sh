#!/usr/bin/env bash
# Tests for .claude/hooks/harness-guard.mjs (PreToolUse): deny writes to manifest-listed
# installed harness files and save a Proposal to ~/.claude/harness-inbox/.
#   file tools (Write/Edit/MultiEdit/NotebookEdit) and Bash/PowerShell write commands;
#   path spellings: canonical, ~, $HOME, ${HOME}, $env:USERPROFILE, relative to cwd, and on
#   win32 backslashes, case, /c/ Git Bash form, 8.3 short names (HOME or target side);
#   allowed: reads, non-managed ~/.claude paths, kept entries, heredoc bodies, no manifest;
#   fail-open on malformed stdin / malformed manifest (logged to fleet/guard-errors.log);
#   m2 fixes: PowerShell common params/aliases/prefixes, # and <# #> comments, [[ ]] / (( ))
#   comparisons, cd/pushd/Set-Location tracking, no fs call on UNC targets (fs probe),
#   node:crypto ids, 256 KiB change cap, command redaction, deny when the proposal save fails.
# Ends with a latency bench (p50 of 20 runs, printed, not asserted).
# Temp HOME/USERPROFILE only; the real ~/.claude is never touched.
# Self-contained; run from anywhere: bash .claude/hooks/test_harness_guard.sh
set -u

HOOK="$(cd "$(dirname "$0")" && pwd)/harness-guard.mjs"
PLAT="$(node -p process.platform)"
TMPN="$(node -e "console.log(require('os').tmpdir().replace(/\\\\/g,'/'))")"
WORK_S="$TMPN/hg_test_$$"
mkdir -p "$WORK_S"
trap 'rm -rf "$WORK_S"' EXIT
# Long (realpath) form; on this Windows host tmpdir is an 8.3 short name, so WORK_S != WORK.
WORK="$(node -e "console.log(require('fs').realpathSync.native(process.argv[1]).replace(/\\\\/g,'/'))" "$WORK_S")"
ERRFILE="$WORK/err.txt"

PASS=0
FAIL=0
check() { # <name> <condition result: 0 ok>
  if [ "$2" -eq 0 ]; then PASS=$((PASS+1)); echo "PASS: $1";
  else FAIL=$((FAIL+1)); echo "FAIL: $1"; echo "  OUT: $OUT"; echo "  ERR: $ERR"; fi
}
skip() { echo "SKIP: $1"; }

winpath() { printf '%s' "$1" | sed 's#/#\\#g'; }
jesc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | awk 'BEGIN{ORS=""} NR>1{print "\\n"} {print}'; }

HOMEL="$WORK/home"
CL="$HOMEL/.claude"
REPO="$WORK/repo"
mkdir -p "$CL/hooks" "$CL/skills/review" "$CL/projects/p1/memory" "$CL/skills/nb" "$REPO"
printf 'old\n' > "$CL/hooks/status.mjs"
printf 'mine\n' > "$CL/skills/review/SKILL.md"
printf '{}\n' > "$CL/skills/nb/demo.ipynb"
printf 'src\n' > "$WORK/status.mjs"
cat > "$CL/.ccv47-manifest.json" <<EOF
{"schema_version": 1, "generated_at": "2026-10-07T12:00:00Z", "repo": "$REPO", "head_sha": "abc1234",
 "dirty": false, "eol": "lf", "files": {
  "hooks/status.mjs": {"repo_path": ".claude/hooks/status.mjs", "sha256": "00", "kept": false},
  "hooks/new-hook.mjs": {"repo_path": ".claude/hooks/new-hook.mjs", "sha256": "01", "kept": false},
  "skills/nb/demo.ipynb": {"repo_path": "harness/skills/nb/demo.ipynb", "sha256": "02", "kept": false},
  "skills/review/SKILL.md": {"repo_path": "harness/skills/review/SKILL.md", "sha256": "03", "kept": true}}}
EOF

if [ "$PLAT" = "win32" ]; then UP_DEFAULT="$(winpath "$HOMEL")"; else UP_DEFAULT="$HOMEL"; fi
RUN_HOME="$HOMEL"
RUN_UP="$UP_DEFAULT"
CWD="$WORK"

# run_hook <stdin payload> — sets OUT, ERR, RC (HOME/USERPROFILE from RUN_HOME/RUN_UP)
NODE_ARGS=()
run_hook() {
  OUT=$(printf '%s' "$1" | env HOME="$RUN_HOME" USERPROFILE="$RUN_UP" node "${NODE_ARGS[@]}" "$HOOK" 2>"$ERRFILE"); RC=$?
  ERR=$(cat "$ERRFILE" 2>/dev/null || true)
}
denied() { case "$OUT" in *'"permissionDecision":"deny"'*) [ $RC -eq 0 ];; *) return 1;; esac; }
allowed() { [ $RC -eq 0 ] && ! denied; }
file_payload() { # <tool> <path> [extra tool_input JSON members]
  local key=file_path; [ "$1" = NotebookEdit ] && key=notebook_path
  printf '{"hook_event_name":"PreToolUse","session_id":"sess-a1","cwd":"%s","tool_name":"%s","tool_input":{"%s":"%s"%s}}' \
    "$(jesc "$CWD")" "$1" "$key" "$(jesc "$2")" "${3:-}"
}
write_payload() { file_payload Write "$1" ',"content":"new body"'; }
shell_payload() { # <tool> <command>
  printf '{"hook_event_name":"PreToolUse","session_id":"sess-a1","cwd":"%s","tool_name":"%s","tool_input":{"command":"%s"}}' \
    "$(jesc "$CWD")" "$1" "$(jesc "$2")"
}
inbox_id() { printf '%s' "$OUT" | grep -o 'inbox id [A-Za-z0-9._-]*' | head -n 1 | sed 's/^inbox id //'; }
pval() { # <proposal file> <js expr over p>
  node -e "const p=JSON.parse(require('fs').readFileSync(process.argv[1],'utf8'));console.log(JSON.stringify($2))" "$1"
}

expect_deny() { # <name> <payload>
  run_hook "$2"
  denied; check "deny: $1" $?
}
expect_allow() { # <name> <payload>
  run_hook "$2"
  allowed; check "allow: $1" $?
}

MANAGED="$CL/hooks/status.mjs"

# --- 1. Write canonical path: deny, message names repo file + inbox id, proposal saved ---
run_hook "$(write_payload "$MANAGED")"
denied; check "deny: Write to managed installed file" $?
case "$OUT" in *".claude/hooks/status.mjs"*) r=0;; *) r=1;; esac; check "deny message names repo_path" $r
case "$OUT" in *"repo"*"status.mjs"*) r=0;; *) r=1;; esac; check "deny message names repo file" $r
ID="$(inbox_id)"
printf '%s' "$ID" | grep -Eq '^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$'; check "inbox id uses new_proposal_id format ($ID)" $?
PF="$CL/harness-inbox/$ID.json"
[ -f "$PF" ]; check "proposal file written at harness-inbox/<id>.json" $?
if [ -f "$PF" ]; then
  [ "$(pval "$PF" 'p.id')" = "\"$ID\"" ]; check "proposal id equals file stem" $?
  [ "$(pval "$PF" '[p.schema_version,p.kind,p.status]')" = '[1,"edit","pending"]' ]; check "proposal schema_version/kind/status" $?
  [ "$(pval "$PF" '[p.change.tool,p.change.content]')" = '["Write","new body"]' ]; check "proposal carries Write content" $?
  [ "$(pval "$PF" 'p.target.repo_path')" = '".claude/hooks/status.mjs"' ]; check "proposal target.repo_path" $?
  [ "$(pval "$PF" '[p.source.project,p.source.session_id]')" = "[\"$(basename "$CWD")\",\"sess-a1\"]" ]; check "proposal source project/session" $?
  pval "$PF" 'p.target.installed_path' | grep -qi 'hooks'; check "proposal target.installed_path set" $?
  pval "$PF" 'p.created_at' | grep -Eq '^"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"$'; check "proposal created_at ISO UTC" $?
fi
[ "$(cat "$MANAGED")" = "old" ]; check "guard never writes the managed file" $?

# --- 2. other file tools ---
run_hook "$(file_payload Edit "$MANAGED" ',"old_string":"old","new_string":"new","replace_all":true')"
denied; check "deny: Edit" $?
PF="$CL/harness-inbox/$(inbox_id).json"
[ "$(pval "$PF" '[p.change.tool,p.change.old_string,p.change.new_string,p.change.replace_all]')" = '["Edit","old","new",true]' ]
check "Edit proposal carries old/new/replace_all" $?
run_hook "$(file_payload MultiEdit "$MANAGED" ',"edits":[{"old_string":"a","new_string":"b"}]')"
denied; check "deny: MultiEdit" $?
PF="$CL/harness-inbox/$(inbox_id).json"
[ "$(pval "$PF" 'p.change.edits')" = '[{"old_string":"a","new_string":"b"}]' ]; check "MultiEdit proposal carries edits" $?
run_hook "$(file_payload NotebookEdit "$CL/skills/nb/demo.ipynb" ',"new_source":"print(1)","cell_id":"c1"')"
denied; check "deny: NotebookEdit (notebook_path)" $?
PF="$CL/harness-inbox/$(inbox_id).json"
[ "$(pval "$PF" '[p.change.tool,p.change.content,p.target.repo_path]')" = '["NotebookEdit","print(1)","harness/skills/nb/demo.ipynb"]' ]
check "NotebookEdit proposal carries new_source + repo_path" $?
expect_deny "Write to manifest-listed file that does not exist yet" "$(write_payload "$CL/hooks/new-hook.mjs")"
expect_deny "Write with ~ path" "$(write_payload "~/.claude/hooks/status.mjs")"

# --- 3. win32 path spellings ---
if [ "$PLAT" = "win32" ]; then
  expect_deny "backslash form" "$(write_payload "$(winpath "$MANAGED")")"
  LOWER="$(printf '%s' "$MANAGED" | tr '[:upper:]' '[:lower:]' | sed 's#/\.claude/#/.CLAUDE/#; s#status\.mjs$#STATUS.MJS#')"
  expect_deny "case-changed path ($LOWER)" "$(write_payload "$LOWER")"
  GB="$(printf '%s' "$MANAGED" | sed -E 's#^([A-Za-z]):#/\L\1#')"
  expect_deny "Git Bash /c/ form ($GB)" "$(write_payload "$GB")"
  expect_deny "Bash cp to /c/ form" "$(shell_payload Bash "cp status.mjs $GB")"
  if [ "$WORK_S" != "$WORK" ]; then
    SHORT="$WORK_S/home/.claude/hooks/status.mjs"
    expect_deny "8.3 short-name target ($SHORT)" "$(write_payload "$SHORT")"
    RUN_HOME="$WORK_S/home"; RUN_UP="$(winpath "$WORK_S/home")"
    expect_deny "8.3 short-name HOME, long target" "$(write_payload "$MANAGED")"
    expect_deny "8.3 short-name HOME, \$env:USERPROFILE command" "$(shell_payload PowerShell 'Set-Content $env:USERPROFILE\.claude\hooks\status.mjs x')"
    RUN_HOME="$HOMEL"; RUN_UP="$UP_DEFAULT"
  else
    skip "8.3 short names unavailable (tmpdir has no short form)"
  fi
else
  skip "win32-only spellings (backslash, case, /c/, 8.3)"
fi

# --- 4. Bash / PowerShell write commands ---
expect_deny "Bash > \$HOME" "$(shell_payload Bash 'echo hi > $HOME/.claude/hooks/status.mjs')"
expect_deny "Bash >> \"\${HOME}\"" "$(shell_payload Bash 'echo hi >> "${HOME}/.claude/hooks/status.mjs"')"
expect_deny "Bash 2> redirect" "$(shell_payload Bash 'node x.js 2>~/.claude/hooks/status.mjs')"
expect_deny "Bash cp to ~" "$(shell_payload Bash 'cp -f status.mjs ~/.claude/hooks/status.mjs')"
expect_deny "Bash mv into managed dir (dir dest)" "$(shell_payload Bash 'mv ./status.mjs ~/.claude/hooks/')"
expect_deny "Bash tee -a" "$(shell_payload Bash 'echo x | tee -a ~/.claude/hooks/status.mjs')"
expect_deny "Bash after && chain" "$(shell_payload Bash 'git status && cp status.mjs "$HOME/.claude/hooks/status.mjs"')"
CWD="$CL"
expect_deny "Bash relative target from cwd ~/.claude" "$(shell_payload Bash 'cp ../../status.mjs hooks/status.mjs')"
CWD="$WORK"
run_hook "$(shell_payload Bash "$(printf "cat > ~/.claude/hooks/status.mjs <<'EOF'\nbody\nEOF")")"
denied; check "deny: Bash heredoc into managed file" $?
PF="$CL/harness-inbox/$(inbox_id).json"
pval "$PF" '[p.change.tool,p.change.command]' | grep -q '"Bash","cat > ~/.claude/hooks/status.mjs'; check "Bash proposal carries the command" $?
expect_deny "PowerShell Set-Content -Path \$env:USERPROFILE\\" "$(shell_payload PowerShell 'Set-Content -Path $env:USERPROFILE\.claude\hooks\status.mjs -Value x')"
expect_deny "PowerShell pipe to Out-File \"\$env:USERPROFILE/\"" "$(shell_payload PowerShell "'x' | Out-File \"\$env:USERPROFILE/.claude/hooks/status.mjs\" -Encoding utf8")"
expect_deny "PowerShell Copy-Item -Destination ~" "$(shell_payload PowerShell 'Copy-Item status.mjs -Destination ~/.claude/hooks/status.mjs -Force')"
expect_deny "PowerShell Move-Item positional \$HOME" "$(shell_payload PowerShell 'Move-Item .\status.mjs $HOME\.claude\hooks\status.mjs')"
expect_deny "PowerShell Add-Content -LiteralPath" "$(shell_payload PowerShell "Add-Content -LiteralPath '~\\.claude\\hooks\\status.mjs' 'x'")"
expect_deny "PowerShell > redirect" "$(shell_payload PowerShell 'Get-Date > $env:USERPROFILE\.claude\hooks\status.mjs')"
expect_deny "PowerShell cp alias" "$(shell_payload PowerShell 'cp status.mjs ~\.claude\hooks\status.mjs')"

# --- 5. allowed ---
expect_allow "Read of managed file" "$(file_payload Read "$MANAGED")"
expect_allow "Write to non-managed ~/.claude/projects memory" "$(write_payload "$CL/projects/p1/memory/m.md")"
expect_allow "Write to non-managed new ~/.claude path" "$(write_payload "$CL/handoffs/x/h.md")"
expect_allow "Write to kept (user-owned) entry" "$(write_payload "$CL/skills/review/SKILL.md")"
expect_allow "Write to repo file" "$(write_payload "$REPO/.claude/hooks/status.mjs")"
expect_allow "Bash read of managed file" "$(shell_payload Bash 'cat ~/.claude/hooks/status.mjs')"
expect_allow "Bash cp from managed file" "$(shell_payload Bash 'cp ~/.claude/hooks/status.mjs ./copy.mjs')"
expect_allow "Bash sync_global (python, not a tool write)" "$(shell_payload Bash 'py -3.13 install/sync_global.py --apply')"
expect_allow "Bash fd dup + /dev/null" "$(shell_payload Bash 'ls ~/.claude/hooks/status.mjs 2>&1 > /dev/null')"
expect_allow "Bash heredoc body mentions managed path" "$(shell_payload Bash "$(printf "cat > out.txt <<'EOF'\necho > ~/.claude/hooks/status.mjs\nEOF")")"
expect_allow "PowerShell Get-Content of managed file" "$(shell_payload PowerShell 'Get-Content $env:USERPROFILE\.claude\hooks\status.mjs')"
expect_allow "PowerShell Copy-Item from managed file" "$(shell_payload PowerShell 'Copy-Item ~/.claude/hooks/status.mjs -Destination .\copy.mjs')"
expect_allow "Bash to unrelated file" "$(shell_payload Bash 'echo x > out.txt')"

# no manifest: allow everything
H2="$WORK/home2"; mkdir -p "$H2/.claude/hooks"; printf 'x\n' > "$H2/.claude/hooks/status.mjs"
RUN_HOME="$H2"; if [ "$PLAT" = "win32" ]; then RUN_UP="$(winpath "$H2")"; else RUN_UP="$H2"; fi
expect_allow "no manifest -> Write allowed" "$(write_payload "$H2/.claude/hooks/status.mjs")"
expect_allow "no manifest -> Bash allowed" "$(shell_payload Bash 'cp a ~/.claude/hooks/status.mjs')"
[ ! -e "$H2/.claude/harness-inbox" ]; check "no manifest -> no proposal written" $?

# malformed manifest: fail open + error logged
H3="$WORK/home3"; mkdir -p "$H3/.claude/hooks"; printf '{not json' > "$H3/.claude/.ccv47-manifest.json"
RUN_HOME="$H3"; if [ "$PLAT" = "win32" ]; then RUN_UP="$(winpath "$H3")"; else RUN_UP="$H3"; fi
expect_allow "malformed manifest -> allow" "$(write_payload "$H3/.claude/hooks/status.mjs")"
[ -s "$H3/.claude/fleet/guard-errors.log" ]; check "malformed manifest logged to fleet/guard-errors.log" $?
printf '{"files": [1, 2]}' > "$H3/.claude/.ccv47-manifest.json"
expect_allow "manifest files not an object -> allow" "$(write_payload "$H3/.claude/hooks/status.mjs")"
RUN_HOME="$HOMEL"; RUN_UP="$UP_DEFAULT"

# malformed stdin: allow, exit 0
expect_allow "malformed stdin (not JSON)" 'not json {'
expect_allow "empty stdin" ''
expect_allow "stdin JSON array" '[1,2]'
expect_allow "tool_input null" '{"tool_name":"Write","tool_input":null}'
expect_allow "file_path not a string" '{"tool_name":"Write","tool_input":{"file_path":42}}'
expect_allow "command not a string" '{"tool_name":"Bash","tool_input":{"command":{"x":1}}}'

# --- 6. m2 review fixes ---
# PowerShell common parameters / aliases / prefixes consume their values (sources stay sources)
expect_allow "PS Copy-Item -ErrorAction before source" "$(shell_payload PowerShell 'Copy-Item -ErrorAction Stop ~/.claude/hooks/status.mjs C:/tmp/x.mjs')"
expect_allow "PS Copy-Item -WarningAction before source" "$(shell_payload PowerShell 'Copy-Item -WarningAction Ignore ~/.claude/hooks/status.mjs .\x.mjs')"
expect_allow "PS cp -ea 0 before source" "$(shell_payload PowerShell 'cp -ea 0 ~/.claude/hooks/status.mjs .\x.mjs')"
expect_allow "PS Copy-Item -ToSession before source" "$(shell_payload PowerShell 'Copy-Item -ToSession $s ~/.claude/hooks/status.mjs C:/x.mjs')"
expect_allow "PS Copy-Item -FromSession before source" "$(shell_payload PowerShell 'Copy-Item -FromSession $s ~/.claude/hooks/status.mjs .\x.mjs')"
expect_allow "PS Copy-Item -InformationAction -Verbose -Debug -Force -Recurse -PassThru" "$(shell_payload PowerShell 'Copy-Item -InformationAction Continue -Verbose -Debug -Force -Recurse -PassThru ~/.claude/hooks/status.mjs .\x.mjs')"
expect_allow "PS Copy-Item -WhatIf -Confirm:\$false -Credential" "$(shell_payload PowerShell 'Copy-Item -WhatIf -Confirm:$false -Credential $c ~/.claude/hooks/status.mjs .\x.mjs')"
expect_allow "PS Copy-Item -Filter -Include -Exclude" "$(shell_payload PowerShell 'Copy-Item -Filter *.mjs -Include a* -Exclude b* ~/.claude/hooks/status.mjs .\x.mjs')"
expect_allow "PS Copy-Item -LP source -Destination" "$(shell_payload PowerShell 'Copy-Item -LP ~/.claude/hooks/status.mjs -Destination .\x.mjs')"
expect_allow "PS Copy-Item -PSPath source, positional dest" "$(shell_payload PowerShell 'Copy-Item -PSPath ~/.claude/hooks/status.mjs .\x.mjs')"
expect_allow "PS Set-Content -Va prefix consumes its value" "$(shell_payload PowerShell 'Set-Content -Va ~/.claude/hooks/status.mjs out.txt')"
expect_allow "PS Out-File -Enc prefix consumes its value" "$(shell_payload PowerShell "'x' | Out-File -Enc ~/.claude/hooks/status.mjs out.txt")"
expect_deny "PS Copy-Item -ErrorAction, managed destination" "$(shell_payload PowerShell 'Copy-Item -ErrorAction Stop .\status.mjs ~/.claude/hooks/status.mjs')"
expect_deny "PS Copy-Item -Destination before -Path" "$(shell_payload PowerShell 'Copy-Item -Destination ~/.claude/hooks/status.mjs -Path .\status.mjs -Verbose')"
expect_deny "PS Set-Content -Va value -Pat managed" "$(shell_payload PowerShell 'Set-Content -Va x -Pat ~/.claude/hooks/status.mjs')"
expect_allow "PS ambiguous -Pa (Path/PassThru) consumes its value" "$(shell_payload PowerShell 'Set-Content -Pa ~/.claude/hooks/status.mjs out.txt')"
expect_deny "PS Out-File -Encoding then -FilePath" "$(shell_payload PowerShell "'x' | Out-File -Encoding utf8 -FilePath ~/.claude/hooks/status.mjs")"

# unquoted # comments are not commands
expect_allow "Bash trailing # comment with redirect" "$(shell_payload Bash 'ls -la  # old way: cat x > ~/.claude/hooks/status.mjs')"
expect_allow "Bash comment line with >>" "$(shell_payload Bash "$(printf 'ls\n# next: echo x >> ~/.claude/hooks/status.mjs\necho done')")"
expect_deny "Bash apostrophe in comment does not hide next line" "$(shell_payload Bash "$(printf "ls # don't\necho x > ~/.claude/hooks/status.mjs")")"
expect_deny "Bash mid-word # is not a comment" "$(shell_payload Bash 'echo a#b > ~/.claude/hooks/status.mjs')"
expect_deny "Bash quoted # is not a comment" "$(shell_payload Bash 'echo "#" > ~/.claude/hooks/status.mjs')"
expect_allow "PS trailing # comment with redirect" "$(shell_payload PowerShell "Get-ChildItem  # old: 'x' > ~/.claude/hooks/status.mjs")"
expect_allow "PS <# #> block comment" "$(shell_payload PowerShell "$(printf "<# 'x' > ~/.claude/hooks/status.mjs\nmore #> Get-Date")")"
expect_deny "PS apostrophe in block comment does not hide redirect" "$(shell_payload PowerShell "Get-Date <# it's #> > ~/.claude/hooks/status.mjs")"

# [[ ]] / (( )) / test comparisons are not redirects
expect_allow "Bash [[ a > b ]] comparison" "$(shell_payload Bash '[[ $a > ~/.claude/hooks/status.mjs ]] && echo y')"
expect_allow "Bash [ a \\> b ] escaped comparison" "$(shell_payload Bash '[ "$a" \> ~/.claude/hooks/status.mjs ] && echo y')"
expect_allow "Bash test a '>' b quoted comparison" "$(shell_payload Bash "test \"\$a\" '>' ~/.claude/hooks/status.mjs")"
expect_deny "Bash redirect after [[ ]]" "$(shell_payload Bash '[[ -n $a ]] && echo y > ~/.claude/hooks/status.mjs')"
expect_deny "Bash redirect after (( ))" "$(shell_payload Bash '(( n > 1 )) && echo y > ~/.claude/hooks/status.mjs')"
CWD="$CL/hooks"
expect_allow "Bash (( n > status.mjs )) arithmetic" "$(shell_payload Bash '(( n > status.mjs )) && echo ok')"
expect_allow "Bash \$(( )) arithmetic" "$(shell_payload Bash 'echo $(( n > status.mjs ))')"
CWD="$WORK"

# cd / pushd / Set-Location within one command set the base for relative targets
CWD="$CL/hooks"
expect_allow "Bash cd elsewhere && cp a status.mjs" "$(shell_payload Bash "cd '$WORK' && cp a status.mjs")"
expect_allow "Bash cd \$UNKNOWN; relative target skipped" "$(shell_payload Bash 'cd $SOMEWHERE; cp a status.mjs')"
expect_allow "PS Set-Location -Path elsewhere; Set-Content" "$(shell_payload PowerShell "Set-Location -Path '$WORK'; Set-Content status.mjs x")"
expect_deny "Bash redirect on the cd segment uses the old cwd" "$(shell_payload Bash "cd '$WORK' > status.mjs")"
CWD="$WORK"
expect_deny "Bash cd ~/.claude && echo > hooks/status.mjs" "$(shell_payload Bash 'cd ~/.claude && echo x > hooks/status.mjs')"
expect_deny "Bash cd twice (cd ..)" "$(shell_payload Bash 'cd ~/.claude/hooks; cd ..; echo x > hooks/status.mjs')"
expect_deny "Bash pushd/popd" "$(shell_payload Bash "cd ~/.claude && pushd '$WORK' && popd && cp a hooks/status.mjs")"
expect_deny "PS Set-Location ~\\.claude; Out-File relative" "$(shell_payload PowerShell "Set-Location ~\\.claude; 'x' | Out-File hooks\\status.mjs")"
expect_deny "PS sl alias" "$(shell_payload PowerShell 'sl ~/.claude/hooks; Set-Content status.mjs x')"

# UNC / network targets are never stat'ed (fs probe preload logs every stat-family call)
FSLOG="$WORK/fs.log"
cat > "$WORK/fsprobe.cjs" <<'EOF'
const fs = require('fs');
const { syncBuiltinESMExports } = require('module');
const log = (n, a) => { try { fs.appendFileSync(process.env.FSLOG, `${n} ${String(a)}\n`); } catch {} };
for (const n of ['statSync', 'lstatSync', 'existsSync', 'accessSync', 'readdirSync', 'opendirSync', 'readFileSync']) {
  const orig = fs[n];
  fs[n] = function (p, ...r) { if (typeof p === 'string' || p instanceof URL) log(n, p); return orig.call(this, p, ...r); };
}
const rp = fs.realpathSync;
const rpn = rp.native;
fs.realpathSync = function (p, ...r) { log('realpathSync', p); return rp.call(this, p, ...r); };
fs.realpathSync.native = function (p, ...r) { log('realpathSync.native', p); return rpn.call(this, p, ...r); };
syncBuiltinESMExports();
EOF
export FSLOG
NODE_ARGS=(--require "$WORK/fsprobe.cjs")
: > "$FSLOG"
expect_deny "probe control: managed Write still denied under the fs probe" "$(write_payload "$MANAGED")"
grep -q 'realpathSync' "$FSLOG"; check "probe control: fs probe records local realpath calls" $?
: > "$FSLOG"
expect_allow "Write to UNC path" "$(write_payload '\\server.invalid\share\x.mjs')"
expect_allow "Write to \\\\?\\UNC path" "$(write_payload '\\?\UNC\server.invalid\share\x.mjs')"
expect_allow "Bash cp to //server/share" "$(shell_payload Bash 'cp x //server.invalid/share/y')"
expect_allow "PS Copy-Item to \\\\server\\share\\ (dir dest)" "$(shell_payload PowerShell 'Copy-Item a \\server.invalid\share\')"
expect_allow "PS Copy-Item to \\\\server\\share\\d (maybe-dir dest)" "$(shell_payload PowerShell 'Copy-Item status.mjs \\server.invalid\share\d')"
CWD='//server.invalid/share/proj'
expect_allow "UNC session cwd, relative targets" "$(shell_payload Bash 'echo x > out.txt; cp a b')"
CWD="$WORK"
if grep -Eq '^[A-Za-z.]+ ([\\/]{2})' "$FSLOG"; then r=1; echo "  UNC fs calls:"; grep -E '^[A-Za-z.]+ ([\\/]{2})' "$FSLOG" | sed 's/^/    /'; else r=0; fi
check "no fs call ever touches a UNC path" $r
NODE_ARGS=()

# proposal ids without global WebCrypto (Node 18 default): node:crypto import
cat > "$WORK/nocrypto.cjs" <<'EOF'
Object.defineProperty(globalThis, 'crypto', { value: undefined, configurable: true, writable: true });
EOF
NODE_ARGS=(--require "$WORK/nocrypto.cjs")
run_hook "$(write_payload "$MANAGED")"
denied; check "deny works with globalThis.crypto undefined" $?
printf '%s' "$(inbox_id)" | grep -Eq '^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$'; check "proposal id format without WebCrypto" $?
NODE_ARGS=()
grep -q "('node:crypto').randomBytes" "$HOOK" && ! grep -q 'globalThis.crypto' "$HOOK"; check "hook takes randomness from node:crypto, not globalThis.crypto" $?
grep -Eq 'Node(\.js)? 18' "$HOOK"; check "hook header documents minimum Node 18" $?

# proposal content capped at 256 KiB and marked truncated
BIG="$WORK/big.json"
run_file() { # <payload file> -> OUT, ERR, RC
  OUT=$(env HOME="$RUN_HOME" USERPROFILE="$RUN_UP" node "$HOOK" < "$1" 2>"$ERRFILE"); RC=$?; ERR=$(cat "$ERRFILE")
}
node -e '
const [cwd, file, out] = process.argv.slice(1);
require("fs").writeFileSync(out, JSON.stringify({ hook_event_name: "PreToolUse", session_id: "sess-a1", cwd, tool_name: "Write",
  tool_input: { file_path: file, content: "a".repeat(300 * 1024) } }));' "$WORK" "$MANAGED" "$BIG"
run_file "$BIG"
denied; check "deny: 300 KiB Write" $?
PF="$CL/harness-inbox/$(inbox_id).json"
[ "$(pval "$PF" '[p.change.content.length, p.change.truncated]')" = '[262144,true]' ]; check "Write content capped at 256 KiB, truncated: true" $?
node -e '
const [cwd, file, out] = process.argv.slice(1);
require("fs").writeFileSync(out, JSON.stringify({ hook_event_name: "PreToolUse", session_id: "sess-a1", cwd, tool_name: "MultiEdit",
  tool_input: { file_path: file, edits: [{ old_string: "o", new_string: "n".repeat(200 * 1024) }, { old_string: "p", new_string: "q".repeat(200 * 1024) }, { old_string: "r", new_string: "s" }] } }));' "$WORK" "$MANAGED" "$BIG"
run_file "$BIG"
denied; check "deny: large MultiEdit" $?
PF="$CL/harness-inbox/$(inbox_id).json"
[ "$(pval "$PF" 'p.change.edits.reduce((n,e)=>n+e.old_string.length+e.new_string.length,0) <= 262144 && p.change.truncated === true')" = 'true' ]
check "MultiEdit edits share the 256 KiB budget, truncated: true" $?
run_hook "$(write_payload "$MANAGED")"
PF="$CL/harness-inbox/$(inbox_id).json"
[ "$(pval "$PF" '"truncated" in p.change')" = 'false' ]; check "small change carries no truncated key" $?

# stored shell command: home path -> ~, privacy terms redacted; file content kept verbatim
export CCV_PRIVACY_TERMS=acmeproj
run_hook "$(shell_payload Bash "cp acmeproj.mjs '$HOMEL/.claude/hooks/status.mjs'")"
denied; check "deny: cp with home path + private term" $?
PF="$CL/harness-inbox/$(inbox_id).json"
C="$(pval "$PF" 'p.change.command')"
case "$C" in *acmeproj*|*"$HOMEL"*) r=1;; *"~/.claude/hooks/status.mjs"*) r=0;; *) r=1;; esac
check "stored command redacted (got $C)" $r
run_hook "$(file_payload Write "$MANAGED" ',"content":"acmeproj body"')"
PF="$CL/harness-inbox/$(inbox_id).json"
[ "$(pval "$PF" 'p.change.content')" = '"acmeproj body"' ]; check "Write content not redacted (needed to apply)" $?
unset CCV_PRIVACY_TERMS

# proposal save failure on a managed target: deny anyway, name the repo file, log the error
H4="$WORK/home4"; mkdir -p "$H4/.claude/hooks"; printf 'old\n' > "$H4/.claude/hooks/status.mjs"
cp "$CL/.ccv47-manifest.json" "$H4/.claude/.ccv47-manifest.json"
printf 'not a dir' > "$H4/.claude/harness-inbox"
RUN_HOME="$H4"; if [ "$PLAT" = "win32" ]; then RUN_UP="$(winpath "$H4")"; else RUN_UP="$H4"; fi
run_hook "$(write_payload "$H4/.claude/hooks/status.mjs")"
denied; check "deny: proposal save failure still denies" $?
case "$OUT" in *"could not be saved"*) r=0;; *) r=1;; esac; check "save-failure deny says the change could not be saved" $r
case "$OUT" in *".claude/hooks/status.mjs"*) r=0;; *) r=1;; esac; check "save-failure deny names the repo file" $r
grep -qi 'proposal save failed' "$H4/.claude/fleet/guard-errors.log" 2>/dev/null; check "save failure logged to guard-errors.log" $?
RUN_HOME="$HOMEL"; RUN_UP="$UP_DEFAULT"

# /upgrade-harness, as installed by sync_global, still points at the repo copy + sync step
REPO_ROOT="$(cd "$(dirname "$HOOK")/../.." && pwd)"
if command -v py >/dev/null 2>&1 && py -3.13 -c '' 2>/dev/null; then PY="py -3.13"; else PY=python3; fi
(cd "$REPO_ROOT" && $PY -c '
import sys
from pathlib import Path
from install.sync_global import render, rewrites
text = render(Path("harness/skills/upgrade-harness/SKILL.md"), rewrites("~/.claude", "py -3.13"), "\n").decode()
assert "<harness repo>/tools/ouros_harness.py" in text, "repo path missing"
assert "install/sync_global.py --apply" in text, "sync step missing"
assert "never the installed one" in text
')
check "upgrade-harness skill (installed render) says: edit the repo copy, then sync_global --apply" $?

# --- 7. latency bench: p50 of 20 runs per payload (printed, not asserted) ---
env HOME="$HOMEL" USERPROFILE="$UP_DEFAULT" node -e '
const { spawnSync } = require("child_process");
const [hook, cwd] = process.argv.slice(1);
const pl = (tool_name, tool_input) => JSON.stringify({ hook_event_name: "PreToolUse", session_id: "sess-a1", cwd, tool_name, tool_input });
const ms = (args, input) => {
  const s = process.hrtime.bigint();
  spawnSync(process.execPath, args, { input });
  return Number(process.hrtime.bigint() - s) / 1e6;
};
const med = (t) => { t.sort((a, b) => a - b); return (t[9] + t[10]) / 2; };
// Baseline and guard spawns interleaved so machine noise hits both alike.
const pair = (input) => {
  const b = [], g = [];
  for (let i = 0; i < 20; i++) { b.push(ms(["-e", "0"], input)); g.push(ms([hook], input)); }
  return [med(b), med(g)];
};
const rows = [
  ["Bash no write target", pl("Bash", { command: "git status --short" })],
  ["Bash write outside ~/.claude", pl("Bash", { command: "echo x > out.txt" })],
  ["PowerShell no write target", pl("PowerShell", { command: "Get-ChildItem" })],
  ["Write outside ~/.claude", pl("Write", { file_path: cwd + "/out.txt", content: "x" })],
];
for (const [name, input] of rows) {
  const [b, g] = pair(input);
  console.log(`BENCH p50 ${name}: guard ${g.toFixed(1)} ms, node -e 0 ${b.toFixed(1)} ms, guard work ~${(g - b).toFixed(1)} ms`);
}' "$HOOK" "$WORK"

echo ""
echo "harness-guard: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
