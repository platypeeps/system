---
title: Jev experiment, journal entry 2 — is the review tier worth having?
created: 2026-10-05
item: sd:2764
---
# PRD — Jev experiment, journal entry 2

## Question

**Is the review tier worth having?** (operator ruling, note #10070 on sd:2764)

`cheap`, `standard` and `deep` each request one review (`TIER_DEPTH` in the pack's router).
`sd-ship` runs the review with `challenge` set, and the pack's `review_depth` then keeps one review even at `skip`.
So the four-tier ladder collapses to two outcomes, review or no review, and a model's tier matters only at `skip`.

The experiment asks whether any model can **safely allow `skip`**, and whether the skips it allows save enough reviews to be worth it.

## Decision

The answer decides the review tier's model:

1. **Drop the model**: rules only. Taken when no model can safely allow `skip`, when the 20-line heuristic matches the best model,
   when no selectable model is both safe and worth having on its own results (note #10183),
   or when the result is still inconclusive after one extra 4-week shadow window (ruling R1).
2. **Switch to local Kev**: Kev-4B on this machine reads the tier, and no path leaves the machine.
3. **Keep Jev**: the hosted model keeps reading the tier.

The pre-registration's decision rule maps results to these three, in order.
Keeping or switching implies a later change that lets that model allow `skip` past today's floor (sd:2132); this item ships no such change.

## Why now

Entry 1 of the journal reported 779 first tier readings from 2026-09-21 to 2026-09-29.
Jev agreed with the rules 732 times and moved 47; 28 moves went from `standard` to `skip`.
Each of the 28 still got a review, because of `challenge`.
Since 2026-09-30 the routed tier is a floor, and with Copilot reviews off (`sd.copilot_review=never` since 2026-10-02) a tier reading changes no reviewer count.
A second extraction of the same checkpoints does not reproduce entry 1's numbers; that must be settled before entry 2 is published.

## Reader

Public blog readers: entry 2 of the journal.
Every number, path and quote in the post passes the privacy review in `design.md`.
The operator fills the pre-registration's open predictions and rulings before the full replay.

## Populations

- **Confirmatory**: about 750 reviewed heads from 2026-09-09 to 2026-09-20, before any Jev reading, and 14 days of live shadow under sd:2761.
  Nobody reads an outcome for these heads before their answers are sealed.
- **Exploratory**: about 1,472 Jev-era heads, 2026-09-21 to 2026-10-05, holding entry 1's 779 and its 28.
  The planner saw outcome counts for some of them before registration; the pre-registration discloses exactly what and when.

## Requirements

0. Reconcile entry 1's extraction with the second one, before anything is published.
1. Re-ask Jev on 50 rebuilt states and match the recorded answers before the full replay (O4).
2. Replay every head once per arm: rules, a heuristic of about 20 lines, Jev, Kev-4B, and Claude Haiku 4.5 through the `anthropic` transport.
3. Label each head review-or-not from the review that ran at that head, then hand-label, in one blinded sheet, a sample of disagreements (cap 150) and every Jev and Kev model skip (up to 300 per arm), shuffled, arm names hidden.
4. Test whether a model can safely allow `skip`, and whether its safe skips are worth having.
5. Test injection: crafted path names, and crafted finding text, that try to push an answer down.
6. Benchmark scanner triage on synthetic credentials, through local Kev only.
7. Record cost and latency per arm and per decision.
8. Commit the pre-registration, `docs/experiments/2026-10-05-jev-entry-2-preregistration.md`, with every open line filled, before the full replay.

## Constraints

- Hosted sends: the replay sends the paths of all 2,222 heads to TypeSafe and to Anthropic **once**, with the same redaction (approved, ruling R2).
- The scanner benchmark never sends a hit to Jev or Haiku, and never uses a real credential.
- Experiment rows go to a separate database through `JEV_METER_DB`, under `/Volumes/local/repo-storage/system/jev-experiment/`, never to the live ledger.
- Raw data lives in that folder, never in git.
- Traces go to `local-genai-traces` with their own `openinference.project.name`.
- Scripts live in `local-jev`. Nothing in the live review path changes in this item.

## Done when

- Step 0 has named the extraction behind entry 1, and the faithfulness check has passed.
- The pre-registration is committed with every open line filled, and its commit SHA is recorded on sd:2764.
- The replay, the 14-day shadow window, both label passes, the injection test and the scanner benchmark have run, each under its stopping rule.
- A results note in the raw data folder states each hypothesis as held, failed or not decidable, with its n and its query.
- The operator has the decision the pre-registered rule gives.
- The post's numbers have passed the privacy review.

## Out of scope

- Relaxing the tier floor. The experiment measures what allowing `skip` would cost; it does not ship it.
- The shadow switch and the paired `--baseline` row (sd:2761) and the docs-lint local path (pack sd:2762).
- The other ten Jev callers.
