---
name: autonomous
description: SDLC pipeline — assess plan prepare execute validate evolve
user-invocable: true
---

Orchestrate. Never implement. Workers build; you plan, decompose, delegate, validate, steer.
Carry: assertion status, file paths, contract state, pass/fail. No file operations yourself.

Pipeline: ASSESS → PLAN → PREMORTEM → PREPARE → EXECUTE → VALIDATE → EVOLVE
All phases run. Depth scales with complexity. Phase detail lives in phases/*.md next to
this file — read phases/{phase}.md when ENTERING that phase, not before.

  plan       phases/plan.md       validation contract first — contract.json, assertion types, depends graph
  premortem  phases/premortem.md  failure analysis gate via /premortem before any code
  prepare    phases/prepare.md    capture context to files once — workers read by path
  execute    phases/execute.md    archetype spells, worker prompt JSON, report JSON
  validate   phases/validate.md   milestone gates: automated → assertions → scrutiny → fix loop
  evolve     phases/evolve.md     aggregate reports, bloks feedback checklist, readiness delta


ASSESS

Read task. Check project: tldr structure, CLAUDE.md, readiness.sh. Record baseline score.
Classify:
  patch — bug fix, one worker, one assertion, no milestones
  feature — one milestone, multiple atomic workers, validation gate
  multi-feature — multiple milestones, validation gates between each
  greenfield — milestone 0 (type: approval) for design if aesthetic, then build milestones
Flag aesthetic if: UI, visual, color, layout, typography. Encode as approval milestone in PLAN.


PATCH FAST PATH

Patch class only (one worker, one assertion, no milestones):
  PREMORTEM inline — scan the contract for tiger-class risk yourself, no /premortem spawn.
  No tigers → auto-pass; record "premortem": "inline-pass (no tigers)" in contract.json.
  Any tiger → run the full phase.
  EVOLVE — skip pattern aggregation (3+ threshold unreachable under 3 reports) but ALWAYS
  run the checklist: bloks report for corrections, discoveries verification, bloks ack/nack,
  readiness.sh delta vs ASSESS baseline. The knowledge loop never skips.
All other phases run in full.


STATE

  continuum/autonomous/{task-id}/
    contract.json     assertions + lifecycle state + depends graph
    plan.md           milestones (multi-feature+)
    context/          PREPARE captures — bloks-context.txt, structure.txt, conventions.md, cards/
    reports/          worker reports (uniform JSON)
    validation/       milestone results


RESUME

Read contract.json. Pending assertions need workers. Pending milestones need validation.
Respect depends graph when resuming — don't spawn assertion if its dependency is pending/failed.
Continue from gap. Show user: assertions N/total, milestones M/total, next pending.


RULES

Never implement — workers via Task tool
TDD: assertion → failing test → implement → pass
Every milestone has validation gate
Workers atomic: one task, one assertion, one report
Reports uniform, every field filled
Max 2 fix rounds then escalate, rollback if declined
Context by reference: PREPARE writes capture files once, worker prompts carry paths
Respect assertion depends graph
Commit after each worker
