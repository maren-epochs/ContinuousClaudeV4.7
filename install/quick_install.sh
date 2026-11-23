#!/usr/bin/env bash
# One-step install of the Continuous Claude v4.7 harness on macOS (or Linux).
# Run from an unzipped download or a clone:  bash install/quick_install.sh [--yes]
# Mirrors install/quick_install.ps1: Python 3.13 and Node 18+ (Homebrew on macOS),
# Python packages, Playwright Chromium, sync into ~/.claude, hooks + status line,
# shared profile. Idempotent: run it again to update.
# Flags: --yes (no prompts), --skip-deps, --skip-profile
set -euo pipefail

YES=0; SKIP_DEPS=0; SKIP_PROFILE=0
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;;
    --skip-deps) SKIP_DEPS=1 ;;
    --skip-profile) SKIP_PROFILE=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."

step() { printf '\n\033[36m== %s\033[0m\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }
ask() {
  [ "$YES" = 1 ] && return 0
  read -r -p "$1 [Y/n] " r </dev/tty || return 0
  case "$r" in [nN]*) return 1 ;; *) return 0 ;; esac
}
brew_install() {
  if ! have brew; then
    echo "$2 is missing and Homebrew is not installed." >&2
    echo "Install Homebrew from https://brew.sh (one command), then run this again." >&2
    exit 1
  fi
  ask "Install $2 with Homebrew?" || { echo "$2 is required." >&2; exit 1; }
  brew install "$1"
}

# Homebrew lives outside the default PATH on Apple Silicon when launched by double-click
for p in /opt/homebrew/bin /usr/local/bin; do
  [ -d "$p" ] && case ":$PATH:" in *":$p:"*) ;; *) PATH="$p:$PATH" ;; esac
done
export PATH

step 'Python 3.13'
PY=""
for c in python3.13 python3; do
  if have "$c" && "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  brew_install python@3.13 'Python 3.13'
  PY="$(brew --prefix python@3.13)/bin/python3.13"
fi
"$PY" --version
# Homebrew's Python refuses pip installs (PEP 668), so the harness gets its own venv;
# skills and hooks are installed pointing at this interpreter
VENV="$HOME/.claude/ccv47-venv"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
PY="$VENV/bin/python"
"$PY" -m pip install --quiet --upgrade pip

step 'Node.js 18+ (runs the hooks)'
node_ok() { have node && [ "$(node -p 'process.versions.node.split(".")[0]')" -ge 18 ]; }
node_ok || brew_install node 'Node.js'
echo "node $(node --version)"

step 'Claude Code'
if have claude; then claude --version
else echo "WARNING: Claude Code (claude) is not on PATH. Install it (https://claude.com/claude-code) before using the harness; the install below still works."
fi

if [ "$SKIP_DEPS" = 1 ]; then
  step 'Python packages: skipped (--skip-deps)'
else
  step 'Python packages'
  "$PY" install/setup_deps.py
  step 'Playwright Chromium (chart rendering for /visualize)'
  "$PY" -m playwright install chromium
fi

step 'Install into ~/.claude'
"$PY" install/sync_global.py --apply --python "$PY"

step 'Register hooks and status line in ~/.claude/settings.json'
"$PY" install/register_hooks.py --all

if [ "$SKIP_PROFILE" = 1 ]; then
  step 'Shared profile: skipped (--skip-profile)'
else
  step 'Shared profile (auto mode, Opus, working style)'
  echo "This also changes your own Claude Code settings, filling in only what you have not set yourself:"
  echo "  - auto mode: Claude runs most commands without asking first"
  echo "  - Opus, a large thinking budget and auto-continue after usage limits: usage is used up faster"
  echo "  - working-style rules added to ~/.claude/CLAUDE.md (e.g. propose three next steps, ask choices as A-D)"
  echo "Your files are backed up first. To undo, edit ~/.claude/settings.json and remove the marked block in ~/.claude/CLAUDE.md."
  "$PY" install/apply_profile.py
fi

step 'Check the install'
VERIFY_ARGS=()
[ "$SKIP_PROFILE" = 1 ] && VERIFY_ARGS+=(--skip-profile)
if ! "$PY" install/verify_install.py "${VERIFY_ARGS[@]+"${VERIFY_ARGS[@]}"}"; then
  exit 1
fi

step 'Done'
printf '\033[32m%s\033[0m\n' "Restart Claude Code (close every session), then run /bootup in any project.
Windows-only parts are inactive on macOS: /fleet open, close, screenshot, answer-box,
the memory guard and the desktop shortcut. Everything else works.
Optional extras (see README.md \"Dependencies\"): tldr, ouros and bloks on PATH;
EXA_API_KEY / NIA_API_KEY in a .env for web and docs search."
