---
name: fleet
description: Fleet control - what every Claude Code session on this machine is doing (alerts, collisions, drift, audit) and the harness inbox of redirected edits and shared lessons - "fleet", "what are other projects doing", "harness inbox", "fleet inbox", "apply proposal", "reject proposal", "handoff all", "every session write a handoff", "fleet questions", "pending questions", "away mode", "I'm away", "I'm back", "open sessions", "open tabs", "open all projects"
user-invocable: true
allowed-tools: [Bash, Read, AskUserQuestion, ListAgents, SendMessage]
---

Read-mostly view over `~/.claude/fleet/state.json`, the proposal inbox
`~/.claude/harness-inbox/` and the question queue `~/.claude/fleet/questions/`. Every action goes through `fleet.py`; never edit inbox files,
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

## /fleet handoff-all

Asks every live Claude Code session on this machine to write a handoff, then checks which
ones landed. `fleet.py` cannot reach other sessions; the messages go out with ListAgents +
SendMessage from this session.

1. `handoffs --fresh` prints `since: <epoch>` and the newest handoff per live session. Keep
   the epoch: it is the baseline for step 4.
2. `ListAgents`. Targets: every peer session on this machine, interactive and `bg`, busy or
   idle. Skip this session, `cloud` rows (they cannot reply) and `offline` rows.
3. `SendMessage` to each target by its exact listed name (append ` [ref]` only for duplicate
   names). Do not interrupt: a message drains at the receiver's next tool round, so the text
   tells the session to finish its current step first. First line self-contained, e.g.:
   `Please write a handoff now with /create-handoff (requested by the user via /fleet handoff-all).`
   then: finish the current step first, list any open questions for the user as A-D options
   inside the handoff, then carry on as before. Do not tell sessions to stop or pause unless
   the user asked for that.
   Pass `notify_when_idle: true` so an idle notice arrives when each one finishes its turn.
4. After the idle notices (or when the user asks), `handoffs --since <epoch>`: `landed` means
   the session's own transcript wrote (a `file_path` tool input) a handoff newer than the
   baseline, so sessions sharing a project root are told apart; `prior` means an ended
   session of the same project wrote it (the session was restarted under a new id: its
   handoff exists, counted apart from landed); `shared` means the root has a
   new file but the session's transcript is missing (writer unknown); `waiting` means not
   yet. A handoff written through a shell command is not seen and reads `waiting`.
   Sessions waiting on their own question may not read the request until the user answers
   it. Report `N/M landed` and the waiting session names.
   A delivery notice saying a session held or refused the message counts as not sent.

## /fleet away on|off

`away on` creates `~/.claude/fleet/away`; `away off` removes it; `away` alone shows the mode.
While it is on, the ask-queue hook stops every session from opening a question box (a box
stops the session, and a stopped session cannot read messages): the questions go to that
session's queue, `~/.claude/fleet/questions/<session_id>.json`, and the session keeps
working and lists them in its reply. With it off, boxes open as usual. Tell the user which
mode is now set; on `on`, add that pending questions show on the dashboard and are answered
with `/fleet questions`.

## /fleet questions

1. `questions --json`: every pending question with `id`, `project`, `session_name`,
   `question`, `options`, `relay_text`. None -> say so and stop.
2. Ask them with AskUserQuestion, up to 4 per call, in order. Each question's text is its
   `relay_text` exactly (the `[fleet q-...]` marker lets the box open even in away mode),
   header = project (max 12 chars), options = its labels and descriptions as given (a
   question with more than 4 options: show the first 4; the user can still pick Other).
3. For each answer: `answer <id> "<answer>" --via fleet`. Exit 1 "already answered via
   session" means the user answered it in the asking session first; report that answer,
   relay nothing. Otherwise the output ends `relay to: <session name>`: SendMessage that
   session, first line `Answer to your queued question <id>: <answer>`, then the question
   text. `not live: kept in queue` -> no message; the project's next session reads it with
   `questions --all`.
4. Repeat until no pending question is left, then report how many were answered and relayed.

## /fleet open [project...]

Opens projects as tabs of ONE new Windows Terminal window, one PowerShell tab per project,
each a fresh `claude '/resume-handoff'` in the project folder. Tab title = the live session's
name, else the folder name (`--suppressApplicationTitle` keeps it).

1. With project names in the request, skip to step 3 with them as picks.
2. `open` lists the candidates, numbered: every project with a transcript in
   `~/.claude/projects`, a folder that still exists and a handoff to resume (home and temp
   folders skipped). Ask which to open with AskUserQuestion, `multiSelect: true`, 4 options
   per question, up to 4 questions (projects 1-4, 5-8, ...), header `Open 1/2` etc. More than
   16: list the rest in prose and let the user name them via Other. A folder not listed (a
   project with no session yet) can be named by path.
3. `open <pick>... --dry-run` shows the `wt` command; then `open <pick>...` (numbers, names or
   folder paths; `--all` for every listed project). It starts the window and returns; the new
   sessions are independent of this one. Exit 1 = unknown name, missing folder or no `wt.exe`.

## /fleet dashboard

`dashboard [out.html]` writes a self-contained HTML page (default
`~/.claude/fleet/dashboard.html`). Pending queued questions show first, as a notice. It contains private project names: keep it under
`~/.claude`, never write it into a repo. Open it for the user or Read the path back.
