---
title: Satellite staleness alarm
created: 2026-10-07
item: sd:2918
---
# Implement — satellite staleness alarm

One pull request in this repository, gated and shipped by the hub (`repo.satellite_gate` is `off`).
Tests first in `local-sd-db/tests/test_satellite_stale.py` and `local-satellite-stale/tests/`.

1. Tests for the claim: write, replace, release, refuse on the hub, write over the wire from a fixture satellite.
   Check: they fail before the `claim` and `unclaim` verbs exist.
2. `sd_db/satellite_stale.py`: the claim rows and the progress read (notes, `updated_at`, branch head).
   Check: the fresh, stale, pushed-commit and skipped-status cases pass; each fails with its signal removed.
3. The episode watermark and `quiet_until`.
   Check: a second run sends nothing; progress then a new stall sends again; a quiet claim sends nothing.
4. The `satellite-stale` verb, its `status` exit codes, `HubOnly` on a satellite, and the `help` sentence.
   Check: the exit-code tests pass, and the wrapper's help carries the sweep's phrase.
5. `local-satellite-stale/` (entrypoint, `.env.example`, README, tests in the `tools` leg),
   `local-cron-jobs/examples/satellite-stale.job` and the `local-sd-db/README.md` section.
   Check: the wrapper suite passes; the job names no machine value.
6. `make check`, `tests/test_citations.py`, `sd-docs-lint` and the secrets scan.

## Operator ruling

2026-10-07, relayed by the hub lead: Q1 to Q7 accepted as recommended in `design.md`.
Changes found in the build are marked "Changed in the build" in `design.md`:
the `unclaim` name, the `local-satellite-stale` wrapper for convention 6, `status` without a fetch, and the window held in the verb.
