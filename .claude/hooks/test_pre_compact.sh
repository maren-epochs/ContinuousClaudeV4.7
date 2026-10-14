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

# =====================================================================
# VAL-803: auto-handoffs are redacted like tools/privacy_guard.py redact()
#   home prefix -> ~, OS username -> <user>, private terms -> <private:XXXXXXXX>
#   (keyed HMAC); shared JS module privacy-redact.mjs; byte parity with Python.
# Fixtures never hold a literal home path or UUID (pre-commit privacy guard):
# @U@ -> Users and @UUID@ -> a uuid are substituted at run time.
# =====================================================================
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
REPO="$(cygpath -m "$REPO" 2>/dev/null || echo "$REPO")"
LIB="$HERE/privacy-redact.mjs"
if py -3.13 -c "" > /dev/null 2>&1; then PY=(py -3.13); else PY=(python3); fi
UUIDV="0123abcd-$(printf '89ab')-cdef-0123-456789abcdef"
subst() { sed -e "s/@U@/Users/g" -e "s/@UUID@/$UUIDV/g" "$1" > "$2"; }

node --check "$LIB" 2>/dev/null
check "VAL-803a privacy-redact.mjs exists and passes node --check" $?
grep -q "import(new URL('./privacy-redact.mjs'" "$HERE/fleet-audit.mjs" \
  && ! grep -q "^import .*privacy-redact" "$HERE/fleet-audit.mjs" \
  && ! grep -q "^} from './privacy-redact.mjs'" "$HERE/fleet-audit.mjs"
check "VAL-803a fleet-audit.mjs loads privacy-redact.mjs lazily (dynamic import, no static import)" $?
grep -q '"privacy-redact.mjs"' "$REPO/install/sync_global.py"
check "VAL-803a sync_global lists privacy-redact.mjs as an installed non-event hook" $?

# --- parity: one fixture, fixed key, Python redact() vs JS redact() byte for byte ---
cat > "$WORK/parity.in" <<'EOF'
Home: C:/@U@/alice/Documents/acmeproj/src/main.py
Win: C:\@U@\alice\AppData\Local\Temp\x.txt
Escaped: "C:\\@U@\\alice\\repo" and c:\\\\@U@\\\\alice\\\\deep
Bash: cd /c/@U@/alice/work && ls; also x/c/@U@/alice stays
Transcript: C--@U@-alice-Documents-acmeproj
Placeholders: C:/@U@/<you>/repo and C:/@U@/x/repo and /c/@U@/USER/y stay
User: zedtester ran it; ZEDTESTER too; zedtesters and my_zedtester and zedtesterß stay
Terms: AcmeProj, ACMEPROJ, Zeta Widget and zeta widgetry, ſſh-box and SSH-BOX
Term with user: zedtester-labs owns it
Turkish: KİT and kıt and Kit and kit
Kept: <private:0123abcd> <PRIVATE:0123ABCD> and <user> stay
UUID: @UUID@ stays
Unicode: café naïve — résumé ﬅ
Spaced: C:\@U@\Alice Smith\Docs, "C:\\@U@\\ALICE SMITH\\x", /c/@U@/alice smith/y, C--@U@-Alice-Smith-Docs
Dotted: C:/@U@/a-b.c/x and C--@U@-a-b-c-Docs and C--@U@-a-b.c-z
Boundary: C:/@U@/Alice Smithers/p and C--@U@-a-b-cd-q
EOF
printf 'CRLF line zedtester\r\nlast acmeproj line without newline' >> "$WORK/parity.in"
subst "$WORK/parity.in" "$WORK/parity.txt"
"${PY[@]}" -c "import sys; open(sys.argv[1],'wb').write(bytes(range(32)))" "$WORK/parity.key"
TERMS_JSON='["acmeproj","Zeta Widget","zedtester-labs","ssh-box","kit"]'
cat > "$WORK/parity_py.py" <<'EOF'
import importlib.util, json, sys
repo, src, keyf, out, terms, users, homes = sys.argv[1:8]
spec = importlib.util.spec_from_file_location("pg", repo + "/tools/privacy_guard.py")
pg = importlib.util.module_from_spec(spec); sys.modules["pg"] = pg; spec.loader.exec_module(pg)
text = open(src, "rb").read().decode("utf-8")
g = pg.Guard(json.loads(users), [(t, "t") for t in json.loads(terms)], [], homes=json.loads(homes))
open(out, "wb").write(pg.redact(text, g, open(keyf, "rb").read()).encode("utf-8"))
EOF
cat > "$WORK/parity_js.mjs" <<'EOF'
import { readFileSync, writeFileSync } from 'fs';
import { pathToFileURL } from 'url';
const [lib, src, keyf, out, terms, users, homes] = process.argv.slice(2);
const { redact } = await import(pathToFileURL(lib).href);
const text = readFileSync(src, 'utf-8');
writeFileSync(out, redact(text, { users: JSON.parse(users), terms: JSON.parse(terms), key: readFileSync(keyf), homes: JSON.parse(homes) }), 'utf-8');
EOF
parity() { # <fixture> <out prefix> <terms json> <users json> <homes json> -> PY_RC, JS_RC
  "${PY[@]}" "$WORK/parity_py.py" "$REPO" "$1" "$WORK/parity.key" "$2.py.out" "$3" "$4" "$5"; PY_RC=$?
  node "$WORK/parity_js.mjs" "$LIB" "$1" "$WORK/parity.key" "$2.js.out" "$3" "$4" "$5" 2>/dev/null; JS_RC=$?
}
HOMES_JSON='["Alice Smith","a-b.c"]'
parity "$WORK/parity.txt" "$WORK/parity" "$TERMS_JSON" '["zedtester"]' "$HOMES_JSON"
check "VAL-803b python redact ran on the parity fixture" $PY_RC
check "VAL-803b JS redact ran on the parity fixture" $JS_RC
cmp -s "$WORK/parity.py.out" "$WORK/parity.js.out"
check "VAL-803b Python and JS redaction give identical bytes" $?
grep -q '^Home: ~/Documents/<private:[0-9a-f]\{8\}>/src' "$WORK/parity.js.out" \
  && grep -q '^User: <user> ran it; <user> too' "$WORK/parity.js.out" \
  && grep -q '^Win: ~\\AppData' "$WORK/parity.js.out" && ! grep -qi "acmeproj" "$WORK/parity.js.out"
check "VAL-803b parity output is actually redacted (home, user, terms)" $?
grep -q '^Spaced: ~\\Docs, "~\\\\x", ~/y, ~-Docs$' "$WORK/parity.js.out" \
  && grep -q '^Dotted: ~/x and ~-Docs and ~-z$' "$WORK/parity.js.out" \
  && grep -q '^Boundary: ~ Smithers/p and ~-b-cd-q$' "$WORK/parity.js.out"
check "F2 known home names with space/hyphen/dot consumed whole (JS)" $?
# the reviewer's case: the home name known only as a username, no homes list
printf 'C:\\%s\\Alice Smith\\Docs and C--%s-Alice-Smith-Docs\n' Users Users > "$WORK/spaced.txt"
parity "$WORK/spaced.txt" "$WORK/spaced" '[]' '["Alice Smith"]' '[]'
[ "$PY_RC" -eq 0 ] && [ "$JS_RC" -eq 0 ] && cmp -s "$WORK/spaced.py.out" "$WORK/spaced.js.out" \
  && [ "$(cat "$WORK/spaced.js.out")" = '~\Docs and ~-Docs' ]
r=$?; check "F2 username 'Alice Smith': Python and JS both give ~\\Docs and ~-Docs (got: $(cat "$WORK/spaced.js.out" 2>/dev/null))" $r
# JS default homes come from the environment (homeNames: os.homedir(), HOME, USERPROFILE)
node --input-type=module - "$LIB" <<'EOF' 2>/dev/null
import { pathToFileURL } from 'url';
const [lib] = process.argv.slice(2);
process.env.HOME = '/home/a-b.c'; process.env.USERPROFILE = 'D:\\' + 'Users\\Alice Smith\\';
const { redact, homeNames } = await import(pathToFileURL(lib).href);
const names = homeNames();
if (!names.includes('a-b.c') || !names.includes('Alice Smith')) process.exit(1);
const out = redact('C--' + 'Users-a-b-c-x C:/' + 'Users/Alice Smith/y', { users: [], terms: [] });
if (out !== '~-x ~/y') { console.error(out); process.exit(1); }
EOF
check "F2 JS homeNames() reads HOME/USERPROFILE and redact uses it by default" $?
# lazily created key: no term match -> no key file; JS token == Python placeholder_token
node --input-type=module - "$LIB" "$WORK/lazy/never-key" <<'EOF' 2>/dev/null
import { pathToFileURL } from 'url';
const [lib, keyf] = process.argv.slice(2);
process.env.CCV_PRIVACY_KEY_FILE = keyf;
const { redact } = await import(pathToFileURL(lib).href);
if (redact('nothing private here', { users: ['zedtester'], terms: ['acmeproj'] }) !== 'nothing private here') process.exit(1);
EOF
[ $? -eq 0 ] && [ ! -e "$WORK/lazy/never-key" ]
check "VAL-803b key file not created when no term matches" $?

# --- F1: atomic key creation, shared by Python and JS ---
cat > "$WORK/key_js.mjs" <<'EOF'
import { writeFileSync } from 'fs';
import { pathToFileURL } from 'url';
const [lib, keyf, out] = process.argv.slice(2);
const { loadOrCreateKey } = await import(pathToFileURL(lib).href);
try { writeFileSync(out, loadOrCreateKey(keyf).toString('hex')); }
catch (e) { writeFileSync(out, 'ERR ' + e.message); }
EOF
cat > "$WORK/key_py.py" <<'EOF'
import importlib.util, sys
repo, keyf, out = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("pg", repo + "/tools/privacy_guard.py")
pg = importlib.util.module_from_spec(spec); sys.modules["pg"] = pg; spec.loader.exec_module(pg)
try:
    res = pg.load_or_create_key(keyf).hex()
except Exception as e:
    res = "ERR " + str(e)
open(out, "w").write(res)
EOF
race() { # <key file> <out dir>: 6 node + 4 python creators at once
  mkdir -p "$2"
  for i in 1 2 3 4 5 6; do node "$WORK/key_js.mjs" "$LIB" "$1" "$2/js$i" & done
  for i in 1 2 3 4; do "${PY[@]}" "$WORK/key_py.py" "$REPO" "$1" "$2/py$i" & done
  wait
}
converged() { # <key file> <out dir>: every creator got the key on disk
  local want; want="$(od -An -tx1 -v "$1" | tr -d ' \n')"
  [ "${#want}" -eq 64 ] || return 1
  for f in "$2"/*; do [ "$(cat "$f")" = "$want" ] || { echo "  $f: $(cat "$f")"; return 1; }; done
  [ "$(ls "$2" | wc -l)" -eq 10 ]
}
race "$WORK/race1/privacy-key" "$WORK/race1.out"
converged "$WORK/race1/privacy-key" "$WORK/race1.out" && [ "$(ls -A "$WORK/race1")" = privacy-key ]
check "F1 10 concurrent Python+JS creators converge on one 32-byte key, no temp left" $?
mkdir -p "$WORK/race2"; : > "$WORK/race2/privacy-key"; touch -d '1 minute ago' "$WORK/race2/privacy-key"
race "$WORK/race2/privacy-key" "$WORK/race2.out"
converged "$WORK/race2/privacy-key" "$WORK/race2.out" && [ "$(ls -A "$WORK/race2")" = privacy-key ]
check "F1 stale 0-byte key: 10 concurrent repairers converge on one new key, no lock left" $?
printf 'short' > "$WORK/short-key"
node "$WORK/key_js.mjs" "$LIB" "$WORK/short-key" "$WORK/short.out" 2>/dev/null
grep -q '^ERR .*corrupt (5 bytes' "$WORK/short.out" && [ "$(cat "$WORK/short-key")" = short ]
r=$?; check "F1 JS: 1-31 byte key is a clear error and is left alone (got: $(cat "$WORK/short.out" 2>/dev/null))" $r
: > "$WORK/young-key"
( sleep 0.4; printf '0123456789abcdef0123456789abcdef' > "$WORK/young-key" ) &
node "$WORK/key_js.mjs" "$LIB" "$WORK/young-key" "$WORK/young.out" 2>/dev/null
wait
[ "$(cat "$WORK/young.out")" = "$(printf '0123456789abcdef0123456789abcdef' | od -An -tx1 -v | tr -d ' \n')" ]
check "F1 JS: fresh 0-byte key is waited on while its writer finishes" $?
# N1: an empty key (and its lock) dated 1 h in the future counts as stale (clock moved back)
for lang in js py; do
  d="$WORK/future-$lang"; mkdir -p "$d"
  : > "$d/privacy-key"; touch -d '1 hour' "$d/privacy-key"
  : > "$d/privacy-key.lock"; touch -d '1 hour' "$d/privacy-key.lock"
  t0=$(date +%s%N)
  if [ "$lang" = js ]; then node "$WORK/key_js.mjs" "$LIB" "$d/privacy-key" "$d.out" 2>/dev/null
  else "${PY[@]}" "$WORK/key_py.py" "$REPO" "$d/privacy-key" "$d.out" 2>/dev/null; fi
  ms=$(( ($(date +%s%N) - t0) / 1000000 ))
  [ "$ms" -lt 3000 ] && [ "$(wc -c < "$d/privacy-key")" -eq 32 ] && [ ! -e "$d/privacy-key.lock" ] \
    && [ "$(cat "$d.out")" = "$(od -An -tx1 -v "$d/privacy-key" | tr -d ' \n')" ]
  r=$?; check "N1 $lang: future-mtime 0-byte key + lock repaired in ${ms} ms (<3000), 32-byte key (got: $(head -c 80 "$d.out" 2>/dev/null))" $r
done

# --- end to end: the hook writes a redacted handoff ---
TR="$WORK/private.in.jsonl"
cat > "$TR" <<'EOF'
{"type":"user","isSidechain":false,"uuid":"p01","message":{"role":"user","content":"Fix acmeproj under C:/@U@/alice/work/acmeproj for zedtester and the Zeta Widget"}}
{"type":"assistant","isSidechain":false,"uuid":"p02","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_p1","name":"Edit","input":{"file_path":"C:\\@U@\\alice\\work\\acmeproj\\main.py","old_string":"a","new_string":"b"}}]}}
{"type":"assistant","isSidechain":false,"uuid":"p03","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_p2","name":"Bash","input":{"command":"cd /c/@U@/alice/work && zedtester-run acmeproj"}}]}}
{"type":"assistant","isSidechain":false,"uuid":"p04","message":{"role":"assistant","content":[{"type":"text","text":"Done with Zeta Widget for ZEDTESTER in C--@U@-alice-work."}]}}
EOF
subst "$TR" "$WORK/private.jsonl"
mkdir -p "$FAKEHOME/.claude"
printf '# user-level terms\nZeta Widget\n' > "$FAKEHOME/.claude/privacy-terms"
KEYF="$WORK/keys/nested/privacy-key"
run_private() { # <project> <key file> [CCV_PRIVACY_TERMS] -> OUT, RC
  OUT=$(printf '{"trigger":"auto","session_id":"redact-session-01","transcript_path":"%s"}' "$WORK/private.jsonl" \
        | CLAUDE_PROJECT_DIR="$1" CCV_PRIVACY_KEY_FILE="$2" CCV_PRIVACY_TERMS="${3-acmeproj}" \
          USERNAME=zedtester USER=zedtester node "$HOOK")
  RC=$?
}
PROJ6="$WORK/proj6"; mkdir -p "$PROJ6/thoughts/shared/handoffs"
run_private "$PROJ6" "$KEYF"
check "VAL-803c redacting hook exit 0" $RC
H6=$(find "$PROJ6/thoughts/shared/handoffs" -name 'auto-handoff-*.md' 2>/dev/null | head -1)
[ -n "$H6" ] && [ -f "$H6" ]
check "VAL-803c redacted handoff written" $?
H6="${H6:-$WORK/missing}"
! grep -qi "alice" "$H6" 2>/dev/null && grep -q "~/work/acmeproj\|~/work" "$H6" 2>/dev/null
check "VAL-803c home paths replaced by ~" $?
! grep -qi "zedtester " "$H6" 2>/dev/null && grep -q "<user>" "$H6" 2>/dev/null
check "VAL-803c OS username replaced by <user>" $?
! grep -qi "acmeproj\|zeta widget" "$H6" 2>/dev/null
check "VAL-803c env and ~/.claude/privacy-terms terms removed" $?
[ -f "$KEYF" ] && [ "$(wc -c < "$KEYF" | tr -d ' ')" = "32" ]
check "VAL-803c key created (parents too) with 32 bytes" $?
TOKS=$("${PY[@]}" - "$REPO" "$KEYF" <<'EOF' 2>/dev/null
import importlib.util, sys
spec = importlib.util.spec_from_file_location("pg", sys.argv[1] + "/tools/privacy_guard.py")
pg = importlib.util.module_from_spec(spec); sys.modules["pg"] = pg; spec.loader.exec_module(pg)
key = open(sys.argv[2], "rb").read()
print(pg.placeholder_token("acmeproj", key), pg.placeholder_token("Zeta Widget", key))
EOF
)
T1="${TOKS%% *}"; T2="${TOKS##* }"
[ -n "$TOKS" ] && grep -qF "$T1" "$H6" && grep -qF "$T2" "$H6"
check "VAL-803c tokens equal Python placeholder_token under the same key ($TOKS)" $?
# Python redact over the JS output changes nothing (same rules, idempotent)
cp "$H6" "$WORK/h6.copy"
printf 'acmeproj\n' > "$WORK/clone-terms"
HOME="$FAKEHOME" USERPROFILE="$FAKEHOME" CCV_PRIVACY_KEY_FILE="$KEYF" CCV_PRIVACY_TERMS= \
  "${PY[@]}" "$REPO/tools/privacy_guard.py" --redact --username zedtester \
  --terms-file "$WORK/clone-terms" --allow-file "$WORK/no-allow" "$WORK/h6.copy" 2>/dev/null
[ $? -eq 0 ] && cmp -s "$H6" "$WORK/h6.copy"
check "VAL-803c Python --redact leaves the hook's handoff byte-identical" $?
# clone terms: the project's .git/info/privacy-terms
PROJ7="$WORK/proj7"; mkdir -p "$PROJ7/.git/info" "$PROJ7/thoughts/shared/handoffs"
printf 'acmeproj\n' > "$PROJ7/.git/info/privacy-terms"
run_private "$PROJ7" "$KEYF" ""
H7=$(find "$PROJ7/thoughts/shared/handoffs" -name 'auto-handoff-*.md' 2>/dev/null | head -1)
[ -n "$H7" ] && ! grep -qi "acmeproj" "$H7" && grep -qF "$T1" "$H7"
check "VAL-803c project .git/info/privacy-terms is a term source" $?
# no term matches -> key never created
PROJ8="$WORK/proj8"; mkdir -p "$PROJ8/thoughts/shared/handoffs"
mv "$FAKEHOME/.claude/privacy-terms" "$WORK/user-terms.off"
run_private "$PROJ8" "$WORK/keys/unused-key" "nomatchterm"
H8=$(find "$PROJ8/thoughts/shared/handoffs" -name 'auto-handoff-*.md' 2>/dev/null | head -1)
[ "$RC" -eq 0 ] && [ -n "$H8" ] && [ ! -e "$WORK/keys/unused-key" ]
check "VAL-803d no matching term: handoff written, no key file created" $?
mv "$WORK/user-terms.off" "$FAKEHOME/.claude/privacy-terms"

# F1 end to end: a 0-byte key left by a killed creator no longer forces home-only mode
PROJ11="$WORK/proj11"; mkdir -p "$PROJ11/thoughts/shared/handoffs" "$WORK/keys11"
: > "$WORK/keys11/privacy-key"; touch -d '1 minute ago' "$WORK/keys11/privacy-key"
run_private "$PROJ11" "$WORK/keys11/privacy-key"
H11=$(find "$PROJ11/thoughts/shared/handoffs" -name 'auto-handoff-*.md' 2>/dev/null | head -1)
[ "$RC" -eq 0 ] && [ -n "$H11" ] && ! grep -qi "acmeproj" "$H11" && grep -q '<private:[0-9a-f]\{8\}>' "$H11" \
  && ! printf '%s' "$OUT" | grep -qi "home paths only" && [ "$(wc -c < "$WORK/keys11/privacy-key" | tr -d ' ')" = 32 ]
check "F1 hook: stale 0-byte key replaced, handoff fully redacted (got: ${OUT:0:160})" $?

# --- fail open: key unusable -> home paths only, hook never crashes ---
mkdir -p "$WORK/keydir"
PROJ9="$WORK/proj9"; mkdir -p "$PROJ9/thoughts/shared/handoffs"
run_private "$PROJ9" "$WORK/keydir"
check "VAL-803e key error: exit 0" $RC
printf '%s' "$OUT" | node -e "const d=JSON.parse(require('fs').readFileSync(0,'utf-8')); if(d.continue!==true)process.exit(1)"
check "VAL-803e key error: stdout valid hook JSON" $?
H9=$(find "$PROJ9/thoughts/shared/handoffs" -name 'auto-handoff-*.md' 2>/dev/null | head -1)
[ -n "$H9" ] && ! grep -qi "alice" "$H9" && grep -q "~/work" "$H9"
check "VAL-803e key error: handoff still written with home paths replaced" $?
printf '%s' "$OUT" | grep -qi "home paths only"
check "VAL-803e key error: systemMessage says redaction fell back (got: ${OUT:0:160})" $?
grep -qi "acmeproj" "$H9"
check "VAL-803e key error: only home paths were replaced (terms left, as specified)" $?
# module missing (partial install): the hook still runs and replaces the home dir
mkdir -p "$WORK/lonehook"; cp "$HOOK" "$WORK/lonehook/pre-compact.mjs"
printf '{"type":"user","isSidechain":false,"uuid":"q01","message":{"role":"user","content":"Look in %s/lone-dir please"}}\n' "$FAKEHOME" > "$WORK/lone.jsonl"
PROJ10="$WORK/proj10"; mkdir -p "$PROJ10/thoughts/shared/handoffs"
OUT=$(printf '{"trigger":"auto","session_id":"lone-session-01","transcript_path":"%s"}' "$WORK/lone.jsonl" \
      | CLAUDE_PROJECT_DIR="$PROJ10" node "$WORK/lonehook/pre-compact.mjs")
RC=$?
H10=$(find "$PROJ10/thoughts/shared/handoffs" -name 'auto-handoff-*.md' 2>/dev/null | head -1)
[ "$RC" -eq 0 ] && [ -n "$H10" ] && grep -q "Look in ~/lone-dir" "$H10" && ! grep -qF "$FAKEHOME" "$H10"
check "VAL-803e module missing: exit 0, handoff written with the home dir replaced" $?
printf '%s' "$OUT" | grep -qi "home paths only"
check "VAL-803e module missing: systemMessage says redaction fell back" $?

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
