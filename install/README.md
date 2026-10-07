# Global install from this fork

This fork's `main` is upstream `main` plus every fix branch with a pending upstream PR
(#13, #14, #16, #17, #18, #19, #20), and a few fork-only fixes. It is the source of truth
for a global install in `~/.claude`.

```bash
git pull                                   # origin = this fork
py -3.13 install/sync_global.py --diff     # dry run: what would change
py -3.13 install/sync_global.py --apply    # write; backs up overwritten files
```

`sync_global.py` copies agents, hooks, skills, scripts and tools into `~/.claude`, and
rewrites the skills' repo-relative paths (`python tools/ouros_harness.py`,
`bash scripts/readiness.sh`) to absolute paths with a pinned interpreter. The pin is
`py -3.13` on Windows because ouros publishes wheels only up to cp313. Overwritten files are
backed up to `~/.claude/.ccv47-backup/<timestamp>/`. The installed commit is recorded in
`~/.claude/.ccv47-installed`.

Exit codes: a dry run exits `1` when the target is out of sync and `0` when in sync,
so the script is usable as a check in scripts/CI; `--apply` exits `0` on success.
Tested by `py -3.13 install/test_sync_global.py` (uses a temp `--target`, never `~/.claude`).

The dry run also reports two further drift classes, both read-only and both
informational — they never change the exit code:

- **Stale install record** — if `.ccv47-installed` disagrees with git `HEAD` it prints a
  `warn:` line (when files also drifted) or a `note:` line (when file contents are in sync
  and only the record is stale; re-running `--apply` refreshes it).
- **Unregistered hooks** — it parses `<target>/settings.json` (read-only; the file is still
  never written) and prints a `warn:` line for each repo `.claude/hooks/*.mjs` whose basename
  is not referenced under `hooks.*` or `statusLine` — a hook file that is installed but
  silently inert. A missing or unparseable `settings.json` produces one `note:` line and
  skips the check.

It never touches `settings.json`, `CLAUDE.md`, `.env`, or `*.orig` backups. The hooks are
registered in `settings.json` once, by hand, using absolute `node "<home>/.claude/hooks/<hook>.mjs"`
commands. The FastEdit PreToolUse hook is left out because FastEdit targets Apple-Silicon MLX.

The fleet hooks (harness-guard PreToolUse, fleet-audit PostToolUse) are registered by
`py -3.13 install/register_hooks.py` (user-run; `--dry-run` prints the diff, `--settings PATH`
targets another file). It backs up the file, never duplicates an entry, and registers both
hooks without `if` filters: a live check against Claude Code 2.1.293 showed `Write(~/...)`
does not fire for an 8.3 short-name spelling of the same path, while the CLI itself normalized
`/c/...`, `~`, forward slashes and case before matching.

### Hook spawn filtering (`if` conditions)

Verified against the [hooks reference](https://code.claude.com/docs/en/hooks): the `matcher`
field on tool events matches the **tool name only** (exact string, `|`/`,` lists, or regex) —
a matcher like `Read(*.py)` goes down the regex path against `tool_name` and never fires.
Tool-input filtering IS supported, but one level down: each hook handler takes an optional
`if` field holding exactly one [permission rule](https://code.claude.com/docs/en/permissions)
(no `&&`/`||`, so one handler per pattern). File rules use gitignore glob semantics, so
`"if": "Read(*.py)"` matches `.py` files at any depth, and `Edit` rules apply to all built-in
file-editing tools (Write, MultiEdit included). When no handler's `if` matches, the `node`
process never spawns.

The template `install/settings.template.json` uses this (it lives outside `.claude/` so a
checkout of this repo doesn't register every hook a second time on top of the global
install; the repo has no `.claude/settings.json` of its own since the unregistered FastEdit hook was removed).
The tldr-read handler is narrowed to the
23 code extensions in the hook's own `CODE_EXTENSIONS` set, and post-edit-diagnostics to the
9 extensions in its `ENABLED_EXTENSIONS` set. The in-hook extension early-exits stay as the
authoritative filter (they also handle the test-file and config bypasses the `if` rules can't
express). To apply the same filtering to your live `~/.claude/settings.json`, add the matching
`"if": "Read(*.<ext>)"` / `"if": "Edit(*.<ext>)"` handler entries by hand — one per extension,
copying the structure from the template but keeping your absolute `node` commands.
Requires a Claude Code version with handler `if` support (v2.1.214+ also fixed `src/**`-style
directory-pattern depth).

## tldr shim (optional, faster cold reads)

`tldr-read.mjs` pays a ~2s `tldr` CLI init on every cache-miss (cold) read. The
persistent shim `.claude/hooks/tldr-shim.mjs` owns one `tldr-mcp` process and serves
extracts over a localhost TCP endpoint (port file in `%TEMP%\tldr-shim.json`), cutting
cold reads to ~150-200ms. It is strictly opt-in and fails open: with no shim running the
hook behaves exactly as before.

```bash
node ~/.claude/hooks/tldr-shim.mjs start    # start detached; idle-exits after 10 min
node ~/.claude/hooks/tldr-shim.mjs status   # is it running?
node ~/.claude/hooks/tldr-shim.mjs stop     # shut it down
```

Environment knobs on the hook side:

- `TLDR_READ_SHIM=0` — never consult the shim, always spawn the CLI.
- `TLDR_READ_SHIM_AUTOSTART=1` — on a shim miss, launch the shim detached so the *next*
  cold read is fast (the current read still pays the CLI cost once). Off by default so
  the hook's test suite always exercises the CLI path.

No `settings.json` changes are required. Requires `tldr-mcp` on PATH or in `~/.cargo/bin`.

## SessionStart hook (optional)

`hooks/session-start.mjs` is installed but not registered. On `compact` it injects the newest
handoff from the handoff root (so work resumes from the auto-handoff pre-compact just wrote);
on `startup`/`clear` it injects `bloks context` (capped at 6000 chars, about 2K tokens per
session). Register compact-only for zero per-session cost, or `startup|clear|compact` for both:

```json
"SessionStart": [
  {"matcher": "compact",
   "hooks": [{"type": "command", "command": "node \"<home>/.claude/hooks/session-start.mjs\""}]}
]
```

`SESSION_START_CAP` changes the cap. Tests: `bash .claude/hooks/test_session_start.sh`.

## Visualization suite (optional)

`tools/viz/` and the `/visualize` skill run on host CPython (`py -3.13`), never inside the
ouros sandbox. Install the pinned stack and the headless browser once:

```bash
py -3.13 -m pip install -r tools/requirements-viz.txt
py -3.13 -m playwright install chromium
py -3.13 -m pip install --no-deps plotly-resampler==0.11.1   # declares plotly<7; works on 7.1.0
```

The repo-root `requirements.lock` pins the full transitive closure of `pyproject.toml`
(all groups, viz included); `py -3.13 -m pip install -r requirements.lock` can replace the
first line, and the `--no-deps` plotly-resampler step is still required (it is kept out of
the lock). `make setup` (or `pwsh install/setup.ps1 -Setup`) runs all three steps.
It installs the pre-commit hook only when none exists or its interpreter is gone; `FORCE_HOOK=1` / `-ForceHook` repoints it.
Regenerate the lock with `py -3.13 tools/lock_requirements.py`; `--check` exits 1 when stale.

- **Chrome for kaleido** (plotly static export): kaleido 1.x bundles no browser. It uses an
  installed Chrome, or fetch one with `py -3.13 -c "import kaleido; kaleido.get_chrome_sync()"`.
  If kaleido fails, `tools/viz/export.py` falls back to Playwright Chromium for PNG.
- **Playwright Chromium** renders every HTML output (bokeh, great_tables, artifact pages,
  holoviews via its bokeh backend) in `export.render_html`; pages signal readiness through
  `window.__chartsReady`.
- **bokeh is pinned to 3.9.2**: panel 1.9.4 fails on import with bokeh 3.10.0, which breaks
  holoviews/hvplot. Re-test before raising the pin.
- **Skill sync**: `/visualize` lives in `harness/skills/visualize/`; run
  `py -3.13 install/sync_global.py --apply` to install it into `~/.claude/skills/` alongside
  `tools/viz/`. Re-run it after every pull: outside this repo the skill loads the installed
  `~/.claude/tools/viz`, so a stale install keeps the old code.
- **Import name `ccv_viz`**: the skill's snippets open with a one-line PRELUDE that loads
  `tools/viz` (cwd if it is this repo, else `~/.claude`) as the package `ccv_viz`, then
  `from ccv_viz import palette, style, ...`. A user project's own `tools/` package would shadow
  `tools.viz`; `ccv_viz` leaves it untouched. Inside this repo `from tools.viz import ...`
  also works. Copy the PRELUDE verbatim from `harness/skills/visualize/SKILL.md`.

## Keeping up with upstream

```bash
git fetch upstream
git merge upstream/main                    # into main; then re-run sync_global.py
```

When an upstream PR is merged, its fix branch can be deleted; the merge into `main` is
then a no-op for those lines.
