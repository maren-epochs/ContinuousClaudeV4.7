---
name: changelog
description: Changelog / release notes from git log between two refs, grouped by conventional-commit type, one plain line per change - "changelog", "release notes", "what changed since", "what's new"
user-invocable: true
allowed-tools: [Bash, Read, Write, AskUserQuestion]
---

Turns the commits between two refs into a short markdown changelog a person can read. No helper script: `git log` gives everything needed.

**1 RANGE.** `to` = the ref the user names, else `HEAD`. `from`, first match wins:
1. a ref, tag, date ("since Monday" -> `--since`) or commit the user names;
2. the newest tag: `git describe --tags --abbrev=0` (exit 128 = no tags, go on);
3. in the harness repo, `source_sha` in `~/.claude/fleet/share-snapshot.json` (the last share snapshot);
4. otherwise ask with AskUserQuestion: A last 20 commits (Recommended), B since a date, C since a ref.

Say which `from` was used in the first line of the output.

**2 READ.** `git log --no-merges --format='%h%x09%s' <from>..<to>` (add `-- <path>` when the user scopes it). Subjects only: trailers such as `Co-Authored-By:` and `Claude-Session:` live in the body and never appear. Zero commits -> say "no changes since <from>" and stop.

**3 GROUP.** By the conventional-commit type before the colon (`feat(fleet): ...` -> `feat`; a `!` marks a breaking change, list it under its group with "(breaking)"):

| Type | Heading |
|------|---------|
| feat | New |
| fix | Fixed |
| docs | Docs |
| test | Tests |
| refactor, perf | Changed |
| chore, build, ci, style | Maintenance |
| anything else | Other |

Headings in that order; skip empty ones.

**4 WRITE.** One line per change, rewritten in plain words for someone who did not write the code: drop the `type(scope):` prefix, start with what changed for the user, keep the scope as context where it helps ("Fleet: ..."), end with the short sha in backticks. Merge commits that are clearly the same change into one line with both shas. No commit-message jargon (VAL ids, internal function names) unless the reader is a developer of that code.

```markdown
## Changes since v4.7.1 (`fdb5aff`..`e6704dc`, 6 commits)

### New
- Fleet: `/fleet resume` restarts sessions stopped by the usage limit once it resets `9491828`

### Fixed
- Fleet: Windows Terminal tabs no longer split when a folder name contains `;` `e6704dc`
```

**5 OUTPUT.** Print the markdown. When the user names a file, Write it there (create parents; never overwrite an existing file without asking). Do not commit it unless asked.

The repo may be public: never add other project names, paths or account details that a commit subject might carry; reword them generically.
