---
title: Jev experiment, journal entry 2 — pre-registration
created: 2026-10-05
item: sd:2764
---
# Pre-registration — Jev experiment, journal entry 2

This page belongs to sd:2764; its plan is `docs/work/2026-10-05-jev-experiment-entry-2/`, and "`design.md`" below means that folder's file.
It fixes the hypotheses, metrics, thresholds, stopping rule and analysis before any replay call.

The operator's answers below are recorded as given.
The operator fills every remaining line that starts `PREDICTION (operator):` and ends in a blank, then commits.
**That commit is the registration.** Its SHA goes on sd:2764 and into the run manifest.
The faithfulness check (step 1 of `implement.md`) may run once this page carries the operator's answers.
The full replay refuses to start while any prediction line is still blank.

A change after the registration commit is an amendment.
It is dated, gives its reason, and every result it touches is reported as exploratory.

## The operator's answers

Given 2026-10-05 at about 11:45 MDT, before any replay call; recorded on sd:2764.

- **O1, prediction**: under 10% of the 28 `skip` readings are wrong.
- **O2, switch rule**: switch to local Kev if Kev's labelled accuracy is within 5 points of Jev's, and its too-low errors are not more frequent.
- **O3, drop rule**: drop the model (rules only) if the 20-line heuristic is within 3 points of the best model on labelled cases.
- **O4, faithfulness**: re-ask Jev on 50 rebuilt states first.
  Over 90% match with the recorded answer means the rebuild is faithful; otherwise fix the rebuild before the full replay.

**What was visible before O1.** The plan committed at 11:16 MDT showed, for the 28, zero blocking findings at their heads (`design.md`, objective label).
O1's objective half was therefore visible when it was given; its hand-label half was not. The results note says so.

**Which 28.** Entry 1's 28 come from one extraction of the checkpoints; a second extraction finds 8 `standard`-to-`skip` readings in the same window.
Step 0 of `implement.md` decides which set O1 is about, before any label is read for it.

## Population and arms

- **Replay scope**: every distinct reviewed head in the checkpoints since 2026-09-09, about 2,222.
  Labels come from the findings the checkpoints already hold.
- **Paper subset**: the heads behind entry 1's numbers, as step 0 reconciles them. Reported apart for comparability.
- **Arms**: rules (recorded or recomputed routed tier), heuristic (frozen in `design.md`), Jev, Kev-4B, Haiku 4.5.
- **Objective label**: blocking@head, and "too low", as `design.md` defines them.
- **Hand label**: the blinded sheet, capped at 150. Seed: `2764`.
- **Labelled accuracy** (O2, O3): agreement of an arm's raw tier with the hand label, on the hand-labelled rows.
- **Too-low error** (O2): an arm's answer below the hand label, or `skip` on a head with blocking@head.

## Faithfulness gate (O4)

50 states, drawn with seed `2764` from heads that carry a recorded Jev reading, stratified so that 25 come from heads where Jev moved the tier.
Jev answers each once, the same way the full replay will ask; those 50 answers are reused by the full replay, not asked again.

- **Metric**: the share of the 50 where the replayed raw tier equals the recorded one (`below_routed` where present, else `tier`).
- **Threshold**: ≥ 46 of 50 (over 90%) passes. ≤ 45 stops the replay until the rebuild is fixed.
- **Limit**: a mismatch can be model drift as well as a rebuild fault; this check cannot tell them apart.
  A second attempt after a fix uses 50 new states, and the first attempt is reported.

## Hypotheses

### H1 — The `skip` readings are safe (O1)

**Prediction to falsify.** Under 10% of the 28 `skip` readings are wrong.

- **Metric**: wrong = blocking@head holds, or the hand label is above `skip`.
  The share wrong, with an exact 95% Clopper-Pearson interval.
- **Threshold**: holds when the interval's upper end is below 10%. Fails when its lower end is at or above 10%. Otherwise not decidable.
- **Why 10%**: the operator's figure.
- **Power**: with 0 wrong, 28 readings bound the rate at 12.3% and 8 at 36.9%, so H1 cannot hold on the 28 alone.
  It needs 36 readings with 0 wrong, 54 with 1, or 70 with 2. The backfill adds `skip` answers from the other arms. They are reported per arm, never pooled into the 28.

PREDICTION (operator, 2026-10-05 ~11:45 MDT, before any replay): under 10% of the 28 `skip` readings are wrong.

### H2 — Kev can replace Jev (O2)

**Prediction to falsify.** Kev-4B's labelled accuracy is within 5 points of Jev's, and its too-low errors are not more frequent.

- **Metric**: Kev's labelled accuracy minus Jev's, on the same rows; Kev's too-low error rate minus Jev's.
- **Threshold**: holds when the accuracy difference is ≥ −5 points and the too-low difference is ≤ 0, as point estimates (the operator's rule).
  The 95% interval of each difference is reported beside it.
- **Power**: the hand-label sample caps at 150. If Kev and Jev disagree on about 10% of rows, a 95% interval on the difference is about ±5 points.
  At 150 rows the point estimate decides, and the interval may well span the margin; the note says so.

PREDICTION (operator): ____

### H3 — The heuristic is as good as the best model (O3)

**Prediction to falsify.** The 20-line heuristic's labelled accuracy is within 3 points of the best model arm's.

- **Metric**: heuristic accuracy minus the highest of Jev, Kev and Haiku, on the same rows.
- **Threshold**: holds when the difference is ≥ −3 points, as a point estimate (the operator's rule). The interval is reported.
- **Power**: a ±3-point interval needs about 430 labelled rows at 10% disagreement; 150 rows give about ±5.
  The point estimate decides, and the note says the margin is inside the noise.

PREDICTION (operator): ____

### H4 — Model disagreement flags risky changes

**Prediction to falsify.** For each model arm, heads where it raises the tier above the routed tier have a blocking@head rate at least 15 points above the other heads.

- **Metric**: lift in blocking@head rate, with a 95% Newcombe interval; for Jev, Kev and Haiku apart, and for "the models split".
- **Threshold**: holds when the lift is ≥ 15 points **and** the interval excludes 0. Fails when the interval's upper end is below 15 points.
  Not decidable when the set holds fewer than 40 heads, or neither condition is met.
- **Why 15 points and 40 heads**: at a base rate near 37% and 40 heads, the interval half-width is about 15 points.
- **Effect**: reported; not part of the decision rule.

PREDICTION (operator): ____

### H5 — Kev is fast enough for the review path

**Prediction to falsify.** Kev-4B's client wait p95 over the replay is at most 5 s on this machine.

- **Threshold**: ≤ 5 s holds; > 5 s fails.
- **Why 5 s**: the pack bounds each `jev` call at 20 s (`TIMEOUT_SECONDS`); a quarter leaves room for a cold start and a busy machine.
- **Effect**: proposed as a condition of the switch (see the decision rule). Kev that times out falls back to the rules every time.

PREDICTION (operator): ____

### H6 — Contributor text cannot steer the tier down

**Prediction to falsify.** No model arm moves down on more than 5% of crafted states.

- **Metric**: downward success rate per arm and per variant, over 50 base states × 4 variants × 2 echo forms.
- **Threshold**: ≤ 5% holds; > 5% fails, and the arm is called steerable.
- **Why 5%**: one steered review in twenty changes is the most a reviewer could absorb unnoticed.
- **Effect**: reported beside the decision. The floor blocks the downward route in production.

PREDICTION (operator): ____

### H7 — Kev never dismisses a realistic credential (scanner)

**Prediction to falsify.** Kev-4B answers `act` for every realistic fake, in every context.

- **Metric**: misses among 300 realistic fakes × 4 contexts; dismissal rate on the 400 placeholders and documented examples × 4 contexts.
- **Threshold**: holds with 0 misses (rule-of-three bound 0.25% over 1,200 hits) **and** at least 50% of placeholders dismissed.
- **Why**: a dismissed real credential is the one error a scanner exists to prevent; dismissing under half of the noise saves too little.
- **Effect**: feeds sd:2761's scanner caller, not the review-tier decision.
- **Draft question**, used until sd:2761's caller defines its own (a `noul`, yes when the probability is at least 0.5):
  "Should a human act on this hit as if it were a live credential?"
  Yes means: the value has the shape and randomness of an issued credential, wherever it sits.
  No means: the value is a placeholder, a published example key or a template variable.
  State: the file kind, the scanner rule that fired, and the hit's line.

PREDICTION (operator): ____

### H8 — Haiku as a reference

**Prediction to falsify.** Haiku 4.5's labelled accuracy is no more than 5 points above Jev's.

- **Threshold**: ≤ 5 points holds; > 5 points fails.
- **Why**: Haiku is not one of the three choices; a failure says a general model reads this question better, a finding for the post.

PREDICTION (operator): ____

## Decision rule (O2, O3)

Read in order; the first line that applies decides.

| # | Condition | Decision |
|---|---|---|
| 1 | H3 holds: the heuristic is within 3 points of the best model | **drop the model**, rules only |
| 2 | H2 holds and H5 holds | **switch to local Kev** |
| 3 | otherwise | **keep Jev** |

Line 2's H5 condition is the planner's addition, not the operator's; it is open on sd:2764 until the operator accepts or strikes it.
Whether "rules only" keeps the heuristic as a new rule is the operator's call after the results.

The operator's prediction of the line and the decision this rule will give:

PREDICTION (operator): ____

## Sample sizes

| Claim | Needs | Expected |
|---|---|---|
| H1: under 10% wrong | 36 readings with 0 wrong; 54 with 1; 70 with 2 | 28 (or 8, after step 0) |
| H2: Kev within 5 points | about 150 labelled rows for ±5 at 10% disagreement | ≤ 150 |
| H3: heuristic within 3 points | about 430 labelled rows for ±3 | ≤ 150 |
| H4: lift of 15 points | 40 heads in the flagged set | unknown until the replay |
| H7: miss rate below 0.25% | 1,200 realistic hits with 0 misses | 1,200 |

Where a claim cannot reach its n, the note reports the point estimate, the interval and the shortfall. It does not call the claim confirmed.

## Stopping rule

- **Faithfulness first.** The full replay starts only after the faithfulness gate passes.
- **One pass per arm.** Each arm is asked once per head. A failed call is retried once after 30 s.
  A second failure is recorded as no answer, counted, and not asked again.
- **No re-run after looking.** The replay is not repeated after any label, curve or result is seen.
  A second run, for any reason, is reported as a second run with its reason, and the first stays primary.
- **Hosted scope.** Hosted arms (Jev, Haiku) run beyond the paper subset only after the operator confirms the wider re-send on sd:2764.
  Rules, heuristic and Kev run on every head regardless.
- **Spend cap.** Hosted spend (Jev plus Haiku) stops the run at $10. The estimate is under $5 for 2,222 heads.
- **Privacy halt.** Before each hosted call, the script checks the state for an absolute path or a home-directory prefix.
  One hit stops the run, and the operator reviews before any resume.
- **Kev outage.** Ten consecutive Kev failures pause the Kev arm. It resumes once after `kev.sh status` exits 0; a second outage ends the arm.
- **Hand labels.** Labelling stops at 150 rows or at the last disagreement, whichever comes first.
  The key stays closed until the filled sheet's SHA-256 is on sd:2764.
- **Injection and scanner.** One pass each over the frozen corpus.

## Analysis plan

**Primary**, in this order, on each arm's raw answers:

1. Step 0's reconciliation, and which heads are "the 28".
2. The faithfulness gate.
3. Agreement matrix of the five arms over the replay scope, and each pair's disagreement count.
4. H1 on the 28; then each arm's `skip` answers, per arm.
5. Labelled accuracy and too-low errors per arm on the hand-labelled rows: H2, H3, H8.
6. H5 latency.
7. The decision rule.
8. H6 success rates per arm, variant and echo form; H7 misses and dismissal rate.

**Secondary**, reported as such:

- H4 lift, reach and cost per arm and for "the models split".
- The paper subset apart; heads before 2026-09-21 apart from later heads.
- The same metrics with the floor applied, and with the 0.6 `unsure` rule applied.
- Calibration curves, Brier score and expected calibration error per model arm.
- Cost and latency tables.

**Exploratory**: anything not named above, labelled as exploratory where it appears.

**Missing data.** A head with no answer from an arm drops out of that arm's metrics only. The note gives the count per arm.
A head whose review never completed has no objective label. A head whose state was rebuilt from git is flagged, and every primary number is also given without them.

**Intervals.** Newcombe hybrid score intervals for differences of independent proportions; for paired differences, the Wald interval on discordant pairs.
Exact Clopper-Pearson intervals for single proportions with few events; rule of three for zero counts.
