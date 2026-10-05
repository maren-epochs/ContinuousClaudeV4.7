# PREMORTEM

Run /premortem on contract.json + plan.md. Present findings to user via AskUserQuestion.
BLOCK or WARN: user confirms before proceeding. PASS: continue to PREPARE.

Patch class: use the inline fast path in SKILL.md instead — scan the contract for
tiger-class risk yourself; auto-pass when no tigers (record it in contract.json);
any tiger found → run this full phase.
