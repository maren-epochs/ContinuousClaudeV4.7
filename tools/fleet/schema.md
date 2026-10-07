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
| `~/.claude/.ccv47-manifest.json` | install manifest | `install/sync_global.py` | `manifest_path()` |

Home resolution (`home_dir()`): on win32 `USERPROFILE`, then `HOME`; elsewhere `HOME`,
then `USERPROFILE`; empty values are skipped; last resort `Path.home()`. This matches
Node's `os.homedir()` on Windows. Tests set both variables to a temp dir.

Proposal ids match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` (no separators, no leading dot);
`proposal_path` raises `ValueError` otherwise. `new_proposal_id()` returns
`<YYYYmmddTHHMMSSZ>-<8 hex>`, which sorts by creation time.

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
| `started_at` | str\|null | `startedAt` |
| `updated_at` | str\|null | `updatedAt` / `statusUpdatedAt` |
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
