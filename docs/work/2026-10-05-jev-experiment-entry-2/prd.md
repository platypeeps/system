---
title: Jev experiment, journal entry 2 — keep, swap or drop the review-tier model
created: 2026-10-05
item: sd:2764
---
# PRD — Jev experiment, journal entry 2

## Decision

The experiment decides one thing for the review tier of `sd-review`:

1. **Keep Jev**: the hosted model keeps reading the tier.
2. **Switch to local Kev**: Kev-4B on this machine reads the tier, and no path leaves the machine.
3. **Drop to rules**: the routed tier stands alone, with or without a small deterministic heuristic.

Every other result is evidence for that choice, or a finding for the post.
The scanner benchmark feeds a separate decision (sd:2761's scanner caller) and is reported beside it.

## Why now

Entry 1 of the journal reported 779 first tier readings from 2026-09-21 to 2026-09-29.
Jev agreed with the rules 732 times and moved 47: 6 up to `deep`, 41 down.
Nobody labelled any of the 47.
Since 2026-09-30 the routed tier is a floor (sd:2132): Jev may raise it and never lower it.

Two facts make the decision sharper than entry 1 stated.

- `cheap`, `standard` and `deep` each request one review (`TIER_DEPTH` in the pack's router).
  Only `skip` requests none.
- `sd-ship` runs the review with `challenge` set, and the pack's `review_depth` then keeps one review even at `skip`.
  That is why each of the 28 `skip` readings still recorded a completed review.

So under the floor, with Copilot reviews off (`sd.copilot_review=never` since 2026-10-02), a tier reading changes no reviewer count.
The model must earn its place as a signal, not as a router.

## Reader

Public blog readers: entry 2 of the journal.
Every number, path and quote in the post passes the privacy review in `design.md`.
The operator reads the plan and fills in the predictions before any run.

## Requirements

0. Reconcile entry 1's extraction of the 779 readings with a second extraction that does not reproduce it, before anything is published.
1. Replay the review-tier question once per arm over every reviewed head since 2026-09-09 (about 2,222), after a 50-state faithfulness check.
   The arms are rules, a heuristic of about 20 lines, Jev, Kev-4B and Claude Haiku 4.5.
2. Label each commit from the review that ran at that head (objective label) before any hand label.
3. Hand-label a blinded sample of the disagreements: all of them, capped at 150, shuffled, arm names hidden.
4. Test disagreement between arms as an escalation signal.
5. Draw calibration curves for each arm that reports probabilities.
6. Test injection: crafted path names, and crafted finding text, that try to push an answer down.
7. Benchmark scanner triage on synthetic credentials, through local Kev only.
8. Record cost and latency per arm and per decision.
9. Commit the pre-registration, `docs/experiments/2026-10-05-jev-entry-2-preregistration.md`, with the operator's predictions filled in, before any replay call.

## Constraints

- Hosted re-sends: the replay sends the old paths to TypeSafe and to Anthropic **once** (operator approval, 2026-10-05, given for the 779).
  Hosted arms on the other heads wait for the operator to confirm the wider re-send; local arms run on all of them.
  The injection test uses public-repository states only, so it re-sends no private path.
- The scanner benchmark never sends a hit to Jev or Haiku, and never uses a real credential.
- Raw data lives in `/Volumes/local/repo-storage/system/jev-experiment/`, never in git.
- Traces go to `local-genai-traces` with their own `openinference.project.name`.
- Scripts live in `local-jev`. Nothing in the live review path changes in this item.

## Done when

- Step 0 has named the extraction behind entry 1, and the faithfulness check has passed.
- The pre-registration is committed with every operator blank filled, and its commit SHA is recorded on sd:2764.
- The replay, both label passes, the injection test and the scanner benchmark have run, each under its stopping rule.
- A results note in the raw data folder states each hypothesis as held, failed or not decidable, with its query.
- The operator has the keep, swap or drop recommendation that the pre-registered decision rule gives.
- The post's numbers have passed the privacy review.

## Out of scope

- Relaxing the tier floor. The experiment measures what a relaxation would cost; it does not ship one.
- The shadow switch and the paired `--baseline` row (sd:2761) and the docs-lint local path (pack sd:2762).
- The other ten Jev callers.
