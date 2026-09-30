---
title: Record whether each Jev judgment was right, from later outcomes
created: 2026-09-29
item: sd:2107
---
# Design — Jev judgment correctness from later outcomes

## No migration

The columns exist (migration 011). `question_id` is "the caller's id for the
question, which code sees and the model does not", and is held to the
identifier grammar (`[A-Za-z0-9][A-Za-z0-9._:-]*`, at most 96 characters).
A per-subject id fits that definition better than today's constant
`sd-review-tier`, so the subject goes there and no column is added.

## What a label is

`override` is the answer an authoritative later source would have given, in the
shape of `answer`. A row is right when `override = answer`. For a `choice`
stage the answer is a criterion position, so the label is a position too.

The `sd-review` rule knows only one direction of error. When a later fix shows
the review missed something, the label is the next deeper position; when the
chosen tier was already the deepest, a later fix is not the tier's fault and
the label equals the answer. When no fix follows, the label equals the answer.
The report prints this limit next to the stage: "labels see missed problems,
not wasted depth".

## Ledger: `sd_db.judgment`

- `label(connection, row_id, override, source, *, replace=False, now=...)`:
  validates with `_answer` and `_identifier`, updates one row, and returns
  whether it changed anything. The same value again is a no-op; a different
  value without `replace` raises `JudgmentRefused`.
- `unlabelled(connection, stage, *, prefix)`: rows of a stage with no
  `override`, whose `question_id` starts with `prefix`, oldest first.
- `by_stage` gains `labelled` and `right` per arm, and a `bands` list:
  reported confidence in tenths, each with `labelled` and `right`.
- `sd-db.sh judgments label --row N --override V --source S [--replace]` and
  `sd-db.sh judgments unlabelled --stage S [--prefix P] [--json]` are the
  command surface; the labeller uses these and never opens the database itself.

## Subject: `sd-review` (pack)

`bin/sd_jev.py` builds `--id sd-review-tier:<owner>.<repo>:<sha12>` from the
repository's `origin` remote and `HEAD`. GitHub owners cannot contain `.`, so
the first `.` splits owner from repository. When the remote is not GitHub or
the id would pass 96 characters, it keeps `sd-review-tier`, which the labeller
skips.

## Labeller: `jev label sd-review`

A subcommand of `local-jev/jev.sh`, run by the `local-jev` interpreter so it
can call `sd-db.sh`. For each unlabelled `JEV_SD_REVIEW` row with a subject:

1. Find the pull request that contains the commit
   (`gh api repos/<owner>/<repo>/commits/<sha>/pulls`). None, or not merged:
   skip with `not merged`; after 30 days, `abandoned`.
2. Merged less than 14 days ago: skip with `window open`.
3. List the default-branch commits in the 14 days after the merge commit that
   touch any file of the pull request, in the local checkout
   (`git log <merge>..origin/<base> --since --until -- <files>`).
   A commit is a fix when its subject starts with `fix` or `Revert`, or says
   `revert`, case-insensitive.
4. Label as the design above says, with source `outcome.sd-review.14d`.

The checkout comes from `sd-db.sh repo list` by matching the `origin` slug. No
checkout: skip with `no checkout`. `gh` missing or failing: skip the row,
never label it.

Nightly: `local-cron-jobs/examples/` gains a `jev-label` job running
`jev label sd-review --apply`; the operator copies it into the config folder.

## Tests

- `local-sd-db`: `label` writes, repeats, refuses a changed value, refuses a
  non-number and a non-identifier; `unlabelled` filters by prefix; `by_stage`
  counts right and bands.
- `local-jev`: the labeller on a fixture repository with a stub `gh` on `PATH`
  and a stub `sd-db.sh` (or a temporary database): right, wrong, deepest tier,
  not merged, window open, no checkout, and a dry run that writes nothing.
