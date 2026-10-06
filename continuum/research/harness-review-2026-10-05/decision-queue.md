# Decision queue (autonomous 30-min run, started 20:54)

## D1. tldr-read auto-approves Reads of any code file >1.5KB anywhere on disk
Docs (hooks reference, permissionDecision): "allow" skips the permission prompt; "Deny and ask rules are still evaluated regardless of what the hook returns" — so deny rules are safe (the earlier "allow overrides deny" summary was wrong). Effect: reads OUTSIDE the project that would normally prompt are silently approved when tldr-read rewrites them (limit/nav map).
Options: (a) allow only inside project/cwd, "ask" outside [Recommended]; (b) always "ask" with updatedInput (prompts on every large code read in non-auto modes); (c) leave as is.

## D2. Register session-start.mjs (built + tested, commit in this run)
Startup/clear: injects bloks context (~6K chars ≈ 1.5-2.2K tokens, ~60ms) into EVERY session in every project. Compact: injects the newest handoff (fixes "auto-handoffs written but never read back").
Options: (a) register for compact only — zero per-session token cost, fixes post-compact continuity [Recommended]; (b) register for startup+clear+compact; (c) don't register.
Snippet for ~/.claude/settings.json hooks: "SessionStart": [{"matcher": "compact", "hooks": [{"type": "command", "command": "node \"~/.claude/hooks/session-start.mjs\""}]}]  (matcher "startup|clear|compact" for option b)

## D3. bloks: log a view on each context injection
Today scores come only from ack/nack. Logging views makes un-acked cards drift toward +0.1 per view (neutral term) and gives `bloks stats` real usage data, but changes ranking behavior. Options: (a) defer until a few sessions of ack/nack data exist [Recommended]; (b) implement now.

## D4. bloks lint gates in CI
clippy -D warnings has 11 errors, fmt --check fails on existing upstream code. Options: (a) leave CI as build+test [Recommended — upstream code, archived, churn without benefit]; (b) clean up and gate.

## D5. context: fork for /review, /premortem, /research + per-skill effort
Saves main-context tokens (unmeasured). Changes how those skills report back. Options: (a) measure one long session first [Recommended]; (b) apply now.

### D5 verdict (2026-10-05, measured)
Measured with tools/context_ledger.py over this session plus the 3 largest transcripts on the machine (d5-measurement.md alongside, raw ledger-*.json). Do NOT fork /premortem or /research: per-invocation main-context cost is 8-29K (5-18% of a 200K-window peak, <3% on 1M-window sessions) against a >=52-64K fresh-subagent baseline plus re-reads and handback. /review has zero measured spans; default to not forking until one real run is ledgered. Unattributed orchestrator work (tool results, worker reports, file reads) dominates every transcript; forking the three skills would not move it. The per-skill-effort half of D5 is not answered by a context-side ledger. Re-open if: a /review run shows >60K in-span growth, or sessions routinely run on a 200K window above 70%.


## D6. Worker SubagentStop hook can't resume background workers
Live probe (verified): hook fired, computed the correct block, transcript recorded hook_blocking_error — but the worker had already handed back and never continued. All Agent-tool workers here run in the background, so the hook is advisory; VALIDATE's schema gate is the binding check (it passed in the E2E run). Docs corrected.
Options: (a) keep hook as-is, advisory + transcript trail [Recommended — harmless, may become binding if foreground subagents are used]; (b) remove it (simpler); (c) move enforcement to a PostToolUse hook on Write to reports/*.json so the worker sees errors while still running [worth trying; S effort].

### D6 update (verified with an invocation log)
Worker frontmatter: the Stop hook IS invoked (once, at handback — too late for a background worker); the frontmatter PostToolUse hook is NEVER invoked for the worker's Write (3 probes, invocation log shows only the Stop call) — contrary to the docs example. The PostToolUse branch of worker-report-check.mjs works when invoked (replayed manually).
New recommended option (d): register worker-report-check.mjs as a USER-settings PostToolUse hook (docs: settings hooks fire inside subagents) with `"if": "Write(**/continuum/autonomous/*/reports/*.json)"` (+ Edit) so it only spawns on report writes. Fails open; also validates reports the orchestrator writes. Snippet for ~/.claude/settings.json PostToolUse matcher "Edit|Write|MultiEdit|Update" hooks list:
  {"type": "command", "if": "Edit(**/continuum/autonomous/*/reports/*.json)", "command": "node \"~/.claude/hooks/worker-report-check.mjs\"", "timeout": 25}
Then re-run the probe to confirm feedback reaches a running worker.

## D7. Central handoff folders key on the project folder NAME only
Two projects with the same folder name (e.g. two "api" repos) share ~/.claude/handoffs/api/ and could load each other's handoffs. Options: (a) append a short hash of the full path (api-3f2a1c) [Recommended if you have same-named repos; folder names get less readable]; (b) keep names as-is [fine if folder names are unique].

## D8. test_readiness_parallel.sh wall-time budgets fail on this machine
Full run budget <=12s measured 12-17s (failed before and after today's fix); skip-secure <5s hits exactly 5s. Options: (a) median-of-3 with +50% headroom, same approach as the post-edit test [Recommended]; (b) keep strict budgets.
