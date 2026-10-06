#!/usr/bin/env bash
# test_readiness.sh — criterion-level checks for readiness.sh on tiny fixture repos.
# Covers: file_grep flag support (build_cmd_doc via README, case-insensitive) and
# the skills check counting harness/skills/*/SKILL.md.
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

echo ""
echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
