---
title: Weekly secret scan pages only on new findings
created: 2026-10-07
item: sd:1254
---
# Implement — weekly secret scan pages only on new findings

Two pull requests in this repository, gated and shipped by the hub (`repo.satellite_gate` is `off`).
Tests go in `local-scan-for-secrets/tests/test_scan_for_secrets.py`, against a fixture `$HOME` with synthetic values.

## Pull request 1: the append race

1. Test: a fixture log grows between the read and the write; every appended byte survives, and the file counts as busy.
   Check: it fails against today's `mask`.
2. Test: a log modified inside `S4S_MASK_SETTLE_MIN` is skipped; one older than it is masked.
3. Add the settle age and the compare-before-write guard; print `busy` lines and the busy count.
   Check: both tests pass; each fails with its guard removed.

## Pull request 2: mask before the weekly scan

4. Test: after `mask --apply`, a recent hit in a mask target reads `transient` and `critical` exits 0.
5. Test: a synthetic hit under the fixture `~/repos` still exits 2.
6. Classify recent mask-target hits as transient in `critical`.
7. Change `secret-scan-weekly.job` to mask first; rewrite its header comment and the README "Accepted exposure" section.
8. `make check`, `tests/test_citations.py`, `sd-docs-lint` and the secrets scan.

## Operator ruling

2026-10-07 ~13:30 MDT, relayed by the hub lead: Q1 to Q5 accepted as recommended in `design.md`.
Changes found in the build are marked "Changed in the build" in `design.md`.

## Rollout

Ruled 2026-10-07 ~14:25 MDT. After both pull requests merge, on the machine that runs the weekly job:

1. Dry run; the operator reads the per-file counts and the busy list:

       sh local-scan-for-secrets/scan-for-secrets.sh mask --no-prune

2. Apply by hand at a quiet time:

       sh local-scan-for-secrets/scan-for-secrets.sh mask --apply --no-prune

3. Reinstall the job, so launchd runs the new command:

       sh local-cron-jobs/cron-jobs.sh install secret-scan-weekly

Settling hits from the your-keys pass print as `transient:` lines (ruled the same time).
