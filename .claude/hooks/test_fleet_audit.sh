#!/usr/bin/env bash
# Tests for .claude/hooks/fleet-audit.mjs (PostToolUse Bash|PowerShell)
#   each risky category appends one AuditEvent line to ~/.claude/fleet/audit.jsonl;
#   non-risky commands (incl. temp-dir deletes, venv installs) append nothing;
#   secrets / home paths / username / UUIDs / private terms are redacted;
#   malformed stdin and write failures fail open (exit 0, no stdout);
#   every line loads with tools/fleet/model.py AuditEvent.from_dict.
# Self-contained; run from anywhere: bash .claude/hooks/test_fleet_audit.sh
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/fleet-audit.mjs"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export HOME="$WORK/home" USERPROFILE="$WORK/home"
mkdir -p "$HOME"
AUDIT="$HOME/.claude/fleet/audit.jsonl"
CWD="C:/work/project-A"
TMPN="$(node -e "console.log(require('os').tmpdir().replace(/\\\\/g,'/'))")"

PASS=0
FAIL=0
check() { # <name> <condition result: 0 ok>
  if [ "$2" -eq 0 ]; then PASS=$((PASS+1)); echo "PASS: $1";
  else FAIL=$((FAIL+1)); echo "FAIL: $1"; fi
}

payload() { # <command> [tool] [cwd]
  node -e 'process.stdout.write(JSON.stringify({session_id:"sess-a1",transcript_path:"",cwd:process.argv[3],hook_event_name:"PostToolUse",tool_name:process.argv[2],tool_input:{command:process.argv[1]},tool_response:{}}))' \
    "$1" "${2:-Bash}" "${3:-$CWD}"
}
run_raw() { # <stdin> [env...] -> OUT, RC
  OUT=$(printf '%s' "$1" | env "${@:2}" node "$HOOK" 2>/dev/null); RC=$?
}
run() { # <command> [tool] [cwd] -> OUT, RC (audit file reset first)
  rm -f "$AUDIT"
  run_raw "$(payload "$@")" USERNAME=zedtester USER=zedtester
}
cats() { # categories of the audit lines, comma-joined in file order; NONE when absent
  [ -f "$AUDIT" ] || { printf 'NONE'; return; }
  node -e 'const l=require("fs").readFileSync(process.argv[1],"utf8").split("\n").filter(Boolean);process.stdout.write(l.map(x=>JSON.parse(x).category).join(","))' "$AUDIT"
}
field() { # <name> -> value of the field on the first audit line
  node -e 'const l=require("fs").readFileSync(process.argv[1],"utf8").split("\n")[0];process.stdout.write(String(JSON.parse(l)[process.argv[2]]))' "$AUDIT" "$1" 2>/dev/null
}
expect() { # <name> <command> <expected categories|NONE> [tool] [cwd]
  run "$2" "${4:-Bash}" "${5:-$CWD}"
  local got; got="$(cats)"
  [ "$RC" -eq 0 ] && [ -z "$OUT" ] && [ "$got" = "$3" ]
  check "$1 (want $3, got $got, rc $RC)" $?
}

# --- risky categories ---
expect "force-push --force"            'git push --force origin main' force-push
expect "force-push -f"                 'git push -f' force-push
expect "force-push short cluster -uf"  'git push -uf origin main' force-push
expect "force-push with-lease, git -C" 'git -C ../repo push --force-with-lease' force-push
expect "force-push +refspec"           'git push origin +main' force-push
expect "history-rewrite filter-repo"   'git filter-repo --path secret.txt --invert-paths' history-rewrite
expect "history-rewrite filter-branch" "git filter-branch --tree-filter 'rm -f x' HEAD" history-rewrite
expect "hard-reset"                    'git reset --hard HEAD~1' hard-reset
expect "recursive-delete rm -rf rel"   'rm -rf build' recursive-delete
expect "recursive-delete rm -r -f abs" 'rm -r -f /c/work/out' recursive-delete
expect "recursive-delete rm -Rf abs"   'rm -Rf /srv/projects/old' recursive-delete
expect "~ and \$HOME expand (test HOME is temp)" 'rm -rf $HOME/x ~/y' NONE
expect "recursive-delete Remove-Item"  'Remove-Item -Recurse -Force C:\work\out' recursive-delete PowerShell
expect "recursive-delete rm -r -fo ps" 'rm -r -fo .\dist' recursive-delete PowerShell
expect "recursive-delete rd /s /q"     'cmd /c rd /s /q C:\work\out' recursive-delete
expect "recursive-delete temp ..escape" 'rm -rf /tmp/../work' recursive-delete
expect "settings-edit redirect"        'echo {} > ~/.claude/settings.json' settings-edit
expect "settings-edit sed -i"          "sed -i 's/a/b/' .claude/settings.local.json" settings-edit
expect "settings-edit Set-Content"     "Set-Content -Path \$HOME\\.claude\\settings.json -Value '{}'" settings-edit PowerShell
expect "settings-edit cp destination"  'cp new.json ~/.claude/settings.json' settings-edit
expect "settings-edit scripted write"  "node -e \"require('fs').writeFileSync('settings.json','{}')\"" settings-edit
expect "global-install pip"            'pip install requests' global-install
expect "global-install py -m pip"      'py -3.13 -m pip install --user rich' global-install
expect "global-install npm -g"         'npm install -g typescript' global-install
expect "global-install npm i --global" 'npm i --global pnpm' global-install
expect "global-install cargo install"  'cargo install ripgrep' global-install
expect "global-install winget"         'winget install Git.Git' global-install PowerShell
expect "global-install choco"          'choco install nodejs -y' global-install PowerShell
expect "nested bash -c"                'bash -c "git reset --hard"' hard-reset
expect "two categories, two lines"     'git reset --hard && git push --force' hard-reset,force-push
expect "sudo / env prefix"             'sudo FOO=1 pip3 install x' global-install

# --- non-risky: no line, no audit file ---
expect "plain ls"                      'ls -la' NONE
expect "git push (no force)"           'git push origin main' NONE
expect "git reset --soft"              'git reset --soft HEAD~1' NONE
expect "rm -f (not recursive)"         'rm -f file.txt' NONE
expect "rm -rf /tmp"                   'rm -rf /tmp/scratch' NONE
expect "rm -rf \$TMPDIR"               'rm -rf "$TMPDIR/x"' NONE
expect "rm -rf os tmpdir"              "rm -rf '$TMPN/x'" NONE
expect "rm -rf AppData Temp"           'rm -rf /c/Users/x/AppData/Local/Temp/run1' NONE
expect "Remove-Item \$env:TEMP"        'Remove-Item -Recurse -Force $env:TEMP\x' NONE PowerShell
expect "rm -rf rel, cwd in temp"       'rm -rf build' NONE Bash "$TMPN/proj"
expect "cat settings.json"             'cat ~/.claude/settings.json 2>/dev/null' NONE
expect "npm install (local)"           'npm install lodash' NONE
expect "venv pip by path"              '.venv/Scripts/pip install x' NONE
expect "venv activated first"          'source .venv/bin/activate && pip install x' NONE
expect "pip --target"                  'pip install --target vendor x' NONE
expect "pip freeze"                    'pip freeze' NONE
expect "grep mentions git push -f"     "grep -n 'push' notes.txt" NONE

# --- event shape ---
run 'git push --force' PowerShell
[ "$(field tool)" = PowerShell ]; check "tool recorded" $?
[ "$(field session_id)" = sess-a1 ]; check "session_id recorded" $?
[ "$(field project)" = project-A ]; check "project = basename of cwd" $?
field ts | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'; check "ts is ISO UTC" $?
[ "$(field command)" = 'git push --force' ]; check "command kept verbatim when clean" $?
[ "$(wc -l < "$AUDIT" | tr -d ' ')" = 1 ]; check "one line per event" $?

# second risky call appends rather than overwrites
run_raw "$(payload 'git reset --hard')" USERNAME=zedtester
G="$(cats)"; [ "$G" = force-push,hard-reset ]; check "appends to existing audit.jsonl (got $G)" $?

# Lines load with the Python data contract (no unknown keys, every field a string).
pick_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"
  elif command -v py >/dev/null 2>&1 && py -3.13 -c '' 2>/dev/null; then echo "py -3.13"
  elif command -v python3 >/dev/null 2>&1; then echo python3
  else echo python; fi
}
PY="$(pick_python)"
(cd "$REPO" && $PY -c '
import sys
from tools.fleet.model import AuditEvent, read_audit
from pathlib import Path
evs = read_audit(Path(sys.argv[1]))
assert len(evs) == 2, evs
for e in evs:
    assert not e.extra, e.extra
    d = e.to_dict()
    for k in ("ts", "project", "session_id", "category", "command", "tool"):
        assert isinstance(d[k], str) and d[k], (k, d)
    assert AuditEvent.from_dict(d) == e
' "$AUDIT")
check "lines load with AuditEvent.from_dict" $?

# --- redaction ---
U=Users
UUID="$(printf '%s-%s-%s-%s-%s' 1a2b3c4d 5e6f 7a8b 9c0d 1e2f3a4b5c6d)"
GHP="ghp_$(printf 'A%.0s' $(seq 1 36))"
mkdir -p "$HOME/.claude"
printf '# comment\nfilterterm\n' > "$HOME/.claude/privacy-terms"
CMD="EXA_API_KEY=abc123secret git push --force https://bob:hunter2pass@github.com/o/r.git C:/$U/alice/repo /c/$U/alice/x C--$U-alice-repo /home/zedtester/r $UUID $GHP --token tok999 -H 'Authorization: Bearer bear3rtok' acmeproj filterterm"
rm -f "$AUDIT"
run_raw "$(payload "$CMD")" USERNAME=zedtester CCV_PRIVACY_TERMS=acmeproj
C="$(field command)"
r=0; [ -n "$C" ] || r=1
for leak in abc123secret hunter2pass alice zedtester "$UUID" "$GHP" tok999 bear3rtok acmeproj filterterm; do
  case "$C" in *"$leak"*) r=1; echo "  leaked: $leak";; esac
done
check "secrets, home paths, username, uuid, terms redacted" $r
case "$C" in *"git push --force"*"<redacted>"*) r=0;; *) r=1;; esac
check "redacted command keeps its shape (got: ${C:0:90})" $r
[ "$(cats)" = force-push ]; check "redacted command still categorized" $?
echo "$C" | grep -q "C:/$U/<redacted>/repo"; check "home path keeps its prefix like privacy_guard" $?

# placeholder home names are not redacted (privacy_guard PLACEHOLDER_NAMES)
run "rm -rf C:/$U/user/old"
case "$(field command)" in *"C:/$U/user/old"*) r=0;; *) r=1;; esac
check "placeholder home name kept" $r

# --- fail open ---
rm -f "$AUDIT"
run_raw 'not json at all'
[ "$RC" -eq 0 ] && [ -z "$OUT" ] && [ ! -f "$AUDIT" ]; check "malformed JSON: exit 0, no line" $?
run_raw ''
[ "$RC" -eq 0 ] && [ -z "$OUT" ] && [ ! -f "$AUDIT" ]; check "empty stdin: exit 0, no line" $?
run_raw '[1,2,3]'
[ "$RC" -eq 0 ] && [ ! -f "$AUDIT" ]; check "JSON array: exit 0, no line" $?
run_raw '{"tool_name":"Bash","tool_input":{"command":42}}'
[ "$RC" -eq 0 ] && [ ! -f "$AUDIT" ]; check "non-string command: exit 0, no line" $?
run_raw '{"tool_name":"Bash"}'
[ "$RC" -eq 0 ] && [ ! -f "$AUDIT" ]; check "missing tool_input: exit 0, no line" $?
run_raw "$(payload 'git push --force' Write)"
[ "$RC" -eq 0 ] && [ ! -f "$AUDIT" ]; check "non-shell tool ignored" $?
run_raw '{"tool_name":"Bash","tool_input":{"command":"git reset --hard"}}'
[ "$RC" -eq 0 ] && [ "$(cats)" = hard-reset ]; check "missing session_id/cwd still logs" $?
rm -rf "$HOME/.claude/fleet"; printf 'x' > "$HOME/.claude/fleet"
run_raw "$(payload 'git push --force')"
[ "$RC" -eq 0 ] && [ -z "$OUT" ]; check "unwritable fleet dir: exit 0" $?
rm -f "$HOME/.claude/fleet"

echo
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
