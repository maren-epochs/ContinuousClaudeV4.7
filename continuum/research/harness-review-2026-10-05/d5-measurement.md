# D5 measurement: main-context cost per skill (2026-10-05)

Question (decision-queue D5): fork `/review`, `/premortem`, `/research` into subagents to save main-context tokens, and set per-skill effort. Option (a) was "measure one long session first". This is that measurement.

## 1. Method and caveats

Tool: `py -3.13 tools/context_ledger.py <transcript> [--json]` (stdlib, streams the JSONL). Per main-thread assistant turn it records `context = input_tokens + cache_read_input_tokens + cache_creation_input_tokens` (the `used_percentage` formula) and the `attributionSkill` label; a SPAN is a maximal run of consecutive turns with one label, `delta` is the sum of per-turn context steps inside the span including the step into its first turn (the skill load; for span 1 this is the initial baseline). Drops larger than 20,000 tokens between turns are compaction events, listed separately and excluded from deltas (invariant: `sum(deltas) - sum(drops) == final context`).

Saved `--json` output (reproducible numbers): `ledger-0c00ffcb.json`, `ledger-a17b01cc.json`, `ledger-7889085a.json`, `ledger-405e2d7c.json` (the four required transcripts) plus `ledger-7931435e.json`, `ledger-96d4a227.json`, `ledger-787e3704.json` (supplementary, section 3). The "first-5" and "own work" columns below come from a scratch script over the same `Reader` (not part of the tool): first-5 = sum of the first five per-turn steps of a span (compaction steps zeroed, the transcript's turn-1 baseline zeroed).

Caveats, stated plainly:

- **Attribution is sticky, and it also clears.** `attributionSkill` names the most recently invoked skill, not the skill doing the work. It stays on every later turn until the next `Skill` call **or until the label is cleared** (reverts to absent). Observed on builds 2.1.278, 2.1.289 and 2.1.290: spans end with a `(none)` span, not only with another skill. What triggers the clear is not determinable from the transcript. Consequence: a skill span is an upper bound on the skill's own cost (it can include orchestrator work done after the skill returned), and `(none)` spans after a skill are unattributed orchestrator work, not the previous skill.
- **Main thread only.** Subagent (worker, oracle) transcripts live in `<slug>/<session>/subagents/` and are never in the main file; `isSidechain` was false on every entry. Worker tokens are excluded by construction. The fork's own cost (section 5) is therefore estimated from turn-1 baselines, not measured.
- **Compactions** reset context; their drops are excluded from deltas, so a label's delta over a long session can exceed any context window (it is cumulative growth, not peak).
- **Context, not output.** The ledger measures prompt-side context. The "per-skill effort" half of D5 affects output/thinking tokens, which the ledger reports only as a session total and cannot attribute. That half of D5 is not answered here.
- The current session was live during measurement: the saved JSON is a snapshot at 130 turns; the first-5 script ran at 134 turns (only the final `(none)` span differs).
- Build 2.1.289 and older carry `attributionSkill` on almost no turns; the three largest transcripts are from other projects (project-A, project-B) and never invoked the D5 skills. Hence section 3.

## 2. Transcripts

### 2.1 Current session: `<projects>/this-project/0c00ffcb.jsonl`

build 2.1.290, 130 turns (0 skipped), peak 156,792, total output 186,330, 0 compactions. This session ran `/resume-handoff`, then `/autonomous` (which invoked `/premortem`), then spawned workers.

SPANS (all 5)

| # | label | first-last | turns | start | end | delta | first-5 | in-span growth (end-start) |
|---|-------|-----------|------:|------:|----:|------:|--------:|------:|
| 1 | resume-handoff | 1-25 | 25 | 64,291 | 84,627 | 84,627 (incl. 64,291 baseline) | 967 | 20,336 |
| 2 | (none) | 26-28 | 3 | 87,433 | 87,433 | 2,806 | 2,806 | 0 |
| 3 | autonomous | 29-43 | 15 | 90,012 | 96,024 | 8,591 | 4,238 | 6,012 |
| 4 | premortem | 44-78 | 35 | 98,058 | 124,347 | 28,323 | 3,998 | 26,289 |
| 5 | (none) | 79-130 | 52 | 125,887 | 156,792 | 32,445 | 4,945 | 30,905 |

SKILLS

| label | delta | spans | turns |
|-------|------:|------:|------:|
| resume-handoff | 84,627 | 1 | 25 |
| (none) | 35,251 | 2 | 55 |
| premortem | 28,323 | 1 | 35 |
| autonomous | 8,591 | 1 | 15 |

Span boundaries, from the tool calls in the transcript: turn 28 `Skill:autonomous` -> span 3; turn 43 `Skill:premortem` -> span 4. Inside span 4 the premortem's own work (survey scripts, writing `premortem.yaml`) ends at turn 55 (context 104,355); turns 56-78 are PREPARE (reading SKILL.md, writing context captures, `Agent:worker` at 70-71, `bloks stats` at 76) still carrying the `premortem` label. Premortem own cost: 104,355 - 96,024 = **8,331 tokens over 12 turns**; the other 19,992 of the span's 28,323 is orchestrator work. The `autonomous` span is ASSESS + PLAN only (it ends at the premortem call).

### 2.2 Largest: `<projects>/project-A/a17b01cc.jsonl` (195 MB)

build 2.1.218, 15,176 turns (0 skipped), peak 934,047, total output 10,303,922, 28 compactions (18 to 0, 9 to 83-112K, one `559,391 -> 522,577` at turn 15156). Parse: ~1.1 s. No D5 skill spans; listed for scale.

SPANS, 15 largest deltas of 26

| # | label | first-last | turns | start | end | delta |
|---|-------|-----------|------:|------:|----:|------:|
| 21 | (none) | 7358-12215 | 4,858 | 528,133 | 603,529 | 5,817,908 |
| 25 | (none) | 12346-15162 | 2,817 | 722,433 | 525,890 | 4,718,825 |
| 19 | (none) | 2992-7351 | 4,360 | 500,149 | 520,823 | 4,010,576 |
| 17 | (none) | 1965-2987 | 1,023 | 582,667 | 484,215 | 2,226,277 |
| 15 | (none) | 1434-1958 | 525 | 118,778 | 573,261 | 455,052 |
| 3 | (none) | 56-433 | 378 | 74,466 | 315,566 | 391,327 |
| 7 | (none) | 576-1319 | 744 | 424,701 | 801,501 | 378,172 |
| 5 | (none) | 446-567 | 122 | 328,766 | 412,266 | 84,218 |
| 23 | (none) | 12247-12342 | 96 | 629,350 | 711,249 | 82,366 |
| 12 | doctor | 1379-1423 | 45 | 908,599 | 107,694 | 60,361 |
| 1 | (none) | 1-35 | 35 | 42,708 | 50,000 | 50,000 (incl. 42,708 baseline) |
| 8 | update-config | 1320-1326 | 7 | 849,962 | 851,063 | 49,562 |
| 22 | hookify:writing-rules | 12216-12246 | 31 | 608,130 | 628,883 | 25,354 |
| 2 | fewer-permission-prompts | 36-55 | 20 | 53,584 | 73,534 | 23,534 |
| 26 | create-handoff | 15163-15176 | 14 | 529,326 | 544,568 | 18,678 |

SKILLS

| label | delta | spans | turns |
|-------|------:|------:|------:|
| (none) | 18,243,387 | 13 | 15,004 |
| doctor | 60,361 | 1 | 45 |
| update-config | 49,562 | 1 | 7 |
| fewer-permission-prompts | 34,597 | 2 | 28 |
| schedule | 34,000 | 3 | 28 |
| artifact-design | 31,485 | 3 | 16 |
| hookify:writing-rules | 25,354 | 1 | 31 |
| create-handoff | 18,678 | 1 | 14 |
| frontend-design:frontend-design | 10,370 | 1 | 3 |

### 2.3 Second largest: `<projects>/project-B/7889085a.jsonl` (46 MB)

build 2.1.280, 4,454 turns (0 skipped), peak 881,797, total output 5,231,261, 4 compactions (`801,002 -> 0` at 828; `877,127 -> 64,882` at 913; `881,797 -> 61,292` at 2127; `881,393 -> 64,580` at 3581).

SPANS (all 2)

| # | label | first-last | turns | start | end | delta | first-5 | in-span growth |
|---|-------|-----------|------:|------:|----:|------:|--------:|------:|
| 1 | resume-handoff | 1-126 | 126 | 60,228 | 205,418 | 205,418 (incl. 60,228 baseline) | 4,620 | 145,190 |
| 2 | (none) | 127-4454 | 4,328 | 206,546 | 657,818 | 3,702,965 | 4,101 | 3,701,837 |

SKILLS: (none) 3,702,965 / 1 span / 4,328 turns; resume-handoff 205,418 / 1 / 126. The 126-turn `resume-handoff` span is the sticky-attribution case in its purest form: resuming a handoff does not take 126 turns; the label persisted over ordinary work until it cleared.

### 2.4 Third largest: `<projects>/project-B/405e2d7c.jsonl` (46 MB)

build 2.1.288, 6,297 turns (0 skipped), peak 881,974, total output 4,827,076, 3 compactions (`881,649 -> 67,317` at 1387; `881,974 -> 62,697` at 3006; `880,192 -> 60,411` at 4794).

SPANS (all 4)

| # | label | first-last | turns | start | end | delta | first-5 | in-span growth |
|---|-------|-----------|------:|------:|----:|------:|--------:|------:|
| 1 | resume-handoff | 1-24 | 24 | 60,141 | 87,596 | 87,596 (incl. 60,141 baseline) | 15,369 | 27,455 |
| 2 | (none) | 25-6266 | 6,242 | 88,817 | 828,005 | 3,193,799 | 1,937 | 3,192,578 |
| 3 | create-handoff | 6267-6269 | 3 | 830,457 | 835,254 | 7,249 | 7,249 | 4,797 |
| 4 | (none) | 6270-6297 | 28 | 835,691 | 845,155 | 9,901 | 907 | 9,464 |

SKILLS: (none) 3,203,700 / 2 / 6,270; resume-handoff 87,596 / 1 / 24; create-handoff 7,249 / 1 / 3.

## 3. Supplementary: every transcript on this machine with a D5-skill span

A search over all 37 transcripts in 11 project folders (936 MB) for `"attributionSkill":"(review|research|premortem|autonomous|...)"` found: `premortem` only in the current session; `research` only in project-C `7931435e`; `review` in **no transcript**; `autonomous` in four. The three additional files were run through the ledger (JSON saved alongside).

| transcript | build | turns | peak | compactions | D5-relevant spans (first-last, turns, delta, first-5, load step) |
|------------|-------|------:|-----:|------:|------|
| `<projects>/project-C/7931435e-...jsonl` (10.5 MB) | 2.1.278 | 1,416 | 874,523 | 2 | autonomous 349-359, 11, 11,730, 8,202, 6,160; research 950-968, 19, 22,183, 6,447, 3,695; research 1051-1078, 28, 28,568, 9,026, 2,941 |
| `<projects>/home-dir/96d4a227-...jsonl` (3.5 MB) | 2.1.289 | 397 | 450,580 | 1 | autonomous 34-58, 25, 22,758, 11,942, 6,422 |
| `<projects>/home-dir/787e3704-...jsonl` (5.9 MB) | 2.1.289 | 712 | 488,493 | 0 | autonomous 358-376, 19, 12,213, 3,818, 2,405 |

Both `research` spans end immediately after `SendUserFile` (the skill's final step) and are followed by `(none)`, so they are close to pure measurements of `/research`: ~1,000-1,170 tokens per turn, 22-29K per invocation. Every `autonomous` span ends at the first nested Skill call or at a clear, i.e. it covers ASSESS+PLAN(+PREMORTEM call) only; its load step (6.2-6.4K on two of four) is the largest of the measured ccv47 skills because `/autonomous` SKILL.md is the longest.

## 4. Focus table (D5 skills, `/autonomous` for reference; medians across all 7 transcripts)

| label | spans | median turns/span | median delta/span | median first-5 growth | median load step | own-work bound |
|-------|------:|------:|------:|------:|------:|------|
| review | 0 | - | - | - | - | unmeasured: no span on this machine |
| premortem | 1 | 35 | 28,323 | 3,998 | 2,034 | 8,331 over 12 turns (section 2.1); span remainder is PREPARE |
| research | 2 | 23.5 (19, 28) | 25,376 (22,183; 28,568) | 7,736 (6,447; 9,026) | 3,318 | whole span is research work: 22-29K |
| autonomous | 4 | 17 (15, 11, 25, 19) | 11,972 (8,591; 11,730; 22,758; 12,213) | 6,220 (4,238; 8,202; 11,942; 3,818) | 4,370 | ASSESS+PLAN only; nested skills and `(none)` carry the rest |

Reference baselines: context at turn 1 (system prompt + CLAUDE.md + injected handoff, before any skill) was 64,291 / 60,228 / 60,141 / 52,364 / 58,546 / 57,232 / 42,708 across the seven transcripts, i.e. **52-64K on current builds**. A forked subagent starts from a comparable baseline of its own.

## 5. Verdict

**Do not fork `/premortem` or `/research` now. `/review` cannot be decided from data (zero spans); default to not forking it either until it is measured.** The per-skill effort half of D5 is unanswered by this measurement (context-side only).

Reasoning, bounded by span length and first-N growth:

1. Per-invocation main-context cost is small. `/premortem` own work: 8.3K (upper bound 28.3K if the whole sticky span is charged). `/research`: 22-29K per invocation. First-5-turn growth (load + initial reads) is 4-9K for both. Against this session's 156,792 peak that is 5-18 %; against the 880-934K peaks of the long sessions it is under 3 %.
2. Forking costs more total tokens than it saves. A subagent carries its own 52-64K baseline (section 4), must re-read whatever the orchestrator already holds (premortem needs plan.md and the contract; research needs the question and prior findings), and hands back a report that re-enters main context (worker report JSON files in this session are 2.8-8.6 KB, roughly 1-3K tokens each). Main-context saving per fork is at best 8-29K; total-token cost per fork is at least ~55-70K. On a 1M window there is no main-context pressure to justify that; on a 200K window one fork buys roughly 4-15 % of the window.
3. The spend is elsewhere. In every transcript the `(none)` spans (unattributed orchestrator work: tool results, worker reports, file reads) dominate: 35K of 157K here, 3.2-18.2M cumulative in the three largest. `/resume-handoff` in-span growth was 20K / 145K / 27K / 16K / 13K, of which the 145K case is sticky-attribution noise. Forking the three D5 skills would not move these numbers.
4. Span length argues the same way: D5-skill spans are 12-35 turns and end on their own (SendUserFile, the premortem.yaml write, the next Skill call). They are short, bounded phases, not long-running loops where a subagent's isolation pays off.

## 6. What would change the verdict

- A measured `/review` span. If `/review` routinely grows main context by more than the ~55-65K a fresh subagent costs (plausible for a large diff: the review reads every changed file into main context), forking it becomes total-token-neutral and main-context-positive. One `/review` run on a real PR through the ledger settles it.
- Sessions that hit the 85 % `auto-handoff-stop` guard on a 200K window. There each 25K span is 12.5 % of the window, and a fork converts a forced handoff into continued work. None of the seven transcripts shows this pattern (the long ones ran on a 1M window; this one peaked at 157K).
- `/autonomous-research` loops (0 spans measured). If EVOLVE -> PLAN loops produce research spans of 100+ turns, the per-invocation number above no longer applies.
- Measuring the `subagents/` transcripts to replace the 52-64K fork-cost estimate with observed numbers; the ledger would need a `--subagents` mode.
- A build change that scopes `attributionSkill` to the skill's own turns (or a documented clear trigger). Then per-skill totals become exact and the own-work bound in section 2.1 no longer needs manual reconstruction from tool calls.
- Per-skill effort: needs output-token attribution per span (the ledger has `output_tokens` per turn but does not sum it per span). Adding `output` to the span shape would answer the second half of D5 without a new tool.
