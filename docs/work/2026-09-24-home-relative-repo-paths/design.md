---
title: home-relative repo paths
created: 2026-09-24
item: sd:1439
---
# Design — home-relative repo paths

## The shape

A repository path has two forms from now on. The **key** is what the
database stores and what callers pass around: `~/repos/system`. The **disk
path** is what `git -C`, `cwd=` and `Path.exists` need: `/Users/<login>/repos/system`
on the hub, `/Users/<second-login>/repos/system` on the laptop.

The key is converted from a disk path when it enters the library, and back
to a disk path at the moment something touches the disk. Nothing else
converts. Identifiers travel as keys everywhere between those two points:
rows, URLs, form values, JSON fields that name a repository.

## The stored form

One new module, `local-sd-db/sd_db/paths.py`, owns four functions.

| Function | In | Out | Rule |
|---|---|---|---|
| `store(path)` | disk path or key | key | Expand a leading `~/`. Resolve. If the result is `$HOME` or under it, return `~/` plus the relative POSIX path. Otherwise return the resolved absolute path. |
| `expand(key)` | key | `Path` | `~/rest` becomes `$HOME/rest`. An absolute key is returned as is. |
| `keys(path)` | disk path or key | tuple of 1 or 2 strings | `store(path)`, plus the absolute form when it differs. For `WHERE path IN (...)` probes. |
| `same(a, b)` | two paths | bool | `store(a) == store(b)`. For comparisons in Python. |

The rules that make the pair safe:

- **`$HOME` is read at call time**, from the environment, as
  `source:local-sd-db/sd_db/database.py::default_path` already does. Tests
  set `HOME` for themselves and their children, and a frozen home would
  leak the real one into them.
- **An unset or empty `$HOME` is an error**, never a fall back to the
  password database. sd:1335 gap (h) showed that a wrong home fails open.
- **Only `~/` expands.** `~user/...` is refused, because it names another
  account's home and no writer produces it. A relative input is refused.
- **Home is resolved before the comparison**, so a symlinked home still
  matches. `~/repos` is a real directory on the hub today. A machine whose
  `~/repos` is a symlink out of `$HOME` gets absolute keys, and `status`
  reports them (below).
- **The conversion never runs inside SQL at query time.** Under sd:1335, SQL
  executes on the hub. A SQL function would convert with the hub's home and
  answer the satellite wrongly. The one exception is the migration, which
  runs only on the hub, by `migrate`.

`paths.install(connection)` registers two SQL functions for the migration:
`sd_home_relative(text)` and `sd_home_absolute(text)`. They are `store` and
`expand` without the resolve, so they are pure string functions on values
the database already holds.

## Where normalisation happens

**At write and at compare, in the caller's process. Expansion happens at
disk use.** Not at read.

- **Write.** Every library function that stores a repository path passes it
  through `store`. `upsert_repo` refuses an absolute path under `$HOME`, as
  a guard against a writer that bypassed `store`. A test drives every writer
  under a fixture home to prove none does.
- **Compare.** Every lookup passes the probe through `keys` and queries
  `IN (?, ?)`. Every Python comparison uses `same`. The column is never
  wrapped in a function, so the indexes still serve the lookup.
- **Disk use.** Every site that hands a stored value to git, to `cwd=` or to
  `Path` calls `expand` first.
- **Why not at read.** Rows leave the library as `sqlite3.Row`, and callers
  post values back: the dashboard's repository select, `?repo=` filters,
  `repo=` arguments. Expanding on read would turn keys into disk paths in
  those round trips. That is harmless on one machine and wrong across two.
  Keys stay keys until the disk.

### Legacy absolute values during the transition

`keys` answers both forms, so a lookup matches whichever form the row holds.
That covers three cases with one rule:

1. A 14 library reading a 13 store in the stopped window of the rollout.
2. A fixture database written by a test before this change.
3. A value outside `$HOME`, which is absolute in both forms.

A legacy absolute value under **another** home, such as `/Users/<login>/...`
read on the laptop, matches nothing. That only happens for history (R5), and
history is not a key. `status` counts key-column values that are absolute
under the current home; after the migration that count is 0 on the hub.

## Every site, enumerated

Three read-only sweeps, on 2026-09-23, from the schema and a grep of
`local-sd-db`, `local-sd-runner`, `local-project-dashboard`, the pack's
`bin/` and `~/.config/sd/*.json`. Line numbers are left out on purpose; the
build re-greps.

### Stored columns

| Column | Live `/Users/` rows | Class | 014 rewrites |
|---|---|---|---|
| `repo.path` | 62 of 62 | key | yes |
| `item.repo` | 824 | key | yes |
| `repo_protection.repo` | 62 | key | yes |
| `runner_run.repo`, `runner_lease.repo` | 7, 7 | key | yes |
| `item.external_id` | 109 | key, `<repo>::<relative>` | yes, the prefix |
| `state.key` | 1 | key, `<repo>:status_source` or `<repo>:pieces_source` | yes, the prefix |
| `item.path` | 4 (`register`) | should be repo-relative | rewrite (decision 3) |
| `cost.repo` | 38 | attribution | rewrite (decision 2) |
| `skill_use.cwd` | 87 | log | rewrite (decision 2) |
| `item.fields` | 503 rows | history, and some keys | no |
| `item.body`, `note.body` | 498, 73 | history | no |
| `state.body` | 1408 | receipts, content-bound | no |
| `assignment.scope` | 7 | palette JSON, matched by exact string | no |
| `publication_claim.payload` | 1 | immutable by trigger | no |
| `note.output_path`, `runner_run.work_path`, `retained_path` | 8, 7, 7 | hub-only | no |

`shadow.repo` holds a GitHub slug, not a path. `removal._left` queries it
with a path, which never matches today. It is out of scope and noted for the
removal owner.

### Values that embed a key but are not a column

Each one is written with the key from now on and compared through `same`.
None is rewritten.

- **JSON that names a repository**: `completion.repo` in `item.fields`; the
  palette record in `assignment.scope` and exec note bodies
  (`runner_exec`); the publication manifest's `repo`; the verified-state
  body for runner deliveries (`ship`). `runner_exec` finds a scope by its
  exact JSON string. That stays correct because a new scope is written and
  found with the same key. No palette assignment may be pending at rollout.
  Correction (sd:1450): `completion_record` compared `completion.repo` raw,
  so every pre-014 receipt read invalid, and `finalize_delivery` compared the
  proof's `repo` raw, so a pre-014 proof could not finalize. Neither used
  `same`. Both now use `source:local-sd-db/sd_db/paths.py::same_key`, which
  passes both sides through `home_relative`, the function 014 ran, touches
  no disk, and without `$HOME` fails to match. Nothing stored is rewritten.
- **JSON that is evidence of a local file** stays absolute and machine-local:
  `report.source_path`, the contribution fields `local_clone`,
  `draft_path.path` and evidence `cwd`. `contributions` requires them to be
  absolute. A satellite does not read them; sd:1335 keeps those verbs on the
  hub.
- **Hashes over a path.** The writing cutover journal is named
  `sha256(repo)[:16]` by `source:local-sd-db/sd_db/writing.py::_journal_path`.
  A skill request's `external_id` hashes `{"repo": ...}` in `skills_catalog`.
  The recovery and removal fingerprints hash rows that carry the repo. The
  journal lookup probes the hash of each form in `keys`. Fingerprints are
  computed and checked within one preview-apply pair, so a pair must not
  straddle the migration. A skill request filed before the migration is not
  found as a duplicate after it; accepted, because the catalog has few.
- **Files beside the database.** Runner journals, publication journals and
  the writing cutover journal carry `repo`. Nine of 35 such files do today.
  `backup`'s runner-record check and `publication`'s `for_piece` compare that
  value with a row, and use `same`.
  Correction (sd:1447): the runner's hold check, `persist`'s identity check
  and `backup`'s record check compared the raw journal value, so every
  pre-014 runner record held the runner. `source:local-sd-db/sd_db/runner_journal.py::canonical`
  now gives each record read or written the key 014 gives the row.
  Correction (sd:1450): `for_piece` also compared the manifest's `repo` raw,
  not through `same`. A pre-014 publication journal entry was skipped, so the
  claim guard and recovery did not see it: the guard failed open. It now
  matches through `paths.same_key`.

### The library, `local-sd-db`

The sweep found no single normalisation point. `add`, `set_runner_merge` and
the CLI's repository root call `expanduser().resolve()`. Every other site
uses the string it was given.

| Module | Sites | Change |
|---|---|---|
| `repos` | `add`, `seed`, `set_runner_merge`, `registered_for`, `detect` | `store` before write and lookup; `keys` in `registered_for`; `expand` for `git -C` |
| `writes` | `upsert_repo`, `create_item`, `upsert_item`, `item_by_external`, `record_cost`, `record_skill_use` | `store` on write; guard in `upsert_repo` |
| `workflow` | `register_work_item`, `_fields`, the status-owner checks | `store` the repo; build `external_id` from the key |
| `progress` | `item_for_artifact`, `relink_artifact`, delivery evidence, the `::` parse | build and probe `external_id` from `keys`; `expand` before `resolve` |
| `sources` | `docs_work`, `register` | identity from the key; `expand` for disk |
| `recovery` | `reimport`, `prepare_restore`, `_work`, `_pieces` | key for identity and state key; `expand` for disk |
| `writing` | `pieces_owner`, `list_pieces`, `_path`, `_journal_path`, `recover_cutover`, `cutover_pieces` | as above; the journal probes both hash names |
| `runner`, `runner_retention` | `enqueue`, `claim`, `recover_from_journal`, `merge_authority`, the `JOIN repo` sites | keys flow from `item.repo`; `same` against journals |
| `runner_exec` | `prepare`, `_validate`, `execute_immediate` | `expand` before `cwd = value["repo"]` |
| `runner_controls` | `readiness`, `configure_item` | `keys` lookup; `same` instead of `!=`; `expand` for `git -C` |
| `ship` | `identity`, `_manual_merge_repository`, `finalize_delivery` | `keys`; `expand` before `resolve`; `same_key` for the proof (sd:1450) |
| `skills_catalog` | `location`, `request` | `store` the root |
| `protection` | `sync`, `observe`, `rows` | key on write; `expand` for `.github/workflows` |
| `removal` | `_plan_repo`, `_left`, `_item` | `keys` for the target, which `repo remove` passes raw today; key prefix for `state.key` |
| `reads`, `brief` | `brief_items`, `backlog_items`, `missing_trailers`, `note_brief` | `keys` for filters; `expand` for `git -C` |
| `publication*`, `backup`, `contributions` | `for_piece`, `build`, `_compatible_runner_records`, `projection` | `same` (`for_piece`: `same_key`, sd:1450); `expand` for disk |
| `jobs/cli.py` | `_repository_root`, `command_work`, `command_repo` | `store` the root; normalise `repo remove PATH` |
| `migrate`, `backup` | `migrate`, `_check_restore` | `paths.install` on the connection that runs migrations |

### The runner and the dashboard

Neither compares a stored path with a local one; the library does. Five
sites use a stored path on disk without expanding it, and each breaks on a
key:

- `source:local-sd-runner/sd_runner/gitops.py::clone`, `branch` and
  `forward_hooks` in the same file. `forward_hooks` would fall silently into
  its "external hooks" branch, because `relative_to` fails.
- `watch_deliveries` in `local-sd-runner/sd_runner/runtime.py`, which runs
  `sd-ship observe` with `cwd=item["repo"]`.
- `source:local-project-dashboard/sd_dashboard/screens.py::artifact`, which
  runs `git -C` on the row's repo.

Every other dashboard site displays a key or passes it back: repository
labels, the `?repo=` backlog filter, the capture and prepare selects,
`/api/items` and `/api/items/N/prepare`. They need no change, because keys
round-trip. A bookmark holding `?repo=/Users/<login>/...` still matches, through
`keys`. `~/.config/sd/runner.json` holds hub paths only (`database`, `work`,
`retention`, `pack`) and no repository list.

### The pack, `platypeeps/sd-ai-command-pack`

The pack derives the repository from the working directory, in three forms:
the main worktree root, the raw worktree root, and a Codex session's raw
cwd. Every one reaches a lookup or a write.

| Site | Change |
|---|---|
| `sd_lib.py`: `repo_root`, `registered_base`, `Rows` | `store` the base before `registered_for` and before building a key |
| `sd_lib.py`: `external_id` | builds `<root>::docs/work/...` by hand and skips `registered_for`; build it from `store` of the registered base |
| `sd_handoff_rows.py`: `item_for`, `brief_for` | `store` the base |
| `sd_work.py`: `_task_repo` | raw `SELECT ... WHERE path = ?`; use `keys` |
| `sd_work.py`: `_belongs_to`, `register`, `_standing_in`, `_repo_line` | `store`; `same` instead of `!=` |
| `sd_work.py`: `_delivery_reason` | `expand` before `is_dir` and git |
| `sd_runner.py`: `service` | `store` the root before `configure_item` |
| `sd_writing.py`: `run` | `store` the root |
| `sd-status`: `contributions_section` | `store` the root |
| `sd-ship`: `row_merges`, `merge_authority` | `keys` for the raw `SELECT`; `expand` before `resolve` |
| `sd_restore.py`: `unproven_repositories` | prints keys; `reimport` accepts either form |
| `sd-skill-use`, `sd_codex.py` | `store` the cwd |
| `sd-review`: `charged_call` | `store` the root passed as `repo=` |
| `sd_check_receipts.py` | unchanged: a checkout-local key, written and read by the pack only |

Fourteen pack test files hard-code an absolute `repo` or `external_id`.
`skills/sd-plan/SKILL.md` documents the `<checkout>::docs/work/...` shape and
needs the key form.

The pack has no static pin of `sd_db`. `sd_install.py` installs from
`~/repos/system` at its `HEAD` or an `sd-db-v*` tag, and
`sd_library_guard` refuses a lower `SCHEMA_VERSION`. The pack's CI checks out
this repository at a fixed SHA and installs `local-sd-db` from it.

Where the pack already falls back on an older library, it keeps doing so:
`getattr(sd_db, "paths", None)` absent means identity functions. That lets
the pack merge before the live library is provisioned.

## The migration, 014

**Recommended: a SQL migration that calls the two registered functions.**
014 is the number today; sd:1335's `request_outcome` claims the next free
slot too, and whichever lands second renumbers.

The file does five things, in one transaction that `migrate` already wraps:

1. Insert a `~/` copy of every `repo` row under `$HOME`, all columns
   carried. The primary key refuses a collision, which aborts the file.
2. Point the four children at the copy: `item.repo`, `repo_protection.repo`,
   `runner_run.repo`, `runner_lease.repo`. The new parent exists, so the
   immediate foreign keys hold at every statement.
3. Delete the old `repo` rows. No child names them now, so `RESTRICT` does
   not fire.
4. Rewrite the `item.external_id` prefix for `docs/work` and
   `writing-piece`, and the `state.key` prefix. The unique indexes
   `item_by_external` and `item_by_piece` refuse a collision.
5. Apply decisions 2 and 3: rewrite `cost.repo`, `skill_use.cwd` and the four `item.path` rows.

The insert, repoint and delete order is chosen over `defer_foreign_keys`
because 009 measured that pragma failing for its rebuild. Step 1 of
`implement.md` measures the update case on a copy anyway.

**Idempotent by construction.** A value already `~/` is not under `$HOME`
as a string, so a replay changes nothing. `restore` replays migrations onto a
snapshot that may already carry the new shape, and a replay must be a no-op.

**Where the functions come from.** `migrate` calls `paths.install` before the
script. So does the reference connection in
`source:local-sd-db/sd_db/backup.py::_check_restore`, which executes every
migration on an in-memory database and would otherwise fail with "no such
function". A connection without them fails closed on that error.

**Reversible.** The header of 014 carries the reverse script. It is the same
five steps with `sd_home_absolute`. The round-trip test proves it restores
the key columns byte-identical. The backup before `migrate` is the full
rollback, and `restore` migrates a 13 snapshot forward through 014 again.

**Alternatives.**

- *A version-only 014 plus a Python verb, `sd-db.sh repo rehome`.* Fewer
  moving parts in SQL. Rejected as the default: the rewrite becomes a second
  step, and forgetting it leaves the hub green and the laptop broken. It is
  the fallback if step 1 finds a SQL function unworkable.
- *A SQL migration with `/Users/<login>/` spelled out.* Rejected: wrong on every
  fixture database and on any machine but the hub.
- *No migration, rows converted lazily on write.* Rejected: a row nobody
  writes stays absolute for good.

## Rollout, and the order of the two repositories

`implement.md` carries the exact steps. The order and the reasons:

1. **This repository's pull request merges.** The live store is untouched.
2. **The pack's pull request merges.** It calls `sd_db.paths` when present,
   and identity otherwise. With the old library it behaves as today.
3. **One stopped window**: stop the runner, the dashboard and the writing
   cron jobs; back up; provision the pack's virtualenv from `~/repos/system`;
   migrate; check; restart.

Reversing 1 and 2 is safe, because of the fallback. Provisioning without
migrating leaves a 14 library on a 13 store: reads work through `keys`,
writes refuse with `SchemaTooOld`. Migrating without provisioning makes every
old reader refuse with `SchemaTooNew`. Both fail loud. Neither answers "not
registered".

Preconditions for the window, each checked, not assumed:

- No unreleased `runner_lease` (0 on 2026-09-23).
- No pending palette assignment.
- No writing cutover journal outside `complete` (the one on disk is
  `complete`).
- No preview-apply pair of `repo remove` or `item remove` open across it.

**Revisions.** `item` revisions are content hashes that include `repo`. A
session holding an `expected_revision` from before the window gets
`StaleItem` after it, and refreshes. That is the intended behaviour of the
check.

**sd:1335.** After the window, step 10 of sd:1335 drops the `$HOME` equality
from its first frame and the satellite stage's home `DIFFERS`.

## Tests that catch a missed site

Each test enumerates from the schema or drives a real path, not a list typed
from this page.

1. **The schema sweep.** Migrate a fixture whose repositories live under a
   temporary `HOME`. Enumerate every table and column from `sqlite_master`.
   Fail on any key column holding a value that begins with that `HOME`. Fail
   on any column not classified as key, history or hub-only, so a new
   column must be classified before it passes.
2. **The two-home test**, in the library, the runner, the dashboard and the
   pack. Write under `HOME=A`. Copy the store and the `~/repos` tree to `B`.
   Drive every verb under `HOME=B`: register, capture, edit, prepare, claim,
   ship lookup, brief, backlog filter, repo remove preview, the dashboard's
   item page and artifact view, the pack's `sd today`, `sd-status` and
   `sd task add --here`. Any "not registered", any git failure, any
   `FileNotFoundError` names a missed site.
3. **The write guard.** Every library writer, called with an absolute path
   under `HOME`, stores the key. `upsert_repo` called directly with one is
   refused.
4. **The round trip.** Forward then reverse on a copy of a fixture; the key
   columns are byte-identical. A collision fixture aborts 014 and leaves the
   store at 13.
5. **The restore path.** A 13 snapshot restores and migrates forward through
   014; `_check_restore` passes with the registered functions; a replay on a
   14 copy changes nothing.
6. **The status count.** `sd-db.sh status` reports 0 absolute key values
   under this home after 014, and 1 after a raw insert of one.
7. **The grep gate.** A test fails on `WHERE path = ?`, `WHERE repo = ?` or
   `repo = ?` in `sd_db` and the pack that does not take its argument from
   `keys`. Heuristic, so it carries an allow-list with a reason per entry.

## Risks

| Risk | Consequence | Guard |
|---|---|---|
| A site compares with `==` and is missed | "not registered" on a registered checkout, on one machine only | tests 1, 2 and 7 |
| A site uses a key on disk without `expand` | git runs in `./~/repos/...` relative to cwd, or silently takes another branch (`forward_hooks`) | test 2 |
| A SQL function converts on the hub for a satellite | wrong answer across machines | rule: no conversion in SQL outside 014 |
| `$HOME` unset under launchd | keys expand to nothing | `expand` refuses an unset home |
| `~/repos` is a symlink on some machine | absolute keys there | `status` reports the count |
| A writer bypasses the library | an absolute key reappears | the `upsert_repo` guard; `status` count |
| Journals and hashes keyed by the old form | a recovery misses its file | `same`, both hash names, and the window's preconditions |
| Old library opens the 14 store | refused with `SchemaTooNew` | intended; the window provisions first |
| Pack and library out of step | the pack's lookups miss | the pack's identity fallback; `keys` probes both forms |

## Rejected

- **Prefix rewrite at the remote boundary.** sd:1335 rejected it: paths sit
  in JSON, notes and receipts, and a string rewrite misses some and corrupts
  others.
- **Expansion on read.** Keys would turn into disk paths on the way back in.
- **Rewriting history.** Receipts and fingerprints are content-bound, and
  the publication payload is immutable by trigger.
- **A trigger that enforces the form.** A trigger cannot know `$HOME`
  without an application function, and one would break every raw `sqlite3`
  session against the store.
