# Continuous Claude v4.7

A harness for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that runs software tasks as a gated pipeline: an orchestrating session plans and validates, worker subagents implement, and hooks enforce rules (context limits, lint after edits, protection for installed files) that instructions alone do not guarantee.

**About this fork.** Maintained by maren-epochs. The original harness, and the tldr, Ouros and FastEdit tools it uses, are by [parcadei](https://github.com/parcadei/ContinuousClaudeV4.7). This fork fixes upstream defects, hardens the sandbox and hooks for Windows and adversarial input, adds tests, type checks and Linux/Windows CI, and adds two subsystems: a visualization toolkit and cross-session fleet control. Most of the code was written by Claude agents under the maintainer's direction and review; the commits carry `Co-Authored-By` trailers.

**Read the [project explainer](docs/project-explainer.md)** for the design, the defects found and fixed, the trade-offs, the evidence behind each claim and the current status (readiness score, CI), with a two-minute summary at the top.

## What's in here

```
harness/
  skills/          12 workflow skills
  agents/          worker + oracle
.claude/
  hooks/           9 hooks + tldr-shim helper (.mjs — cross-platform), test_*.sh per hook
scripts/
  readiness.sh     assess project health (27 criteria, 5 levels)
  readiness-fix.sh auto-remediate gaps
tools/
  ouros_harness.py sandboxed Python REPL with external function bridge
  exa_search.py    web search bridge (requires EXA_API_KEY)
  nia_docs.py      documentation search bridge (requires NIA_API_KEY)
  context_ledger.py per-skill main-context token ledger from a session transcript (stdlib)
  fleet/           cross-session fleet view + harness inbox (stdlib, /fleet)
install/
  settings.template.json hook wiring + env config (template for ~/.claude/settings.json)
  sync_global.py   repo -> ~/.claude install (+ install manifest)
  register_hooks.py adds the fleet hooks to ~/.claude/settings.json (user-run)
```

**Visualization suite (optional).** `tools/viz/` is a charting toolkit driven by the `/visualize` skill: one palette (`palette.json`) validated by a six-check palette validator, a chart-form recommender that refuses known anti-patterns (dual axes, crowded pies, more than 8 hues), a single house style for matplotlib, seaborn, plotly, altair and bokeh, an exporter that saves any of those plus great_tables and holoviews to PNG/SVG/HTML (Playwright Chromium for HTML rasterizing), and a builder for self-contained interactive pages. It runs on host CPython: `py -3.13 -m pip install -r tools/requirements-viz.txt` then `py -3.13 -m playwright install chromium` (details in `install/README.md`).

## Skills

| Skill | What it does |
|-------|-------------|
| `/autonomous` | Full SDLC pipeline: assess, plan, premortem, prepare, execute, validate, evolve |
| `/autonomous-research` | Looping research pipeline — hypotheses deepen via Ouros sessions |
| `/research` | Open-ended exploration via Ouros REPL with persistent state |
| `/premortem` | Failure analysis gate — first-principles retrospective before implementation |
| `/bootup` | Assess project readiness, route to research/autonomous/review |
| `/review` | Structural + semantic code review |
| `/create-handoff` | Serialize session context for transfer |
| `/resume-handoff` | Resume an autonomous session from handoff |
| `/upgrade-harness` | Add new external functions to the Ouros sandbox |
| `/analyze-data` | Load, explore, transform, visualize and report on a dataset via Ouros sessions and the host Python bridge |
| `/visualize` | Chart procedure on `tools/viz`: form, color, validated palette, house style, render, then inspect the image |
| `/fleet` | What every Claude Code session on this machine is doing (alerts, collisions, drift, audit) and the harness inbox: show, apply, reject proposals; file cross-project lessons; HTML dashboard |

## Hooks

All hooks are plain `.mjs` (ES modules). No build step, no dependencies — Node.js is guaranteed wherever Claude Code runs.

| Hook | Event | Purpose |
|------|-------|---------|
| `status.mjs` | statusLine | Shows context %, git branch, staged/unstaged counts, goal from handoffs |
| `tldr-read.mjs` | PreToolUse:Read | Injects structural nav map for large code files, truncates to save tokens |
| `post-edit-diagnostics.mjs` | PostToolUse | Lint after edits (Python: ruff direct, broken-code codes as errors; others: tldr diagnostics) |
| `pre-compact.mjs` | PreCompact | Writes an auto-handoff (markdown) before compaction to the handoff root: project `thoughts/shared/handoffs/` if present, else `~/.claude/handoffs/<project>/` |
| `auto-handoff-stop.mjs` | Stop | Blocks at 85% context usage to force handoff; uses transcript usage when no statusline ran (headless) |
| `worker-report-check.mjs` | PostToolUse (settings, filters to `reports/*.json` itself) + SubagentStop (worker frontmatter) | Validates the worker's report JSON while the worker is still running; advisory — the VALIDATE schema gate is binding |
| `session-start.mjs` | SessionStart (registered for `compact`) | Newest handoff after compaction; bloks context on startup/clear is implemented but not registered (about 2K tokens per session) |
| `harness-guard.mjs` | PreToolUse (file tools + Bash/PowerShell, unfiltered) | Denies writes to installed harness files under `~/.claude` and files the intended change in the harness inbox, naming the repo file to change instead |
| `fleet-audit.mjs` | PostToolUse (Bash/PowerShell) | Logs risky shell commands (force-push, hard reset, recursive delete, settings edits, global installs) to `~/.claude/fleet/audit.jsonl`, secrets redacted; never blocks |

Hooks that use `tldr` (tldr-read, post-edit-diagnostics) fall through silently if tldr is not installed.

## Dependencies

### Required

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — the runtime

### Recommended (on PATH)

| Tool | Install | What it does |
|------|---------|-------------|
| [bloks](https://github.com/maren-epochs/bloks) (fork of archived parcadei/bloks — ack/nack scoring fixed) | `cargo install --git https://github.com/maren-epochs/bloks` | Library knowledge cards — API docs, taste, corrections |
| [tldr](https://github.com/parcadei/tldr-code) | `cargo install --git https://github.com/parcadei/tldr-code tldr-cli` | Token-efficient code analysis (AST, call graphs, impact, diagnostics) |
| [ouros](https://github.com/parcadei/ouros) | `python -m pip install ouros` | Sandboxed Python REPL with fork/save/resume |

Two notes on those installs. `tldr` is published from the `tldr-cli` workspace member rather than
as a `tldr-code` crate, and its releases carry no Windows asset, so the git build above is the only
path on Windows. `ouros` is a Python module, not a cargo binary — `tools/ouros_harness.py` imports
it directly — and PyPI ships wheels for CPython 3.10 through 3.13 only, so install it into an
interpreter in that range.

Install it with `python -m pip`, not a bare `pip`, and use the same interpreter that runs the
harness. A bare `pip` can belong to a different interpreter than `python`, in which case the install
succeeds and `python tools/ouros_harness.py` still fails on `import ouros`. Where several versions
are present, name one on both sides — on Windows `py -3.13 -m pip install ouros` alongside
`py -3.13 tools/ouros_harness.py`; elsewhere `python3.13 -m pip install ouros`. A 3.14 default
interpreter has no wheel and will fail regardless.

### Optional (API keys in .env)

```bash
cp .env.example .env
# Add your keys:
# EXA_API_KEY=     — for web search via exa_search.py
# NIA_API_KEY=     — for documentation search via nia_docs.py
```

Python dependencies for tools that use API keys:
```bash
pip install -r tools/requirements.txt  # aiohttp
```

### FastEdit (not registered)

[FastEdit](https://github.com/parcadei/fastedit) (by parcadei, `pip install fastedits`, CLI `fastedit`) edits code through a merge model. Upstream shipped a PreToolUse hook that redirects the built-in `Edit` tool to it. This fork does not register that hook: it denies every `Edit` call in favor of a `fast_edit` MCP server, so without that server configured no edit can succeed, and its default model targets Apple-Silicon MLX. The hook is absent from `install/settings.template.json`, and the repo's former `.claude/settings.json`, which held only that hook, was removed. To use FastEdit anyway, follow the setup in its own repository and configure its MCP server before adding the hook.

## Build / setup

From Git Bash (or any POSIX shell; GNU make):

```bash
make setup && make test    # deps, Playwright Chromium, pre-commit hook; then every suite
make help                  # also: lint, format, typecheck, readiness, sync
```

`make setup` is idempotent: it installs `requirements.lock` when present (else the `pyproject.toml` dependencies), then `plotly-resampler==0.11.1` with `--no-deps` (it declares `plotly<7`, so it stays out of the lock), runs `playwright install chromium`, and runs `pre-commit install` when pre-commit is available. `make test` runs pytest over `tools/`, `install/` and `tests/` (the `tests/integration/` suite carries the `integration` marker), every `.claude/hooks/test_*.sh` suite, and `scripts/test_readiness.sh`. `make sync` writes into `~/.claude` (see Option B below). The interpreter defaults to `py -3.13` on Windows and `python3` elsewhere; override with `make test PYTHON=...`. `pre-commit install` writes the interpreter that ran it into the clone's shared `.git/hooks/pre-commit`, so the hook step (`install/setup_deps.py --hook`) runs it only when no hook exists or the hook's `INSTALL_PYTHON` path no longer exists, and otherwise prints a one-line note; a `make setup` from a throwaway venv leaves the hook alone. Repoint it deliberately with `make setup FORCE_HOOK=1`, `pwsh install/setup.ps1 -Setup -ForceHook`, or `py -3.13 install/setup_deps.py --force-hook`.

Without make, use the PowerShell 7 twin (same commands, exit codes propagated; needs Git Bash for the shell suites):

```powershell
pwsh install/setup.ps1 -Setup -Test    # also: -Lint, -Format, -Typecheck, -Readiness
```

## Development

`.pre-commit-config.yaml` runs `ruff check`, `ruff format --check` and `privacy-guard` (`tools/privacy_guard.py`, run by pre-commit's own Python on any OS) on every commit; enable with `py -3.13 -m pre_commit install`, check the tree with `py -3.13 -m pre_commit run --all-files`.
The guard rejects absolute user-home paths, your OS username, session-UUID-shaped ids, and every private term from the union of three sources: env var `CCV_PRIVACY_TERMS` (newline- or comma-separated; CI and cloud sessions), `~/.claude/privacy-terms` (shared by every clone on the machine) and the per-clone `.git/info/privacy-terms` (files: one term per line, `#` comments; keep them local, never commit them). With no source it warns on stderr and checks only paths, username and session ids.
Reviewed false positives go in `.privacy-allow` (one regex per line, full match against the flagged text).

## Quick start

### Option A: Add to an existing project

Copy `harness/skills/` to `.claude/skills/` and `harness/agents/` to `.claude/agents/` in your project root, plus this repo's `.claude/hooks/`, `scripts/`, and `tools/`, and use `install/settings.template.json` as the project's `.claude/settings.json` (its hook commands are project-relative). Skills, hooks, and agents are then available immediately. (Skills and agents live under `harness/` in this repo so a checkout doesn't double-register them alongside a global install.)

### Option B: Global install

```bash
py -3.13 install/sync_global.py --diff     # dry run: what would change
py -3.13 install/sync_global.py --apply    # write into ~/.claude
```

This syncs skills, agents, hooks, scripts, and tools into `~/.claude` with repo-relative paths rewritten to absolute ones. See `install/README.md`.

### Fleet (optional)

`/fleet` shows every Claude Code session on the machine and the harness inbox. All of its data stays under `~/.claude`: `fleet/state.json` (collected state, refreshed by the existing Stop hook at most every 2 minutes, shown in the statusline), `fleet/audit.jsonl` (risky shell commands), `harness-inbox/` (proposals: edits harness-guard redirected, lessons) and `.ccv47-manifest.json` (written by `sync_global.py --apply`; harness-guard protects exactly the files it lists). After the global install, register the two fleet hooks once:

```bash
py -3.13 install/register_hooks.py --dry-run   # show the settings.json diff
py -3.13 install/register_hooks.py             # back up and write ~/.claude/settings.json
```

It needs Node 18+, never duplicates entries and keeps everything else in `settings.json`. Restart running sessions afterwards.

### Then

1. Run `/bootup` to assess readiness and route to a workflow
2. Or jump straight to `/autonomous` with a task description
3. Or `/autonomous-research` for open-ended research

## How it works

**The loop:** Research explores unknowns. Autonomous plans and builds. Workers execute atomic tasks. Validation gates each milestone. Evolve aggregates findings, recommends lint rules and tool configs for the user to approve, and feeds corrections back to bloks cards, so conventions found in one run are enforced in the next.

**Knowledge flow:** Bloks cards carry API knowledge, taste, and corrections. Cards are consumed during PREPARE (injected into worker prompts) and produced during EVOLVE (from worker findings). Cards score through ack/nack — useful cards surface, bad cards get revised or retired.

**Research flow:** `/autonomous-research` runs a looping pipeline where workers research inside Ouros. Results persist in the REPL heap and do not enter the orchestrator's context. Workers share state via Ouros sessions (`--load` for sequential, `--fork` for parallel). Only compact artifacts cross back to the orchestrator.

**Enforcement hierarchy:** When workers report conventions, evolve maps them to the strongest deterministic enforcement: lint rule > type system > formatter > pre-commit hook > CI check > CLAUDE.md (last resort). Probabilistic instructions are always the fallback, never the first choice.

## Project structure (created by autonomous)

```
continuum/
  autonomous/{task-id}/
    contract.json    assertions + lifecycle state
    plan.md          milestones (multi-feature+)
    reports/         worker reports (uniform JSON)
    validation/      milestone results
  research/{topic}/
    research_contract.json   hypotheses + lifecycle + iteration history
    findings.md              final synthesis
    artifacts/               per-hypothesis per-iteration evidence
    reports/                 worker reports (compact JSON)
```

## License

MIT
