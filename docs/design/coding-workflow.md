---
title: The coding workflow
eyebrow: How work gets built here
stand: A change starts as a folder, becomes a row in a database, and reaches done only when a remote merge is confirmed. This is what happens in between, and where it stops.
---

## Start here

Two things track one change, and they are easy to confuse.

A **folder** under `docs/work/` holds the thinking: `prd.md`, and optionally
`design.md` and `implement.md`. The optional two are written only when asked
for. The `sd-plan` skill is explicit that it will not generate them to look
thorough.

A **row** in the shared `sd` database holds the status. The dashboard, the
runner and every session on this machine read that row without a network call.

The trap is that writing the folder does not create the row. Nothing creates
it for you. So a folder written today has no row and no readable status
until you run, from the repository root:

```sh
./local-sd-db/sd-db.sh work register docs/work/<item>/prd.md
```

Skip it and `sd-status` reports the item as `status-unreadable`. That is not
hypothetical: `2026-09-10-held-bumps-watch-their-blockers` cost a hand-written
`INSERT` into the shared database to undo.

@diagram coding-pipeline

## The row and what moves it

Registration takes the title and date from the prd's frontmatter and the
commit from git, and files the item as `planning`. It resolves the repository
by the checkout's `git remote get-url origin` rather than by disk path, so a
runner clone of the same repository maps to the same row. It is idempotent —
running it twice reports the row that already exists.

It records `branch:` only when the checkout is on a branch that is not the
default and not detached. Otherwise `branch` stays NULL until
`sd work register` sets it. It never writes `origin/main`; that was
a real bug, and repairing it meant fixing 65 rows.

A work item moves through six statuses:

| Status | What it means | What moves it there |
|---|---|---|
| `planning` | The item exists; no branch, no work | `work register`, always |
| `ready` | Acceptance criteria stated, no open `BLOCKING:` line | `sd-plan`, as a deliberate promotion |
| `in_progress` | Work started; the row carries a `branch:` | Branch created |
| `ready_to_send` | An author session finished cleanly | The runner, never a person |
| `blocked` | A hard stop, with its evidence named | The runner, or a manual block |
| `done` | Terminal | Verified delivery or explicit cancellation |

Two of those deserve emphasis.

**`ready_to_send` is written only by the runner.** It is the trigger that an
automatic-merge sweep watches, and it exists for work items only. The
narrower status set used by tasks, personal items and followups has no such
value.

**`done` cannot be hand-set.** A direct status write to `done` on a work item
is refused outright — completion needs `deliver_work` with verified remote
merge ancestry, or `cancel_work` with an explicit cancellation receipt. A
whole-item merge that carried `Item:` where `Delivers:` was meant completes
only through `deliver_associated_work`, which takes a reason, checks the same
ancestry and the `Item:` trailer, and marks the receipt as after the fact. Once
the item is done, re-delivering with a matching outcome is a no-op, and
re-delivering with a different one is refused rather than silently
reclassified. A task or followup cancels through the same `cancel_work`,
called with `task_guard` (sd:1005); its receipt drops if the task reopens.

## The gates, in the order they run

A change meets deterministic gates before any model is asked anything.

**`sd-check`** runs whatever this repository calls check, test or lint. It
reports one of four states, and `absent` is deliberately not a failure. If it
fails, `sd-review` stops there and no provider is dispatched — a failing
deterministic gate is a failing review, and there is no point asking a model
to guess at a change that does not build.

**`sd-review`** classifies the diff into a tier, resolves a reviewer chain,
dispatches, and disposes of each finding locally as blocking or advisory. It
never posts to GitHub. The full detail lives in
[Code review](code-review.html).

**`make check`** is the merge gate. `sd-ship merge` runs it through `sd-check`
and posts `sd/local-gate`, the one required check; GitHub Actions CI is off.
It runs `tests/check.sh`: one preflight, then four legs in parallel, plus the
macOS-only suites on a Mac. The preflight runs once, before any leg:

1. `sd-docs-lint`, from the repository root with no `--work-dir`
2. `tests/test_jev_contract.py`
3. `tests/test_product_name.py`, which fails naming each tracked line outside
   a `mezmo-*` folder that names the product
4. a guard that enumerates `*/tests/test_*.py` from the filesystem and fails
   naming any folder no `run_suite` line names

The guard closes a specific failure: a suite that exists but was never
wired into a leg stays silently green. The guard reads the filesystem rather than
a list, so it cannot drift behind the tree.

A skipped test fails the check. That is deliberate, and `run_suite` refuses both a
`skipped=` count above zero and a missing `Ran N tests` summary line.

## Two permissions, commonly conflated

Merging without a human clicking merge depends on two independent switches,
plus a third thing neither can override.

- **`sd.merge_authorization`** is the *assistant's* grant, read with
  `sd config get`. `controlled` permits assistant merges for active, in-scope
  pull request work in repositories the user controls.
- **`repo.merge_policy`** is the *runner's* switch, a per-repository database
  column defaulting to `manual`. Only `auto` lets the unattended runner queue
  a merge assignment without a person triggering it.
- **Branch protection** on GitHub is neither of those, and nothing local
  overrides it.

Read the two settings; never infer merge permission from prose. They answer
different questions, and a document that says otherwise is wrong.

## What a pull request must say

One line, at the start of a line, naming the row it advances:

```
Work: sd:402
```

The value is either a `docs/work` path resolving to a directory with a
`prd.md`, or `sd:<positive integer>`. `sd-docs-lint --pr-body` checks it,
and fails a second `Work:` line and a malformed id — `sd:0402` and `sd:+402`
do not pass.

Two things this rule does **not** do, both worth knowing:

- A body with **no** `Work:` line is a note, not a failure. The rule exists
  for the claim being wrong, not for the claim being absent.
- Nothing checks that the row exists or is yours. `sd-ship` writes the line
  for the item it is shipping; a hand-typed id is verified nowhere.

So the gate checks spelling. It does not check truth.

## Where this fits

The pack's own `docs/coding-to-release.md` is the outer loop: the generic
sequence of inspect, plan, isolate, check, review, ship, record, close out,
with the owner and exit condition for each step. This document is how *this*
repository fills those steps in — its `docs/work` layout, its runner, its four
check legs, its row-based tracking.

The pack also has a `docs/work/` of its own, governed by its own guide. It is
not the one described here.

## Status

**Verified in this build.** The status list and the refusal of a direct write
to `done`; the `Work:` line grammar and that an absent line passes; the
preflight steps and the unwired-suite guard.

**Not verified here.** Whether any individual repository currently sets
`repo.merge_policy` to `auto` — that is a per-repository database value, not a
property of this document. Read it rather than assuming.
