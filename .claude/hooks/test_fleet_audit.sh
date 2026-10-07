#!/usr/bin/env bash
# Tests for .claude/hooks/fleet-audit.mjs (PostToolUse Bash|PowerShell)
#   each risky category appends one AuditEvent line to ~/.claude/fleet/audit.jsonl;
#   non-risky commands (incl. temp-dir deletes, venv installs) append nothing;
#   secrets / home paths / username / UUIDs / private terms are redacted;
#   more credential shapes (URL userinfo, curl -u, JSON/YAML, headers, token prefixes,
#   ConvertTo-SecureString, mysql -p<pass>); truncation before redaction; 100k-char
#   pathological inputs < 50 ms in-process; unreadable worktree .git keeps the event;
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

# credential shapes beyond KEY=value (m2 review); each line: <command><TAB><secret that must not survive>
# token prefixes are assembled at runtime so no secret-scanner shape lands in the repo
pfx() { printf '%s%s' "$1" "$2"; }
SKL="$(pfx sk_ live_)"; SKT="$(pfx sk_ test_)"; RKT="$(pfx rk_ test_)"; GLP="$(pfx glp at-)"
NPMP="$(pfx np m_)"; HFP="$(pfx h f_)"; XB="$(pfx xo xb-)"; XP="$(pfx xo xp-)"; XA="$(pfx xo xa-)"
while IFS="$(printf '\t')" read -r cmd leak; do
  [ -n "$cmd" ] || continue
  run "git push --force && $cmd"
  C="$(field command)"
  case "$C" in *"$leak"*|'') r=1;; *) r=0;; esac
  check "redacts: ${cmd:0:60} (got: ${C:18:70})" $r
done <<EOF
git clone https://${GLP}AbCdEfGhIjKlMnOpQrSt@gitlab.com/o/r.git	AbCdEfGhIjKl
git clone https://oauthTok12345678@github.com/o/r.git	oauthTok12345678
git remote add o https://deploy:Pw0rdSecret9@host.example/r	Pw0rdSecret9
curl -u admin:s3cretPw https://x.example	s3cretPw
curl --user admin:s3cretPw2 https://x.example	s3cretPw2
curl -uadmin:s3cretPw3 https://x.example	s3cretPw3
curl -d '{"token":"tokJson123"}' https://x.example	tokJson123
curl -d "{\"password\": \"pwJson456\"}" https://x.example	pwJson456
curl -d '{"client_secret": "csJson457", "api_key": "akJson458"}' https://x.example	csJson457
curl -d '{"client_secret": "csJson457", "api_key": "akJson458"}' https://x.example	akJson458
printf 'api_key: yamlKey789\n' > c.yml	yamlKey789
printf 'password: yamlPw790\n' > c.yml	yamlPw790
curl -H "x-api-key: hdrKey111" https://x.example	hdrKey111
curl -H 'Authorization: token ghAuth222' https://x.example	ghAuth222
curl -H "X-Auth-Key: authKey333" https://x.example	authKey333
curl -H "Private-Token: privTok334" https://x.example	privTok334
stripe ${SKL}AbCdEfGhIjKlMnOpQrStUvWx	AbCdEfGhIjKl
stripe ${SKT}ZyXwVuTsRqPoNmLkJiHg	ZyXwVuTsRq
stripe ${RKT}QwErTyUiOpAsDfGh	QwErTyUiOp
npm config set //r/:_authToken ${NPMP}AbCdEfGhIjKlMnOpQrStUvWxYz0123456789	AbCdEfGhIjKl
hf auth login --add ${HFP}AbCdEfGhIjKlMnOpQrStUvWxYz01234567	AbCdEfGhIjKl
slack ${XB}1234567890-abcdefghij	1234567890
slack ${XP}1234567890-abcdefghij	1234567890
slack ${XA}1234567890-abcdefghij	1234567890
\$p = ConvertTo-SecureString "PlainPw444" -AsPlainText -Force	PlainPw444
\$p = ConvertTo-SecureString -AsPlainText -Force -String 'PlainPw555'	PlainPw555
\$p = 'PlainPw666' | ConvertTo-SecureString -AsPlainText -Force	PlainPw666
\$p = ConvertTo-SecureString PlainPw667 -AsPlainText -Force	PlainPw667
mysql -u root -phunter2secret db	hunter2secret
mysqldump -pdumpPw777 shop	dumpPw777
mysql.exe -p'quotedPw778' shop	quotedPw778
EOF
run 'git push --force && mysql -p mydb && ssh://git@github.com/o/r && mkdir -p build'
case "$(field command)" in *"-p mydb"*"git@github.com"*"-p build"*) r=0;; *) r=1;; esac
check "non-attached -p and ssh git@ user kept (got: $(field command))" $r
run 'git push -u origin main && git push --force'
case "$(field command)" in *"-u origin main"*) r=0;; *) r=1;; esac
check "git push -u origin (no colon) not redacted" $r

# truncation happens before redaction, without leaking a secret that straddles the cut
PAD="$(node -e 'process.stdout.write("x".repeat(3990))')"
run "git push --force $PAD TOKEN=Zq9StraddleSecretValue0123456789abcdef"
C="$(field command)"
case "$C" in *Zq9*) r=1;; *) r=0;; esac; check "secret straddling the 4000-char cut is redacted" $r
[ "${#C}" -le 4003 ] && [ "${C: -3}" = "..." ]; check "long command truncated to 4000 chars + ... (len ${#C})" $?

# pathological 100k-char inputs: in-process audit work (categorize + truncate + redact), min of 3
# runs < 150 ms each. Load-tolerant (~25 ms idle) yet a quadratic regex regression takes 5-46 s.
node --input-type=module -e '
import { pathToFileURL } from "node:url";
const { auditCommand } = await import(pathToFileURL(process.argv[1]).href + "?lib");
const N = 100000;
const cases = {
  "dash run": "git push -f " + "-".repeat(N),
  "KEY run": "git push -f " + "KEY".repeat(N / 3),
  "token flag run": "git push -f --" + "token".repeat(N / 5),
  "unclosed redirect quotes": "git push -f && " + "x>\x27a ".repeat(N / 5),
  "open( run": "git push -f && node -e " + "open(".repeat(N / 5),
  "settings run": "git push -f && " + "settings".repeat(N / 8),
  "-fff run": "git push -" + "f".repeat(N) + "!",
  "sudo prefixes": "sudo ".repeat(N / 5) + "git push -f",
  "unclosed double quote": "git push -f \"" + "a".repeat(N),
  "assignments": "git push -f " + "a=b ".repeat(N / 4),
  ":// run": "git push -f " + "://".repeat(N / 3),
  "userinfo run": "git push -f https://" + "a:".repeat(N / 2),
  "SecureString run": "git push -f; " + "ConvertTo-SecureString ".repeat(N / 23),
  "mysql -p run": "git push -f; mysql " + "-p".repeat(N / 2),
  "json key run": "git push -f " + "\"token\":".repeat(N / 8),
  "header run": "git push -f -H " + "x-key-".repeat(N / 6),
  "word run": "git push -f " + "x".repeat(N),
  "rm path": "rm -rf " + "/a".repeat(N / 2),
  "settings redirects": "git push -f; " + "echo {} > settings.json; ".repeat(N / 25),
  "newlines": "git push -f\n".repeat(N / 12),
  "nested shells": "bash -c \"" + "bash -c ".repeat(N / 8) + "git push -f\"",
  "user run": "git push -f " + "zedtester".repeat(N / 9),
  "home path run": "git push -f " + "C:/Users/".repeat(N / 9),
};
let bad = 0;
for (const [name, cmd] of Object.entries(cases)) {
  let ms = Infinity, r;
  for (let i = 0; i < 3; i++) {
    const t0 = performance.now();
    r = auditCommand(cmd, "C:/work/project-A", { users: ["zedtester"], terms: ["acmeproj"] });
    ms = Math.min(ms, performance.now() - t0);
  }
  const ok = ms < 150 && (r.command === null || r.command.length <= 4003);
  if (!ok) bad++;
  console.log(`${ok ? "PASS" : "FAIL"}: 100k ${name}: ${ms.toFixed(1)} ms (len ${cmd.length}, cats ${r.cats.join(",") || "none"})`);
}
process.exit(bad ? 1 : 0);
' "$HOOK"
check "100k-char pathological inputs each finish < 150 ms in-process (min of 3)" $?
BIGP="$WORK/big.json" # via a file: a 100k argv exceeds the Windows command-line limit
node -e 'require("fs").writeFileSync(process.argv[1], JSON.stringify({session_id:"sess-a1",cwd:"C:/work/project-A",hook_event_name:"PostToolUse",tool_name:"Bash",tool_input:{command:"git push --force " + "-".repeat(100000)}}))' "$BIGP"
rm -f "$AUDIT"
S0=$(node -p 'Date.now()'); OUT=$(env USERNAME=zedtester node "$HOOK" < "$BIGP" 2>/dev/null); RC=$?; S1=$(node -p 'Date.now()')
[ "$RC" -eq 0 ] && [ -z "$OUT" ] && [ "$(cats)" = force-push ] && [ "$(field command | wc -c)" -le 4003 ]
check "100k-char command end to end: logged, truncated ($((S1 - S0)) ms incl. node spawn)" $?

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

# an unreadable worktree .git file must not drop the event (terms from it are just skipped)
WT="$WORK/wt"; mkdir -p "$WT"; printf 'gitdir: ../nowhere/.git/worktrees/wt\n' > "$WT/.git"
LOCKED=1
WTN="$WT"
if [ "$(node -p process.platform)" = win32 ]; then
  WTN="$(cd "$WT" && pwd -W)"; WTGIT="$WTN/.git"
  MSYS2_ARG_CONV_EXCL='*' icacls "$WTGIT" /deny "${USERNAME}:(R)" >/dev/null 2>&1 || LOCKED=0
  unlock() { MSYS2_ARG_CONV_EXCL='*' icacls "$WTGIT" /remove:d "${USERNAME}" >/dev/null 2>&1; }
else
  chmod 000 "$WT/.git"; unlock() { chmod 644 "$WT/.git"; }
fi
if [ "$LOCKED" -eq 1 ] && ! cat "$WT/.git" >/dev/null 2>&1; then
  rm -f "$AUDIT"
  run_raw "$(payload 'git push --force' Bash "$WTN")" USERNAME=zedtester
  [ "$RC" -eq 0 ] && [ "$(cats)" = force-push ]; check "unreadable worktree .git: event still logged" $?
else
  echo "SKIP: cannot make a file unreadable here (running as root?)"
fi
unlock

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
