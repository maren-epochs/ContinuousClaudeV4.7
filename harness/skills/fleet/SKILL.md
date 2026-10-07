---
name: fleet
description: Fleet control - what every Claude Code session on this machine is doing (alerts, collisions, drift, audit) and the harness inbox of redirected edits and shared lessons - "fleet", "what are other projects doing", "harness inbox", "fleet inbox", "apply proposal", "reject proposal"
user-invocable: true
allowed-tools: [Bash, Read, AskUserQuestion]
---

Read-mostly view over `~/.claude/fleet/state.json` plus the proposal inbox
`~/.claude/harness-inbox/`. Every action goes through `fleet.py`; never edit inbox files,
state files or installed harness files under `~/.claude` by hand (harness-guard denies those
writes and files them as proposals; that is what the inbox is for). Never open `*.key` files.

**Where it runs.** Bash state does not persist between calls, so open every command with this
prefix (repo copy when cwd is the ccv47 repo, else the installed copy):

```bash
F=tools/fleet/fleet.py; [ -f install/sync_global.py ] && [ -f "$F" ] || F="$HOME/.claude/tools/fleet/fleet.py"; py -3.13 "$F" <command>
```

Exit codes: 0 done, 1 refused (reason on stderr, nothing written), 2 usage error or dashboard
missing. Do not pipe output through `tail`/`head` (pipes mask the exit code).

## /fleet (no argument): status

Run `report --fresh` (collects first). It prints one row per live session (project, status,
model, context %, running agents, handoff age, alert count, last activity), then alerts
(error first, each with `file:line` evidence), collisions, harness drift, pending inbox and
recent audit events. Summarize for the user in this order: error alerts and path collisions,
stuck sessions, compliance warnings, drift (a `stale` manifest or `modified` installed files
means `py -3.13 install/sync_global.py --apply` is due; sessions started before the last sync
need a restart), inbox count, notable audit events (force-push, hard-reset, history rewrite).
Quote evidence paths; do not open transcripts unless the user asks.

## /fleet inbox

`inbox` lists pending proposals (`inbox --all` adds applied/rejected). `show <id>` prints one
in full: source session, target (installed path and repo path), reason, the exact change.

## /fleet apply <id>

The change lands in the harness REPO file (manifest `repo_path` under the manifest `repo`,
or `--repo <root>`), never the installed copy. `apply` never commits.

1. `show <id>`. Shell proposals (`Bash`/`PowerShell`) are refused by `apply`: describe the
   command, redo it by hand against the repo path only after the user approves, then
   `reject <id>` to clear it.
2. Check the target is clean: `git -C <repo> status --short -- <repo_path>`. Uncommitted
   changes there would be mixed into the commit; if dirty, ask before continuing.
3. Lesson proposals (`kind: lesson`) append to a repo doc you choose with the user. Ask with
   AskUserQuestion, recommended option first, picked by the lesson's subject: A the
   `harness/skills/<name>/SKILL.md` it concerns, B `harness/agents/worker.md` (worker
   behavior), C repo `CLAUDE.md` (harness-wide rule), D skip (leave pending). The global
   `~/.claude/CLAUDE.md` is private and not repo-managed; `apply` refuses it.
4. `apply <id> --dry-run` (add `--doc <path>` for lessons) prints the unified diff. Show it,
   then ask: A apply (Recommended: diff matches the reason), B show the full proposal,
   C reject, D leave pending.
5. `apply <id>` (`--doc <path>` for lessons). Refusals to relay, not work around: old_string
   not found (the repo file moved on, or the session saw an installed `.md` that carries sync
   path rewrites), old_string ambiguous without replace_all, target outside the repo or inside
   `~/.claude`, unknown kind/status, id mismatch, status not pending.
6. Run the tests for the changed file from the repo root, foreground: `.claude/hooks/<name>.mjs`
   -> `bash .claude/hooks/test_<name>.sh` when it exists; `tools/**.py` -> `py -3.13 -m pytest -q
   <its dir>`, `ruff check <file>`, `ruff format --check <file>`, `py -3.13 -m mypy`;
   `install/*.py` -> `py -3.13 -m pytest -q install`; skill/agent docs -> `py -3.13 -m pytest -q
   install` (sync rendering). Unsure -> `make test`.
7. Ask with AskUserQuestion: A commit (Recommended when tests pass) - `git -C <repo> add --
   <repo_path>` and a conventional commit naming the proposal id, that file only; B leave it
   uncommitted for review; C revert - `git -C <repo> restore -- <repo_path>` (delete it if
   `apply` created it), then `reject <id>`; D show `git -C <repo> diff -- <repo_path>`. When
   tests fail, recommend B or C instead of A.
8. After a commit tell the user to run, from the repo: `! py -3.13 install/sync_global.py --apply`
   (it writes `~/.claude`; the user runs it), then restart sessions that should load it.

## /fleet reject <id>

`reject <id>` sets `status: rejected` in place; the file stays, so the lesson dedupe never
proposes it again. Works on pending or applied proposals; refuses an already rejected one.

## /fleet lessons

`lessons [--source memory|bloks|all] [--max N]` (default memory, 10 per run) scans project
memories (and bloks rule/taste cards) for cross-project lessons, masks private terms, skips
ones already in the harness docs or already in the inbox, and files them as `kind: lesson`
proposals. `--dry-run` previews. Then walk the new ids through `/fleet apply` or `reject`.

## /fleet dashboard

`dashboard [out.html]` writes a self-contained HTML page (default
`~/.claude/fleet/dashboard.html`). It contains private project names: keep it under
`~/.claude`, never write it into a repo. Open it for the user or Read the path back.
