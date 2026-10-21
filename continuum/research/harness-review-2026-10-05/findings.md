# harness-review-2026-10-05

Independent analyst review (read-only) at fork 6801b56 + bloks 79ace25, merged with parent-session verification. [V] verified, [I] inferred.

## Shipped same day (from this review's A-D)
- A report schema validation -> d5e0ff7 (13/15 historical reports fail)
- B status.mjs +45K double count -> 35ae8a4 (guard fired at ~62% real on 200K)
- C readiness fabricated pass on failed tldr -> c6ea729
- D ruff severity + Python313 path -> 24bf1a3

## Open, ranked
| # | Item | Cat | Evidence | Effort |
|---|---|---|---|---|
| 1 | ouros run_command: prefix allow-list + shell=True permits chaining (`git log & <cmd>`); read_allow "." = cwd exposes ~/.claude/.env when cwd is home; glob_files unchecked | security | ouros_harness.py:549-558, :607 [V parent] | M |
| 2 | Installed /analyze-data calls relative `py -3.13 tools/ouros_harness.py` -> broken outside repo; installer regex misses `py -3.13` prefix | capability | ~/.claude/skills/analyze-data/SKILL.md:30 [V parent] | S |
| 3 | Stop guard inactive when no statusline runs (-p, agent_call); compute pct from transcript_path last usage | reliability | auto-handoff-stop.mjs:33-36 [V code; -p behavior I] | S |
| 4 | User-level Read/PostToolUse hooks lack `if` filters (~115ms node spawn per non-code Read); fork project settings register same hooks under different command strings -> double fire | latency | timed [V]; dedupe per docs [V], runtime [I] | S |
| 5 | Worker frontmatter: Stop hook (-> SubagentStop) running validate_report.py; maxTurns/effort/model | reliability | worker.md:1-5 [V]; fields per docs [V] | S |
| 6 | Ruff fast path drops pyright type checking (header claims ruff + pyright) | capability | post-edit-diagnostics.mjs:31 [V] | S |
| 7 | SessionStart hook: bloks context once; on source=compact re-inject latest auto-handoff (pre-compact writes .md nothing reads back) | token/reliability | pre-compact.mjs:248-253, status.mjs:99 [V] | M |
| 8 | Auto-handoffs written into any project's cwd thoughts/ (risk of committing) | reliability | pre-compact.mjs:248 [I] | S |
| 9 | bloks: log view events on context injection; add CI | capability | db.rs:470 [V] | S |
| 10 | context: fork for review/premortem/research; per-skill effort vs global 31999 thinking | token | no skill sets context: [V] | S |
| 11 | Tests: status.mjs, ouros policy negatives; tldr-read returns permissionDecision allow for any >1.5KB code file anywhere | reliability/security | tldr-read.mjs:277/:328 [V]; deny-rule override [unverified] | M |
| 12 | readiness.sh depends on python3 WindowsApps alias; _call_agent spawns claude -p without --max-turns; oracle disallows WebSearch/WebFetch | reliability | [V/I mixed] | S |

## Lower / refuted
- J patch ceremony: partially refuted (patch fast path exists, autonomous/SKILL.md:33-42) — micro class still possible
- I tldr-mcp MCP registration: low value, shim covers it; adds tool-schema tokens
- Largest always-loaded token cost is ~330 plugin skills outside the harness (harness itself ~7.7KB)
