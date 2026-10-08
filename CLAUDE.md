# Continuous Claude v4.7

Autonomous SDLC pipeline for Claude Code. 12 skills, 2 agents, 9 hooks (+ tldr-shim helper).

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
| `/visualize` | "chart", "plot", "dashboard", "visualize", "graph" | form → color → validate → style → render → look (Read the PNG); outputs static PNG/SVG, Artifact page, or Render site; data prep hands off to `/analyze-data` |
| `/fleet` | "fleet", "what are other projects doing", "harness inbox", "apply proposal" | Read-mostly view of every Claude Code session on the machine (alerts, collisions, drift, audit) + the harness inbox: show / apply (into the repo file, never the installed copy) / reject proposals, file cross-project lessons, `handoff-all` (asks every live session for a handoff via SendMessage, `handoffs --since` checks which landed), HTML dashboard; all via `tools/fleet/fleet.py` |

## Agents

| Agent | Role |
|-------|------|
| `worker` | Executes atomic tasks — full autonomy over implementation. `model: claude-opus-5-5` (pinned; independent of the session model), `effort: high`, `maxTurns: 60`; frontmatter Stop hook validates its report |
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
| `harness-guard.mjs` | PreToolUse:Write\|Edit\|MultiEdit\|NotebookEdit\|Bash\|PowerShell (unfiltered) | Denies writes to installed harness files listed in `~/.claude/.ccv47-manifest.json` (file tools and shell write targets; case, `/c/`, `~`, 8.3 and realpath spellings normalized in the hook) and saves the intended change as a proposal in `~/.claude/harness-inbox/`; kept paths allowed; fails open before a managed target is identified. Unfiltered because a live check (CLI 2.1.293) showed a `Write(~/...)` `if` filter misses 8.3 short-name spellings |
| `fleet-audit.mjs` | PostToolUse:Bash\|PowerShell (unfiltered) | Appends risky shell commands (force-push, history rewrite, hard reset, recursive delete, settings edit, global install) to `~/.claude/fleet/audit.jsonl`, secrets and private terms redacted; never blocks |

Registration lives in `~/.claude/settings.json` (absolute paths, per-extension `if` filters); template: `install/settings.template.json`. The two fleet hooks are added by `py -3.13 install/register_hooks.py` (user-run). Tests: `.claude/hooks/test_*.sh`.

**Handoff root (all handoff readers/writers):** project `thoughts/shared/handoffs/` if it exists, else `~/.claude/handoffs/<project-dir-basename>/` — user repos never get a `thoughts/` dir.

## External Tools (optional, on PATH)

| Tool | What it does |
|------|-------------|
| [bloks](https://github.com/maren-epochs/bloks) (fork; upstream archived) | Library knowledge cards — API docs, taste, corrections |
| [tldr](https://github.com/parcadei/tldr-code) | Token-efficient code analysis (AST, call graphs, diagnostics) |
| [ouros](https://github.com/parcadei/ouros) | Sandboxed Python REPL with fork/save/resume |
| [fastedit](https://github.com/parcadei/fastedit) | Fast code editing via merge model (`pip install fastedits`). Not registered: its hook denies Edit in favor of a `fast_edit` MCP server, and it targets Apple-Silicon MLX |

## Scripts

- `Makefile` — `setup` (`install/setup_deps.py`: requirements.lock, then plotly-resampler `--no-deps`; Playwright Chromium; pre-commit hook), `test` (pytest + `.claude/hooks/test_*.sh` + `scripts/test_readiness.sh`), `lint`, `format`, `typecheck`, `readiness`, `sync` (writes `~/.claude`). POSIX sh recipes (Git Bash); `PYTHON=` overrides `py -3.13`. Without make: `pwsh install/setup.ps1 -Setup -Test -Lint -Format -Typecheck -Readiness` (no sync switch)
- `scripts/readiness.sh` — assess project health (27 criteria, 5 levels); a failed tldr sub-analysis is SKIP with a reason, never a fabricated pass. Run it in the FOREGROUND: background shells get reaped under memory pressure (its tldr jobs peak ~0.1 GB, ~30-50 s). tech_debt scans a copy of tracked source minus test files (user decision; other tldr checks still scan tests); `file_grep` passes grep flags before the file arg; `harness/skills/*/SKILL.md` counts as skills. Tests: `scripts/test_readiness.sh`
- `scripts/readiness-fix.sh` — auto-remediate readiness gaps
- `install/sync_global.py` — repo -> `~/.claude` install (dry run default, `--apply` writes with backups); `<target>/.ccv47-keep` (untracked, one install-relative path per line, `#` comments) pins paths it never writes, shown as `keep    <path>`; `--apply` also writes `<target>/.ccv47-manifest.json` (installed path -> repo path + sha256), which harness-guard and fleet drift read
- `install/register_hooks.py` — user-run, idempotent: adds harness-guard (PreToolUse) and fleet-audit (PostToolUse) to `~/.claude/settings.json` (`--settings PATH`) as absolute `node "<home>/.claude/hooks/<x>.mjs"` commands, backup `settings.json.bak-<ts>-register-hooks` first, `--dry-run` prints the diff; never duplicates, keeps all other keys, order, indent and EOL; refuses without Node 18+ or the installed hook files. Exit 0 done / 1 refused

## Tool Bridges (in tools/)

- `ouros_harness.py` — Ouros REPL bridge with exa_search, nia_search, llm_call, agent_call (`--max-turns` default 25)
- `validate_report.py` — worker report + contract.json schema check (stdlib); VALIDATE gates on it
- `context_ledger.py` — per-span / per-skill main-context token ledger from a session transcript (stdlib; `--json`, `--session <id>` (must resolve inside this project's transcript folder, else exit 2), default = newest transcript for cwd); create-handoff writes its summary as `context:`
- `exa_search.py` — web search (requires EXA_API_KEY in .env)
- `nia_docs.py` — documentation search (requires NIA_API_KEY in .env)
- `api_common.py` — shared aiohttp HTTP layer (request, SSE lines, .env key, headers) for exa_search/nia_docs
- `privacy_guard.py` — pre-commit hook: rejects user-home paths, the OS username, UUID-shaped ids and every term in the untracked `.git/info/privacy-terms`; reviewed false positives in `.privacy-allow`
- `lock_requirements.py` — regenerates root `requirements.lock` (pyproject closure pinned to the installed env, no network); `--check` exits 1 when stale
- `_testing.py` — shared unittest helpers (module loader, async runner, fake aiohttp); not collected

Python checks (config in `pyproject.toml`; `tools/viz/validate_palette.py` is vendored and excluded): `ruff check .`, `ruff format --check .`, `py -3.13 -m mypy`, `py -3.13 -m pytest -q` (every `tools/**/test_*.py` + `install/test_*.py` unittest suite, plus `tests/integration/`, marker `integration`, ~50 s; skip with `-m "not integration"`), `py -3.13 -m coverage run -m pytest -q && py -3.13 -m coverage report`.

## Visualization (tools/viz)

Host CPython only (`py -3.13`); install: `tools/requirements-viz.txt` + `py -3.13 -m playwright install chromium` (see `install/README.md`). Driven by `/visualize`.

| Module | Role | Entry point |
|--------|------|-------------|
| `palette.json` + `palette.py` | Source of truth for every color/font token; no hex literals anywhere else | `categorical(mode, n)`, `sequential()`, `ordinal(mode)` (light 5 / dark 6 levels, more repeat), `diverging(mode)`, `status(mode)`, `gray(mode)` (emphasis context; highlight = slot 1), `css_tokens(mode)`; `py -3.13 tools/viz/palette.py` prints CSS tokens |
| `validate_palette.py` | Six-check palette validator (vendored) + `validate()` adapter | `py -3.13 tools/viz/validate_palette.py "#hex,#hex,..." [--mode light\|dark] [--pairs adjacent\|all] [--ordinal]` |
| `recommend.py` | Chart form from data job/profile; refuses dual axis, pies past 5, >8 hues | `py -3.13 tools/viz/recommend.py data.csv\|data.parquet [--job JOB] [--json]` |
| `style.py` | One house style: matplotlib/seaborn rc, plotly template, altair theme, bokeh theme, great_tables | `apply_matplotlib(mode)`, `plotly_template(mode)`, `altair_theme(mode)`, `bokeh_theme(mode)`, `gt_style(gt, mode)` (set the table id before, not after); `py -3.13 tools/viz/style.py [mode]` |
| `export.py` | `save(fig, path, formats)` for matplotlib/plotly/altair/bokeh/great_tables/holoviews; `render_html` via Playwright Chromium (waits on `window.__chartsReady`) | `py -3.13 tools/viz/export.py render page.html out.png [--mode dark] [--width 1200] [--height 800]` |
| `artifact_page.py` | Self-contained Artifact HTML page from Plotly/Vega-Lite/ECharts specs (tokens on `:root`, dark mode, table view) | `build_page(charts, title, description='', mode_default='auto', table_rows=None)` -> HTML; `write_page(charts, path, **kw)` -> abs path; `from_altair(chart)`, `from_plotly(fig)`; spec colors `token('<name>')` (any css token, e.g. `gray`, `series-1`); ECharts `end_labels: True\|[names]`; `py -3.13 tools/viz/artifact_page.py charts.json out.html --title "Two Words"` |

Chrome: kaleido (plotly PNG/SVG) uses an installed Chrome or `kaleido.get_chrome_sync()`; on failure `export.py` falls back to Playwright Chromium. bokeh pinned 3.9.2 (panel 1.9.4 breaks on 3.10.0).

Consumers import `from ccv_viz import ...` after the one-line PRELUDE in `/visualize` (a project's own `tools/` package would shadow `tools.viz`); in-repo `from tools.viz import ...` also works.

## Fleet (tools/fleet)

Cross-session view and harness inbox for every Claude Code session on this machine. Stdlib Python (`py -3.13 tools/fleet/fleet.py collect|report|json|inbox|show|apply|reject|lessons|handoffs|dashboard`), driven by `/fleet`. Data lives only under `~/.claude`, never in a repo:

| Path | What |
|------|------|
| `~/.claude/fleet/state.json` | Collected FleetState (sessions, alerts, collisions, drift); refreshed by the existing Stop hook (`auto-handoff-stop.mjs` spawns a detached `fleet.py collect` at most every 2 min; `FLEET_COLLECT=0` disables) and read by the statusline `fleet N live \| M inbox \| K alert` segment |
| `~/.claude/fleet/audit.jsonl` | fleet-audit events |
| `~/.claude/harness-inbox/<id>.json` | Proposals: edits harness-guard redirected, plus lessons; `apply` writes into the repo file, never `~/.claude` |
| `~/.claude/.ccv47-manifest.json` | Install manifest written by `sync_global.py --apply`; no manifest = guard allows everything |

Setup (user-run, both write `~/.claude`): `py -3.13 install/sync_global.py --apply`, then `py -3.13 install/register_hooks.py` (`--dry-run` first), then restart sessions. Shapes: `tools/fleet/schema.md`. Never open `~/.claude/sessions/*.key`.

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

**Security:** deny-by-default — `read_file`/`glob_files` only read project (cwd; ignored when cwd is home or a drive root) + `/tmp/ouros` + any dirs in `OUROS_DATA_ROOTS` (os.pathsep-separated absolute paths, read-only), after resolving symlinks/`..`; credentials are never readable even under an allowed root (`.env`/`.env.*` except `.example/.sample/.template`, `*.pem`/`*.key`, `id_*` keys, `~/.claude.json`, `~/.claude/.credentials*`, `~/.ssh`, `~/.aws`, ...). `write_file` only writes to `/tmp/ouros-sandbox-output` (drive-relative on Windows: `C:\tmp\ouros-sandbox-output`). `run_command` parses the command and runs it WITHOUT a shell — no pipes, chaining, redirection or substitution — and only argv prefixes in the allowlist (`tldr`, `grep`, `rg`, `wc`, `echo`, `git log/diff/show/blame`, `cargo build/test/clippy`, `npm test/run`, `python -m pytest`, `uv run python`); `rg --pre` and `git --output/--ext-diff/--textconv` are denied; spawn agents via `agent_call` (default `--max-turns 25`), not `run_command`. `llm_call` POSTs only to its endpoint allowlist: https to `api.anthropic.com`/`api.openai.com`/`openrouter.ai` (port 443), plain http only to `localhost`/`127.0.0.1:1234`; any other URL returns an error before a request is built. Exception: `run_python` executes arbitrary code on host CPython by design (not an escalation — the agent already has Bash); cwd pinned to a per-session work dir, one concurrent run, timeout kills the process tree. New files under the output root are printed as `artifacts:` lines (absolute host paths) after each run.

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
