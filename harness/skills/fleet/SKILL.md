---
name: fleet
description: Fleet control - what every Claude Code session on this machine is doing (alerts, collisions, drift, audit) and the harness inbox of redirected edits and shared lessons - "fleet", "what are other projects doing", "harness inbox", "fleet inbox", "apply proposal", "reject proposal", "handoff all", "every session write a handoff", "fleet questions", "pending questions", "away mode", "I'm away", "I'm back", "open sessions", "open tabs", "open all projects", "fleet digest", "daily summary", "denials", "what got blocked", "grant permission", "standing authorization", "fleet resume", "session limit", "resume after limit"
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
recent audit events, plus a machine line (free/total memory; a `memory` warn alert when free
memory is below 15% of total, or below `FLEET_MEM_WARN_GB` GB when set). Summarize for the
user in this order: error alerts and path collisions, stuck sessions, the memory alert (close
idle sessions or apps), compliance warnings, harness sync (`differs from repo (N files)` =
installed copies differ from what a sync of the repo working tree would write), drift (a `stale` manifest or `modified` installed files
means `py -3.13 install/sync_global.py --apply` is due; sessions started before the last sync
need a restart), inbox count, notable audit events (force-push, hard-reset, history rewrite).
Quote evidence paths; do not open transcripts unless the user asks.

The statusline shows the same counts as `fleet N live | M inbox | Q ask | K alert`: `ask` =
other live sessions waiting on a question box, alerts include the memory alert; a zero count
is omitted.

## /fleet digest

`digest [--fresh]` prints a plain-language markdown summary: machine memory and alert,
harness sync, drift, inbox, risky commands in the last 24 h, live sessions needing attention
first (waiting question, then warn/error alerts), and a "needs you" list. `--json` prints the
same data; `--write` saves `~/.claude/fleet/digests/YYYY-MM-DD.md` (local date) and prints
the path. `collect` (also the Stop hook's background run) writes that day's file when it is
absent. A failed daily write never changes `collect`'s exit code or output: its masked
reason goes to `~/.claude/fleet/digest.err`, which the next successful run (file written, or
that day's file already there) deletes. While it exists, `report`, `digest` and the dashboard add one line
`digest: last daily write failed (<reason>)` (`digest --json`/`--write`: on stderr); relay
it. Private terms, home paths and session ids are masked. Relay the "needs you" list first.

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
8. After a commit run `git -C <repo> push` and `py -3.13 install/sync_global.py --apply` yourself;
   hand the user a `!` line only if your own run is denied. Then restart sessions that should load it.

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
   Sessions with status `waiting` are stopped on their own question box and will not read
   the message until it is answered. Answer the box yourself (do not hand it to the user):
   `answer-box <tab> --dry-run` (tab = the Windows Terminal tab title, the project folder
   name) confirms a box is open, then `answer-box <tab>` types the default answer into its
   "Type something." option and submits it: write a handoff now with /create-handoff and put
   that question and every other open question in it as A-D options. Every key is sent only
   with the tab's window in front and each step read back from the screen; the user's
   previous tab and window are restored. Refusal (no box, tab not found or not unique, focus
   lost, screen mismatch) = nothing more typed; report it.
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
   relay nothing. Exit 4 "busy" = queue locked, nothing written: run it again. Otherwise the output ends `relay to: <session name>`: SendMessage that
   session, first line `Answer to your queued question <id>: <answer>`, then the question
   text. `not live: kept in queue` -> no message; the project's next session reads it with
   `questions --all`.
4. Repeat until no pending question is left, then report how many were answered and relayed.

## /fleet open [project...]

Opens projects as tabs of ONE new Windows Terminal window, one PowerShell tab per project,
each a fresh `claude '/resume-handoff'` in the project folder. Tab title = the project folder
name (`--suppressApplicationTitle` keeps it).

1. With project names in the request, skip to step 3 with them as picks.
2. `open` lists the candidates, numbered: every project with a transcript in
   `~/.claude/projects`, a folder that still exists and a handoff to resume (home and temp
   folders skipped). Ask which to open with AskUserQuestion, `multiSelect: true`, 4 options
   per question, up to 4 questions (projects 1-4, 5-8, ...), header `Open 1/2` etc. More than
   16: list the rest in prose and let the user name them via Other. A folder not listed (a
   project with no session yet) can be named by path.
   "Open all" in the request still goes through these boxes: the user picks, not `--all`.
3. `open <pick>... --dry-run` shows the `wt` commands; then `open <pick>...` (numbers, names or
   folder paths). `--stagger SECONDS` (default 3) opens one tab per `wt` call that many seconds
   apart: the first creates a uniquely named window (`wt -w fleet-<ms>`), the rest add tabs to
   it; `--stagger 0` = one chained `wt -w new` command. It starts the window and returns once
   the last tab is requested; the new sessions are independent of this one. Exit 1 = unknown name, missing folder or no `wt.exe`. `--all` without a terminal only
   lists (nothing opens); `--all --yes` opens every project, only when the user ticked all of
   them. In the user's own terminal, `open --all` lists them and asks which to open.

## /fleet resume

Restarts sessions the usage limit stopped ("You've hit your session limit · resets ...").
1. `limits --fresh --json`: every live session whose transcript ends on the limit message,
   with `resets` (Eastern) and `ready` (reset time passed). None -> say so and stop.
2. For each `ready` row: `SendMessage` to its `name` with the message `resume` (an idle
   session takes a message as its next turn), `notify_when_idle: true`. Do it yourself;
   never hand the user the typing.
3. Rows not yet ready: one `CronCreate` (recurring: false) at the earliest `resets` time +
   2 min with the prompt `/fleet resume`, so this session sends the rest when they reset.
4. Report who was resumed and when the rest will be.

## /fleet denials

`denials [--project NAME] [--since DAYS]` lists tool calls the permission system or the
auto-mode classifier refused, per project and (tool, reason), commands masked. For each
recurring, low-risk group (not credentials, live money, production data, force-push or
history rewrites) ask with AskUserQuestion whether to grant it, naming the exact rule.

## /fleet grant

`grant add '<exact rule>' --reason '...'` (or `--note '...'` for an auto-mode note, `--revoke '<rule>'` to remove an allow rule) queues an
agreed grant; refusals (blanket, interpreter wildcard, settings.json, destructive git) are
final - narrow the rule instead. `grant list` shows the queue, `grant drop <id>` removes one.
Claude cannot write `~/.claude/settings.json`: end the reply with the one batch line
`! py -3.13 ~/.claude/tools/fleet/fleet.py grant apply` for the user; never run `grant apply`
yourself.

## /fleet dashboard

`dashboard [out.html]` writes a self-contained HTML page (default
`~/.claude/fleet/dashboard.html`). Pending queued questions show first, as a notice. It contains private project names: keep it under
`~/.claude`, never write it into a repo. Open it for the user or Read the path back.
