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

It never touches `settings.json`, `CLAUDE.md`, `.env`, or `*.orig` backups. The hooks are
registered in `settings.json` once, by hand, using absolute `node "<home>/.claude/hooks/<hook>.mjs"`
commands. The FastEdit PreToolUse hook is left out because FastEdit targets Apple-Silicon MLX.

## Keeping up with upstream

```bash
git fetch upstream
git merge upstream/main                    # into main; then re-run sync_global.py
```

When an upstream PR is merged, its fix branch can be deleted; the merge into `main` is
then a no-op for those lines.
