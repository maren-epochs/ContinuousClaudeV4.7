# EVOLVE

Aggregate worker reports after task completes. Surface recommendations for user approval.

  glob continuum/autonomous/{task-id}/reports/*.json
  Extract: corrections[], issues[], conventions[] from each report
  Group by similarity, count frequency. Patterns confirmed at 3+ occurrences.

Patch fast path: with fewer than 3 reports the pattern threshold is unreachable — skip the
aggregation + recommendation presentation entirely and go straight to the checklist below.
The checklist ALWAYS runs in full.

For each confirmed pattern, recommend the highest enforcement tier:
  lint rule (eslint, ruff, clippy) > type system (tsconfig, mypy) > formatter (prettier, black) >
  pre-commit hook > CI check > CLAUDE.md (last resort — only when no tool can express it)

Present recommendations via AskUserQuestion:
  "Based on N worker reports, M confirmed patterns:
   1. {pattern} — Recommendation: {tool change} — Tier: {tier}
   2. ...
   Apply all / Select which / Skip evolve?"

User approves. Worker applies only approved changes. Then run this checklist in order:

  1. corrections → bloks report {lib} {error_type} "{description}" for each worker correction
  2. discoveries → verify each bloks new rule from research workers landed (bloks context .)
  3. new patterns → bloks new rule "{convention}" --tags {tags}
  4. issues → surface blocking items, create follow-ups
  5. bloks ack/nack — MANDATORY. For each card injected during PREPARE:
     - Worker used it and it was correct → bloks ack {card-id}
     - Worker found it wrong/outdated → bloks nack {card-id} (one nack hides a rule
       from future context); if the right answer is known, also `bloks new rule "<fix>"`.
       `bloks report` only works for registry-indexed library cards.
     - Worker never referenced it → skip (no signal)
     If PREPARE injected 0 cards (no bloks output), skip this step.
  6. readiness.sh again → diff against ASSESS baseline for health delta
     Greenfield: if ASSESS baseline was zero, STILL run — the delta IS the outcome.

Steps 1-5 are the knowledge feedback loop. Skipping them means the project doesn't learn.

Project ratchets with every task. Cards improve. Error surface shrinks.
