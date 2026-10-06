# Continuous Claude v4.7

Autonomous SDLC pipeline for Claude Code. 10 skills, 2 agents, 7 hooks (+ tldr-shim helper).

## Skills

| Skill | Trigger | Role |
|-------|---------|------|
| `/autonomous` | Bounded implementation tasks | ASSESS → PLAN → PREMORTEM → PREPARE → EXECUTE → VALIDATE → EVOLVE |
| `/autonomous-research` | Open-ended research | Looping research pipeline via Ouros — EVOLVE loops back to PLAN |
| `/research` | Quick exploration | Single-pass Ouros REPL exploration |
| `/analyze-data` | "analyze this data", "explore this CSV", "plot" | load → explore → transform → visualize → report via Ouros sessions + `run_python` DS bridge |
| `/premortem` | Before implementation | Failure analysis gate — first-principles risk check |
| `/bootup` | Session start | Assess readiness, route to research/autonomous/review |
| `/review` | Code review | Structural + semantic review |
| `/create-handoff` | Session end | Serialize context for next session |
| `/resume-handoff` | Session start | Resume from handoff document |
| `/upgrade-harness` | Extend Ouros | Add new external functions to Ouros sandbox |

## Agents

| Agent | Role |
|-------|------|
| `worker` | Executes atomic tasks — full autonomy over implementation. `model: inherit`, `effort: high`, `maxTurns: 60`; frontmatter Stop hook validates its report |
| `oracle` | External research — docs, APIs, best practices |

## Hooks (all .mjs — cross-platform)

| Hook | Event | What it does |
|------|-------|-------------|
| `status.mjs` | statusLine | Context % (Claude Code's `used_percentage`, no added overhead), git info, goal from handoffs |
| `tldr-read.mjs` | PreToolUse:Read | Injects structural nav map for large code files, truncates (mtime cache; `tldr-shim.mjs` serves cold reads, autostart via `TLDR_READ_SHIM_AUTOSTART=1`) |
| `post-edit-diagnostics.mjs` | PostToolUse:Edit\|Write\|MultiEdit\|Update | Lint after edits; Python via ruff direct (E9/F63/F7/F82/syntax = error), others via tldr |
| `pre-compact.mjs` | PreCompact | Auto-handoff before context compaction |
| `auto-handoff-stop.mjs` | Stop | Blocks at 85% context to force handoff; falls back to transcript usage when no statusline ran (headless) |
| `worker-report-check.mjs` | PostToolUse (settings, unfiltered - the hook self-filters on `reports/*.json` paths, ~80ms otherwise) + SubagentStop (worker frontmatter) | Validates report JSON with `tools/validate_report.py`; PostToolUse feedback reaches a running worker on Edit and Write (verified live; a settings `if: Edit(...)` filter matched Edit only, so it was removed), SubagentStop is a late second check. VALIDATE's schema gate stays binding |
| `session-start.mjs` | SessionStart (registered for `compact`) | After compaction: injects the newest handoff from the handoff root. startup/clear bloks injection available but not registered (~2K tokens/session) |

Registration lives in `~/.claude/settings.json` (absolute paths, per-extension `if` filters); template: `install/settings.template.json`. Tests: `.claude/hooks/test_*.sh`.

**Handoff root (all handoff readers/writers):** project `thoughts/shared/handoffs/` if it exists, else `~/.claude/handoffs/<project-dir-basename>/` — user repos never get a `thoughts/` dir.

## External Tools (optional, on PATH)

| Tool | What it does |
|------|-------------|
| [bloks](https://github.com/maren-epochs/bloks) (fork; upstream archived) | Library knowledge cards — API docs, taste, corrections |
| [tldr](https://github.com/parcadei/tldr-code) | Token-efficient code analysis (AST, call graphs, diagnostics) |
| [ouros](https://github.com/parcadei/ouros) | Sandboxed Python REPL with fork/save/resume |
| [fastedit](https://github.com/parcadei/fastedit) | Fast code editing via merge model (`pip install fastedits`). Not registered: its hook denies Edit in favor of a `fast_edit` MCP server, and it targets Apple-Silicon MLX |

## Scripts

- `scripts/readiness.sh` — assess project health (27 criteria, 5 levels); a failed tldr sub-analysis is SKIP with a reason, never a fabricated pass
- `scripts/readiness-fix.sh` — auto-remediate readiness gaps

## Tool Bridges (in tools/)

- `ouros_harness.py` — Ouros REPL bridge with exa_search, nia_search, llm_call, agent_call (`--max-turns` default 25)
- `validate_report.py` — worker report + contract.json schema check (stdlib); VALIDATE gates on it
- `context_ledger.py` — per-span / per-skill main-context token ledger from a session transcript (stdlib; `--json`, `--session <id>`, default = newest transcript for cwd); create-handoff writes its summary as `context:`
- `exa_search.py` — web search (requires EXA_API_KEY in .env)
- `nia_docs.py` — documentation search (requires NIA_API_KEY in .env)

## Ouros Sandbox

Sandboxed Python REPL for research, codebase exploration, and multi-step data processing.

```bash
tools/ouros_harness.py --file /tmp/script.py            # from file (preferred — no quoting issues)
tools/ouros_harness.py --file /tmp/script.py --session my-task --storage thoughts/shared/dives
tools/ouros_harness.py --file /tmp/script.py --session my-task          # existing session loads by default
tools/ouros_harness.py --file /tmp/script.py --session my-task --reset  # discard saved state, start fresh
tools/ouros_harness.py --session my-task --list-vars    # inspect session state
tools/ouros_harness.py --session my-task --get-var results
```

### Available functions

Call `nia_help()` inside the sandbox for full details. Summary:

| Function | What it does |
|----------|-------------|
| `research_package(pkg, version, registry)` | All-in-one: nia + exa, filters noise, compact structured data |
| `nia_search(query)` / `nia_universal(query)` / `nia_web(query)` | Indexed docs/repos, 10k+ public sources, web search |
| `nia_package(pkg, query, registry)` / `nia_package_grep(pkg, pattern, registry)` | Semantic / regex search within a package's source |
| `exa_search(query, num_results=5)` | Semantic web search via Exa AI |
| `read_file` / `write_file` / `glob_files` / `run_command` | Host filesystem + allowed shell commands (`write_file` is binary-safe: str or bytes) |
| `run_python(code, timeout=60)` | DS bridge: runs code on HOST CPython (py -3.13 — pandas, numpy, matplotlib, polars, duckdb, sklearn). The sandbox itself cannot import these. Returns stdout+stderr tail (~8KB cap) |

Registries: `npm`, `py_pi`, `crates_io`, `go_modules`.

**Token efficiency:** the sandbox processes data internally — only `print()` output enters the agent's context. Always filter, truncate, and structure results inside the script.

**Security:** deny-by-default — `read_file`/`glob_files` only read project (cwd; ignored when cwd is home or a drive root) + `/tmp/ouros` + any dirs in `OUROS_DATA_ROOTS` (os.pathsep-separated absolute paths, read-only), after resolving symlinks/`..`; credentials are never readable even under an allowed root (`.env`/`.env.*` except `.example/.sample/.template`, `*.pem`/`*.key`, `id_*` keys, `~/.claude.json`, `~/.claude/.credentials*`, `~/.ssh`, `~/.aws`, ...). `write_file` only writes to `/tmp/ouros-sandbox-output` (drive-relative on Windows: `C:\tmp\ouros-sandbox-output`). `run_command` parses the command and runs it WITHOUT a shell — no pipes, chaining, redirection or substitution — and only argv prefixes in the allowlist (`tldr`, `grep`, `rg`, `wc`, `echo`, `git log/diff/show/blame`, `cargo build/test/clippy`, `npm test/run`, `python -m pytest`, `uv run python`); `rg --pre` and `git --output/--ext-diff/--textconv` are denied; spawn agents via `agent_call` (default `--max-turns 25`), not `run_command`. Exception: `run_python` executes arbitrary code on host CPython by design (not an escalation — the agent already has Bash); cwd pinned to a per-session work dir, one concurrent run, timeout kills the process tree. New files under the output root are printed as `artifacts:` lines (absolute host paths) after each run.

## Architecture

Skills orchestrate — they never implement directly. Workers build. Bloks cards carry knowledge between sessions. Handoffs carry session state.

**Enforcement hierarchy:** lint rule > type system > formatter > pre-commit hook > CI check > CLAUDE.md (last resort). Deterministic enforcement always preferred over probabilistic instructions.

**Knowledge flow:** PREPARE consumes bloks cards → workers execute → EVOLVE produces new cards via `bloks new rule`. Cards score through ack/nack (score in [-1,1]; `bloks context` ranks rules/tastes by score and hides cards below -0.5 — fork fix, upstream scoring was a no-op).

## Project structure (created at runtime)

```
continuum/
  autonomous/{task-id}/   contract.json, plan.md, reports/, validation/
  research/{topic}/       findings.md (telegraphic artifact)
thoughts/
  shared/handoffs/        session handoffs (manual + auto-generated)
```
