# Fleet data contract

Source of truth for every fleet file. Code: `tools/fleet/model.py` (stdlib dataclasses,
JSON round-trip). Node hooks that write these files (harness-guard, fleet-audit) follow
the same shapes.

## Locations

All fleet data lives under `~/.claude`, outside every repo. Nothing in this contract is
ever written to a tracked file; fixtures use synthetic values (`project-A`, `sess-a1`).

| File | Type | Writer | Helper |
|------|------|--------|--------|
| `~/.claude/fleet/state.json` | `FleetState` | collector (`fleet.py collect`), atomic replace | `state_path()`, `save_state`, `load_state` |
| `~/.claude/fleet/audit.jsonl` | `AuditEvent`, one per line | fleet-audit hook, append | `audit_path()`, `append_audit`, `read_audit` |
| `~/.claude/harness-inbox/<id>.json` | `Proposal` | harness-guard hook, lessons | `inbox_dir()`, `proposal_path(id)`, `save_proposal`, `load_proposal`, `list_proposals` |
| `~/.claude/fleet/guard-errors.log` | text | harness-guard (fail-open errors) | `guard_errors_path()` |
| `~/.claude/.ccv47-manifest.json` | install manifest (section below) | `install/sync_global.py --apply`, atomic replace | `manifest_path()` |

Home resolution (`home_dir()`): on win32 `USERPROFILE`, then `HOME`; elsewhere `HOME`,
then `USERPROFILE`; empty values are skipped; last resort `Path.home()`. This matches
Node's `os.homedir()` on Windows. Tests set both variables to a temp dir.

Proposal ids match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` (no separators, no leading dot)
and are not a Windows device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`,
`LPT1`-`LPT9`, any case, with or without an extension). `proposal_path` raises
`ValueError` otherwise and `load_proposal` returns `None`; `list_proposals` skips files
whose stored `id` is unsafe or differs from the filename stem. `new_proposal_id()` returns
`<YYYYmmddTHHMMSSZ>-<8 hex>`, which sorts by creation time.

Writers emit strict JSON: `NaN`/`Infinity` raise `ValueError` (Node's `JSON.parse`
rejects them).

## Parsing rules

- Every key is optional. A missing key, or a value of the wrong JSON type, takes the
  field default (`null`, `false`, `0`, `[]` or an empty object). `true`/`false` never count
  as numbers and numbers never count as booleans. Integral floats are accepted for ints.
- List items of the wrong shape are dropped; the rest of the list is kept.
- Unknown keys are tolerated: kept in the record's `extra` and written back unchanged
  (newer writers, undocumented Claude Code fields). `extra` itself never appears in JSON.
- Free strings: session `status` and `kind`, alert `kind`/`severity`, audit `category`,
  drift `status`. Known values are listed below but readers must accept others.
- Timestamps are ISO 8601 UTC strings (`now_iso()` writes `YYYY-MM-DDTHH:MM:SSZ`);
  values copied from Claude Code session files keep their source form.
- Paths inside records are absolute host paths as observed (they stay in `~/.claude`).

## FleetState (`state.json`)

| Field | Type | Meaning |
|-------|------|---------|
| `schema_version` | int | `1` |
| `generated_at` | str\|null | collection time |
| `harness` | Harness | repo vs installed harness |
| `sessions` | Session[] | one per `~/.claude/sessions/<pid>.json` |
| `inbox_count` | int | pending proposals in the inbox |
| `audit_recent` | AuditEvent[] | newest audit events |
| `collisions` | Collision[] | cross-session overlaps |
| `machine` | Machine | host memory |

**Harness**: `repo` (str\|null, repo root), `head_sha` (repo HEAD), `installed_sha`
(sha recorded at last sync), `drift` (DriftEntry[]).

**DriftEntry**: `installed_path`, `repo_path`, `expected_sha` (manifest), `actual_sha`
(file on disk), `status` (e.g. `modified`, `missing`, `stale`). All str\|null.

**Session**

| Field | Type | Source / meaning |
|-------|------|------------------|
| `pid` | int\|null | session file `pid` |
| `session_id` | str\|null | `sessionId` |
| `name` | str\|null | `name` |
| `project` | str\|null | basename of `cwd` |
| `cwd` | str\|null | `cwd` |
| `status` | str\|null | free string; observed `busy`, `idle`, `waiting`, `shell` |
| `kind` | str\|null | free string; observed `interactive`, `bg` |
| `version` | str\|null | Claude Code CLI version |
| `alive` | bool | pid running (and `procStart` matches when available) |
| `started_at` | str\|null | `startedAt` (epoch ms converted to ISO UTC) |
| `updated_at` | str\|null | `updatedAt` / `statusUpdatedAt` (epoch ms converted to ISO UTC) |
| `model` | str\|null | last assistant `message.model` in the transcript |
| `context_pct` | float\|null | last usage vs window (auto-handoff-stop.mjs rule) |
| `last_activity` | str\|null | newest transcript record time |
| `handoff` | Handoff\|null | newest handoff of the project |
| `agents_running` | int | spawned minus finished subagents |
| `alerts` | Alert[] | check findings for this session |

**Handoff**: `path` (str\|null), `age_h` (float\|null, hours), `goal`, `now` (str\|null).

**Alert**: `kind` (e.g. `collision`, `stuck`, `compliance`, `drift`, `schema_unknown`),
`severity` (e.g. `info`, `warn`, `error`), `session` (session id), `detail`, `evidence`
(`file:line` in the transcript). All str\|null.

**Collision**: `kind` (`path` or `repo`), `target` (path or repo root), `sessions`
(str[] of session ids), `detail`.

**Machine**: `mem_total_gb`, `mem_free_gb` (float\|null).

### Collector extras (`tools/fleet/collect.py`)

Keys the collector adds outside the declared fields (kept in `extra`):

| Record | Key | Meaning |
|--------|-----|---------|
| FleetState | `warnings` | str[]: unreadable session files, invalid manifest, failed audit rotation |
| FleetState | `collect_s` | float: collection time in seconds |
| Harness | `in_sync` | bool\|null: `head_sha == installed_sha` (null when either is unknown) |
| Harness | `dirty`, `synced_at` | manifest `dirty` and `generated_at` |
| Session | `transcript` | absolute path of the transcript read |
| Session | `status_updated_at`, `waiting_for` | `statusUpdatedAt` (ISO), `waitingFor` |

The collector reads only `sessions/<digits>.json` (never `*.key`), the last 512 KB of
each transcript and the last 256 KB of `audit.jsonl`, which it renames to
`audit.jsonl.1` once past 5 MB. Missing expected session keys or transcript fields
(`type`, assistant `message.usage`) add a `schema_unknown` alert.

### Checks (`tools/fleet/checks.py`)

`collect()` ends with `checks.run_checks(state, now)`, which re-reads each session's
`extra.transcript` tail (512 KB, absolute line numbers) plus subagent transcripts
under `<transcript stem>/` modified since that tail began, and fills `collisions`,
`harness.drift` and per-session `alerts`. Evidence is `<transcript path>:<line>`
(`<pid>.json` when a session has no transcript). Session files and `*.key` are never
opened; manifest keys that are absolute, contain `..`, sit under `sessions/` or end in
`.key` are skipped.

| Alert kind | Severity | Raised when |
|------------|----------|-------------|
| `collision` | `error` | 2+ alive sessions wrote the same absolute path (Write/Edit/MultiEdit/NotebookEdit `file_path`/`notebook_path`; case-insensitive on win32, `/c/` form accepted) within the last 30 min; also a `Collision` of kind `path` |
| `collision` | `warn` | 2+ alive sessions whose cwd lies in one git root (`.git` dir or file; separate worktrees differ; home is never a root); also a `Collision` of kind `repo` |
| `stuck` | `warn` | alive only: an AskUserQuestion without a result for > 20 min; else status `waiting` for > 20 min (`status_updated_at`, `waiting_for` in the detail); else the final completed turn ends in a question > 20 min old with `agents_running == 0` and status not `busy` |
| `stuck` | `warn` | alive only: the last 3+ tool calls are the same tool with the same input and every result is `is_error` |
| `stuck` | `warn` | alive only: an Agent/Task result (`toolUseResult` status/subtype/stop_reason, the result text or a `<task-notification>`) says it stopped at its turn limit, and the report path in its prompt (`"output": "...json"`, else the first `.../reports/<name>.json`) does not exist |
| `compliance` | `warn` | last main-chain assistant model does not start with `claude-opus-5-5` and no user prompt in the tail (system reminders stripped) names another model (`sonnet`, `haiku`, `opus 1-4`, `claude-...`, `/model`) |
| `compliance` | `info` | completed turns (closed by a user prompt, or the final turn when status is not `busy` and no tool call is pending) whose last block is text ending in `?` with no AskUserQuestion in the turn; one alert with the count, evidence = newest |
| `compliance` | `warn` | a write outside the session's own root (git root of cwd, else cwd) into another git root or another session's cwd; `~/.claude` excluded; one alert per foreign root |
| `drift` | `info` | alive session `started_at` before the last sync (max of manifest `generated_at` and the `.ccv47-installed` mtime) |

**Drift entries**: for every non-kept manifest file with a `sha256`, the sha256 of
`<manifest dir>/<key>` is compared to it: `modified` (differs) or `missing`. When the
manifest itself is stale (the first token of `<manifest dir>/.ccv47-installed` differs
from manifest `head_sha`, or the marker is more than 60 s newer than `generated_at`:
a `sync --apply` that failed before rewriting the manifest), `drift` holds one entry
instead: `status` `stale`, `installed_path` = the manifest, `expected_sha` = marker sha,
`actual_sha` = manifest `head_sha`, `extra.detail` starting `manifest stale`.

## Proposal (`harness-inbox/<id>.json`)

| Field | Type | Meaning |
|-------|------|---------|
| `schema_version` | int | `1` |
| `id` | str\|null | file stem; see id rule above |
| `created_at` | str\|null | creation time |
| `kind` | str | `edit` (redirected harness write) or `lesson`; default `edit` |
| `source` | {`project`, `session_id`, `cwd`} | session that produced it |
| `target` | {`installed_path`, `repo_path`} | installed file and its repo source |
| `change` | Change | the exact intended change |
| `reason` | str\|null | why (guard message or lesson rationale) |
| `status` | str | `pending`, `applied` or `rejected`; default `pending` |

**Change**: `tool` (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `Bash`, `PowerShell`,
`lesson`), then one of `content` (Write, lesson text), `old_string`/`new_string` (Edit)
or `command` (Bash/PowerShell). Unused members are `null`. Writers may add keys
(e.g. `replace_all`, a lesson `content_hash`); they survive in `extra`.

### Inbox commands (`tools/fleet/fleet.py`)

`apply`, `reject` and `show` refuse (exit 1, nothing written) unless the id is safe, the
file exists, the stored `id` equals the filename stem, `kind` is in `PROPOSAL_KINDS` and
`status` in `PROPOSAL_STATUSES` (checked on the raw JSON; a missing key means the
default). They rewrite the raw JSON in place (every other key kept), adding:

| Command | Requires | Sets |
|---------|----------|------|
| `apply <id>` | status `pending` | `status: applied`, `applied_at`, `applied_to` (repo-relative path) |
| `reject <id>` | status `pending` or `applied` | `status: rejected`, `rejected_at` (the file is never deleted: lesson dedupe reads it) |

`apply` resolves the harness repo from `--repo`, else manifest `repo`, else
`target.repo`; the repo must contain `.git`. An edit targets `target.repo_path` (or the
manifest `repo_path` of `target.installed_path`; refused when both exist and differ),
which must be repo-relative without `..`, resolve inside the repo, lie outside
`~/.claude` and not end in `.key`. Write replaces the content; Edit needs `old_string`
exactly once unless `replace_all`; MultiEdit applies `edits` in order, all or nothing;
NotebookEdit replaces, inserts after or deletes the cell `notebook.cell_id`
(`edit_mode`). A CRLF repo file gets CRLF strings. Bash/PowerShell proposals are refused.
A lesson appends `change.content` after a blank line to `--doc` (an existing repo file,
same path rules). `apply` never commits; `--dry-run` prints the diff only.

## AuditEvent (`audit.jsonl`)

One compact JSON object per line, appended. Readers skip blank or invalid lines.

| Field | Type | Meaning |
|-------|------|---------|
| `ts` | str\|null | event time |
| `project` | str\|null | basename of the session cwd |
| `session_id` | str\|null | session id |
| `category` | str\|null | e.g. `force-push`, `history-rewrite`, `hard-reset`, `recursive-delete`, `settings-edit`, `global-install` |
| `command` | str\|null | the command, secrets redacted |
| `tool` | str\|null | `Bash` or `PowerShell` |

## Example (synthetic)

```json
{
  "schema_version": 1,
  "generated_at": "2026-10-07T12:00:00Z",
  "harness": {"repo": "repo-root", "head_sha": "abc1234", "installed_sha": "def5678", "drift": []},
  "sessions": [{"pid": 4242, "session_id": "sess-a1", "project": "project-A", "status": "waiting",
                "kind": "interactive", "version": "2.1.292", "alive": true, "agents_running": 0,
                "handoff": null, "alerts": []}],
  "inbox_count": 0,
  "audit_recent": [],
  "collisions": [],
  "machine": {"mem_total_gb": 32.0, "mem_free_gb": 7.5}
}
```

## Install manifest (`~/.claude/.ccv47-manifest.json`)

Written by `install/sync_global.py --apply` at `<target>/.ccv47-manifest.json`
(`--target` given: that dir; default `~/.claude`, i.e. `manifest_path()`), after every
file write, by atomic replace (temp file in the same dir + `os.replace`). Rewritten on
every `--apply`, including an in-sync one. Dry runs never write it. It sits outside the
mapped subtrees, so the sync never plans, backs up or reports it as drift; a corrupt
manifest is simply replaced on the next `--apply`. No `Record` class: readers parse it as
plain JSON with the same tolerance rules.

| Field | Type | Meaning |
|-------|------|---------|
| `schema_version` | int | `1` |
| `generated_at` | str | write time, `YYYY-MM-DDTHH:MM:SSZ` |
| `repo` | str | absolute repo root the sync ran from |
| `head_sha` | str\|null | repo HEAD at sync time (null outside git) |
| `dirty` | bool | working tree had uncommitted changes |
| `eol` | str | `crlf` or `lf` (EOL the files were rendered with) |
| `files` | {installed_path: ManifestEntry} | every file the sync maps into the target |

Keys of `files` are install-relative posix paths (`hooks/status.mjs`), one per
installable repo file, kept or not.

**ManifestEntry**: `repo_path` (str, repo-relative posix path, e.g.
`.claude/hooks/status.mjs`), `sha256` (str\|null, hex sha256 of the installed bytes as
on disk after the sync; `.md` files carry the path rewrites and every text file the
chosen EOL, so this is not the repo file's hash; null when a kept path is absent),
`kept` (bool, path pinned in `<target>/.ccv47-keep`: not written by the sync, `sha256`
is the user's copy). Drift checks compare a file's current sha256 to `sha256`; kept
entries are user-owned and are not drift.

```json
{
  "schema_version": 1,
  "generated_at": "2026-10-07T12:00:00Z",
  "repo": "repo-root",
  "head_sha": "abc1234",
  "dirty": false,
  "eol": "crlf",
  "files": {
    "hooks/status.mjs": {"repo_path": ".claude/hooks/status.mjs", "sha256": "9f2c...", "kept": false},
    "skills/review/SKILL.md": {"repo_path": "harness/skills/review/SKILL.md", "sha256": null, "kept": true}
  }
}
```
