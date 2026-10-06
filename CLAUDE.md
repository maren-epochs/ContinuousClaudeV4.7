# Continuous Claude v4.7

Autonomous SDLC pipeline for Claude Code. 11 skills, 2 agents, 7 hooks (+ tldr-shim helper).

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
