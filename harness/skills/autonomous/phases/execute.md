# WORKER ARCHETYPES

Four worker shapes. Each activated by a specific prompt — every word load-bearing.

implement:
  Drive minimal correct code through failing tests. Each change justified by one assertion.

research:
  Decompose unknowns to primary sources. Synthesize into compact card — signatures, constraints, gotchas.
  Write findings to bloks: bloks new rule "{finding}" --tags {tags}.
  Use kind `rule`: bloks context only emits rule and taste cards, so a correction
  (what bloks learn writes by default) is stored but never read back by PREPARE.
  bloks learn also refuses any library it has not indexed from npm/PyPI/crates.io,
  so findings about build tools or platform behaviour cannot be recorded with it.
  One finding = one bloks card. "app.listen returns http.Server" is one card. Don't batch.
  If you discovered 5 things, call bloks new rule 5 times. Atomic cards compose; monoliths rot.

review:
  Audit diff against contract coldly. Find violations, implicit assumptions, regressions. Evidence only.

evolve:
  Harden infrastructure. Eliminate error classes through constraints and types. Measure reduction.


# EXECUTE

Task tool. Respect assertion depends: assertions with depends: [] first, then dependents.
Independent assertions (no mutual deps) MAY run parallel — but only if they touch disjoint files.
Before parallelizing: check if workers will modify the same files (especially main entry points like
main.rs, index.ts, app.py). If file sets overlap, serialize them even if assertions are independent.
Each sequential worker receives the PATH to the previous worker's report.

Worker prompt — structured JSON. Every field present. Null = no data, not forgotten.
All context fields are FILE PATHS to the captures PREPARE wrote — never pasted content.

  {
    "role": "{archetype spell}",
    "assertion": {"id": "VAL-001", "text": "Auth tokens never in logs"},
    "context": {
      "bloks_context": "continuum/autonomous/{task-id}/context/bloks-context.txt",
      "bloks_cards": [
        {"id": "card:reqwest:blocking", "path": "continuum/autonomous/{task-id}/context/cards/reqwest-blocking.txt"},
        {"id": "card:scraper:all", "path": "continuum/autonomous/{task-id}/context/cards/scraper-all.txt"}
      ],
      "conventions": "continuum/autonomous/{task-id}/context/conventions.md",
      "structure": "continuum/autonomous/{task-id}/context/structure.txt",
      "prior_report": null
    },
    "bounds": {
      "files": ["src/auth.ts", "src/middleware.ts"],
      "test_command": "npm test -- --grep auth",
      "tdd": true,
      "commit_after": true
    },
    "output": "continuum/autonomous/{task-id}/reports/{worker-id}.json"
  }

Field rules:
  role — one of: implement, research, review, evolve
  context.* — every field is a path written by PREPARE; workers read the ones they need.
  context.bloks_context — path to the verbatim capture. Null if bloks not available.
  context.bloks_cards — array of {id, path} objects. ID format matches bloks stats/ack/nack:
    card:{lib}:{module} for library cards, deck:{lib} for deck overviews,
    symbol:{lib}:{symbol} for symbol cards, or user card slug for learned cards.
    Workers reference these IDs in bloks_used so EVOLVE can ack/nack them.
    Empty array [] if no cards found for these libs.
  context.prior_report — path to the previous worker's report JSON (reports/{worker-id}.json)
    if this assertion depends on another. Null if first.
  bounds.files — orchestrator's best guess at affected files. Worker may touch others if needed.
  bounds.tdd — test must fail before implementation. False only for research/review archetypes.

Worker reads the JSON, reads the context files it needs, does the work, writes report to output path.
If blocked: report immediately with {"result": "blocked", "reason": "..."}.
If a bloks card is wrong: note in bloks_used with helpful: false.

Report — exact JSON, every field filled:

  {
    "task": "assigned task",
    "assertion": "VAL-003",
    "result": "success | partial | blocked",
    "implemented": "what was done",
    "remaining": "",
    "tests": {
      "added": [{"file": "auth.test.ts", "name": "rejects expired token", "verifies": "VAL-001"}],
      "command": "npm test -- --grep auth",
      "exit_code": 0
    },
    "checks": [
      {"command": "npm run typecheck", "exit_code": 0},
      {"action": "opened /login in agent-browser", "observed": "form renders"}
    ],
    "bloks_used": [
      {"card": "reqwest-blocking", "helpful": true},
      {"card": "motion-v12", "helpful": false, "reason": "animate() renamed to motion()"}
    ],
    "corrections": [{"block": "motion-v12", "issue": "animate() renamed to motion()"}],
    "discoveries": [{"lib": "express", "finding": "app.listen returns http.Server", "bloks_cmd": "bloks new rule \"express: app.listen returns http.Server\" --tags express"}],
    "issues": [{"severity": "non-blocking", "description": "flaky test auth.test.ts:42"}],
    "conventions": ["single quotes not double", "API handlers return {data, error}"]
  }

Last 5 fields — bloks_used, corrections, discoveries, issues, conventions — are EVOLVE inputs.
bloks_used → EVOLVE runs ack on helpful cards, nack + report on unhelpful ones
corrections → bloks report (card self-correction)
discoveries → bloks new rule (research workers write directly, EVOLVE verifies they landed)
issues → surface to user
conventions → enforcement tiers
