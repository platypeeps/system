---
title: home-relative repo paths
created: 2026-09-24
item: sd:1439
---
# PRD — home-relative repo paths

Tracked as sd:1439, which superseded task sd:1429. It closes gap (a) of
sd:1335, `docs/work/2026-09-22-run-the-framework-from-a-second-machine/`,
whose steps 10 and 11 wait for it.

## Problem

The sd database keys every repository by its absolute path. A second machine
with a different login resolves none of them.

The operator decided on 2026-09-24 that repository paths become
home-relative: `~/repos/...` in the database, expanded by every reader. The
first satellite, the second laptop, runs as `/Users/<second-login>`. Every row today
begins `/Users/<login>/`. So on the laptop every registered checkout resolves to
a directory that does not exist.

`repo.path` is not one column. It is the primary key that four foreign keys
name, a prefix inside `item.external_id`, and a value that callers in two
repositories derive from the working directory and compare. Changing its form
in one place and not another turns a lookup into a silent miss: "not a
registered repository" on a checkout that is registered.

## What exists today (measured)

Measured read-only on 2026-09-23 against `~/.local/share/sd/sd.db`, schema
version 13. The scan enumerated every column of every table from
`sqlite_master` and `PRAGMA table_info`, not from a list.

| Column | Rows | Values starting `/Users/` | Role |
|---|---|---|---|
| `repo.path` | 62 | 62 | primary key |
| `item.repo` | 1383 | 824 (559 NULL) | foreign key to `repo(path)` |
| `repo_protection.repo` | 62 | 62 | primary key and foreign key |
| `runner_run.repo` | 7 | 7 | foreign key |
| `runner_lease.repo` | 7 | 7 | foreign key |
| `item.external_id` | 1383 | 109 | `<repo>::<relative path>` for `docs/work` (86) and `writing-piece` (23) |
| `state.key` | 3818 | 1 | `verified` row `<repo>:pieces_source` |
| `item.path` | 1383 | 4 | normally repo-relative (109 are); 4 `register` rows are absolute |
| `cost.repo` | 241 | 38 (22 more under `/private/tmp`) | attribution, not a key |
| `skill_use.cwd` | 91 | 87 | log of where a skill ran |
| `note.output_path` | 3660 | 8 | `~/.local/share/sd/executions/*.log`, hub-only |

Paths also sit inside text that no key reads by equality:
`item.fields` (503 rows; `report.source_path` 461, `completion.repo` 39,
`contribution.*` 6), `item.body` (498), `note.body` (73), `state.body`
(1408, nearly all `checkpoint` receipts), `assignment.scope` (7 palette
records) and `publication_claim.payload` (1, immutable by trigger).

Every `/Users/` value names `/Users/<login>/`. No row is `~`-relative today.
All 62 `repo` rows sit under `/Users/<login>/repos/`. `~/repos` is a real
directory, not a symlink. Nine of 35 files in the hub-only journal
directories beside the database carry a repository path.

## Requirements

- **R1 — one stored form.** A repository path under the current `$HOME` is
  stored as `~/` plus its home-relative POSIX path. A path outside `$HOME`
  stays absolute, unchanged. No other form is written.
- **R2 — one pair of functions.** One module in `sd_db` owns the conversion
  both ways. Every writer, every comparison and every disk use in this
  repository and in the pack goes through it. No caller rolls its own
  `resolve()` comparison against a stored value.
- **R3 — conversion runs in the caller's process.** It reads the caller's
  `$HOME` at call time. It never runs inside SQL at query time, because under
  sd:1335 the SQL executes on the hub, under the hub's home.
- **R4 — the existing rows move once.** A migration rewrites every key column
  in one transaction, reversibly, after a backup. Foreign keys hold after it.
- **R5 — history stays as written.** JSON fields, note and item bodies,
  receipts, the publication payload and the journals are not rewritten.
  Readers that compare a path found there compare through R2.
- **R6 — both repositories land in a named order**, and no window lets a
  reader answer "not registered" for a registered checkout without saying
  why.
- **R7 — a missed site fails a test**, not a satellite session.

## Acceptance criteria

1. After the migration, zero values in the key columns begin with the
   migrating machine's `$HOME`. Checked by a scan that enumerates columns
   from the schema.
2. `PRAGMA foreign_key_check` returns no rows after the migration, and the
   row count of every table is unchanged.
3. The reverse script returns the key columns byte-identical to the
   pre-migration copy.
4. A database written under `HOME=A` and read under `HOME=B`, with the same
   `~/repos` layout, resolves every registered checkout. `sd today`,
   `sd-db.sh repo list`, `sd-db.sh work register`, runner prepare and the
   dashboard item page all answer as they do under `A`.
5. A caller that passes an absolute path under the current home finds the
   `~/` row. A caller that passes the `~/` form finds it too.
6. `sd-db.sh status` reports how many key-column values are absolute under
   this machine's home. It reports 0 on the hub after rollout.
7. The pack's test suite passes against the new library, and a pack test
   proves the working-directory lookup under a second home.
8. `sd-docs-lint` is clean, and the `local-sd-db`, runner and dashboard
   suites are green with zero skips.

## Deliberately out of scope

- **The `HomeMismatch` refusal.** sd:1335 builds it and drops it at its step
  10, after this item lands.
- **Paths relative to `SD_REPO_ROOT`.** Rejected in sd:1335's design, gap
  (a): launchd and cron do not see the variable, and `repos.add` registers
  checkouts outside the root.
- **Hub-only paths.** `note.output_path`, `runner_run.work_path` and
  `retained_path`, `~/.config/sd/runner.json`, `commands.yaml` and the
  LaunchAgent plists name hub files. sd:1335 keeps their readers on the hub.
- **Rewriting history.** R5.
- **Case folding.** A checkout reached as `/Users/<Login>/...`, case changed, on a
  case-insensitive volume is not canonicalised.

## Decisions

The operator took every recommendation on 2026-09-24. Each entry
below records the choice and the alternative it rejected.

1. **How the migration learns `$HOME`.** Recommended: `migrate` registers the
   conversion functions on its connection, and 014 calls them. The
   alternative is a Python verb run after a version-only migration.
   Decided: the registered functions.
2. **Whether `cost.repo` and `skill_use.cwd` are rewritten.** Recommended:
   yes. Neither is a key, but reports group by them. Decided: yes.
3. **The four absolute `register` rows in `item.path`.** Recommended: rewrite
   them repo-relative in 014, as the other 109 rows are. Decided: rewrite.
4. **Whether a write of an absolute path under `$HOME` is refused** or
   silently converted. Recommended: converted by the library, refused by
   `upsert_repo` only when a caller bypasses the conversion. Decided:
   convert, and refuse in `upsert_repo` only.

The migration number follows the design: sd:1335 also claims 014, and
whichever lands second renumbers. The work is tracked on one row. The
operator chose sd:1429, but a task row holds no document path, so the
folder's status could not be read from it. Work row sd:1439 carries it,
and sd:1429 is closed as superseded.

## Not verified

- Whether any caller outside the three trees swept reads `repo.path`. The
  sweep covered `local-sd-db`, `local-sd-runner`, `local-project-dashboard`,
  the pack's `bin/`, and `~/.config/sd/*.json`. The vault's scripts and the
  research repositories were not read.
- Whether in-flight sessions holding an `expected_revision` go stale when
  `item.repo` changes. The design names it as a risk; the migration runs with
  the runner and the dashboard stopped.
