# Design — retire a repo or item row

The shape is a plan and an apply. The plan reads every row the verb would
remove, every reason it must refuse, and every file and structured reference
it leaves behind; the fingerprint hashes the rows and refusals only. Note bodies
are free text and are not searched (section 1, second table). The apply takes the
fingerprint and a backup, re-plans inside one `BEGIN IMMEDIATE` transaction,
files one removal report that holds every removed row, removes the rows
children-first, and commits only if `PRAGMA foreign_key_check` is empty inside
that transaction.

The owner decided Q1-Q10 on 2026-09-13 (decision note 1846 on sd:754). This
design follows those decisions. `implement.md` gives the steps. The planning
adversarial review runs next, at the "prd and design" point (cap 5) in the
pack's `.claude/rules/sd-planning-adversarial-review.md`.

## Decisions

Decided by the owner on 2026-09-13, on draft PR #336 head `632859db`, in
decision note 1846. The draft's questions Q1-Q10 map to D1-D10. Note 1856
confirmed two points of D2 and D9 (D2a, D9a), and note 1879 decided the
retained-clone path of D4 (option a). Note 1900 decided the runner journal
files of D4 (option c, review round 2, C-22). It replaces the journal clause
of note 1846 Q4, "leave the journal files".

- **D1. Delete the rows.** A record holds every removed row in full. There is
  no mark column. (Q1 A.) Section 4.
- **D2. The record is one `report` item, an existing kind.** The removed rows
  are in its note bodies. There is no new `state` kind and no migration 010.
  (Q2 B. The draft recommended A.) This matches sd:755 D3 (PR #334), so
  both verbs file their record through `reporting.ingest` with the same
  `fields.report.actor` shape. Section 4.1 gives the shape, 4.2 the size
  limits, and 4.3 how a reader finds a removed row.
- **D3. Allow every note, and copy each one into the record.** Refuse only an
  item with an unresolved `followup` or `question` note. (Q3 B.) Refusal I7.
- **D4. Remove released runs and their leases, and move their journal files
  to quarantine.** Refuse an unreleased run, an open lease, or a retained
  clone still on disk. (Q4 B.) Refusals I6 and P4.
  **Journal files: option (c), decided by the owner in decision note 1900
  (review round 2, C-22).** Note 1846 Q4 said to leave the journal files.
  Note 1900 replaces that clause. A journal file left for a removed run makes
  `reconciliation.plan` report a blocked "assignment is absent" entry
  (`reconciliation.py:86`, `:95-104`). While any entry exists,
  `archive_refresh._safe_state` holds archive refresh for every run
  (`archive_refresh.py:119-122`), retried every 60 seconds
  (`runtime.py:828-830`). So after its commit, the apply moves each removed
  run's `runner-journal/<run>.json` and `<run>.lock` into
  `runner-recovery-evidence/removed-<fingerprint>/`, beside the database.
  If the move fails, the rows stay removed, the command prints the exact
  `mkdir` and `mv` commands with every path passed through `shlex.quote`,
  and it exits 4. Section 4, step 7 gives the move, and section 4.4 says why
  nothing reads the quarantine directory as a journal. The retained-clone
  path below is unchanged.
  **Retained clones: option (a), decided by the owner in decision note 1879
  (review round 1, C-1).** No supported verb removes a retained clone today.
  `maintenance.plan_prune` only plans (`"dry_run": True`,
  `maintenance.py:57-90`), and retention freezes each clone with
  `chflags -R uchg` (`storage.py:226-234`). So the refusal does the work of
  telling the operator what to do. For each retained clone still on disk, it
  prints the path (`runner_run.retained_path`) and the two commands to run,
  in order, with the path passed through `shlex.quote`:

  ```
  chflags -R nouchg <path>
  rm -rf <path>
  ```

  The path is database text, so the plan checks its shape before it prints
  anything, as `reconciliation._paths` does for the runner
  (`reconciliation.py:109-115`): it is absolute, it ends in
  `<runner_run.assignment>/<runner_run.run>/clone`, no component is a
  symlink, and it resolves inside the retention root. A path that fails the
  check is refused with "retained path has an unexpected shape", and no
  command is printed for it (review round 2, C-24). The last two clauses
  overlap, and PR 1 wrote them as one condition (`removal.py:129`). The
  retention root comes from the path itself (`parents[2]`,
  `removal.py:126`), so with no symlink in the path `path.resolve()` and
  `root.resolve()` normalise the same way and the resolve clause cannot fail
  where the symlink clause passes. It earns its keep against a race alone: a
  component replaced by a symlink between the two reads. Both clauses
  stay.

  The operator runs them, then runs the remove again. The refusal clears
  once no retained clone remains. The verb itself never clears the flag and
  never deletes a file. When the retained volume is not mounted, I6 and P4
  refuse and say that the volume is not mounted. A missing volume is not
  read as "no clones". A runner `prune apply` for retained clones (option b)
  is the later path, filed as followup sd:770.
- **D5. `repo remove --with-items`** takes the repo's items with it. The plan
  is all or nothing, and each item must pass the item rules. (Q5 B.) Refusal
  P3.
- **D6. Refuse where a routine command would recreate the row and the check
  is local.** Report importer-keyed rows in the preview. (Q6 C.) Refusals I8
  and P5.
- **D7. Services keep running.** The verb takes `control_gate` and `BEGIN
  IMMEDIATE`, keeps `foreign_keys` on, and refuses active runs. (Q7 A.)
  Section 5.
- **D8. The verb is `remove`, on the system CLI only:** `sd-db.sh repo remove`
  and `sd-db.sh item remove`. There is no pack verb and no dashboard button.
  (Q8, name `remove`, surface A.) Section 6.
- **D9. `--who` and `--reason` are required.** There is no `getpass` fallback
  for `who`. (Q9 A.) Section 6.
- **D10. The apply runs `backup.run` first** and writes the snapshot directory
  into the record. (Q10 A.) Section 4.

**Decided 2026-09-13, decision note 1856 (confirming two points left open at
`d1046822`, note 1855):**

- **D2a. The rows go in comment notes, and the manifest goes in `body.text`.**
  The full rows are JSON lines in `comment` notes on the record report, split
  only between rows. The report's `body.text` holds the manifest, one key line
  per row. This refines note 1846's "note body". The manifest plays the part
  of sd:755's "body lists the ids". Section 4.1 gives the format, and section
  4.2 gives the split.
- **D9a. The record stores `principal` next to `who`.** `principal` is the
  login account, as in sd:755 D4. `who` is required and never falls back to
  `getpass`, so D9 holds.

**Coordination with sd:755, from its planning review (finding C-6).** These
points are not owner decisions. They were agreed between the two planning
lanes on 2026-09-13, so that neither verb removes or sweeps the other's
records:

- **Marker.** Every record report carries a top-level `fields.record` marker.
  sd:754 writes `fields.record = "item-remove"` for an item remove and
  `"repo-remove"` for a repo remove, the job name in both cases. sd:755 writes
  `fields.record = "reports-acknowledge"`. sd:755's `--all-clean` declines
  any report that has a `fields.record` marker (section 4.1).
- **No record removes a record.** `item remove` refuses any item that carries
  a `fields.record` marker, whatever the value (I9). This extends D3 and D6:
  a record is the only copy of what it holds, so removing it is refused like
  an open request or a row that cannot come back.
- **Filed done, with attention false.** The record report has
  `attention = false`, so it adds no followup and raises no needs-you. It is
  moved to `done` in the same transaction that files it. No bulk acknowledge
  or retention settle ever selects it (section 4.4). sd:755 will move its
  batch record to `done` in the same transaction too (sd:755 planning review
  round 2, C-17), so neither kind of record stays `planning`.
- **The `ingest` keywords.** Each plan adds `actor` and `record` to
  `reporting.ingest` only if they are absent, and neither plan's rollback
  removes a keyword the other item already uses (sd:755 round 2, C-18;
  section 9).
- **Guards check that the marker exists, not its value.** The one predicate,
  the same in sd:754 and sd:755 (sd:755 round 2, C-19; sd:755 plan at #334
  head `9bdf89c2`, design 3 and 8), is:

  ```
  CASE WHEN json_valid(fields) THEN json_type(fields,'$.record') END IS NOT NULL
  ```

  It is not a job list, which would need an edit for every new kind of
  record.
  - `json_type` and not `json_extract`: the two differ only on a JSON `null`
    marker, `{"record": null}`. `json_type` gives `'null'`, so the row counts
    as a record. `json_extract` gives SQL `NULL`, so the row does not.
    `ingest` cannot write that shape, because `record=None` stores no key, so
    only a hand edit can. For a hand-edited row the safe answer is "record" in
    both verbs: `item remove` refuses it (I9), and sd:755's bulk clean
    declines it. Both fail closed.
  - Every `json_extract` and `json_type` on `fields` sits inside `CASE WHEN
    json_valid(...) THEN ... END`, so malformed text never raises inside a
    plan. Malformed text is not read as "no marker": refusal I11 refuses it,
    so `item remove` fails closed, as sd:755's bulk clean declines a report
    with malformed `fields` (review round 2, C-31). The live store has 0
    items with invalid `fields` (2026-09-14).

## 1. What references `repo` and `item`, from the schema

This was enumerated from the live store, not from memory:

```sql
select m.name, p."from", p."table", p."to", p.on_delete
from sqlite_master m join pragma_foreign_key_list(m.name) p
where m.type = 'table' order by p."table", m.name;
```

`PRAGMA user_version` is 9, which matches `schema/001` through `009` on
`origin/main` `0b5394ae`.

| child column | parent | on delete | what a remove does |
|---|---|---|---|
| `item.repo` | `repo.path` | RESTRICT | Refuse, or `--with-items` (D5) |
| `repo_protection.repo` | `repo.path` | NO ACTION | Removed with the repo. It is an observation that `protection.py:618` rebuilds from `repo` |
| `runner_run.repo` | `repo.path` | NO ACTION | D4 |
| `runner_lease.repo` | `repo.path` | NO ACTION | D4 |
| `note.item` | `item.id` | CASCADE | D3. Deleted explicitly, so the plan lists each note |
| `assignment.item` | `item.id` | CASCADE | Removed if terminal. Otherwise refuse (section 3) |
| `publication_claim.item` | `item.id` | NO ACTION | Refuse |
| `publication_claim.active_item` | `item.id` | NO ACTION | Refuse |

These dependents are reached through `assignment`, which an item remove
removes:

| child column | parent | on delete | what a remove does |
|---|---|---|---|
| `assignment.after` | `assignment.id` | NO ACTION | Refuse if the child is on another item |
| `assignment.parent` | `assignment.id` | NO ACTION | Refuse if the child is on another item |
| `cost.assignment` | `assignment.id` | NO ACTION | Refuse. The ledger is never pruned (`retention.py:6-9`) |
| `runner_run.assignment` | `assignment.id` | NO ACTION | D4 |
| `runner_lease.run` | `runner_run.id` | NO ACTION | D4 |

`cost.provider`, `cost.bill` and `assignment.provider` point away from these
tables and do not matter here. `shadow`, `skill_use`, `trial` and `state` have
no foreign keys.

**Children.** The item text asks about "children". `item` has no parent
column, so there is no item-to-item parent link in the schema. The only
parent-child links are `assignment.after` and `assignment.parent`, shown
above.

**References without a foreign key.** The schema cannot refuse these. The plan
reports them and does not change them. The one exception is the runner
journal files of the removed runs, which the apply moves after its commit
(D4, note 1900):

| where | shape | live measurement |
|---|---|---|
| `cost.repo` | path text | 0 cost rows |
| `shadow.repo` | tracker slug, not a path | 3704 rows, 0 equal to a registered path |
| `state` `verified` key | `<repo>:pieces_source` | 1 of 8 `verified` keys names a repo |
| `item.external_id` | `<repo>::` plus the item's repo-relative `prd.md` path | 71 of 71 `docs/work` items |
| `item.fields` | `contribution`, `completion.item`, `runner_branch` | 8 items carry `contribution` |
| `assignment.scope` | palette JSON with `item` and `repo` | on the probe's assignments 2 and 3 |
| note bodies | free text such as `sd:72` | not measured |
| `runner-journal/<run>.json` | run id, repo | 0 files without a row (the 2 probe files were removed by hand on 2026-09-13; measured 2026-09-14T03:03Z). Moved after the commit (D4, note 1900) |
| `executions/<uuid>.log` | referenced by `exec` notes | 2 probe logs with no note today |
| `publications/<claim>/manifest.json` | item id | 1 claim, on item 15 |
| retained clones | `/Volumes/sd-work/retained/<assignment>/<run>/` | 0 clones without a row (the 2 probe clones were removed by hand on 2026-09-13; `retained/` holds 4-9 at 2026-09-14T03:03Z). Refused, I6 and P4 |
| checkout folders, `repos.personal.conf` | paths | D6 |
| another item's `fields.contribution.depends_on` | `{"kind": "item", "item": <id>}` | 0 live (2026-09-14T03:03Z). Refused, I10 |
| `contribution-refresh-queue` checkpoint | pending entries with `item_id` and `depends_on` | 0 live `depends_on` of kind item. Refused, I10 |
| `state` `contribution:item:<id>` checkpoints | item id in the key | 7 rows, on items 242 and 245. Listed under `left:` |
| `state` `ship:<sha>` checkpoints | sha256 of repository, branch and item (`ship.py:30`), item id in the body | 60 rows. Listed under `left:` |
| `state` `runner-delivery:<run>` verified proofs | run, assignment, item and repo in the body (`ship.py:132`, `:150-167`) | one per merged run. Listed under `left:` |
| `runner-journal/<run>.json` and `<run>.lock` | assignment id | see C-2 and refusal A1. Moved with the `.lock` into `runner-recovery-evidence/removed-<fingerprint>/` after the commit, and listed under `move after commit:` (D4, note 1900) |
| `runner-ending/<run>.lock` | run id (`runtime.py:630`) | Listed under `left:` |
| `<retention root>/<assignment>/<run>/retention.json` | the clone's retained time (`storage.py:249`) | Left. Listed under `left:` (section 4.4) |
| `<retention root>/<assignment>/<run>/.archive.lock` | lock file of prune planning and archive refresh (`maintenance.py:67`, `archive_refresh.py:167`) | Left. Listed under `left:` when present (section 4.4) |
| `.sd-restore-<token>.lock` | lock file of a clone restore, in the parent of the restore destination (`restoration.py:95-97`), which the operator chooses (`sd_runner/controls.py:62-73`) | Left. Listed under `left:` when present in the run directory, which holds one only when a restore targeted a path inside it (section 4.4, review round 4, C-61) |
| `<retention root>/<assignment>/<run>/kept.tar`, `archives/<generation>/kept.tar` and its `manifest.json` | the run's archived work (`runtime.py:654`, `archive_refresh.py:187`) | Left. Listed under `left:` (section 4.4) |
| `runner-reconciliation/<fingerprint>.json` | a reconciliation receipt with a `run` field (`reconciliation.py:120-130`) | Left. Listed under `left:` when its `run` is a removed run (section 4.4) |
| `runner-recovery-evidence/removed-<fingerprint>/` | the moved journal files and `receipt.json` | Written by the apply after the commit. No runner reader treats it as a journal (section 4.4) |
| `executions/<uuid>.receipt.json` | beside each execution log (`retention.py:134`) | Listed under `left:` |
| `<work root>/<item>/` | item id (`reconciliation.py:110`) | Listed under `left:` |
| `sd_runner` recovery plan | a journal record whose assignment is absent is a blocked entry, "assignment is absent" (`reconciliation.py:86`) | No entry once the journal file is moved: the plan reads only `runner-journal/` (`reconciliation.py:36-43`, `runner_journal.py:21-22`). A failed move leaves the entry until the operator runs the printed commands (section 4.4) |

## 2. Measurements

All of these were run with `sqlite3 -readonly ~/.local/share/sd/sd.db`,
except where a backup file is named. The counts were re-measured at
2026-09-14T03:03Z for review round 1 (C-9). Where a count changed since the
first draft (2026-09-13), both are given.

| query | result |
|---|---|
| `select count(*), max(id) from item` | 768, 769 at 03:03Z (761, 762 on 2026-09-13). Only id 72 is missing (`min(id)` = 1) |
| `select count(*) from item i where not exists (select 1 from note n where n.item=i.id)` | 0 |
| `... where (select count(*) from note n where n.item=i.id)=1` | 153 |
| `... exists (... n.kind in ('followup','question') and n.resolved_at is null)` | 23 |
| `select count(distinct item) from note where kind='exec'` | 2 |
| `select status, count(*) from assignment group by status` | blocked 5, cancelled 1, done 1. Ids 1, 4-9; `max(id)` 9 |
| `select count(*), sum(released_at is null) from runner_run` | 6, 0 |
| `select count(*) from repo` | 16. All 16 paths exist on disk |
| repos with 0 items | 1, `<app-d>`. It has a `repo_protection` row and is in the conf |
| `select count(*) from pragma_foreign_key_check` | 0 |
| `select count(*) from publication_claim` | 1, on item 15, `active_item` NULL |
| `select count(*) from cost` | 0 |
| `select source, count(*) from item group by source` | NULL 311, cron-report 310, docs/work 71, drafts 50, writing-piece 22, register 4 (NULL was 304 on 2026-09-13) |

Which of those six `source` values has a writer on main `62319e67`:
`docs/work` (`sources/docs_work.py:74`) and `register`
(`sources/register.py:60`) are importers that write `item` rows, and so is
`vault` (`sources/vault.py:58`), which has 0 item rows today.
`writing-piece` comes from `writing.import_pieces` (`writing.py:395`), and
`cron-report` from `reporting.ingest` (`reporting.py:98`). `drafts`, 50 rows,
matches no importer on `main`: nothing recreates those rows. The two source
modules that are not in the list, `github-issues` (`sources/issues.py:33`)
and `index.sqlite` (`sources/index_cache.py:39`), land through
`upsert_shadow` (`issues.py:107`, `index_cache.py:132`) and write `shadow`
rows, never an `item` row.

sd:744's rows were rebuilt from `~/Documents/sd-backups/2026-09-10.3/sd.db`.
That snapshot was taken 2026-09-10 17:43 local, before the 23:52Z CLI
deletion, and its `foreign_key_check` returns 0. Section 7 uses it.

**Record size.** Each row was serialised with `json_object` over every column
and measured with `length(CAST(... AS BLOB))`, which counts UTF-8 bytes. Each
row line also gets the `{"table":...,"row":...}` wrapper, 35 bytes at most.
Rows were packed greedily into notes of at most 200,000 bytes, split only
between rows. The measurement is `sd754-size.sql` and `sd754-rows.sql` in the
lane scratchpad, and the packing script is `sd754-pack.py`. Re-run at
2026-09-14T03:04Z. The row counts are `item`, `note`, `assignment` and
`repo_protection` rows. They leave out the `repo` row and runs and leases,
which add under 2 % (system: 1 + 6 + 6 rows) and change no note count. The
round 1 reviewer measured the same plans independently with Python
`json.dumps` and all rows, and got the same note counts within 0.5 % of the
bytes.

| candidate | rows | bytes | notes | largest row |
|---|---|---|---|---|
| `repo remove sd-ai-command-pack --with-items` (119 items, 458 notes, 1 assignment, 1 `repo_protection`) | 579 | 778,891 | 4 | 26,593 |
| `repo remove writing-pack --with-items` (28 items, 58 notes, 1 `repo_protection`) | 87 | 523,103 | 3 | 60,832 (item 23) |
| `repo remove system --with-items` (64 items, 285 notes, 6 assignments, 1 `repo_protection`) | 356 | 447,575 | 3 | 6,976 |
| sd:744 probe, from the 2026-09-10.3 backup | 15 | about 5,000 of column values | 1 | under 3,200 |
| largest single item row in the store | 1 | 207,797 (item 679, a report with `repo` NULL) | refused (R1) | 207,797 |

Other size facts:

- Items whose rows, notes and assignments pass 200,000 bytes: 1 of 768
  (item 679). Past 100,000 bytes: 2. Past 50,000: 6.
- The largest single note is 25,789 bytes (note 1717). The largest item
  `body` is 203,425 bytes (item 679), because `ingest_log` stores up to
  `MAX_REPORT` bytes of log text (`reporting.py:14`, `:199-207`).
- Every one of these counts would be refused today by other rules: for
  example, `sd-ai-command-pack` has open followups and `docs/work` files
  (I7, I8). The table sizes the worst case, not a likely apply.

## 3. Refusals

Each refusal names the table and the id. Every refusal is found in the plan,
and the plan runs again inside the apply's transaction.

**Both verbs:**

- G1. The store already has foreign key violations. This verb does not repair
  orphans. Otherwise the in-transaction check could not tell old damage from
  new damage.
- G2. An unresolved `restore` state row exists. `recovery.reimport` expects the
  rows it recovers, and the runner holds dispatch until the restore is
  resolved.
- G3. The fingerprint differs from a fresh plan. It is checked twice in the
  apply: before `control_gate` and the backup, and again inside the
  transaction (section 4).
- G4. `who` or `reason` is blank (`workflow._text`), or longer than 200
  characters, or `session` (from `SD_SESSION`), `principal` or `program` is
  longer than 200 characters. These five are the `actor` values of section
  4.1, and 200 characters is the `actor` value limit that sd:755 sets on
  `ingest`. So no `actor` value can refuse inside the transaction (review
  round 2, C-29). G4 is checked first in the apply, before `control_gate` and
  before the backup, so a value `ingest` would refuse never costs a snapshot.
  One function checks G4 on both paths:
  `removal.check_actor(*, who, reason, session, principal, program)`. The
  CLI calls it before the preview, and `apply` calls it first. `plan_item`
  and `plan_repo` take no actor values, and G4 is not a plan refusal
  (review round 4, C-61). `SD_SESSION` is refused, not truncated. sd:755
  truncates it in its own verb (sd:755 round 2); this verb refuses, because
  the record is the only copy of who removed the rows, and a cut session id
  names no session. The difference from sd:755 is on purpose. Without this
  check, an over-long `SD_SESSION` would
  reach `ingest` inside the transaction and refuse there, after the backup
  (review round 1, C-7).
- G5. The apply's connection has no `main` file (an in-memory connection),
  checked at step 1 of section 4, before `control_gate` (review round 3,
  C-52; round 4, C-60). Or `backup.run` raised, or its snapshot reports
  foreign key violations (`Snapshot.violations`, D10). All three are checked
  in the apply, before the transaction. A G5 from the count race of section 4.4 is safe to retry: the
  store holds no removal write, only the backup's own checkpoint row, and a
  second apply with the same fingerprint plans the same rows.
- G6. A removal record for this fingerprint already exists: an item with
  `source` `cron-report` and `external_id` `<kind>-remove:<fingerprint>`.
  That happens after a put-back (section 4.3), which re-inserts the same
  rows with the same values, so the plan gives the fingerprint of the first
  remove again. G6 is checked in the plan after the fingerprint is
  computed, and it is not part of the fingerprint. So the preview lists it
  and exits 3, and the apply refuses at step 2, before `control_gate` and
  the backup. Without G6, the second apply would take a snapshot and then
  fail inside the transaction: `ingest` raises "this report identity
  already has different evidence" (`reporting.py:92-95`), and step 7.4
  would refuse the existing `removed-<fingerprint>/` in any case (review
  round 5, C-64). The message is "a removal record for these rows exists
  (item <id>); they were put back after that remove, and this verb does not
  remove them again". Removing put-back rows again needs a new record
  identity. That is later work, not this item.
- A1. Review round 1, C-2: a removed `assignment.id` is not smaller than the
  largest `assignment.id` that survives the plan. `assignment.id` is
  `INTEGER PRIMARY KEY` without `AUTOINCREMENT`, so SQLite gives the next
  assignment `max(id)+1`. If the removed ids are the largest, the next
  assignment reuses one of them. Runner state outside the store still names
  the removed id: the retained path is
  `<retention root>/<assignment>/<run>/clone` (`runner.py:154-196`), and a
  journal record names the assignment. The apply moves that record after
  its commit (D4), but a failed move leaves it, and a full restore of an
  older snapshot can bring it back. `reconciliation._entry` blocks a
  journal record only while its assignment is absent (`reconciliation.py:86`),
  so a reused id unblocks it against the wrong row. The refusal clears when
  newer work creates a larger assignment id.

  **When no assignment survives.** If the plan removes every assignment in
  the store, `max(id)` over the survivors is SQL `NULL` (or a `ValueError`
  from Python `max()`), a comparison with `NULL` reads as "not refused", and
  SQLite gives the next assignment id 1, which reuses a removed id. So A1
  states this case: when the plan removes at least one assignment and no
  assignment outside the plan exists, A1 refuses. It clears when an
  assignment outside the plan exists with an id larger than every removed
  id (review round 2, C-23).

  **Why a refusal, not a placeholder row.** A placeholder `assignment` row
  with the largest removed id would keep `max(id)` high. The schema allows
  it: `assignment.item` and `assignment.provider` are nullable
  (`schema/001_initial.sql`). But it is a row that never ran. Every reader
  of `assignment` (the queue, `runner.claim`, the dashboard queue views)
  would need a rule to skip it, and a later remove must never take it. The
  refusal adds no row and no reader rule. It is rare, because it applies
  only when the removed work is the newest assignment in the store. Measured today:
  `max(id)` is 9 (item 442), so a remove of item 442's assignments alone
  would be refused, and any older assignment would not.

**The record (section 4.2):**

- R1. One row line is larger than one record note can hold.
- R2. The rows need more than `MAX_RECORD_NOTES` notes (16).
- R3. The manifest is larger than `reporting.MAX_REPORT` (200,000 bytes).

**Item:**

- I1. No such item.
- I2. `publication_claim.item` or `active_item` names the item. Publication is
  external and its payload is immutable (the `publication_payload_immutable`
  trigger).
- I3. An assignment on the item has a status other than `done` or
  `cancelled`. This covers `blocked`, because `runner.py:262` says "operator
  must review and requeue", and it covers `queued`, `running` and `ending`
  (`runner.py:125`).
- I4. A `cost` row references one of its assignments.
- I5. An assignment on another item points at one of its assignments through
  `after` or `parent`.
- I6. D4: an unreleased `runner_run` or an open `runner_lease` for its
  assignments, or a retained clone still on disk for one of those runs. The
  clone check reads `runner_run.retained_path`. It also refuses when the
  retention root, the directory three levels above `retained_path`
  (`<root>/<assignment>/<run>/clone`), does not exist or is not a directory.
  An unmounted `/Volumes/sd-work` then refuses with "retained volume is not
  mounted", where `exists()` on the clone alone would pass falsely. For each
  clone on disk, the refusal names `retained_path` and prints
  `chflags -R nouchg <path>` and `rm -rf <path>` (D4, option a).
- I7. D3: an unresolved `followup` or `question` note.
- I8. D6: the `docs/work` file exists in the checkout, `piece` is not NULL,
  or `fields` carry `contribution` or `skill_review`. The file test is
  `Path(row["repo"], row["path"]).exists()` (`removal.py:222-224`), so it
  reads the working copy alone. A file committed on an unmerged remote branch
  and absent from the checkout passes I8; the D6 paragraph below says what
  follows. The `piece` clause covers the `writing-piece` items, which
  `writing.import_pieces` (`writing.py:395`) reads back from the working
  copy: it globs `content/*/*/index.md` there (`writing.py:384`) and reads
  each file (`:294`). So they are refused rather than reported.
- I10. Review round 1, C-3: another item's `fields.contribution.depends_on`
  has `{"kind": "item", "item": <this id>}`, or a pending entry in the
  `contribution-refresh-queue` checkpoint names this id as `item_id` or as an
  item dependency. `contributions.py:290` and `:712` call `item_state` on each
  item dependency. A removed item raises `MissingItem`, which
  `contribution_sync.py:223` records as "refused", so the dependent's nightly
  sync and its config edit would fail from then on. Live count: 0.
- I9. D3 and D6, from the sd:755 coordination: the item's `fields` carry a
  `record` marker, with any value, including a JSON `null`. The predicate is
  `CASE WHEN json_valid(fields) THEN json_type(fields,'$.record') END IS NOT NULL`, the same as sd:755's (C-19). That covers sd:754's `item-remove` and `repo-remove` records and
  sd:755's `reports-acknowledge` batch records. A record is the only copy of
  what it holds, so no remove may take it, and a remove cannot take its own
  record.
- I11. Review round 2, C-31: the item's `fields` is not valid JSON
  (`NOT json_valid(fields)`), or another item's `fields` is not valid JSON,
  or the `contribution-refresh-queue` checkpoint body is not valid JSON.
  With malformed text, I8, I9 and I10 cannot read `contribution`,
  `skill_review`, the record marker or a dependent's `depends_on`, so the
  plan cannot tell whether the item is safe to remove. It refuses instead,
  and names each malformed row. Live count: 0 (2026-09-14). PR 1 widened I11
  to a stored value the record cannot hold, such as a blob in a text column:
  the guard is an `isinstance` test: `_unreadable` refuses any column value
  that is not `str`, `int`, `float` or `None` (`removal.py:317`). Nothing
  raises. The refusal is recorded first (`removal.py:392-394`) and the
  fingerprint follows (`:397`), but the fingerprint still covers the row,
  because `_finish` canonicalises with `default=repr` (`removal.py:395-396`)
  and a value `json.dumps` cannot encode is hashed as its `repr`. The
  docstring at `removal.py:310-312` names `json.dumps` raising as the
  mechanism and says the value is refused before the fingerprint reads it.
  Neither is what the code does; PR 2 corrects that docstring (Copilot on
  #336). The design gave no
  code for the case, and I11 is the refusal it belongs to.

**Repo:**

- P1. No such repo.
- P2. `status_source` or `pieces_source` is `retiring`.
- P3. D5: an item names the repo and `--with-items` is absent. With the flag,
  every item must pass I1-I11.
- P4. D4: an unreleased run, an open lease, or a retained clone on disk for a
  run on this repo, with the same retention-root check and the same printed
  commands as I6 (D4, option a).
- P5. D6: `repos.personal.conf` names a checkout that resolves to this path
  (`repos.checkouts`, `repos.py:125`), and the checkout is present on disk.
- P6. Review round 1, C-4: a `runner_run` row on this repo belongs to an
  assignment whose item is not in the plan. `runner.claim` copies
  `item.repo` into `runner_run.repo` when the run starts
  (`runner.py:154-196`). An item that later moved to another repo, or lost
  its repo, leaves a run on this repo that the plan would otherwise remove
  without its assignment and item. Measured today: all 6 runs have
  `runner_run.repo` equal to the item's repo, so the case is latent.

**Reported, not refused (D6).** An item whose `source` and `external_id`
belong to a writer that can key the row again through the unique index
`item_by_external`. On main `62319e67` those writers are the three importers
that write `item` rows, `docs/work` (`sources/docs_work.py:74`), `vault`
(`sources/vault.py:58`) and `register` (`sources/register.py:60`), and
`reporting.ingest`, which keys a `cron-report` item by `job + ":" + run_id`
(`reporting.py:78`, `:98`), so a replay of the same report writes it again.
The preview names the source.

One more writer keys an `item` row and never reaches the report:
`skills_catalog.request` writes `source` `skill-request` with an
`external_id` (`skills_catalog.py:234`, `:259`) and sets
`fields.skill_review` (`:260`), which I8 always refuses
(`removal.py:227-232`). 0 rows live today.

Three source names are not in that set. `github-issues`
(`sources/issues.py:33`) and `index.sqlite` (`sources/index_cache.py:39`)
write `shadow` rows through `upsert_shadow` (`issues.py:107`,
`index_cache.py:132`) and no `item` row at all, so no item warning applies to
them. `drafts` matches no importer on `main` (section 2). PR 1 shipped the
set as `IMPORTED = ("register", "drafts", "cron-report")`
(`removal.py:38`), which names `drafts` and omits `docs/work` and `vault`.
The set above is the correct one, and `implement.md` step 6 derives it before
the CLI prints the warning (the #350 review, N-1).

**A report is not a refusal, and three kinds of row come back.** Nothing
refuses these three, and the next sitting writes them again:

- A `register` item. **Certain, and live today.** The register migration
  reads its file from a committed tree, `git show <ref>:<path>`
  (`register.py:192`), where `<ref>` is the default branch
  (`register.py:174`, `HEAD` when the checkout has no `origin`). Its own
  words: "The working copy is not consulted" (`register.py:199`), and
  "still the committed tree and never the working copy"
  (`register.py:165-166`). It upserts by `(source, external_id)`
  (`register.py:251-254`). No refusal reads the register, and I8's file test
  never runs on such a row: it is gated on `row["source"] == "docs/work"` or
  a `docs/work/` path (`removal.py:222`), while a register row's `path` is
  the register file. Unlike the `docs/work` case, no unmerged branch is
  needed: the read is of the default branch, so it always succeeds. Live
  today, read-only on 2026-09-14: items 362, 364 and 366 (`register`, `O27`,
  `O29`, `O30`, of 4 register rows) have no unresolved `followup` or
  `question`, no assignment, `piece` NULL, no `contribution` or
  `skill_review`, and no record marker. Nothing refuses them, and each one
  comes back.
- A `docs/work` item whose file is committed on an unmerged remote branch
  that the checkout is not on. Conditional on such a branch. That importer
  never reads the working copy. It takes every unmerged remote branch plus
  the default (`branches`, `docs_work.py:135`) and lists each one's
  committed tree with `git ls-tree -r --name-only <ref>`
  (`docs_work.py:282`), driven by `for ref in branches(root)` and
  `files(root, ref)` (`:527-528`). Its own docstring says so: "Committed
  trees, never the working copy" (`docs_work.py:13`). I8 asks
  `Path(repo, path).exists()`, which is the checkout, so a file that lives
  only on the other branch passes I8 and the item is removable.
- A `vault` item. Conditional on the vault file, and 0 rows live today
  (section 2). The vault importer reads the vault directory
  (`base.rglob("*.md")`, `vault.py:171`) and upserts the item again
  (`vault.py:213`). No refusal reads the vault: I8 tests a `docs/work` path
  alone (`removal.py:222-224`).

Those three are the re-import gap D6 leaves. An operator who removes such a
row must expect it back at the next sitting, under a new item id. The two
conditional cases need a check that reads the vault, or every remote branch
of every registered repository, which D6 excludes ("the check is local").
The `register` case does not: the same `git show <ref>:<register path>` the
importer runs is as local as the `docs/work` file test, and a refusal could
ask whether the row's `external_id` is still an open entry there. Adding it
would be a new refusal, which this plan does not carry. That is an owner
decision, recorded here rather than folded (the planning review cap is
spent, note 1922).

## 4. The apply, and what backup, restore and retention do

**Apply order.**

1. The CLI checks its flags before it opens the store: `--apply` needs
   `--if-fingerprint`, `--who` and `--reason`. The library then checks G4
   with `check_actor` (`who`, `reason`, `session`, `principal`, `program`) before anything else.
   Next it reads the connection's `main` file with `PRAGMA database_list`.
   An empty name (an in-memory connection) is refused with G5 here, before
   `control_gate`. Otherwise `control_gate` would raise `WorkflowError`
   "service controls require a file-backed database" at step 3
   (`operations.py:39-41`) (review round 3, C-52).
2. Plan read-only, outside any transaction, and compare the fingerprint
   (G3). Any refusal raises here. So a stale or refused apply takes no
   `control_gate` and writes no snapshot (review round 2, C-25).
3. Enter `operations.control_gate` (`operations.py:34`). It must come before
   any transaction.
4. Run `backup.run(home=home, database=<file>, keep=None)` (`backup.py:657`),
   where `<file>` is the `main` file of the apply's own connection, read
   with `PRAGMA database_list`. `backup.run` opens `database` when it is
   given, and the default path under `home` when it is not, so passing it
   makes the snapshot a copy of the store the apply changes. An in-memory
   connection never reaches this step: step 1 refuses it with G5 (review
   round 2, C-29; round 3, C-52).
   Refuse (G5) if `backup.run` raises or if `Snapshot.violations` is not
   empty. Keep `Snapshot.directory` for the record. `backup.run` writes its
   own checkpoint row, so it runs outside the removal transaction.
5. Open one `transaction(connection)`, which is `BEGIN IMMEDIATE`
   (`database.py:126`):
   1. Plan again, and compare the fingerprint (G3). A write between step 2
      and this lock that changes a planned row or adds a refusal changes the
      fingerprint; the backup's own checkpoint row does neither. Any refusal raises. Nothing is
      written yet.
   2. File the record (section 4.1): one `reporting.ingest` call, then one
      `add_note` for each chunk, in order, then
      `transition(connection, record, "done", who=who, reason="removal record")`
      (`writes.py:182`). The record is written before the
      deletes. `item.id` and `note.id` are `INTEGER PRIMARY KEY` without
      `AUTOINCREMENT` (`schema/001_initial.sql`), so SQLite gives a new row
      `max(id)+1`. If the deletes came first and removed the largest id, the
      record would take a removed item's or note's id.
   3. `DELETE` from `runner_lease`, then `runner_run`, then `assignment`, then
      `note`, then `item`, then `repo_protection`, then `repo`. Each statement
      uses explicit keys from the plan. With `foreign_keys` on, a NO ACTION
      constraint is checked at the end of each statement, so the order must be
      children first. The plan does not rely on CASCADE. All of a plan's
      assignments go in one `DELETE ... WHERE id IN (...)`, so an `after` or
      `parent` chain inside the plan is removed in one statement and never
      trips its own constraint (review round 1, C-8).
   4. `PRAGMA foreign_key_check`. Any row raises and rolls everything back,
      the record included (`recovery.py:187` is the precedent).
6. Commit.
7. Move the journal files (D4, note 1900). This runs after the commit and
   still inside `control_gate`, so no restore can run beside it
   (`backup.py:974`). The store directory is the directory of the
   connection's `main` file, as in step 4. The quarantine directory is
   `<store directory>/runner-recovery-evidence/removed-<fingerprint>/`. For
   each removed run, in plan order:
   1. Skip the run when neither `runner-journal/<run>.json` nor
      `<run>.lock` exists. Otherwise take
      `runner_journal.lock(<store directory>/runner-ending/<run>.lock,
      blocking=False)` first, and hold it until this run's move is done
      (Copilot on #336, owner decision 2026-09-14). `Runner.finish` holds
      that same lock for the whole ending sequence (`runtime.py:630`), and
      inside it commits `store.release` and only then calls
      `journal.persist` (`runtime.py:671-672`). The per-run journal lock of
      sub-step 2 serialises against `persist`, not against the release
      commit. Without the ending lock an apply can see `released_at`, remove
      the rows, move the pair, and let that `persist` write `<run>.json`
      again for a run with no row, which is the blocked recovery entry this
      move exists to prevent. A held ending lock fails the move, like a
      held journal lock. Two holders are possible and the lock names
      neither: an ending sequence, which takes it blocking
      (`runtime.py:630`), and an archive refresh on a kept clone of that
      run, which takes the same lock non-blocking before `.archive.lock`
      (`archive_refresh.py:154`). `journal.lock` reports no owner, so the
      refusal names the path and both possible holders rather than
      diagnosing one. The recovery does not depend on which it is: the rows
      are already gone, the apply exits 4, and the printed `mv` lines move
      the pair once the holder has released.
   2. When `<run>.lock` exists, check it first, as
      `reconciliation.quarantine` checks its locks (`reconciliation.py:176-181`):
      a regular file, not a symlink, owned by the user, with one link.
      `lock` opens the path with `a+b` (`runner_journal.py:44`), which would
      follow a symlink and create its target. Then take
      `runner_journal.lock(<store directory>/runner-journal/<run>.lock,
      blocking=False)` (`runner_journal.py:42-52`). `persist` writes a
      journal only under that lock (`runner_journal.py:84`). A held lock
      fails the move. The apply does not take `runner.lock`: a live runner
      holds it for its whole life (`runtime.py:814`), and D7 keeps the
      runner running.
   3. When `<run>.json` exists, check it as `_journal_view` does
      (`reconciliation.py:44-47`): a regular file, not a symlink, owned by
      the user, with one link.
   4. Create `runner-recovery-evidence` with mode `0700` when it is absent.
      An existing one must be a directory owned by the user, not a symlink,
      with no group or other bits, as `reconciliation._private_directory`
      requires (`reconciliation.py:24-27`). Create the quarantine directory
      once, before the first rename, with `os.mkdir(path, 0o700)`. An
      existing `removed-<fingerprint>/` refuses, so nothing in it is
      overwritten.
   5. Before the first rename, write `receipt.json` in the quarantine
      directory: the record id, the fingerprint, and each planned file's
      source path and sha256. It lists the planned files, not the moved
      ones: a move that stops leaves some of them in `runner-journal/`
      (review round 4, C-59). Open it with `x`, set mode `0600`, flush and `fsync` it, as
      `quarantine` writes its receipt (`reconciliation.py:191-196`). Before
      each rename, refuse when the target name exists, because `os.rename`
      would replace it. Then `os.rename` `<run>.json`, then `<run>.lock`. The
      `.json` goes first because it is the file that makes an entry; a
      `.lock` name alone is skipped (`reconciliation.py:61-62`). All the
      directories are under the store directory, on one volume, so each
      rename is atomic. Then `fsync` the quarantine directory,
      `runner-recovery-evidence` and `runner-journal`
      (`runner_journal.fsync_directory`).

   Any failure stops the move: a held lock, a failed check, an `OSError`,
   any other `Exception`, or a `KeyboardInterrupt` (review round 3, C-48).
   A stop request never stops it (see Interrupts below). The rows stay
   removed and the record stays filed, because the move is outside the
   transaction. `apply` does not raise after the commit. It returns
   `{"record": <report item id>, "notes": <chunk count>,
   "removed": {table: count}, "backup": <snapshot directory>,
   "moved": [<path>, ...], "move_error": <reason or None>,
   "move_commands": [<line>, ...]}`. On a failure, `move_commands` holds
   one `mkdir -m 700 <directory>` line for each of the two directories that
   does not exist: `runner-recovery-evidence` first, then the quarantine
   directory, because `mkdir` without `-p` needs the parent (review round 4,
   C-58). Then one line for each file not yet moved, `.json` before `.lock`:
   `mv -n <source> <quarantine directory>/<name> && test ! -e <source> && test ! -L <source>`.
   `mv -n` never replaces an existing target, as step 7.5 refuses one and
   `local-sd-runner/README.md:164` says "existing evidence is never
   overwritten". `mv -n` can skip an existing target and still exit 0, so
   `test ! -e <source>` makes that line fail (C-62). `test -e` follows a
   symlink, so it passes for a dangling symlink that `mv -n` skipped, such
   as a `.lock` symlink that step 7.2 refused. `test ! -L <source>` makes
   that line fail too (review round 5, C-70). Every path goes through
   `shlex.quote`, as in D4. The CLI prints the reason and the lines, and
   exits 4 (section 6). The operator runs them once no process holds the
   run's lock. A line that fails replaces nothing; the operator compares the
   two files before anything else. The fingerprint does not cover the
   journal files: after I6 and P4 no runner writes a removed run's journal.

   **Interrupts (review round 4, C-57; round 5, C-63, C-65, C-66, C-68,
   C-69).** A Python signal handler runs when a C call returns. So a
   `KeyboardInterrupt` raised as `COMMIT` returns leaves the
   `with transaction(...)` block after the commit (`database.py:124-137`).
   An exception out of that block does not prove a rollback. The apply
   handles interrupts in three parts:

   - `removal.signal_stop()` is a context manager. On entry it installs
     handlers for SIGINT, SIGTERM and SIGHUP (C-66), and it yields a `stop`
     callable. Each handler only stores `True` in a plain flag, a
     one-element list, with no lock (C-65). It never raises. A second signal
     only stores `True` again. A `threading.Event` is not used: `Event.set`
     takes a non-reentrant lock, and a second signal handled inside that
     lock blocks forever (the round 5 probe had to end the process with
     SIGKILL). On exit, in `finally`, it restores the old handlers, so every
     path restores them: exit 0, 3, 4 or 1, and an uncaught exception
     (C-69). During `--apply`, the CLI wraps `apply` and the printing of its
     output in `with removal.signal_stop() as stop:` and passes
     `stop=stop`. `signal.signal` works only in the main thread, and the CLI
     runs there.
   - `apply` calls `stop()` at three points, all before the commit: before
     `control_gate`, after `backup.run` returns, and inside the transaction
     after `foreign_key_check`. A true `stop()` raises `RemovalInterrupted`,
     an `SdDbError`, "interrupted before the commit; nothing was removed".
     Inside the transaction the raise rolls it back. After the commit,
     `apply` does not call `stop()`. It finishes the move: a non-blocking
     lock and renames on one volume, so it is short. Only a real failure of
     step 7 gives `move_commands` (C-66).
   - For a caller without `signal_stop`, `apply` catches only
     `KeyboardInterrupt` around the transaction. Every refusal and every
     other exception propagates (C-63). `apply` keeps the record id that
     this call's `ingest` returned, in a local variable that stays `None`
     until `ingest` returns. It treats the transaction as committed only
     when that id is set, `connection.in_transaction` is false, and a row
     with that id and the `external_id` `<kind>-remove:<fingerprint>`
     exists on the same connection. Then it returns `move_error`
     "interrupted" and `move_commands` for every file. Otherwise it
     re-raises. A row with that `external_id` alone does not prove that this
     apply committed: the record of an earlier remove of the same rows has
     it too, after a put-back (the round 5 probe). G6 refuses that case in
     the plan (section 3), and the bound id keeps the handler right even
     when G6 is bypassed.

   What a signal under the CLI gives (C-68):

   - A signal seen at a check before the commit: exit 1, and no row is
     removed. `backup.run` is never cut off, because `stop()` is called only
     after it returns, so its snapshot is complete and stays.
   - A signal after the last check, after `foreign_key_check`: the apply
     commits and finishes the move. It exits 0, or 4 when the move fails
     for its own reason. A signal after the move has finished also exits 0.
   - After a SIGHUP the output may reach nobody. The move has finished, and
     the record is the account.

   In a caller without `signal_stop`, a `KeyboardInterrupt` after the commit
   stops the move and returns `move_commands`, which the CLI turns into
   exit 4.

   A SIGKILL or a power loss after the commit prints nothing. Then the
   record's `move after commit:` list is the recovery source. A SIGHUP is
   not such a case, because `signal_stop` handles it.
   `sd_runner recovery-plan` shows a blocked entry for each unmoved run. The
   operator creates `runner-recovery-evidence` when it is absent, then the
   `to` directory, each with `mkdir -m 700`. Then, for each listed file that
   still exists in `runner-journal/`, `.json` first, the operator runs
   `mv -n <source> <to>/<name> && test ! -e <source> && test ! -L <source>`
   (C-58, C-62, C-70). The manifest lines are data, not shell, so the
   operator quotes each path. The README of step 6 gives these commands.
   The apply cannot run again for the same target, because its rows are
   gone (I1, P1). After a put-back, G6 refuses it (section 3, C-64).

### 4.1 The record

**The report item.** It is filed by `reporting.ingest` (`reporting.py:43`),
the same function that sd:755 D3 uses:

```
ingest(connection,
       job="item-remove" or "repo-remove",
       run_id=<fingerprint, 64 hex characters>,
       started=<apply start>, ended=<time of the ingest>,
       exit_code=0,
       text=<manifest>,
       source_path=<program: "sd-db.sh item remove" or "sd-db.sh repo remove">,
       attention=False,
       removed={<table>: <count>, ...},
       actor={"who", "principal", "program", "pid", "ppid", "session", "reason"},
       record="item-remove" or "repo-remove")
```

What `ingest` makes of that:

- `kind` is `report`, `source` is `cron-report`, and `repo` is NULL.
  `ingest` opens it as `planning`, and the apply moves it to `done` in the same
  transaction (section 4 step 5.2). The status note reads
  `planning -> done by <who>: removal record`. The record must not name the repo: `item.repo` is RESTRICT,
  so a record on the repo would block the repo's own delete, and it must
  outlive the repo.
- `external_id` is `item-remove:<fingerprint>` or `repo-remove:<fingerprint>`.
- The title is `ingest`'s fixed form, `f"{job}: run report"`
  (`reporting.py:97`): `item-remove: run report` or `repo-remove: run report`.
  This design does not add a title keyword, because sd:755 does not. The
  manifest's first line names the target.
- `fields.report.removed` holds the counts by table name. `ingest` already
  accepts this keyword (`reporting.py:63-66`), and retention's prune uses it.
- `fields.report.actor` holds the actor. sd:755 added the `actor` keyword
  (system #347, merged as `e377da74`; `reporting.py:67-72`, `:86-87`): a dict of at most 10 `str` keys, each value a `str` of at most 200
  characters, an `int`, or `None`. This verb uses sd:755's six keys (`who`,
  `principal`, `program`, `pid`, `ppid`, `session`) and adds `reason`.
  `principal` is `getpass.getuser()`, recorded beside `who`, never instead of
  it. `session` is `SD_SESSION` or `None`. `apply` derives `pid` and `ppid`
  itself, as `os.getpid()` and `os.getppid()`, because it runs in the
  command's own process; they are not parameters, so no caller can give them
  another value (Copilot on #336, owner decision 2026-09-14).
- `fields.report.text_sha256` is the hash of the manifest (`reporting.py:81`).
- `attention` is false, so `ingest` adds no followup, and the report never
  shows as needs-you. With status `done` as well, nothing asks a person to
  act on it.
- `fields.record` is `"item-remove"` or `"repo-remove"`. It sits beside
  `fields.attention` and `fields.report`, not inside `report`. `ingest` builds
  `fields` itself (`reporting.py:88-90`), and it has the optional keyword
  `record=None`. When given, it must match the job-name pattern
  (`reporting.py:31`, `:73-74`), and it is stored as `fields["record"]`.
  Without it, `fields` stays byte-identical, so replays of existing reports
  still match. sd:755 added the keyword for `"reports-acknowledge"` in
  #347. Each item adds it only if it is absent, so this item adds nothing
  to `ingest` when PR 1 opens on that main (section 9).

**The manifest (`body.text`).** Plain text, one fact per line, in this order:

```
remove item 72
reason: runner provisioning probe of 2026-09-09 is finished
who: operator
fingerprint: <64 hex>
backup: ~/Documents/sd-backups/<snapshot directory>
rows: 15 in 1 note(s)
removed:
repo ~/.local/share/sd/provisioning-probe-20260909/source
item 72
note 135
...
assignment 2
runner_run 35f34207295e4c6faa502397fda67e22
runner_lease 35f34207295e4c6faa502397fda67e22
move after commit:
file ~/.local/share/sd/runner-journal/35f34207295e4c6faa502397fda67e22.json
file ~/.local/share/sd/runner-journal/35f34207295e4c6faa502397fda67e22.lock
to ~/.local/share/sd/runner-recovery-evidence/removed-<64 hex>
left:
file /Volumes/sd-work/retained/2/1/retention.json
reference assignment.scope item 72
```

- The first line is `remove item <id>` or `remove repo <path>`, with
  `--with-items` added when it was given.
- Under `removed:`, each line is `<table> <key>`. The key is `id` for `item`,
  `note` and `assignment`, `run` for `runner_lease`, and `path` or `repo` for
  `repo` and `repo_protection`. Lines are in insert order: parents first.
- Under `move after commit:`, each line is `file <path>` for a journal
  `.json` or `.lock` of a removed run that exists at plan time. The last
  line is `to <quarantine directory>`. The section is absent when there is
  no such file. The record states what the apply will move, and the
  quarantine `receipt.json` lists the same planned files with their sha256.
  What moved is in the command's output (`moved` and `move_commands`) or in
  a listing of the quarantine directory (section 4, step 7; review round 4,
  C-59).
- Under `left:`, each line is `file <path>` or `reference <where>`, from
  section 1's second table.

**The chunk notes.** One or more `comment` notes on the report item, written
with `add_note(..., session=who)`. Each body is:

```
removed rows 1 of 2, sha256 <hex of the row lines that follow>
{"row":{...},"table":"repo"}
{"row":{...},"table":"item"}
```

- One row per line, `json.dumps(..., sort_keys=True, separators=(",", ":"),
  ensure_ascii=False)`. JSON escapes newlines inside strings, so a row is
  always exactly one line.
- `row` holds every column of the row, by column name, with the stored value.
  `fields`, `body`, `scope` and `result` stay as the stored JSON text, not
  parsed, so the value is byte-for-byte the stored one.
- Rows are in manifest order, and the `n of N` numbers give the note order.
- The sha256 lets a reader prove a chunk is whole before it rebuilds rows.

### 4.2 Size limits

- One chunk note is at most `reporting.MAX_REPORT` bytes (200,000), header
  line included. The same limit already bounds a report's text, and item 679
  shows that the dashboard and `sd task show` already carry a body that
  large.
- Rows are split only between lines. A single row line that cannot fit in one
  note is refused (R1). Today that is item 679 alone (207,797 bytes). That
  item has `repo` NULL, so no `repo remove` meets it. An `item remove 679`
  is refused, and its preview says why. Splitting one row across notes was
  rejected: a reader would have to join lines before parsing, and one
  missing note would break a row silently.
- A plan that needs more than `MAX_RECORD_NOTES = 16` notes (3.2 MB) is
  refused (R2). The largest real candidate needs 4 (section 2), so 16 is four
  times the worst case in the store today. Like sd:755's `MAX_BATCH`, this is
  a guard against a mistake, not a measured limit. The operator can split the
  work: remove the largest items first, one `item remove` each.
- The manifest must fit in `MAX_REPORT` (R3), or `ingest` would refuse it.
  A manifest line is about 12 to 110 bytes, so 579 rows give about 10 KB.
  R2 refuses a plan long before R3 can.
- The plan computes the chunks, so the preview prints the note count, and
  R1-R3 refuse in the preview, not only in the apply.
- The fingerprint covers the rows. The chunks are derived from them, so
  equal rows give equal chunks.

### 4.3 How a reader finds a removed row

- **By the item or repo it was.** The manifest has one line per row. SQL:

  ```sql
  select id from item
  where kind = 'report'
    and CASE WHEN json_valid(fields) THEN json_extract(fields, '$.record') END
        in ('item-remove', 'repo-remove')
    and instr(char(10) || CASE WHEN json_valid(body) THEN json_extract(body, '$.text') END || char(10),
              char(10) || 'item 72' || char(10)) > 0;
  ```

  The library gives the same answer as `removal.records(connection, table,
  key) -> list[int]`. It is a read, and it has no CLI verb (D8).
- **In the list of reports.** `sd reports list` and the dashboard's reports
  screen show the record as `item-remove: run report`, because it is a
  `report` item.
- **To rebuild a row.** Read the chunk notes of that report in `n of N`
  order. Check each sha256, and parse each line. The snapshot on the
  `backup:` line is a second source, but only through a scratch home:
  `restore(directory, home=<scratch home>)` (`backup.py:912`) into a
  temporary home, then read the row from that copy. Never restore the
  snapshot over the live store to get one row back. `restore` replaces the
  whole store, so every write since the snapshot is lost, the record
  included, and later item ids would be issued again (review round 1, C-5).
- **Who and why.** `fields.report.actor.who`, `.reason` and `.principal`, and
  the `reason:` and `who:` lines of the manifest.
- **To put back a row removed by mistake.** This item adds no re-insert
  verb; it is an accepted risk, and a re-insert verb is later work
  (review round 2, C-30). The manual path, written in the README section of
  `implement.md` step 6:
  1. Stop and read the record: every chunk note, each sha256 checked.
  2. Open the store with `connect()`, so `foreign_keys` is on, and take
     `control_gate` and one `BEGIN IMMEDIATE` transaction.
  3. Insert parent-first, the reverse of the delete order: `repo`,
     `repo_protection`, `item`, `note`, `assignment` (one statement for all,
     so an `after` or `parent` chain inserts together), `runner_run`,
     `runner_lease`. Use the recorded column values, ids included.
  4. Run `PRAGMA foreign_key_check`; commit only when it returns 0 rows.
  5. Still inside `control_gate`, move each put-back run's journal files
     back (review round 3, C-45). List
     `runner-recovery-evidence/removed-<fingerprint>/` and take the run's
     files from that listing, because `receipt.json` names the planned
     files, not the moved ones (review round 4, C-59). Check each listed
     file's name and sha256 against `receipt.json`. A file that the receipt
     names but that is still in `runner-journal/` (a move that stopped) is
     checked there and left in place. When the quarantine directory does
     not exist (the move stopped before step 7.4, and its lines were never
     run), there is no `receipt.json`: both files are still in
     `runner-journal/`, and nothing is moved. Refuse when the target in
     `runner-journal/` already exists. Then move `<quarantine>/<run>.json`
     to `<store directory>/runner-journal/<run>.json`, then the same for
     `<run>.lock`. Use `os.rename` in the same session, so `control_gate` is
     still held. The files go back after the commit, not before. Before the
     commit, a runner tick would see a journal record with no row. If the
     transaction then rolled back, that record would stay as a blocked
     entry, "assignment is absent", and hold archive refresh. After the
     commit, the short gap is a released row with no journal. It makes no
     recovery entry and no hold. `plan` takes its runs from journal records
     and active runs only (`reconciliation.py:99-101`), `active_runs`
     selects unreleased runs (`sd_db/runner.py:147-151`), and every removed
     run is released (I6, P4). `restore_holds` checks active runs only
     (`runtime.py:234-236`). The gap does fail every backup with "runner row
     <id> differs from the backup journal" (`backup.py:326`), until both
     files are back. A put-back that skips this step leaves every backup
     failing (review round 4, C-55, which corrects the round 3 text).
  6. Add a `comment` note on the record naming who put the rows back and
     why. The record stays; I9 keeps it. While the record stays, a remove of
     the same rows is refused with G6 (section 3, review round 5, C-64).
  An id taken since the remove makes the insert fail on its primary key,
  and the transaction rolls back. A1 is why an assignment id is never taken
  in between.

### 4.4 Backup, restore and retention

**Backup.** `backup.run` takes `_counts` and then runs `VACUUM INTO` without a
read transaction around both. So a remove that commits in that gap fails that
night's count comparison, and so does any other writer. This risk is accepted:
the race exists today for every writer, and a later run can pass. The
apply's own backup has the same race with a live runner or dashboard write:
`backup.run` then raises and the apply refuses with G5 before its
transaction, so no removal write happened, and the same apply can run again
(review round 2, C-25). A snapshot
taken after a remove does not contain the removed rows. It does contain the
record.

**Restore.** `restore` replaces the whole store with the snapshot. A full
restore is a disaster-recovery act for the whole store, not a way to undo one
remove: it loses every later write, the record included. To look at removed
rows, restore into a scratch home only (section 4.3). The runner journal stays
consistent. The apply's snapshot is taken before its transaction, so it holds
the removed rows and their journal files (`backup.py:296-311`). A full
restore of it brings both back: `_restore_runner_journal` links each missing
journal name (`backup.py:387-408`). A snapshot taken after the move holds
neither. `_check_runner_records` compares rows against files
(`backup.py:315-326`), and a file without a row is not checked.
Restore links each missing journal name from its restore material
(`backup.py:403`), and that material stays after the restore
(`backup.py:407`). So a restored journal file has two links, and
`_journal_view` reports it as a blocked journal issue, "unsafe, unowned,
linked or oversized journal entry" (`reconciliation.py:46-48`). That is how
every full restore of a missing journal behaves today, not only after a
remove (see Out-of-scope findings). While any issue exists, `plan` returns
no entries for any run (`reconciliation.py:101`). So one linked file hides
every run's entry, and archive refresh is not held for any run while it
stands, a run with a real hold included (review round 4, C-56). Dispatch
holds still work: `restore_holds` checks paths with `validate_path`, which
does not check links (`runner_journal.py:112-123`). This verb raises the
exposure. Before it, a live journal name was never removed, so a full
restore rarely found one missing. After a remove, every full restore of a
snapshot taken before it, the apply's own or any earlier nightly, links
each removed run's `.json` back: one linked issue per removed run. The fix
changes restore or the runner, which D7 and note 1900 keep outside this
verb. It is followup sd:779 (see Out-of-scope findings).
`recovery-reconcile` refuses every run (`reconciliation.py:141-142`), and
`recovery-quarantine` refuses a blocked issue (`reconciliation.py:185`).
Step 7.3 of section 4 also refuses such a file, so removing a run whose
journal was restored this way exits 4. Its printed `mv -n` line takes the link out of
`runner-journal/` and clears the issue. The
publication and execution checks behave the same way. An execution log without
a note is copied, and a note without a log only matters for notes that still
exist.

**Retention.**

- The prune never removes `item` or `repo` rows, and it never removes `exec`
  notes (`retention.py:1-50`). The remove is the one path by which an `exec`
  note "leaves with its item". `expire_exec_outputs` selects from notes
  (`retention.py:158`), so a log whose note was removed never expires. The
  manifest lists those logs under `left:`. Removing them is separate future
  work, not this verb (requirement 8).
- The record is already `done` when the apply commits. `settle_clean_reports`
  selects only `planning` reports through `reporting.clean_candidates`
  (`retention.py:216`, `reporting.py:277-283`), so it never touches
  the record. sd:755's `--all-clean` selects only `planning` reports too, and
  it also declines any report with a `fields.record` marker. The record keeps
  its status and its revision.
- The chunk notes are `comment` notes. Retention does not touch `comment`
  notes.

**Runner recovery plan and archive refresh (D4, note 1900).**
`reconciliation.plan` builds entries from the `.json` records in
`runner-journal/` and from the active runs (`reconciliation.py:95-104`). It
lists only that directory (`reconciliation.py:36-43`,
`runner_journal.py:21-22`), and it skips `<run>.lock` names
(`reconciliation.py:61-62`). `_entry` turns a journal record with no row
into a blocked entry, "assignment is absent"
(`reconciliation.py:86`). While any entry exists,
`archive_refresh._safe_state` raises "archive refresh held by unresolved
restore or ownership evidence" (`archive_refresh.py:119-122`), and
`refresh` calls it first (`archive_refresh.py:212`). So a left journal file
would hold archive refresh for every run, not only for the removed one.

- After step 7, a removed run has no record in `runner-journal/`, and it is
  not active, so `plan` has no entry for it and `_safe_state` does not hold
  refresh because of it.
- Between the commit and the move, a runner tick can see the blocked entry.
  That holds one refresh attempt. The runner starts the next attempt 60
  seconds later (`runtime.py:828-830`).
- If the move fails, the entry stays and refresh stays held until the
  operator runs the printed commands. That is why the CLI exits 4, not 0.
- A rename can also land while a runner read lists `runner-journal/`
  (review round 3, C-50). `restore_holds` then raises "unsafe run journal
  entry" from `validate_path`, and `_tick` treats it as a hold for that
  tick: it writes `healthy` false and dispatches nothing. At daemon start
  the same raise comes through `serve` → `recover` → `restore_holds`, and
  `serve` exits 1. launchd restarts it after 30 seconds (`ThrottleInterval`
  in the plist that `runner.sh install-plan` describes), so that start
  fails once.
  Reconciliation now skips an entry that vanishes during its read:
  `_journal_view` in `local-sd-runner/sd_runner/reconciliation.py` catches
  only `FileNotFoundError` around its `lstat` and `open` calls. It skips a
  `.json` that `journal.read` refused only when the name no longer exists.
  So `reconciliation.plan`, `archive_refresh.refresh` and a hand-run
  `runner.sh recovery-plan` no longer fail or hold on a vanished entry.
  Skipping hides nothing: `plan` still names a run whose journal is gone as
  a `journal-from-database` entry, and `restore_holds` still reports it. An
  entry swapped for a symlink is still blocked. A hand-run `recovery-plan`
  never printed a traceback here: the CLI catches `OSError` and `SdDbError`,
  so any failure exits 1 with a one-line `runner:` error. The runner side
  stays unguarded: a guard there belongs in `validate_path` in `sd_db`, or
  needs the runner to take the per-run lock while it lists the directory,
  which changes the system runner. Note 1900 rejected option (b) for that
  same reason.

**The quarantine directory.** It is
`<store directory>/runner-recovery-evidence/removed-<fingerprint>/`, with
mode `0700`, holding `<run>.json`, `<run>.lock` and `receipt.json`. Nothing
treats it as a journal:

- `reconciliation.plan` and `restore_holds` list only `runner-journal/`
  (`reconciliation.py:38`, `runner_journal.py:107-109`, `runtime.py:233`).
- `backup` copies `runner-recovery-evidence` as diagnostic evidence, "without
  treating them as active journals" (`backup.py:412-441`).
- A restore intent accepts only
  `runner-recovery-evidence/restore-*/runner-journal` (`backup.py:350-360`).
  The `removed-` prefix never matches it.
- `reconciliation.quarantine` names its directories `quarantine-*`
  (`reconciliation.py:190`), so the names do not collide.

**The other runner files of a removed run (review round 1, C-3).** None is
moved and none is printed as a command. Each is left and listed under
`left:`. None makes a recovery entry or holds archive refresh, because
`plan` does not read them (above), and every other reader needs a
`runner_run` row or a clone:

- `<retention root>/<assignment>/<run>/retention.json` is left. It is an age
  receipt, read only beside a clone that exists. `storage.prune_inventory`
  globs `*/*/clone` (`storage.py:277-278`). `maintenance.plan_prune` starts
  from `runner_run` rows and skips a missing clone (`maintenance.py:62-65`,
  `:68`). P4 refuses the remove while the clone exists, so after the apply
  no reader reaches the receipt. Deleting it is outside this verb
  (requirement 8).
- `<retention root>/<assignment>/<run>/kept.tar` and
  `archives/<generation>/kept.tar` with its `manifest.json` are left. They can
  be the last copy of the run's work, so the verb never deletes them
  (requirement 8). Every reader takes a run row. `archive_refresh.refresh`
  visits `store.active_runs` (`archive_refresh.py:219`), which selects
  `released_at IS NULL` (`runner.py:147-151`), and it derives the archive
  root from the row's `retained_path` (`archive_refresh.py:44`, `:65`).
  `maintenance.plan_prune` (`maintenance.py:81`) and `restoration._restore`
  (`restoration.py:101-103`) read `kept.tar` through a run row too. A1 keeps
  the `<assignment>/<run>` directory from being reused by a new run.
- `runner-reconciliation/<fingerprint>.json` is left. `_receipt` writes it
  and reads it back only to compare a receipt of the same fingerprint
  (`reconciliation.py:120-130`). `plan` never reads that directory. It is
  the evidence of a past reconciliation.
- `runner-ending/<run>.lock` is left (section 1). Three places in shipped
  code name it, and two of them take it: `Runner.finish` holds it, blocking,
  for a whole ending sequence (`runtime.py:630`), and an archive refresh
  takes it non-blocking for one kept clone, before `.archive.lock`
  (`archive_refresh.py:154`). The third names the path as a left file and
  takes nothing (`removal.py:339`). Step 7.1 makes this verb the third
  taker, non-blocking, for the moment it moves a run's journal pair, because
  the release commit and `journal.persist` sit inside the ending run's hold
  (`runtime.py:671-672`). The file itself outlives every holder.
- `<retention root>/<assignment>/<run>/.archive.lock` is left. It is an
  empty lock file, taken only for a run row (`maintenance.py:67`,
  `archive_refresh.py:167`). The printed `rm -rf` of D4 removes only the
  clone, so it stays in the run directory.
- `.sd-restore-<token>.lock` is left. A clone restore creates it in the
  parent of its destination (`restoration.py:95-97`), and the operator
  chooses the destination (`sd_runner/controls.py:62-73`). So it is in the
  run directory only when a restore targeted a path inside that directory.
  The plan lists one found there. One in another directory is outside the
  plan's reach (review round 4, C-61).
- A later `item remove` of the record, or of an sd:755 batch record, is
  refused (I9).

## 5. Live services (D7)

- **Integrity.** Every production connection comes from `connect()`, which
  sets `foreign_keys = ON` (`database.py:65`). A runner or dashboard write that
  references a removed row fails with `FOREIGN KEY constraint failed`. It
  cannot create an orphan.
- **Races.** The plan's guard read, the record and the deletes run inside
  `BEGIN IMMEDIATE` (`database.py:126`), so no writer can land between them.
  Other writers wait up to `busy_timeout = 5000` (`database.py:68`). The
  record adds at most 16 notes of 200 KB, so the write lock is still short.
  The lock time was not measured.
- **Controls and restore.** `operations.control_gate` (`operations.py:34`) is
  the lock that `restore` holds while it installs. The remove takes the gate
  before its backup and its transaction, so it cannot run inside a restore or
  a service control.
- **Runner.** Unreleased runs and open leases are refused (I6, P4). At startup,
  `restore_holds` treats a released journal record with no row as nothing to
  hold (`runtime.py:239-247`). After step 7 of section 4, a removed run has
  no journal record in `runner-journal/` at all.
- **Dashboard.** Every POST carries `expected_revision`. `item_state` raises
  `MissingItem` for a removed item (`workflow.MissingItem`), so an open page gets
  an error, not a write.
- **Not verified.** Nobody has run a remove against live services. The claim
  that the runner and dashboard hold no in-memory copy of `repo` or `item`
  rows comes from a grep, not from a run.

## 6. Surface and dry run (D8, D9)

```
sd-db.sh item remove ID --who NAME --reason TEXT [--apply --if-fingerprint HEX]
sd-db.sh repo remove PATH [--with-items] --who NAME --reason TEXT [--apply --if-fingerprint HEX]
```

- `repo remove` joins `repo add|seed|list` in `command_repo`
  (`jobs/cli.py:158`). `item remove` is a new `item` entry in `COMMANDS`
  (`jobs/cli.py:484`).
- `--who` and `--reason` are required in the preview too, because the
  fingerprint does not cover them but the preview prints the exact apply
  command. A blank value is refused (G4). Nothing falls back to
  `getpass.getuser()` for `who`.
- Without `--apply`, the command prints the plan and changes nothing. The plan
  lists the rows by table and key, the refusals, the journal files it moves
  after the commit, the references it leaves, the warning for each row a
  writer can key again (section 3, the D6 paragraph), the number of record
  notes, and the fingerprint. Its last line is the exact apply command.
- Exit codes: 0 for a plan with no refusal or a complete apply, 3 for a plan
  with refusals (the backup's "finding, not failure" exit), 4 for an apply
  that committed but did not finish the journal move (it prints the reason
  and the `mkdir` and `mv` commands, section 4 step 7), 1 for an error. An
  interrupt seen before the commit is an error: it exits 1 with
  "interrupted before the commit; nothing was removed" (review round 4,
  C-57). A signal after the last check exits 0, or 4 when the move fails for
  its own reason (section 4 step 7, review round 5, C-66, C-68).
- With `--apply`, the fingerprint is required, as `recovery.reimport` requires
  one (`recovery.py:158-163`). The same pattern appears in the runner's
  `plan_discard` (`maintenance.py:93-106`).

Library, in a new module `sd_db/removal.py`:

- `check_actor(*, who, reason, session, principal, program) -> None`
- `plan_item(connection, item, *, home) -> dict`
- `plan_repo(connection, path, *, with_items, home) -> dict`
- `apply(connection, plan_kind, target, *, fingerprint, who, reason, principal, program, home, with_items=False, session=None, stop=None) -> dict`
- `records(connection, table, key) -> list[int]`
- `signal_stop()`, a context manager that yields `stop`

`home` is needed for the retained-clone and conf checks and for the backup.
`who`, `reason`, `principal` and `program` are keyword-only with no default.
`pid` and `ppid` are not parameters: `apply` derives them (section 4.1).
`check_actor` raises G4. The CLI calls it before the preview, and `apply`
calls it first, so a library caller cannot skip it. The plans take no actor
values (review round 4, C-61). `stop` is a callable that returns true once
`signal_stop` has seen SIGINT, SIGTERM or SIGHUP. `apply` calls it only
before the commit (section 4, step 7).
The class test `NoVerbNamesItsOperatorForTheCaller` already covers every
`sd_db` function that takes `who`.

No dashboard control and no pack verb are added (D8).

## 7. sd:744's six rows, through the verb

The rows come from `~/Documents/sd-backups/2026-09-10.3/sd.db`.

**Starting state, 2026-09-10:**

- repo `~/.local/share/sd/provisioning-probe-20260909/source`:
  `status_source` `file`. It is not in `repos.personal.conf`.
- item 72: a `task`, status `done`, `source` NULL, `fields` `{}`. It has 7
  notes: 135 `status_change`, 136 `decision`, 137 `exec`, 138 `decision`,
  139 `exec`, 140 `decision`, 537 `status_change`. None is an open
  `followup` or `question`.
- assignments 2 and 3: `exec`, `done`. No `cost` rows, and no `after` or
  `parent` children.
- runs `35f34207...` (assignment 2) and `32da1ad4...` (assignment 3): both
  released on 2026-09-09. Leases on both are released.
- retained clones `/Volumes/sd-work/retained/2/1/clone` and `.../3/1/clone`:
  present on 2026-09-10, with `retention.json` `retained_at` 2026-09-09.
  They are gone now: `ls /Volumes/sd-work/retained/` at 2026-09-14T03:03Z
  lists only 4-9, and neither probe journal file is in `runner-journal/`.
  The owner removed them by hand on 2026-09-13 at about 20:37 local.
- `publication_claim`: none. `repo_protection`: the table did not exist yet.

**The preview on 2026-09-10:**

```
sd-db.sh repo remove ~/.local/share/sd/provisioning-probe-20260909/source \
    --with-items --who operator --reason "runner provisioning probe of 2026-09-09 is finished"
```

The plan would remove repo 1, item 1, notes 7, assignments 2, runner_run 2 and
runner_lease 2: 15 rows, in 1 record note. It would exit 3 with two P4
refusals, one for each retained clone, and one A1 refusal: the snapshot's
assignments are 1, 2 and 3, so removing 2 and 3 leaves `max(id)` 1, and the
next assignment would take id 2.

**What happens next (D4, option a).** Each P4 refusal prints its clone path
and the commands `chflags -R nouchg /Volumes/sd-work/retained/2/1/clone` and
`rm -rf /Volumes/sd-work/retained/2/1/clone` (and the same for `3/1`). The
operator runs them and runs the preview again. The P4 refusals then clear.
Waiting does not clear them, because `maintenance.plan_prune` only plans. The
A1 refusal clears once newer work creates assignment 4, which happened on
2026-09-11. The owner's hand removal at about 20:37 on 2026-09-13 took the
whole run directories and the journal files. The printed commands take only
the clone, and the rest of each run directory, such as `retention.json`,
stays under `left:`. The apply itself moves the journal files (note 1900).

With both refusals cleared, the apply would:

1. take a backup;
2. file report `repo-remove: run report`, with a manifest of the 15 row keys,
   a `move after commit:` list (the two journal files, and their `.lock`
   files if present), and a `left:` list (each run's `retention.json`, the
   two execution logs `345a26ef....log` and `691b7f28....log` with their
   receipts, and the probe checkout directory
   `~/.local/share/sd/provisioning-probe-20260909/`);
3. add one chunk note, `removed rows 1 of 1`, with the 15 rows, and move
   the report to `done`, with `fields.record` `"repo-remove"` and
   `attention` false;
4. remove the 15 rows children-first;
5. find `foreign_key_check` empty, and commit;
6. move both journal files into
   `~/.local/share/sd/runner-recovery-evidence/removed-<fingerprint>/`.
   `sd_runner recovery-plan` then has no entry for either run, and the
   command exits 0.

**What it would not have done.** It would not have repaired the 2026-09-13
state. The orphans' parents were already gone, so the verb has no row to
start from, and G1 refuses on a broken store. Repairing orphans is a separate
operation. sd:744's backup change (PR #316) now detects violations the night
they appear.

## 8. Rejected alternatives

- **A new `state` kind `retired`, with migration 010.** It was the draft's
  recommendation for Q2. The owner chose the report item (D2), which needs no
  migration and matches sd:755.
- **One report item per chunk.** It breaks "one report item" (D2), and a
  reader would have to find the siblings.
- **All rows in `body.text`.** `ingest` bounds the text at 200,000 bytes, and
  the largest real candidate is 778,891 bytes. Raising `MAX_REPORT` changes
  every cron report.
- **Rely on `ON DELETE CASCADE` alone.** Only `note` and `assignment` cascade.
  Every runner table and `publication_claim` would still refuse, and a cascade
  removes rows the preview never listed.
- **Add CASCADE to the runner foreign keys.** That needs a migration that
  rebuilds `runner_run` and `runner_lease`. It would also make removing a repo
  silently remove run history, and that history is what `restore_holds` reads.
- **A generic `sd-db.sh sql` escape hatch that runs through `connect()`.** It
  would turn foreign keys on, but it would still be raw SQL with no refusals,
  no preview and no record. This item exists to remove that pattern.

## 9. Order of landing, and sd:755

- This item's PR 1 (`implement.md` steps 1-3) is system #350, merged on main
  as `c7809f63`: new `local-sd-db/sd_db/removal.py`,
  `local-sd-db/tests/test_removal.py` and
  `local-sd-db/tests/test_removal_ingest.py`, and no change to any existing
  module. It settled two things this design had left open: I11 also refuses a
  stored value the record cannot hold (section 3), and the retained-path
  check keeps both the symlink clause and the resolve clause (D4). PRs 2 and
  3 are unchanged.
- sd:755 is `in_progress` (decision note 1906). Its plan head is `9bdf89c2`
  on #334. Its implementation PR 1 is system #347, merged on main as
  `e377da74`.
- sd:755's #347 added the `actor` and `record` keywords to `ingest`
  (`reporting.py:43-45`, `:67-74`), with tests in
  `local-sd-db/tests/test_report_bulk_acknowledge.py`, class
  `IngestTakesAnActorAndARecord` (`:417-449`). This item needs both keywords, with the same
  validation. The rule for both plans: add `actor` and `record` to
  `reporting.ingest` only if absent, with sd:755's validation and tests. The
  second item uses them and does not change them. `implement.md` step 1
  checks which case applies (sd:755 round 2, C-18).
- Rollback. Reverting this item's pull requests never removes a keyword that
  sd:755 already uses. If sd:755 is on `main` and calls `ingest(actor=...,
  record=...)`, the revert of PR 1 keeps both keywords and their validation
  and removes only this item's code and tests (`implement.md`, Rollback).
- Everything else is new: `sd_db/removal.py`, the CLI entries, the `item`
  word in the `sd-db.sh` dispatch arm (`sd-db.sh:211`) and the tests. No
  existing signature changes, and there is no migration.
- Two READMEs change with PR 3 (review round 3, C-47).
  `local-sd-runner/README.md:153`, `:160`, `:162` and `:163` say that
  missing-assignment journals stay unchanged, that quarantine needs the
  daemon lock, that healthy entries cannot be quarantined, and that
  `quarantine-` directories are the only evidence. The remove verb moves a
  healthy journal pair under the per-run lock and `control_gate`, without
  the daemon lock (section 4, step 7). `local-sd-db/README.md:548-557`
  describes `runner-journal/` and the diagnostic archives.
- No pack change is needed (D8), so there is no pin move.

## Risks

- **An incomplete refusal list.** Section 1 enumerates the foreign keys from
  the schema. The references without a foreign key are measured, not
  exhaustive: note bodies were not searched. A test built from
  `pragma_foreign_key_list` fails when a future migration adds a foreign key
  to `repo` or `item` that the plan does not handle (`implement.md` step 2).
- **sd:755 and this item change `ingest` at the same time.** If both add
  `actor` separately, the second PR conflicts. Section 9 handles it.
- **A record report is a `report` item, so another verb could still select
  it.** Three guards cover it: status `done` at filing, the `fields.record`
  marker that sd:755 declines, and I9. A future bulk verb over reports must
  check the marker too.
- **Accepted:** the nightly backup's count race (section 4.4), and logs that
  never expire after their note is removed.
- **Accepted:** a large `--with-items` record is up to 16 notes of 200 KB on
  one report item. The dashboard item page shows them all.
- **A failed journal move (D4, note 1900).** The rows are committed, and the
  journal file stays in `runner-journal/`. Archive refresh stays held for
  every run until the operator runs the printed `mkdir` and `mv` commands.
  The CLI exits 4 and prints the reason, so the failure is not silent. A
  runner that skips a record with no row was option (b), rejected in note
  1900 because it changes the system runner.
- **Accepted:** a backup that copies `runner-journal/` between the commit and
  the move keeps the journal files without their rows. A full restore of
  that snapshot links them back with two links each (`backup.py:403`,
  `:407`). They show as blocked journal issues, not entries
  (`reconciliation.py:46-48`, `:101`). Archive refresh is not held for any run while
  they stand (C-56), and `recovery-reconcile` refuses every run (`reconciliation.py:141-142`) and
  `recovery-quarantine` refuses them (`reconciliation.py:185`). The `mv -n`
  line of step 7 for each listed file clears them. When the `to` directory
  already holds the name, because the move finished before the restore, the
  line fails and replaces nothing (C-62). The file in `runner-journal/` is
  then a second link to the restore material, which stays (`backup.py:407`).
  The operator compares the sha256 of both files, and when they match
  removes that one name from `runner-journal/`. A backup that runs during the move can also fail once, with "runner
  journal is not a verified recovery source" (`backup.py:294`), "runner
  journal was not copied" (`backup.py:310`) or "recovery archive changed
  during backup" (`backup.py:433`). The next backup passes.
- **Fixed by followup sd:779: a linked journal issue now withholds only its
  own run's entry, a restore installs one-link journal files, and a link an
  earlier restore left has a supported cleanup (C-56).** A full restore of a
  snapshot taken before a remove puts each removed run's journal name back
  with one link. It links an fsynced install copy and then removes that copy,
  and a resumed restore removes an install copy that a crash left. A journal
  file that an earlier restore left linked to its restore evidence is still a
  blocked journal issue. `runner.sh recovery-unlink` gives it one link, and
  acts only when the other link is that restore evidence copy. While such an
  issue stands, `recovery-plan` withholds only that run's entry, and
  reconciliation refuses every run. Archive refresh is not held for the
  linked run itself (followup sd:819). The fix is in restore and the runner,
  outside this verb.
- **Addressed for reconciliation, accepted for the runner (C-50).** An entry
  that vanishes during a reconciliation read is skipped, so `recovery-plan`
  and archive refresh no longer hold on it. A rename during a runner tick
  still holds that one tick. One during daemon start fails that start, and
  launchd restarts it after 30 seconds (section 4.4).
- **Accepted: rows put back are not removed again by this verb (C-64).**
  After a put-back, G6 refuses the same rows, because their record already
  holds the `external_id`. A new record identity for a second remove is
  later work.
- **An apply killed after its commit (C-48).** SIGINT, SIGTERM and SIGHUP
  only set a flag, which `apply` does not read after the commit, so the
  apply finishes the move and exits 0, or 4 with the commands when the move
  fails (C-57, C-66). A SIGKILL prints nothing. The record's
  `move after commit:` list and `recovery-plan` are then the recovery.
- **Accepted (D4, option a):** clearing a retained-clone refusal is a hand
  step. The operator runs the printed `chflags` and `rm -rf` commands. A
  wrong path typed by hand is the risk; the refusal prints the exact path so
  the operator copies it. The later path is a runner `prune apply`
  (sd:770).

## Out-of-scope findings

These are recorded here and not fixed:

- sd:744 stranded 2 retained clones (304K total) and 2 runner journal files.
  No supported verb could remove them, because both paths need the
  `runner_run` row (`maintenance.py:62`, `:93-106`). The owner removed them
  by hand on 2026-09-13 (measured gone at 2026-09-14T03:03Z).
- Item 68, `task38-queue-only-20260909T141401Z-26ef90`, is an acceptance-test
  row in the live store. Its status is `done`, it has 1 cancelled assignment
  and 4 notes. Whether to keep it is a separate call.
- A full restore that links a missing journal name leaves the link in
  `runner-recovery-evidence/restore-*/runner-journal` (`backup.py:403`,
  `:407`). The restored journal file then has two links, and the runner
  reports it as a blocked journal issue (`reconciliation.py:46-48`), which
  `recovery-quarantine` refuses (`reconciliation.py:185`). This affects
  every restore, not only this verb (review round 3, C-49). One such issue
  also hides every other run's entry (`reconciliation.py:101`, review round
  4, C-56). Filed as followup sd:779, "A full restore links journal files
  back, and one linked file hides every run's recovery entry". It covers
  both halves: the link, and the entries hidden while any issue exists.
- `ingest` writes the report's opening `status_change` note with session
  `cron` (`reporting.py:97-99`), also for an operator's record. sd:755 has the
  same effect. A fix belongs to `ingest`, not to either verb.

## Review

Round 1 of 5 ran on head `f7738526` and returned NEEDS REMEDIATION (C-1 to
C-10). C-2 to C-10 were fixed at `b6653595`. C-1 is folded in with the
owner's D4 decision, option (a) (decision note 1879): C-1 is closed as
option (a), with option (b) filed as sd:770. The sd:755 round 2 add-ons are
in too: C-17 checked, C-18 (keywords only if absent), C-19 (the aligned
`json_type` predicate in I9) and the `SD_SESSION` refusal.

Round 2 ran on head `9b78110b` and returned NOT CONVERGED. C-22 (a journal
file left for a removed run holds the runner's archive refresh) was
blocking. The owner decided it as option (c) in decision note 1900, which
replaces the journal clause of note 1846 Q4. The apply now moves the journal
files to quarantine after its commit, and prints `mkdir` and `mv` commands
and exits 4 when the move fails. D4, section 1, section 4 step 7, sections
4.4, 5, 6 and 7, and Risks changed. Section 4.4 also gives the C-3
leftovers: `retention.json`, `kept.tar`, archive generations and
reconciliation receipts are left and listed. The non-blocking C-23 to C-31
are folded in.

Round 3 ran on head `01caa8f8` and returned CONVERGED, with no blocking
concern. The owner asked for the non-blocking C-45 to C-54 to be folded in
before round 4:

- C-45, addressed: the put-back moves the journal files back (4.3).
- C-46, addressed: the `sd-db.sh` dispatch arm gets `item`.
- C-47, addressed: both READMEs change (section 9).
- C-48, addressed: an interrupt after the commit prints the commands.
- C-49: the restore wording is corrected to linked journal issues.
- C-50, addressed: an entry that vanishes during a reconciliation read is skipped, not reported unreadable (`_journal_view`); a rename during a runner tick still holds that one tick, or one daemon start that launchd restarts after 30 s, because the fix there would be in `sd_db`. A hand-run `recovery-plan` skips the entry too, and any failure it does hit exits 1 with a one-line `runner:` error, not a traceback.
- C-51, addressed: step 7 gets the lock check, the fsynced receipt and the target check.
- C-52, addressed: the in-memory G5 check moves to step 1.
- C-53: text slips are fixed.
- C-54, corrected: sd:755 head references are updated.

Round 4 ran on head `4d8109be` and returned CONVERGED, with no blocking
concern. The owner asked for the non-blocking C-55 to C-62 to be folded in
before round 5:

- C-55, corrected: a put-back without the move makes no recovery entry and
  no hold; only backups fail (4.3). This corrects the round 3 C-45 text.
- C-56, addressed by followup sd:779: one linked journal issue hides every
  run's entry (4.4, Risks). Risks records the fix.
- C-57: SIGINT and SIGTERM set a flag that `apply` checks at fixed points,
  and an interrupt out of `COMMIT` is detected by the record row (section 4
  step 7, section 6).
- C-58, addressed: the `runner-recovery-evidence` `mkdir` line comes first.
- C-59, addressed: `receipt.json` lists the planned files; the put-back lists the
  quarantine directory.
- C-60, addressed: G5 in section 3 names the in-memory trigger.
- C-61, addressed: the restore lock location, the plan signatures and `check_actor`.
- C-62, addressed: the printed and manual moves use `mv -n` and check the source.

Round 5, the last pass under the cap, ran on head `c281925e` and returned
CONVERGED, with no blocking concern. The owner asked for C-63 to C-71 to be
folded in with no further review pass. sd:755's #347 merged on main as
`e377da74` meanwhile, and the `reporting.py` and `retention.py` citations
are against that main.

- C-63, addressed: the committed test uses the record id this apply's `ingest`
  returned, and the handler catches only `KeyboardInterrupt`.
- C-64, addressed: G6 refuses a remove of rows put back after an earlier remove,
  before the backup.
- C-65, addressed: the signal handlers store into a plain flag with no lock.
- C-66, addressed: SIGHUP is handled, and the move always finishes after the commit.
- C-67, addressed: one test per stop point, and a preview G4 test (`implement.md`).
- C-68, addressed: the exit code of a signal after the last check is stated.
- C-69, addressed: `signal_stop` restores the handlers in `finally`.
- C-70, addressed: printed moves also check `test ! -L <source>`.
- C-71, addressed: step 1 names the sd:755 test module and two greps, and the
  citations follow main `e377da74`.
- INFO-1: the put-back says what to do when no quarantine directory
  exists. INFO-2 and INFO-3 are left as written.

Pre-edit state for the planning review, at PR #336 head `632859db`:

- `prd.md` sha256 `b638345ca754c0bad44cd21fbc079f804963ee1ef2b07800b1c00beb1bf5b11a`
- `design.md` sha256 `6e088338aaa23c89960113e34d2fe04e528b470ea3e1948ef91d3249b0bf87f5`
- `implement.md` did not exist.
