---
title: Secret scan masks by hand and scans weekly
created: 2026-10-07
item: sd:1254
---
# Implement — secret scan masks by hand and scans weekly

Two pull requests in this repository, prepared and merged by the hub (`repo.satellite_gate` is `off`).
Tests use a fixture `$HOME` with synthetic values.

## Pull request 1: the append race (shipped, #208)

1. Test: a fixture log grows between the read and the write; every appended byte survives, and the file counts as busy.
2. Test: a log modified inside `S4S_MASK_SETTLE_MIN` is skipped; one older than it is masked.
3. The settle age and the compare-before-write guard.

## Pull request 2: the job scans only; two `mask` fixes

4. Test in `local-cron-jobs/tests/test_example_commands.py`: the job's command never names `mask`, it calls `critical` only, and its exit code is the scan's.
   Check: it fails against the first build's mask-first command.
5. Test in `local-scan-for-secrets/tests/test_scan_for_secrets.py`: `mask` with no export masks a pattern hit.
   Check: it fails against `main`, which exits 1.
6. Test: `mask --apply` on a fixture home with no target leaves a `~/repos` file untouched.
   Check: before the guard it rewrote that file; removing the guard fails the test.
7. Restore `secret-scan-weekly.job` to `critical` only, with a header that names the manual procedure.
8. README "Accepted exposure": masking is manual; the page returns after a mask.
9. `make check`, `tests/test_citations.py`, `sd-docs-lint` and the secrets scan.

## Rollout

After pull request 2 merges, on the machine that runs the weekly job:

1. Dry run; the operator reads the per-file counts and the busy list:

       sh local-scan-for-secrets/scan-for-secrets.sh mask --no-prune

2. Apply by hand at a quiet time:

       sh local-scan-for-secrets/scan-for-secrets.sh mask --apply --no-prune

3. Reinstall the scan-only job:

       sh local-cron-jobs/cron-jobs.sh install secret-scan-weekly
