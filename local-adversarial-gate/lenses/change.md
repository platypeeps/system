## What to read

The document is a proposed change: the commits on one branch against its base, and the pull-request body that describes them.

- The diff: run `git diff {BASE}...{HEAD}`, and `git log --oneline {BASE}..{HEAD}` for the commits. Read the changed files in full where a hunk alone hides the context.
- `{INPUT_PATH}` — the branch, the head and base commits, the work item it belongs to, and the pull-request body, or a line saying none was given.
- The repository's `CLAUDE.md` and `.claude/rules/`, when they exist: the rules the change is held to.

The quoted line in each finding is a line from the diff or the body, with its path. You see this checkout, not the review threads or the machines the change will run on; say so when a finding depends on them.

## Attack it on these axes, in priority order

**1. Does the diff do what the body says?** Each claim in the body against the hunk that should carry it. A claim such as "removed", "no longer" or "every caller" needs a search of the tree; run it and quote it.

**2. Failure paths.** A failed probe, read or write that the change treats as a negative answer, a pass or exit 0; an error swallowed; a timeout with no bound.

**3. State that moves in steps.** If the change writes state in more than one step, what happens when the process stops between two of them? Does the next run finish it or undo it?

**4. What the previous version left behind.** Installed copies, scheduled jobs, old files, stored rows and a rollback to the previous commit. Does the change still work against them?

**5. Guards.** A deletion, a new path or an early return that skips a check the old code made. Name the check and whether a test still covers it.

**6. Tests.** Would each new test fail without the change? A test that passes on the base commit proves nothing about the fix. Name the test and the line that would still pass.

Give each hypothesis a `###` heading of its own: its rank and a short title, such as `### 1. The done check cannot fail`. The dashboard's note names the first three titles.

## Ending

End with one line: should this change merge as it stands, YES or NO. If NO, name the single hypothesis that blocks it.
