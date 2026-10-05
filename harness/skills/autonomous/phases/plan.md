# PLAN

Validation contract first — mission-level TDD. Done defined before code exists.

contract.json — single file, tracks full lifecycle:

  {
    "task": "Add user authentication",
    "complexity": "feature",
    "milestones": [
      {"name": "auth", "status": "pending", "assertions": ["VAL-001", "VAL-002"]}
    ],
    "assertions": [
      {"id": "VAL-001", "type": "invariant", "text": "Auth tokens never in logs",
       "milestone": "auth", "status": "pending", "depends": [],
       "worker": null, "evidence": null},
      {"id": "VAL-002", "type": "behavioral", "text": "Login redirects to dashboard",
       "milestone": "auth", "status": "pending", "depends": ["VAL-001"],
       "presentation": "agent-browser", "worker": null, "evidence": null},
      {"id": "VAL-003", "type": "approval", "text": "User approves design direction",
       "milestone": "design", "status": "pending", "depends": [],
       "presentation": "variants", "variants": 3, "medium": "agent-browser",
       "worker": null, "evidence": null}
    ]
  }

Lifecycle — who updates contract.json:
  PLAN creates it (all assertions pending)
  EXECUTE sets worker field on assertions it dispatches
  Workers never touch contract.json — they write reports/ only
  VALIDATE reads reports, flips assertion status to passed/failed, fills evidence
  EVOLVE reads final state, feeds corrections to bloks

Types: invariant (test), behavioral (e2e), contract (schema), property (prop test),
fuzz (fuzz harness), approval (human — AskUserQuestion, agent-browser, or screenshot).

Aesthetic work: encode as milestone with type: approval assertions. Generate N variants
in parallel, present via medium, user picks or gives direction. Loop until approval passes.
Chosen direction becomes context for subsequent workers. Not a separate phase — just a
milestone that gates on human approval before implementation proceeds.

Decomposition: one task = one assertion. If "and" needed to describe work, two tasks.
Ask user which stages to approve. Encode as type: approval.
Assertions with depends: [] run first. Assertions depending on others wait.
Write: continuum/autonomous/{task-id}/contract.json
Multi-milestone also: continuum/autonomous/{task-id}/plan.md
