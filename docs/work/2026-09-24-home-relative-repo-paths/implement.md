---
title: home-relative repo paths
created: 2026-09-24
item: sd:1439
---
# Implement — home-relative repo paths

## Order

Steps 1 to 6 land in this repository as one pull request. Step 7 is the
pack's pull request. Step 8 is the rollout. Nothing is migrated on the live
database before step 8.

1. **Spike the migration on a copy.** Copy `sd.db` to the scratchpad with
   `sqlite3 .backup`, never `cp` of a live WAL database. Run the draft 014
   against the copy through `migrate`, with `HOME` unchanged. Record: rows
   changed per column, `PRAGMA foreign_key_check` output, table counts before
   and after, and whether the insert, repoint and delete order passes the
   foreign keys without `defer_foreign_keys`. 009 measured that pragma
   failing for a drop; this step measures the update case rather than
   assuming it.
   Check: every count in the prd's measured table moves to `~/`; foreign key
   check empty; counts equal.
2. **`sd_db/paths.py`.** `store`, `expand`, `keys` and `same`, as the
   design's "The stored form" section defines them. `install` registers
   `sd_home_relative` and `sd_home_absolute` on a connection.
   Check: unit tests for a path under home, the home itself, a path outside
   home, a symlinked home, `~user`, a relative input, and an unset `HOME`.
3. **Migration 014 and its reverse.** `014_home_relative_repo_paths.sql`,
   `SCHEMA_VERSION` 13 → 14 in the same commit. The number is 014 on the day
   of writing. sd:1335 claims the next free slot for `request_outcome`, so
   read `local-sd-db/sd_db/schema/` at build time; whichever lands second
   renumbers. `migrate` and the reference connection in `_check_restore`
   call `paths.install`. Rewrite `one_migration_back` in
   `local-sd-db/tests/test_backup.py` to reverse 014.
   Check: acceptance criteria 1, 2 and 3 on a fixture whose repositories sit
   under a temporary `HOME`; a restore of a 13 snapshot migrates it forward;
   a replay on an already-migrated copy changes nothing.
4. **The library's sites.** Every site in the design's library table routes
   through `paths`. `upsert_repo` refuses an absolute path under `$HOME`.
   `sd-db.sh status` prints the count of key-column values absolute under
   this home.
   Check: the sweep test in the design's "Tests that catch a missed site"
   passes; `sd-db.sh test` green.
5. **The runner and the dashboard.** Every site in the design's runner and
   dashboard table routes through `paths`.
   Check: `local-sd-runner` and `local-project-dashboard` suites green, zero
   skips; the two-home test for the dashboard's item page.
6. **The two-home test.** `local-sd-db/tests/test_home_relative.py` builds a
   store under `HOME=A`, copies it, and drives the library's read and write
   paths under `HOME=B` with the same `~/repos` layout.
   Check: acceptance criterion 4 for the library's verbs.
7. **The pack.** In `platypeeps/sd-ai-command-pack`: every site in the
   design's pack table calls `sd_db.paths`; the pack's CI pin of this
   repository moves to the step 1 to 6 merge commit; a pack two-home test for
   the working-directory lookup.
   Check: acceptance criterion 7; `make check` in the pack green.
8. **Rollout**, below.
9. **Tell sd:1335.** Add a note to sd:1335 that this item has landed, with
   the merge commits of both pull requests. Its step 10 then drops the
   `HomeMismatch` equality.

## Verification

Named before the work:

- **The schema sweep.** After migrating a fixture, a test enumerates every
  column from `sqlite_master` and fails on a key column holding a value that
  begins with the fixture's `HOME`. It also fails on any column not yet
  classified as key, history or hub-only. A new column cannot slip past it.
- **The two-home test.** Criterion 4. If any verb answers "not registered"
  under `HOME=B`, a site was missed.
- **The round trip.** Forward then reverse yields the key columns
  byte-identical to the copy. Any difference fails.
- **The live counts.** After step 8, this returns 0 for each key column:
  `select count(*) from repo where path like '/Users/%'`, and the same for
  `item.repo`, `repo_protection.repo`, `runner_run.repo`,
  `runner_lease.repo` and `item.external_id`.
- **The foreign keys.** `PRAGMA foreign_key_check` on the live store after
  step 8 returns no rows.
- `sd-docs-lint` clean; `python3 tests/test_citations.py` green.

## Rollout

Migration 14 raises the store's version. `connect` refuses a database newer
than its library for every open, reads included. A 14 library on a 13 store
reads, and refuses writes with `SchemaTooOld`. So the provision and the
migration run in one stopped window.

1. **Merge** the pull request of steps 1 to 6. Merge the pack's pull request
   of step 7. Neither changes the live store.
2. **Stop** the runner and the dashboard. Pause cron jobs that write: the
   nightly jobs in `local-cron-jobs` run `sd` verbs.
3. **Back up:** `local-sd-db/sd-db.sh backup`. Also keep a named copy,
   `sd.db.pre-1429`, beside the store, as `sd.db.pre-1335` was kept. Record
   the backup directory.
4. **Provision** the pack's virtualenv from the pack checkout:
   `bin/sd_install.py --provision-library`. Install the same build into any
   interpreter that `SD_RUNNER_PYTHON` or `SD_DASHBOARD_PYTHON` names.
5. **Migrate:** `local-sd-db/sd-db.sh migrate`.
6. **Check:** `sd-db.sh status` names version 14 and 0 absolute key values
   under this home; the live counts and the foreign key check above.
7. **Restart** the runner and the dashboard, then resume cron.
8. **Smoke:** `sd today`; `sd-db.sh repo list`; `sd-status` from
   `~/repos/system`; the dashboard's item page for sd:1439; one runner
   dry-run.

**Roll back** in the same stopped window. Run the reverse script from the
header of 014, or `sd-db.sh restore <backup dir>` followed by
`sd restore resume`. Then reinstall the previous library build into the
pack's virtualenv. A reverse without the reinstall leaves a 14 library on a
store whose rows are absolute again; it reads, and misses nothing, because
`keys` probes both forms.

## BLOCKING

None. The operator took every recommendation in `prd.md` on 2026-09-24.

## Log

2026-09-24 — filed from sd:1335 gap (a), decided by the operator on
2026-09-24. Design pass only: no code, no migration file, no schema change.
The row counts in `prd.md` were measured read-only on 2026-09-23.
`sd-db.sh work register` refused from the planning worktree: "repository
... is not registered". The worktree's path is not a row. Its origin is the ssh
URL and the row's remote is https, which `same_remote` deliberately does
not equate. The folder has no docs/work row yet; register
it from `~/repos/system` once this branch merges. sd:1429, the task row,
exists.

2026-09-24 — the operator took all four recommendations in `prd.md`, which
now records them under Decisions. sd:1429 carries the work.

2026-09-24 — `work register` filed this folder as sd:1439. A task row holds
no document path, so sd:1439 carries the work and sd:1429 is closed as
superseded. The frontmatter now names sd:1439.

2026-09-24 — steps 1 to 6 built on `feat/sd-1439-home-relative-paths`, one
local commit, not pushed. Step 7, the pack, is not done. Steps 8 and 9 wait
for the merge.

Step 1, the spike. A `sqlite3 .backup` copy of the live store, migrated
through `migrate` with `HOME` unchanged, went from 13 to 14. Values that
moved from `/Users/` to `~`: `repo.path` 62, `item.repo` 826,
`repo_protection.repo` 62, `runner_run.repo` 7, `runner_lease.repo` 7,
`item.external_id` 110, `state.key` 1, `cost.repo` 38, `skill_use.cwd` 88.
The four `register` rows of `item.path` became repo-relative. The 22
`cost.repo` rows under `/private/tmp` stayed absolute. `PRAGMA
foreign_key_check` returned no rows, and every table count was equal. The
insert, repoint and delete order passed the immediate foreign keys, so
`defer_foreign_keys` was not needed. The header's reverse script, run on the
migrated copy, returned version 13 and every key column identical to the
copy. The counts are two above the prd's for `item.repo` and one above for
`external_id` and `skill_use.cwd`: rows written since 2026-09-23.

Deviations from the design, each small:

- `keys` returns up to three forms, not two: the key, the resolved absolute
  path, and the text as given when it is an absolute path that differs from
  both. The third covers a caller that did not resolve.
- A writer converts only a path under `$HOME` (`paths.key`). Any other value
  is stored exactly as given, which is R1 read literally.
- `paths.disk` is added beside `expand`. It expands a `~` key and passes any
  other value as the path it is. Readers whose input can also be a
  caller's relative path use it, so a fixture's bare name keeps its meaning.
- `upsert_repo` refuses an unconverted path with `paths.PathRefused`, a
  subclass of `SdDbError`.
- The restore-path check of step 3 is `test_backup`, whose
  `one_migration_back` now runs the header's reverse. `_check_restore`
  registers the functions on its reference connection, and the bare-file
  restore test failed without that.
- The grep gate is a per-file count of `path = ?` and `repo = ?` in
  `sd_db`, each with the reason its argument is a stored row value. The pack
  is not scanned; that is step 7.

Red before green. `test_paths` and `test_home_relative` fail to import on
`origin/main`. With only `paths.py`, 014 and `migrate`'s registration
copied onto `origin/main`, six of the site tests fail: the writers, the
guard, the status count, the grep gate, and both two-home tests. The
dashboard's `TheItemPageUnderASecondHome` fails on `origin/main` with "could
not be read from HEAD".

Out of scope, for step 7: the pack's sites, its CI pin of this repository,
and its two-home test. Its virtualenv still holds a schema 13 library, so
the pack's `sd` refuses a 14 store until step 8's provision.

The runner's `ShipLifecycle` fixture registers its checkout under the real
home, then patched `HOME` to the directory above it. The row then read as a
legacy absolute path under the new home, and `upsert_repo` refused it. The
patched `HOME` is now a directory beside the checkout, so the row stays
absolute and the pack's unchanged `sd-ship` still finds it. The gates ran
against the pack at the SHA `system-native` pins. Two `ShipLifecycle` tests
fail against the local pack checkout's `HEAD` with "no route for GET
/--include". They fail the same way on `origin/main`, so this change did not
cause them.

2026-09-24 — closed. Steps 1 to 6 merged as #554 (`78e43d8`), the pack pin
as #559 (`929b528`). Two follow-ups fixed raw repository comparisons that
014 exposed: sd:1447 (#560, `4a0e624`) and sd:1450 (#561, `25d9f36`). The
library was provisioned from `25d9f36`, and the runner and dashboard
restarted healthy. Measured at close: `sd-db.sh status` reports schema
version 14, and 0 of 62 `repo` rows hold an absolute path. Step 9 is done
by sd:1335's decision note of 2026-09-24, and sd:1335's pages drop the
`HomeMismatch` stopgap. This commit closes the row: #554 carried
`Work: sd:1439` but no delivery trailer.
