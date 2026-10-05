# PREPARE

Front-load. Workers never discover what you already know.
Context by reference: capture each source ONCE to a file on disk; worker prompts carry
PATHS to those files (see phases/execute.md), never the pasted content.

Create continuum/autonomous/{task-id}/context/ and write each capture via shell redirect:

  bloks context .                > context/bloks-context.txt      project rules, tastes, corrections
  bloks recipe {lib} {keywords}  > context/cards/recipe-{lib}.txt  task-specific API docs + user recipes
  bloks card {lib} {symbol}      > context/cards/{card-slug}.txt   symbol-level signatures + gotchas
  tldr structure {path}          > context/structure.txt           affected module structure
  CLAUDE.md excerpt (test/build commands, project rules) > context/conventions.md
  ouros session if research needed → returns compact card → context/cards/
  previous worker reports for sequential deps → already on disk at reports/{worker-id}.json;
    pass that path as prior_report — do not copy or re-emit it

Do not summarize or rewrite bloks output. Redirect verbatim. The cards are already compressed.
If a command returns empty (no cards for this lib), delete the empty file and pass null for
that field — don't fill it manually.

One capture, many readers: every worker that needs a capture reads the same file by path.
A path costs ~50 tokens in a prompt; a pasted capture costs thousands, duplicated per worker.
