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

A label needs evidence of the right answer that does not depend on the
answer under test. A rule that computes the label from the prediction ("one
tier deeper than chosen") scores itself.

## Ledger: `sd_db.judgment`

- `label(connection, row_id, override, source, *, replace=False, now=...)`:
  validates with `_answer` and `_identifier`, updates one row, and returns
  whether it changed anything. The same value again is a no-op; a different
  value without `replace` raises `JudgmentRefused`.
- `label` refuses a row with no answer: a label on it could only be wrong.
  Labels compare as exact decimals (`2` is `2.0`; 2**53 is not 2**53 + 1).
- `unlabelled(connection, stage, *, prefix)`: rows of a stage with an answer
  and no `override`, whose `question_id` starts with `prefix`, oldest first.
- `by_stage` gains `labelled` and `right` per arm, and a `bands` list per arm:
  reported confidence in tenths, each with `labelled` and `right`. Rightness
  is decided in Python, since SQLite has no exact decimal.
- `sd-db.sh judgments label --row N --override V --source S [--replace]` and
  `sd-db.sh judgments unlabelled --stage S [--prefix P] [--json]` are the
  command surface; a labeller uses these and never opens the database itself.

## Subject: `sd-review` (pack)

`bin/sd_jev.py` passes `--id sd-review-tier --subject
sd-review-tier:<owner>.<repo>:<sha12>`, built from the repository's `origin`
remote and `HEAD`. GitHub owners cannot contain `.`, so the first `.` splits
owner from repository. When the remote is not GitHub or the subject would
pass 96 characters, it passes no `--subject`, and the row keeps
`sd-review-tier`, which names no subject.

The pack change lands after the system change. An older `jev` refuses the
unknown `--subject` flag; `sd-review` then declines the reading loudly and
keeps the routed tier, so the wrong order costs readings, never a wrong tier.

## Withdrawn: an `sd-review` tier labeller

The first cut shipped `jev label sd-review`: a merged change was wrong when a
commit within 14 days touched one of its files and began `fix` or said
`revert`, and the label was the next deeper tier. Review of #41 found two
blocking faults, and both hold:

1. File overlap and a fix-like subject do not show that the reviewed change
   caused the defect. One unrelated fix in a shared file marks every recent
   change that touched it wrong, as an authoritative override.
2. The label came from the prediction: the same evidence labelled answer 1 as
   2 and answer 2 as 3, and a deepest-tier answer was always right.

The labeller, its tests and its cron example are removed. The right review
tier is not observable from later commits. What is observable is outcome
evidence (a linked revert, a fix that names the change); recording that apart
from `override`, as evidence and not as a right answer, is a follow-up.
`LABEL_LIMITS` stays, empty, for the first rule that has a known blind spot.

## Tests

- `local-sd-db`: `label` writes, repeats, refuses a changed value, refuses a
  non-number and a non-identifier; `unlabelled` filters by prefix; `by_stage`
  counts right and bands.
- `local-jev`: `--subject` is absent from the request and present as the row's
  `question_id`, on every primitive, on `record`, and on both shadow arms.
