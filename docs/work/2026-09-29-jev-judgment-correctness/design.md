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

The subject reaches the row through `jev --subject NAME`, not through `--id`.
`jev.py` sends `{<id>: definition}` as the request's questions, so a
repository name in `--id` would reach the endpoint. `--subject` is held to the
identifier grammar, recorded as `question_id` in place of `--id`, and never
enters the payload; `jev record` and the shadow pair's baseline row take it
too. (Changed after the plan: the first draft put the subject in `--id`.)

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

`bin/sd_jev.py` passes `--id sd-review-tier --subject
sd-review-tier:<owner>.<repo>:<sha12>`, built from the repository's `origin`
remote and `HEAD`. GitHub owners cannot contain `.`, so the first `.` splits
owner from repository. When the remote is not GitHub or the subject would
pass 96 characters, it passes no `--subject`, and the row keeps
`sd-review-tier`, which the labeller skips.

The pack change lands after the system change. An older `jev` refuses the
unknown `--subject` flag; `sd-review` then declines the reading loudly and
keeps the routed tier, so the wrong order costs readings, never a wrong tier.

## Labeller: `jev label sd-review`

A subcommand of `local-jev/jev.sh`, run by the `local-jev` interpreter so it
can call `sd-db.sh`. It lives in `local-jev/jev_label.py`, beside `jev.py`
and not in it, so `jev.py` stays standalone. For each unlabelled `JEV_SD_REVIEW` row with a subject:

1. Find the pull request that contains the commit
   (`gh api repos/<owner>/<repo>/commits/<sha>/pulls`). None, or not merged:
   skip with `not merged`; after 30 days, `abandoned`.
2. Merged less than 14 days ago: skip with `window open`.
3. List the default-branch commits in the 14 days after the merge commit that
   touch any file of the pull request, in the local checkout
   (`git log <merge>..origin/<base> --since --until -- <files>`).
   A commit is a fix when its subject starts with the word `fix` (`fix:`,
   `fix(scope):`, `Fixes`) or says `revert`, case-insensitive. The word
   boundary is an addition: `Fixture data` starts with `fix` and is not one.
4. Label as the design above says, with source `outcome.sd-review.14d`.
   The ledger holds a position, not the tier list, so the deepest position
   is `--deepest N`, default 4 for the default order `skip`, `cheap`,
   `standard`, `deep`. (Added: the plan did not say where the labeller
   learns the deepest tier.)
5. Skip a row with no numeric answer (`no answer`, a declined reading) and a
   checkout whose `origin/<base>` does not contain GitHub's head of `<base>`
   (`checkout behind`, from `gh api repos/<owner>/<repo>/branches/<base>`).
   (Added: a stale checkout would read a fix it has not fetched as no fix.)

The checkout comes from `sd-db.sh repo list` by matching the `origin` slug. No
checkout: skip with `no checkout`. `gh` missing or failing: skip the row,
never label it.

Nightly: `local-cron-jobs/examples/` gains a `jev-label-nightly` job running
`jev label sd-review --apply` at 03:30, after `repo-sync-nightly`; the
operator copies it into the config folder.

`by_stage` also lists each stage's label `sources`, and the report prints a
rule's known limit from `LABEL_LIMITS` next to the stage it labelled, so the
ledger names no stage. `label` refuses a gate row: it is not a decision.

## Tests

- `local-sd-db`: `label` writes, repeats, refuses a changed value, refuses a
  non-number and a non-identifier; `unlabelled` filters by prefix; `by_stage`
  counts right and bands.
- `local-jev`: the labeller on a fixture repository with a stub `gh` on `PATH`
  and a stub `sd-db.sh` (or a temporary database): right, wrong, deepest tier,
  not merged, window open, no checkout, and a dry run that writes nothing.
