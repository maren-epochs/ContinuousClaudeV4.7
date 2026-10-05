# VALIDATE

Each milestone boundary, sequential — each gates the next:
  report schema: py -3.13 tools/validate_report.py reports/{milestone workers}.json --contract contract.json
    Exit 1 → re-dispatch that worker ONCE with the ERROR lines: rewrite the report only, no code changes.
    Still exit 1 → orchestrator normalizes the report by hand, records "report_normalized": [worker-ids]
    in the validation file. WARN lines never gate. Reports are EVOLVE input — never feed it an invalid one.
  automated: test + typecheck + lint. Fail → stop here.
  assertion check: verify evidence in reports per type. Update contract.json status/evidence.
  scrutiny: review worker on milestone diff. Match contract? Regressions? Security?
  fix loop: targeted fix workers on failures. Max 2 rounds, then escalate via AskUserQuestion.
  rollback: if fix rounds exhausted and user declines, revert milestone via git reset to pre-milestone commit.
Write: continuum/autonomous/{task-id}/validation/{milestone}.json. Update contract.json.
