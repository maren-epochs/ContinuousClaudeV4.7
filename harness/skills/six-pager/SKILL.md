---
name: six-pager
description: Writes an Amazon-style six-page narrative memo (six-pager) plus the meeting background and agenda for the silent-reading review meeting it is written for. Use whenever the user asks for a six-pager, 6-pager, narrative memo, Amazon-style memo, OP1/OP2 or annual operating plan narrative, business review memo, decision memo, or "a doc instead of a deck" for a leadership or board review, even if they do not say "Amazon". Also use when they ask how Amazon runs memo meetings or want an agenda for a silent-reading meeting. Not for a PR/FAQ press release, a one-page status update or a slide deck.
user-invocable: true
allowed-tools: [Read, Write, Edit, Bash, Glob, Grep, AskUserQuestion]
---

# Six-pager

An Amazon six-pager is a narrative memo of at most six pages, read in silence at the start of the meeting, then discussed. Full sentences expose missing reasoning that slides hide. The readers are senior and skeptical: they stop trusting a memo at the first wrong number or the first place it disagrees with itself, and they remember the one comparison that settled the question. Everything below protects those two things.

Deliverables, in the user's folder or else the current directory: `<slug>-six-pager.md` (body at most six pages, appendix unlimited), `<slug>-meeting.md`, and `<slug>-figures.py` with its output `<slug>-figures.txt`.

## Work plan: about eight tool calls

Quality comes from thinking once, carefully, before writing, not from rewriting. Do the hard thinking (steps 1-2) before the first draft, as a compact list (one line per item, not prose plans), draft once with every rule below applied, check mechanically, fix only what the check and the checklist find, and stop. Budget: about ten tool calls in total. Do not list or explore the skill folder, write scratch or brief files, re-read files already in context, re-plan the memo after drafting, or redraft whole sections.

1. Read every input file in one call
2. Frame (in your head, no notes file): purpose and ask; decider; type and structure (table below); every fact with its source; the runner-up option; the deciding comparison; the key assumption. If the purpose or decider is unclear and no input answers it, ask once with AskUserQuestion (A-D options, batched); otherwise state your assumption in the final reply
3. Write `<slug>-figures.py` and run it in the same Bash call (`py -3.13 <slug>-figures.py > <slug>-figures.txt; cat <slug>-figures.txt`)
4. Write the memo once, complete, applying the rules below
5. Run the checker: `py -3.13 <skill-dir>/scripts/check_memo.py <slug>-six-pager.md --sources <inputs...> <slug>-figures.txt`
6. One fix round: fix every FAIL, every untraced number, and every checklist item the draft misses, with targeted Edits. If a figure is wrong, fix the ledger, re-run it and the checker in one call
7. Write the meeting doc (template below)
8. Reply

`references/` holds the longer rationale and examples (structures, style, meeting); open one only when the type or a rule is unclear.

## The figure ledger

`<slug>-figures.py` defines each input as a named constant with its source in a comment, each estimate as a constant marked `# ESTIMATE: <basis>`, computes every derived number the memo uses, and prints them as a labeled table, rounded values included. The memo quotes only inputs and ledger output; arithmetic in your head is where memos go wrong.

Missing numbers: data someone must pull becomes a visible placeholder `[TBD: what, period]`; a magnitude the reader needs to judge the ask (its cost, effort, size of the problem) becomes a labeled estimate with its basis. A memo whose ask has no size cannot be approved. Claim only what the inputs support ("partly because" stays "partly"; an unmentioned vendor feature is a thing to verify, not a fact). When inputs conflict, pick one, say why, put the other in the appendix.

## Framing: the three things to settle before drafting

- **Runner-up**: the option closest to the recommendation on the measure that matters, not the easiest to beat and not "do nothing" when several ways to act exist
- **Deciding comparison**: one number set against another that settles recommendation vs runner-up (break-even volume vs evidenced demand; the volume cut a plan needs vs the size of the largest fixable cause; the price at which the runner-up wins vs the quote). If it rests on estimates, say so in the same sentence and give a range
- **Key assumption**: the one assumption that flips the choice to the runner-up, with its flip value. Same words in the opening and the risks section

## Structures (pick one; rename headings to fit)

- **Business review / operating plan**: Introduction; Goals (last period target vs actual table, next period goals); Tenets; State of the business; Lessons learned; Strategic priorities (options, what we will not do, resources); Appendix and FAQ
- **Decision or proposal**: Introduction; Background and why now; Tenets (criteria agreed before comparing); Options (three or more real ones, including doing nothing, one comparison table); Recommendation; Risks and what would change our mind; Plan, goals and resources; Appendix and FAQ
- **New initiative**: as decision, with Customer problem (who, evidence, size) before Options and How we measure success after the plan. A customer-facing launch pitch is a PR/FAQ, a different document: offer it instead

## Writing rules

- **Opening paragraph**: purpose, the ask with its size, the recommendation, the deciding comparison, the key assumption. About six sentences and at most three numbers
- **Prose, not bullets**: bullets only in Goals, Tenets and the appendix; tables for numbers that compare. Each paragraph opens with its point; topic sentences read in order tell the argument
- **Numbers, not adjectives**: every claim has its number, its comparison (vs plan, last period, the alternative) and its so-what. No weasel words (significantly, very, many, several, robust, seamless, leverage). Round to what the decision needs ($6.94M, not $6,937,843). Most sentences under 20 words, few over 30
- **Like for like**: every option carries the same cost categories over the same period and time basis (first-year counted from the decision date, steady-state from the same year; Q4 vs Q4, not full-year totals vs year-end counts). Show first-year and run-rate when timing differs. A missing category gets a labeled estimate, not zero. An item on one side only is stated both ways
- **Show the downside**: the options table shows the recommendation if its key assumption holds and if it fails; flip points are stated against the runner-up; the cost of being wrong is computed
- **Tenets** are criteria the losing side's advocate would also accept. A tenet only the recommendation can satisfy makes the argument circular
- **Goals** are specific, dated, owned by one role, and labeled evidence-backed (and reachable on the memo's own numbers) or judgment target
- **Candor**: misses in the team's own voice with cause and what changes; the strongest objection stated as its best advocate would, then answered; what the recommendation still costs the people who live with it; every story used as evidence matches the data
- **Plan fits the calendar**: effort fits its window (holidays, named commitments); work that depends on a result starts after the result is due
- **Length**: body 1,500-2,500 words (about 550 per page, so three to four and a half pages); six pages is a ceiling, not a target, and a shorter memo that settles the question is better. Appendix: a short computations summary pointing to the ledger, any input conflicts, and an FAQ of at most five hard questions

## Checklist for the fix round (step 6)

Scan the draft once against this list: opening has ask, size, recommendation, deciding comparison and key assumption; key assumption identical in opening and risks; options like for like with both outcomes of the recommendation shown; no claim contradicted by the memo's own table ("only option" claims especially); every "X is cheaper / higher / more than Y" sentence matches the ledger in direction as well as size; the deciding comparison sets the recommendation against the runner-up alone, without bundling in actions that suit either option; every number in the ledger or inputs; every goal labeled; dates and durations agree; a meeting date that falls on a weekend is flagged.

## Meeting doc (`<slug>-meeting.md`, half a page to one page)

Amazon practice (Bezos, 2017 letter): no presentation; the memo is handed out at the start and read in silence "in a kind of study hall", about 3 minutes per page; readers mark comments; the author answers questions rather than presenting; the most senior person speaks last; the meeting ends with decisions, owners and dates. Sections: header (date or `[TBD]`, length, decision requested in the memo's words, decider); Background (2-4 sentences); Roles table (decider, author, readers, note-taker, experts as needed); Agenda table with times (open 3 min, silent reading sized to the page count, discussion biggest questions first, decision, 5-minute read-back); Ground rules; Decision log table (decision or open question, owner, due); and, when the group is new to the format, a 4-6 sentence advance note explaining it.

## Reply

File paths, page estimate, the ask in one sentence, the deciding comparison, every placeholder, every labeled estimate to confirm, every assumption. Do not paste the memo into the chat.
