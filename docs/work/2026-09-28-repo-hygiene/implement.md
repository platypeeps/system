---
title: repo-sync sweeps stale worktrees, dead locks and landed branches
created: 2026-09-28
item: sd:1987
---
# Implement — Repo hygiene after the nightly sync

## Order

All steps land as one pull request in this repository.

1. **Tests first.** `local-repo-sync/tests/test_hygiene.py`, one case per
   acceptance criterion plus the report-only classes, built on `Fixture`
   from `test_repo_sync.py`. Run them against the script without `hygiene`.
   Check: every case fails except the silent-nightly guard, which passes
   because the old nightly mails nothing on a clean fleet.
2. **`hygiene` in `repo-sync.sh`.** The functions the design names, the
   `hygiene` dispatch, and `hygiene` in the three usage lines.
   Check: `sh local-repo-sync/repo-sync.sh test` green.
3. **`WORKTREE` in `reconcile`.** `scan_disk` skips a linked worktree and
   writes it to `$WORKTREES`; `reconcile` prints the list.
   Check: the reconcile case passes; the existing `MISMATCH` cases still
   pass.
4. **`nightly`.** `hygiene --apply` after `sync`, and its mail.
   Check: the two nightly cases pass; the existing nightly cases still pass.
5. **Help and README.** `hygiene` and `REPO_SYNC_SD_DB` in `help`; a
   `hygiene` section in `local-repo-sync/README.md`.
6. **Fail-first by mutation.** Remove one guard at a time from a copy and
   aim the suite at it with `REPO_SYNC_TEST_SCRIPT`:
   - the squash probe: the three-ways case fails;
   - the start-time comparison: the lock case fails;
   - the dirty check: the dirty-worktree case fails;
   - the busy check: the live-worktree case fails.
7. **Gates.** `make check` from the worktree root when the load allows;
   `sd-docs-lint` prints `clean`; `python3 tests/test_citations.py` passes.

## Verification

Named before the work:

- **The suite.** `sh local-repo-sync/repo-sync.sh test` reports `OK` with
  the new cases, and each new case was seen failing first.
- **No real checkout.** Every fixture sits under a `tempfile.mkdtemp`
  directory with `HOME` and `REPO_SYNC_ROOT` inside it. A grep of the test
  file for `~/repos` or `.config/system` returns nothing.
- **The merge gate.** `make check` exits 0.

## Rollout

Nothing to migrate. The next `nightly` after the merge runs the sweep with
`--apply`. Run `repo-sync hygiene` once by hand first on each machine and
read the report: the first nightly on a machine with a long backlog deletes
many branches at once, and each line carries its restore command.
