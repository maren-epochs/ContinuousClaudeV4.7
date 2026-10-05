# VALIDATE

Each milestone boundary, sequential — each gates the next:
  automated: test + typecheck + lint. Fail → stop here.
  assertion check: verify evidence in reports per type. Update contract.json status/evidence.
  scrutiny: review worker on milestone diff. Match contract? Regressions? Security?
  fix loop: targeted fix workers on failures. Max 2 rounds, then escalate via AskUserQuestion.
  rollback: if fix rounds exhausted and user declines, revert milestone via git reset to pre-milestone commit.
Write: continuum/autonomous/{task-id}/validation/{milestone}.json. Update contract.json.
