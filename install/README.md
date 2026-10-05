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

The repo template `.claude/settings.json` uses this: the tldr-read handler is narrowed to the
23 code extensions in the hook's own `CODE_EXTENSIONS` set, and post-edit-diagnostics to the
9 extensions in its `ENABLED_EXTENSIONS` set. The in-hook extension early-exits stay as the
authoritative filter (they also handle the test-file and config bypasses the `if` rules can't
express). To apply the same filtering to your live `~/.claude/settings.json`, add the matching
`"if": "Read(*.<ext>)"` / `"if": "Edit(*.<ext>)"` handler entries by hand — one per extension,
copying the structure from the repo template but keeping your absolute `node` commands.
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

## Keeping up with upstream

```bash
git fetch upstream
git merge upstream/main                    # into main; then re-run sync_global.py
```

When an upstream PR is merged, its fix branch can be deleted; the merge into `main` is
then a no-op for those lines.
