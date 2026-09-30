---
title: Record whether each Jev judgment was right, from later outcomes
created: 2026-09-29
item: sd:2107
---
# PRD — Jev judgment correctness from later outcomes

## Problem

The meter (sd:2087) writes one `judgment` row per Jev decision. It records the
answer and the confidence the model reported, but never whether the answer was
right. The `override`, `override_source` and `override_at` columns exist for
that, and every row has them empty: nothing writes them.

Reported confidence is not accuracy. jev-kit reports a set of all-wrong answers
with a mean reported confidence of 0.9744. Each caller sets `--unsure-below`
by guess; `sd-review` uses a fixed value. Without labels, no floor can be
checked and no stage can be shown to help.

A per-call human label does not scale and is not needed. For most stages a
later event says whether the judgment held.

## Requirements

1. A judgment row can carry a later, authoritative answer: `override` holds it
   in the same shape as `answer` (a number: a probability, a score or a
   position). `override_source` names the rule that produced it, and
   `override_at` says when.
2. The ledger owns the one write path for a label. It refuses a label for a
   row that does not exist, a value that is not a number, and a source that is
   not an identifier. Writing a label twice with the same value is a no-op;
   a different value is refused unless the caller says to replace it.
3. A caller whose judgment can be labelled passes a subject in `--subject`:
   an identifier that names the judged thing, never submitted content. It is
   recorded as the row's `question_id` and never sent. `--id` keeps its role
   as a key of the request, so a subject there would leave the machine.
4. `sd-review` is the first labelled stage. Its subject is the repository and
   the head commit it reviewed.
5. A labeller reads unlabelled rows of one stage, finds each subject's outcome,
   and writes a label only when the outcome is final. It reports what it
   labelled, what it skipped and why, and writes nothing without `--apply`.
6. `sd-db judgments` reports, per stage, how many rows are labelled and how
   many of those were right, and the same by reported-confidence band.
7. Nothing may depend on Jev: the labeller reads the ledger and git history,
   and calls no model.

## Out of scope

- Review-finding triage (sd:2092), ordering stages (sd:2091, sd:2094, sd:2095)
  and duplicate hints (sd:2093). Each needs its caller to exist first; each
  gets its own rule in the labeller when it does.
- Setting floors. A floor is chosen from the report after about 50–100
  labelled rows per stage; that is a follow-up, not code here.
- Detecting over-review. The `sd-review` rule sees a missed problem, not wasted
  depth; the design says so where the report prints.

## Acceptance

- A label written through the ledger shows in `sd-db judgments` for its stage.
- A second, different label for the same row is refused without `--replace`.
- The `sd-review` labeller, run on a fixture repository, labels a merged change
  with no later fix as right, one with a later fix touching its files as
  wrong, and skips an unmerged or too-recent change with a reason.
- `sd-review` rows written after the pack change carry
  `sd-review-tier:<owner>.<repo>:<sha12>` as their `question_id`, and the
  request `jev` sends carries no part of it.
