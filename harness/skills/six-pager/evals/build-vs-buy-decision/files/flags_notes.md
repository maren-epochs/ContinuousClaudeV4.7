# Feature flags: rebuild or buy? (notes from 3 meetings + Slack, Oct-Nov)

Context: Fernhill (B2C budgeting app, 2.3M monthly active users, ~140 engineers in 18 teams). Our homegrown flag service "Toggle" was written in 2019 by two people, neither still here. Platform team (6 engineers, lead: Omar) owns it but nobody on the team knows the code well.

Problems:
- Two Sev-1 incidents this year caused by Toggle: April (flag cache didn't refresh, a half-built payments screen showed to 100% of Android users for 47 minutes) and September (Toggle DB failover took the config API down for 2h 10m, deploys frozen).
- No percentage rollouts by attribute, no audit log, no experiment support. Growth team runs experiments in a separate tool (cost $38K/yr) and hand-syncs flags.
- Flag count: 1,940 flags, ~1,100 believed stale. No ownership metadata.
- Engineers' survey (Sept): Toggle rated 2.1/5, lowest of all internal tools.

Options discussed:
A) Rebuild Toggle properly. Omar's estimate: 3 engineers x 5 months. Rafael (staff eng) said 2 engineers x 4 months "if we cut scope to parity + audit log". Then ongoing ~0.5 engineer to run it.
B) Buy a vendor (Vendor A, the market leader). List price quote $96K/yr for our seat count; procurement thinks they can get ~$78K. Includes experiments, so we could drop the $38K tool. Migration estimate: 2 engineers x 2 months, plus each team cleans up its own flags.
C) Self-host an open-source flag server (OpenFeature-compatible). Free license, hosting ~$9K/yr. 2 engineers x 3 months to set up + migrate, ongoing ~0.5 engineer. No experiment support out of the box.
D) Do nothing / patch: fix the cache bug and DB failover, ~1 engineer x 6 weeks.

Constraints & facts:
- Loaded engineer cost we use for planning: $210K/yr.
- Security/compliance: we are SOC 2 Type II; flag evaluation must not send user PII to third parties. Vendor A supports local evaluation in SDKs (no PII leaves) - security (Dana) confirmed OK in principle, needs DPA review (~3 weeks).
- Vendor A had a 3-hour outage in 2024 (public status page). Their SDKs fall back to last-known values.
- Platform team's H1 roadmap already has Kubernetes upgrade (needs 3 engineers through March).
- Decision needed by Jan 15 so it fits H1 planning. Decider: VP Engineering (Kim).
- Omar leans rebuild ("we control it"), Growth leans buy, Rafael leans open source.
