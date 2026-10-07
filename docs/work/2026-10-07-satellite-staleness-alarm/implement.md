---
title: Satellite staleness alarm
created: 2026-10-07
item: sd:2918
---
# Implement — satellite staleness alarm

One pull request in this repository, gated and shipped by the hub (`repo.satellite_gate` is `off`).
Tests first in `local-sd-db/tests/test_satellite_stale.py`; no code starts before the operator answers the open questions.

1. Tests for the claim: write, replace, release, refuse on the hub, write over the wire from a fixture satellite.
   Check: they fail before the `claim` and `release` verbs exist.
2. `sd_db/satellite_stale.py`: the claim rows and the progress read (notes, `updated_at`, branch head).
   Check: the fresh, stale, pushed-commit and skipped-status cases pass; each fails with its signal removed.
3. The episode watermark and `quiet_until`.
   Check: a second run sends nothing; progress then a new stall sends again; a quiet claim sends nothing.
4. The `satellite-stale` verb, its `status` exit codes, `HubOnly` on a satellite, and the `help` sentence.
   Check: the exit-code test and the health-check sweep fixture pass.
5. `local-cron-jobs/examples/satellite-stale.job` and the `local-sd-db/README.md` section.
   Check: the cron-jobs example test passes; the job names `change-me` for any machine value.
6. `make check`, `tests/test_citations.py`, `sd-docs-lint` and the secrets scan.

## Open questions for the operator

Q1 to Q6 are in the hub lead's relay of 2026-10-07; record each answer here before step 1.
