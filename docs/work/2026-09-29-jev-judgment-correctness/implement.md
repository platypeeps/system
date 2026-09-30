---
title: Record whether each Jev judgment was right, from later outcomes
created: 2026-09-29
item: sd:2107
---
# Implement — Jev judgment correctness from later outcomes

## Order

Two pull requests: the pack change is small and independent.

**Pack (`sd-ai-command-pack`):**

1. Test first: the captured `jev` argv carries
   `--id sd-review-tier:<owner>.<repo>:<sha12>` for a GitHub remote, and
   `sd-review-tier` for a non-GitHub remote.
2. `bin/sd_jev.py` builds the id.
   Check: the test passes; `make check` green.

**System:**

1. Tests first for the ledger: `label`, `unlabelled`, the `by_stage` counts.
   Check: they fail against the current `sd_db`.
2. `sd_db.judgment.label`, `unlabelled`, the counts and bands, and the two
   `sd-db.sh judgments` verbs, with help lines.
   Check: `sd-db.sh test` green.
3. Tests first for `jev label sd-review` on a fixture repository.
4. The labeller in `local-jev`, `label` in `jev help`, and a README section.
   Check: the `local-jev` suite green.
5. The cron example and its README line.
6. Fail-first by mutation: drop the `window open` guard, and the deepest-tier
   rule, one at a time. Check: a named case fails for each.
7. `make check` through `sd-check`.

## Follow-ups, filed when this lands

- Floors per stage once `sd-db judgments` shows 50 labelled rows for it.
- A rule per stage for sd:2091–2095 when each caller exists.
