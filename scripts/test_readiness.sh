#!/usr/bin/env bash
# test_readiness.sh — criterion-level checks for readiness.sh on tiny fixture repos.
# Covers: file_grep flag support (build_cmd_doc via README, case-insensitive) and
# the skills check counting harness/skills/*/SKILL.md; tech_debt scanning a copy
# of source minus test files (git-tracked, find fallback outside git).
#
# Usage: bash scripts/test_readiness.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
READINESS="$SCRIPT_DIR/readiness.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0
ok()  { echo "PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }

winpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else echo "$1"; fi; }

# criterion <fixture_dir> <criterion_id> -> prints numerator (1, 0, null) or ERR
criterion() {
  local dir="$1" id="$2" out="$1.json"
  READINESS_SKIP_SECURE=1 timeout 120 bash "$READINESS" "$dir" > "$out" 2>/dev/null || { echo "ERR"; return; }
  py -3.13 -c "import json,sys; d=json.load(open(sys.argv[1])); n=d['report'][sys.argv[2]]['numerator']; print('null' if n is None else n)" "$(winpath "$out")" "$id" 2>/dev/null || echo "ERR"
}

expect() { # <label> <fixture_dir> <criterion_id> <expected>
  local got
  got="$(criterion "$2" "$3")"
  if [[ "$got" == "$4" ]]; then ok "$1"; else bad "$1 (expected $4, got $got)"; fi
}

mkfix() { mkdir -p "$WORK/$1"; echo "$WORK/$1"; }

# ── build_cmd_doc: README path uses file_grep -i ───────────────
F=$(mkfix readme_install); printf '# Demo\n\nRun pip install to get started.\n' > "$F/README.md"
expect "build_cmd_doc passes for README mentioning install (no Makefile)" "$F" build_cmd_doc 1

F=$(mkfix readme_upper); printf '# Demo\n\nINSTALL: copy the files.\n' > "$F/README.md"
expect "build_cmd_doc README match is case-insensitive" "$F" build_cmd_doc 1

F=$(mkfix readme_none); printf '# Demo\n\nNothing here.\n' > "$F/README.md"
expect "build_cmd_doc fails for README without build words" "$F" build_cmd_doc 0

# ── file_grep without flags keeps its meaning ──────────────────
F=$(mkfix pyproject_ruff); printf '[tool.ruff]\nline-length = 100\n' > "$F/pyproject.toml"
expect "lint_config still passes via file_grep ruff pyproject.toml" "$F" lint_config 1

F=$(mkfix pyproject_none); printf '[project]\nname = "x"\n' > "$F/pyproject.toml"
expect "lint_config still fails without a linter" "$F" lint_config 0

# ── skills: harness/skills/*/SKILL.md counts ───────────────────
F=$(mkfix harness_skills); mkdir -p "$F/harness/skills/x"; printf -- '---\nname: x\n---\n' > "$F/harness/skills/x/SKILL.md"
expect "skills passes for harness/skills/x/SKILL.md" "$F" skills 1

F=$(mkfix claude_skills); mkdir -p "$F/.claude/skills/y"; printf -- '---\nname: y\n---\n' > "$F/.claude/skills/y/SKILL.md"
expect "skills still passes for .claude/skills" "$F" skills 1

F=$(mkfix no_skills); printf '# Demo\n' > "$F/README.md"
expect "skills fails with no skills" "$F" skills 0

F=$(mkfix harness_empty); mkdir -p "$F/harness/skills/z"
expect "skills fails for harness/skills without any SKILL.md" "$F" skills 0

# ── tech_debt: test files are out of the debt scan only (VAL-613) ──
# debt_field <fixture_dir> -> "<numerator> <ratio%>" or ERR
debt_field() {
  local dir="$1" out="$1.json"
  READINESS_SKIP_SECURE=1 timeout 180 bash "$READINESS" "$dir" > "$out" 2>/dev/null || { echo "ERR"; return; }
  py -3.13 -c "
import json, re, sys
c = json.load(open(sys.argv[1]))['report']['tech_debt']
m = re.search(r'Debt ratio ([0-9.]+)%', c['rationale'])
print(c['numerator'], m.group(1) if m else 'ERR')
" "$(winpath "$out")" 2>/dev/null || echo "ERR"
}

# Documented module: no debt on its own
write_documented() {
  printf '"""Small documented module."""\n\n\ndef add(a, b):\n    """Return a + b."""\n    return a + b\n' > "$1"
}
# <file> <prefix> <count>: undocumented, deeply nested functions (missing_docs + deep_nesting)
write_debt() {
  local i
  for i in $(seq 1 "$3"); do
    printf 'def %s%d(x):\n    if x:\n        for i in range(x):\n            if i:\n                while i:\n                    if i > 2:\n                        i -= 1\n                    i -= 1\n    return x\n\n\n' "$2" "$i"
  done > "$1"
}
# Test files in every excluded shape: test_*.py, *_test.py, conftest.py, tests/ dir
write_test_files() {
  mkdir -p "$1/tests"
  write_debt "$1/test_x.py" test_f 30
  write_debt "$1/x_test.py" check_f 10
  write_debt "$1/conftest.py" fixture_f 10
  write_debt "$1/tests/helpers.py" helper_f 10
}

[ -d "$HOME/.cargo/bin" ] && PATH="$PATH:$HOME/.cargo/bin"
if command -v tldr >/dev/null 2>&1 && command -v git >/dev/null 2>&1; then
  F=$(mkfix debt_git); write_documented "$F/mod.py"; write_test_files "$F"
  git -C "$F" init -q && git -C "$F" config core.autocrlf false && git -C "$F" add -A
  read -r N1 R1 <<< "$(debt_field "$F")"
  if [[ "$N1" == "1" ]]; then ok "tech_debt passes: undocumented test files excluded (git)"
  else bad "tech_debt passes: undocumented test files excluded (git) (got numerator=$N1 ratio=$R1)"; fi

  write_debt "$F/big.py" work_f 30; git -C "$F" add big.py
  read -r N2 R2 <<< "$(debt_field "$F")"
  if [[ "$R1" != ERR && "$R2" != ERR ]] && py -3.13 -c "import sys; sys.exit(0 if float(sys.argv[2]) > float(sys.argv[1]) else 1)" "$R1" "$R2"; then
    ok "tech_debt ratio rises with an undocumented non-test module ($R1% -> $R2%)"
  else bad "tech_debt ratio rises with an undocumented non-test module (got $R1% -> $R2%)"; fi
  if [[ "$N2" == "0" ]]; then ok "tech_debt fails with an undocumented non-test module"
  else bad "tech_debt fails with an undocumented non-test module (got numerator=$N2)"; fi

  F=$(mkfix debt_nogit); write_documented "$F/mod.py"; write_test_files "$F"
  read -r N3 R3 <<< "$(debt_field "$F")"
  if [[ "$N3" == "1" ]]; then ok "tech_debt passes: test files excluded (non-git find fallback)"
  else bad "tech_debt passes: test files excluded (non-git find fallback) (got numerator=$N3 ratio=$R3)"; fi
else
  echo "SKIP: tech_debt tests (tldr or git not on PATH)"
fi

echo ""
echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
