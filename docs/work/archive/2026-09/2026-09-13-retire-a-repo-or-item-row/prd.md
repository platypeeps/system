---
title: retire a repo or item row through a supported verb
created: 2026-09-13
status: done
item: sd:754
---

# PRD — retire a repo or item row

## Problem

No supported path removes a `repo` row or an `item` row from the store. A
grep of `local-sd-db/sd_db`, `local-sd-runner/sd_runner` and
`local-project-dashboard/sd_dashboard` for `DELETE FROM` finds two statements
in production code: `writes.end_trial` deletes a `trial` row
(`writes.py:615`), and `retention.compact_heartbeats` deletes `heartbeat`
state rows (`retention.py:185`). Nothing deletes from `repo`, `item`, `note`,
`assignment`, `runner_run` or `runner_lease`.

That gap has cost something twice:

1. **2026-09-10, 23:52Z.** A hand-run `sqlite3` one-liner removed the runner
   provisioning probe's repo row and item 72. The CLI opens with
   `foreign_keys` off, so nothing cascaded and nothing refused. Six rows were
   left pointing at nothing: `runner_run` 1 and 2, `runner_lease` 1 and 2,
   and `assignment` 2 and 3. Three nightly backups copied them and reported
   success (sd:744, note 1712).
2. **2026-09-13, 18:01Z.** sd:744 deleted those six orphans. It used
   `sd_db.database.connect()` from a one-off wrapper, after it booted the
   dashboard and the runner out of launchd by hand (sd:744, note 1709). The
   work was careful, but it was a script. No test covers it, the only record
   of what it removed is free text in a note on another item, and the next
   person who must remove a row has only that script as a pattern.

The second event did not close the hole. It left two things the store cannot
reach any more. Both were measured read-only on 2026-09-13:

- `~/.local/share/sd/runner-journal/35f34207295e4c6faa502397fda67e22.json`
  and `32da1ad4422d4becb0a77b0e5bc3a1a3.json` describe the two probe runs,
  and no `runner_run` row has those ids.
- `/Volumes/sd-work/retained/2/1/clone` (156K) and
  `/Volumes/sd-work/retained/3/1/clone` (148K) are the probe's retained
  clones. The retained-clone prune finds candidates from `runner_run` rows
  (`local-sd-runner/sd_runner/maintenance.py:62`, through
  `runner_retention.candidates`), so no supported verb could discard them.

The owner removed both clones and both journal files by hand on 2026-09-13,
at about 20:37 local. A read-only check at 2026-09-14T03:03Z found neither
clone under `/Volumes/sd-work/retained/` and neither file in
`runner-journal/`. That hand step is the gap again: no verb removes a
retained clone. The owner decided D4 as option (a) (decision note 1879): the
remove refusal prints the exact commands for each clone, and the operator
runs them. A runner `prune apply` for retained clones is followup sd:770.
For the runner journal files, the owner decided option (c) (decision note
1900): the apply moves them into a quarantine directory after its commit. A
journal file left for a removed run would hold the runner's archive refresh
for every run.

The owner split this item out of sd:743 on 2026-09-13 (sd:743, note 1762),
because retiring needs its own safety rules. It has the same cause as sd:743
(no supported way to change a kind) and sd:755 (no attributable bulk
acknowledge): when a verb is missing, someone writes an ad-hoc script.

## Requirements

1. A supported library function retires one `item` row, and another retires
   one `repo` row. Both open the store only through `sd_db.database.connect()`,
   so `foreign_keys` is on for every statement they run.
2. The retire refuses a row that has a dependent the verb does not own, and
   the refusal names each dependent by table and id. The list of dependents
   comes from the schema's foreign keys and is written out in `design.md`
   section 1. It is not written from memory.
3. The retire is all or nothing. When it finishes, `PRAGMA foreign_key_check`
   returns zero rows, and that check runs inside the transaction, before the
   commit. The precedent is `recovery.reimport` (`recovery.py:187`).
4. Every retire records who acted and why. `who` is a keyword-only argument
   with no default, as `writes.transition` requires and as sd:747 and sd:749
   applied to every operator verb. A blank `who` or a blank reason is refused.
5. The record of a retire outlives the row. It names the actor, the reason,
   the time, and every row removed, with that row's full column values. A
   person must be able to rebuild a removed row from the record, without a
   backup. The record is one `report` item filed through `reporting.ingest`,
   with the rows in its notes, as sd:755 files its record (note 1846, D2).
   No migration is added.
6. A dry run is the default. It prints every row the apply would remove,
   every refusal, every runner journal file the apply moves after its commit,
   and every file and every structured reference the verb leaves in place.
   Note bodies are free text and are not searched (`design.md` section 1,
   second table), so the criterion covers the structured references the plan
   enumerates, not a text search.
   It also prints a fingerprint. The apply requires that fingerprint and
   refuses if anything changed since the preview.
7. The verb runs with the dashboard and the runner live (D7). A concurrent
   writer cannot make the verb leave an orphan, and the verb cannot make a
   live run lose its row.
8. The verb never deletes files outside the database: execution logs,
   retained clones, retention receipts, kept archives, reconciliation
   receipts or checkout folders. The dry run and the record list the files
   it leaves. For a retained clone, the refusal prints
   `chflags -R nouchg <path>` and `rm -rf <path>` for the operator to run
   (D4, option a). The one file the verb changes is the runner journal.
   After its commit, the apply moves each removed run's
   `runner-journal/<run>.json` and `<run>.lock` into a quarantine
   directory, so no recovery entry holds archive refresh. If the move fails,
   the command prints the exact `mv` commands, with quoted paths, and exits
   with its own status (decision note 1900). This replaces the "leave the
   journal files" clause of note 1846 Q4.
9. A retire that `repo seed`, `work register` or the writing import would undo
   at once, and that can be checked locally, is refused. A row a writer can
   key again is reported in the preview (D6): the `docs/work`, `vault` and
   `register` importers, and `cron-report` rows from `reporting.ingest`.
   `github-issues` and `index.sqlite` write `shadow` rows, not items, and
   `drafts` has no importer. A `skill-request` row is keyed too, and I8
   always refuses it. The report is not a refusal, and three cases come back
   at the next sitting: a `register` item, which is certain and live today,
   because that migration reads the default branch's committed tree; a
   `docs/work` item whose file is committed on an unmerged remote branch and
   absent from the checkout; and a `vault` item (`design.md` section 3).
10. The apply takes a backup first and records its snapshot directory (D10).
11. The record has a stated size limit. Rows are split between notes of at
    most 200,000 bytes, and a plan beyond the limits in `design.md` section
    4.2 is refused in the preview.
12. A record never removes a record, and no bulk verb sweeps one. Every
    record report carries a `fields.record` marker, and `item remove` refuses
    any item that has one. That includes sd:755's `reports-acknowledge` batch
    records (agreed with sd:755's planning review, finding C-6). The record
    is filed with `attention` false and moved to `done` in the same
    transaction, so it raises no needs-you.

## Assumptions

These are assumptions, not requirements. Each one can be checked:

- Retire is rare. The store has one missing item id (72): at
  2026-09-14T03:03Z, `select count(*), max(id) from item` gives 768 and 769
  (761 and 762 on 2026-09-13). sd:744 is the only recorded repo removal. The design optimises for being correct and
  explaining itself, not for speed or batch use.
- Every production connection to `sd.db` goes through `connect()`, so
  `foreign_keys` is on. `local-sd-db/tests/test_one_store.py:40` enforces this
  with a grep. The exceptions in `backup.py` open snapshot copies, an
  in-memory reference database and the restore destination. None of them
  deletes rows.
- The runner and the dashboard read the store on each tick or request and do
  not cache `repo` or `item` rows in memory. A grep of `sd_runner` and
  `sd_dashboard` for `lru_cache`, `functools.cache` and repository caches
  found nothing. This was not proven by running the services.

## Acceptance criteria

These follow the owner's decisions in decision note 1846, listed as D1-D10 in
`design.md`.

- [ ] A fixture store rebuilt with the sd:744 probe rows, as they were in
      `~/Documents/sd-backups/2026-09-10.3/sd.db` (repo, item 72, notes
      135-140 and 537, assignments 2 and 3, and two released runs and leases),
      previews `repo remove --with-items` that lists exactly those 15 rows.
      For the apply, the fixture adds what the snapshot lacks: a newer
      assignment on another item (so A1 passes; the snapshot's ids are 1, 2
      and 3), a retention root that is present, and no clone directories (so
      P4 passes). After the apply with its fingerprint, `PRAGMA foreign_key_check`
      returns 0 rows and `integrity_check` returns `ok`.
- [ ] That apply files exactly one new `report` item with `external_id`
      `repo-remove:<fingerprint>` and `repo` NULL. Its manifest lists the 15
      row keys. Its chunk notes, parsed line by line, give back every removed
      row with column values equal to the fixture's, and each chunk's sha256
      matches. `removal.records(connection, "item", 72)` returns that report's
      id.
- [ ] That record has `fields.record` `"repo-remove"` (`"item-remove"` for an
      item remove), `fields.attention` false, status `done`, and no followup
      note. `retention.settle_clean_reports` 30 days later leaves its revision
      unchanged.
- [ ] `item remove` refuses (I9) a report whose `fields.record` is
      `"item-remove"`, `"repo-remove"` or `"reports-acknowledge"`, or a
      hand-edited JSON `null` (`{"record": null}`), and a report with no
      marker is not refused by I9.
- [ ] The same fixture, with the retained clone directory present, is
      refused. The refusal names the clone path and prints
      `chflags -R nouchg <path>` then `rm -rf <path>`. After the clone
      directory is removed, the same preview has no P4 refusal. With the
      retention root absent (volume not mounted), it is refused, and the
      message says the volume is not mounted (`design.md` D4, option a).
- [ ] A remove whose assignment ids are the largest in the store is refused
      (A1). After a newer assignment exists, the apply passes, and an
      assignment inserted after the apply gets an id larger than every
      removed id.
- [ ] A remove is refused when another item's `depends_on` or a pending
      contribution-queue entry names the item (I10), and when a run on the
      repo belongs to an item outside the plan (P6).
- [ ] Each refusal in `design.md` section 3 (G1-G6, A1, R1-R3, I1-I11,
      P1-P6) has one test. The test fails when the guard is removed (a
      mutation check) and leaves every table count unchanged. A `who`,
      `reason`, `SD_SESSION`, principal or program value over 200 characters
      is refused before any backup is taken. `SD_SESSION` is refused, not
      truncated. A stale fingerprint or any other refusal takes no backup.
- [ ] A remove that would leave no assignment in the store is refused (A1).
- [ ] On a temporary home whose `runner-journal/` holds each removed run's
      `.json` and `.lock`, after the apply `reconciliation.plan` has no entry
      for a removed run, `archive_refresh._safe_state` does not raise, and
      both files are in `runner-recovery-evidence/removed-<fingerprint>/`
      with unchanged sha256. The same test fails when the move is skipped
      (`design.md` section 4, step 7).
- [ ] When the move fails because another process holds the run's `.lock`,
      the rows stay removed, the command prints the reason and `mkdir` and
      `mv` lines with quoted paths for every file not moved, and it exits 4.
      After the holder exits, running the printed lines leaves
      `reconciliation.plan` with no entry for the run. A `KeyboardInterrupt`
      raised by the first rename, or right after `COMMIT` returns, gives the
      same lines and exit 4. A SIGINT, SIGTERM or SIGHUP seen at a check
      before the commit exits 1 and removes nothing. One after the last
      check commits, finishes the move and exits 0, or 4 when the move
      fails for its own reason (C-57, C-66, C-68).
- [ ] After a put-back of `design.md` section 4.3, a remove of the same rows
      is refused with G6 before any backup is taken, and no apply after a
      put-back is reported as committed (C-63, C-64).
- [ ] The manual put-back of `design.md` section 4.3, run on the fixture
      after an apply, moves the journal files back after its commit. Then
      `reconciliation.plan` has no entry, and `backup.run` completes. Without
      that move, `plan` still has no entry and `_safe_state` does not raise,
      because a released row with no journal is not a recovery entry, but
      `backup.run` raises "runner row ... differs from the backup journal"
      (C-55).
- [ ] `sd-db.sh item remove ID --who W --reason R`, run through the
      `sd-db.sh` entrypoint, reaches `jobs.cli` and exits 0 on a clean
      fixture.
- [ ] A valid journal record inside `runner-recovery-evidence/removed-*/`
      makes no `reconciliation.plan` entry and no journal issue. After the
      apply, the left `retention.json`, run `kept.tar`,
      `archives/<generation>/kept.tar` and a `runner-reconciliation` receipt
      naming a removed run are unchanged, are listed under `left:`, and make
      no entry, and `_safe_state` does not raise.
- [ ] An item whose `fields` is not valid JSON, or a store where another
      item's `fields` is not valid JSON, is refused (I11) without an
      exception.
- [ ] A retained path with a space is printed shell-quoted, and a retained
      path that does not end in `<assignment>/<run>/clone` under the
      retention root is refused with no command printed.
- [ ] A plan whose rows need two notes writes two chunk notes, `1 of 2` and
      `2 of 2`, with no row split. A single row larger than one note (R1) and
      a plan over `MAX_RECORD_NOTES` (R2) are refused in the preview, with the
      limits lowered by the test.
- [ ] Calling the library function without `who` or `reason` raises
      `TypeError`. The CLI without `--who` or `--reason` exits non-zero before
      it opens the store. The class test `NoVerbNamesItsOperatorForTheCaller`
      (`local-sd-db/tests/test_controls.py`) stays green.
- [ ] An apply whose fingerprint differs from a fresh preview is refused and
      writes nothing, the record included. For the test, a note is added
      between preview and apply.
- [ ] An apply that runs while another connection holds a write transaction
      waits for that transaction (up to `busy_timeout`) and then either
      completes cleanly or refuses. It never commits with a foreign key
      violation.
- [ ] The apply writes a backup snapshot before any row changes, and the
      record's `backup:` line names that directory. `restore` of that
      snapshot into a scratch home (never over the store under test) brings
      the rows back and passes `_check_restore`.
- [ ] `sd-db.sh backup` on the fixture after an apply exits 0, and its summary
      line has no `BROKEN:` clause (`sd_db/jobs/backup.py:69-72`).
- [ ] If `reporting.ingest` already has `actor` and `record` when PR 1 is
      opened, PR 1 changes no `ingest` code. A revert of PR 1 while sd:755
      is on `main` keeps both keywords (`design.md` section 9).
- [ ] `local-sd-db` suite: the same pass and fail names as `origin/main`,
      plus the new tests.

## References

- `sd:754`: this item.
- `sd:744`: the six orphans, the delete wrapper and the backup finding
  (notes 1709, 1710, 1712 and 1723; PR #316).
- `sd:743`: the owner's scope decision that split this item out (note 1762),
  and the kind-edit guards in the unmerged `platypeeps/system#332`.
- `sd:747`: `acknowledge` lost its default `who` (PR #319).
- `sd:749`: 44 `who` defaults removed and the class test added
  (`platypeeps/system#322`, pack #908 and #912).
- `sd:755`: the sibling gap, an attributable bulk acknowledge. Its design
  (`platypeeps/system#334`, D3, plan head `9bdf89c2`) gives the report record
  shape this item shares. It is `in_progress` since decision note 1906.
  Its PR 1, system #347, merged on main as `e377da74` and added the
  `actor` and `record` keywords to `ingest`.
- This item's PR 1: system #350, merged on main as `c7809f63`
  (`sd_db/removal.py` and its two test modules, steps 1-3 of
  `implement.md`).
- Decision note 1846 on sd:754: the owner's answers to Q1-Q10.
- Decision note 1879 on sd:754: D4 option (a), printed commands for retained
  clones.
- `sd:770`: followup, a runner `prune apply` for retained clones (D4 option b).
- Decision note 1900 on sd:754: C-22 option (c), the journal files of a
  removed run move to quarantine after the commit.
- `local-sd-db/sd_db/schema/001_initial.sql` through `009_personal_and_followup_kinds.sql`:
  the foreign keys.
- `docs/work/2026-09-05-one-database-one-front-door/prd.md:596` and `:1124`:
  an `exec` note "leaves only with its item".

## Log

- 2026-09-13 created. The prd and design were drafted, and the owner
  decisions are open in `design.md`. `implement.md` waits for those decisions
  and for the planning adversarial review.
- 2026-09-13 the owner decided Q1-Q10 (decision note 1846). Q2 changed: the
  record is one `report` item, not a new `state` kind, and there is no
  migration 010. Requirements 5, 7 and 9 were updated, 10 and 11 were added,
  and the acceptance criteria now follow the decisions. `implement.md` was
  written. The planning adversarial review is next.
- 2026-09-13 coordination with sd:755 (its planning review, finding C-6):
  record reports carry `fields.record`, `item remove` refuses any item with
  that marker, and the record is filed with `attention` false and moved to
  `done` at once. Requirement 12 and two acceptance criteria were added.
- 2026-09-14 review round 1 (NEEDS REMEDIATION). C-2 to C-10 fixed: refusals
  A1 (assignment id reuse), I10 (contribution dependencies) and P6 (a run on
  the repo outside the plan), the G4 session limit, restore into a scratch
  home only, counts re-measured, and the work split into three pull
  requests. C-1 is open: D4 goes back to the owner.
- 2026-09-14 the owner decided D4 as option (a) (decision note 1879): the
  refusal prints `chflags -R nouchg` and `rm -rf` for each retained clone, an
  unmounted volume refuses, and sd:770 holds the later `prune apply`.
  Requirement 8 and the clone criterion were updated. From sd:755 round 2:
  `SD_SESSION` is refused, not truncated; the `ingest` keywords are added
  only if absent and survive a rollback (C-18); the I9 predicate was
  still open (C-19, settled in the next entry); sd:755 will also move its record to `done` (C-17), so
  requirement 12 still agrees.
- 2026-09-14 C-1 closed as option (a); option (b) is sd:770. C-19 aligned: I9
  uses `CASE WHEN json_valid(fields) THEN json_type(fields,'$.record') END IS NOT NULL`, the same predicate as sd:755, so a `{"record": null}` marker
  counts as a record. The I9 criterion gained that case. `SD_SESSION` over 200
  characters stays a refusal, a deliberate difference from sd:755's
  truncation.
- 2026-09-14 review round 2 (NOT CONVERGED; C-22 blocking, held for an owner
  decision on D4 and requirement 8). Non-blocking C-23 to C-31 folded in: A1
  with no surviving assignment, quoted and shape-checked clone paths, the
  read-only plan before the backup, G3 and G5 mutations, stale values, the
  sd:755 grep and one-keyword stop, G4 over principal and program with the
  connection's database file, the manual put-back path, and refusal I11 for
  malformed JSON. The probe criterion now states its A1 and P4
  preconditions.
- 2026-09-14 the owner decided C-22 as option (c) (decision note 1900). It
  replaces the journal clause of note 1846 Q4 ("leave the journal files").
  After its commit, the apply moves each removed run's journal `.json` and
  `.lock` into `runner-recovery-evidence/removed-<fingerprint>/`. When the
  move fails, it prints `mkdir` and `mv` commands and exits 4. Requirements
  6 and 8 and three criteria changed. The C-3 leftovers are decided in
  `design.md` section 4.4: `retention.json`, `kept.tar`, archive generations
  and reconciliation receipts are left and listed, because no reader reaches
  them without a `runner_run` row or a clone, and `reconciliation.plan` reads
  only `runner-journal/`.
- 2026-09-14 review round 3 (CONVERGED at `01caa8f8`, no blocking). The
  owner asked for the non-blocking C-45 to C-54 before round 4:
  - the put-back moves the journal files back after its commit (C-45);
  - `sd-db.sh` dispatches `item` (C-46);
  - both READMEs change (C-47);
  - SIGINT and SIGTERM after the commit print the move commands and exit 4
    (C-48);
  - restored journal files are linked journal issues (C-49);
  - the rename race is accepted (C-50);
  - step 7 checks the lock, fsyncs the receipt and refuses an existing
    target (C-51);
  - the in-memory refusal is G5 at step 1 (C-52);
  - text slips are fixed (C-53);
  - sd:755 references are updated (C-54).
  Two criteria were added.
- 2026-09-14 review round 4 (CONVERGED at `4d8109be`, no blocking). The
  owner asked for the non-blocking C-55 to C-62 before round 5:
  - a put-back without the move makes no recovery entry and does not hold
    refresh; only backups fail. This corrects the round 3 C-45 text and
    note 1912 (C-55);
  - one linked journal issue hides every run's entry, followup sd:779
    (C-56);
  - SIGINT and SIGTERM set a flag that the apply checks; an interrupt after
    `COMMIT` still prints the commands and exits 4, and one before the
    commit exits 1 (C-57);
  - the `runner-recovery-evidence` `mkdir` line comes first (C-58);
  - `receipt.json` lists the planned files (C-59);
  - G5 names the in-memory trigger (C-60);
  - the restore lock location and the plan signatures are fixed (C-61);
  - printed moves use `mv -n` and check the source (C-62).
  Two criteria changed.
- 2026-09-14 review round 5 (CONVERGED at `c281925e`, no blocking, the cap
  is spent). The owner asked for C-63 to C-71 with no further review pass:
  - the committed test is bound to the record id this apply's `ingest`
    returned, and only `KeyboardInterrupt` is caught (C-63);
  - G6 refuses rows put back after a remove, before the backup (C-64);
  - the signal handlers store into a plain flag with no lock (C-65);
  - SIGHUP is handled, and after the commit the move always finishes
    (C-66);
  - one test per stop point, and a preview G4 test (C-67);
  - a signal after the last check exits 0 or 4 (C-68);
  - handlers are restored in `finally` (C-69);
  - printed moves also check `test ! -L` (C-70);
  - step 1 follows sd:755's #347, merged as `e377da74`, and the citations
    follow that main (C-71).
  One criterion changed, one was added, and G6 joined the refusal list.
- 2026-09-14, before the merge of the planning pull request. The owner
  approved the merge, and one correction landed first. D6's importer set was
  wrong in `design.md`, and this document and `implement.md` repeated its
  phrasing. `github-issues` and `index.sqlite` write `shadow` rows and no
  item row, `drafts` matches no importer, and the item-writing importers are
  `docs/work`, `vault` and `register`. The
  `docs/work` importer reads committed trees on every unmerged remote
  branch, so a file absent from the checkout passes I8 and comes back. The
  re-import gap is three cases, not two: `register` first, which is certain
  and live, because that migration reads the default branch's committed tree
  and no refusal reads the register; then the `docs/work` branch case; then
  every `vault` item. The branch also took
  `main` at `62319e67`, where this item's PR 1 (#350, `c7809f63`) is merged:
  steps 1-3 are done, I11 also refuses a blob in a text column, and the
  retained-path check keeps its symlink clause and its resolve clause.
- 2026-09-14, the off-cap read of the merge correction. `register` was
  missing from the re-import gap, in four places: `design.md`, criterion 9,
  this Log and the pull request body. That migration reads its file from the
  default branch's committed tree and re-keys by `(source, external_id)`, so
  the case is certain rather than conditional, and items 362, 364 and 366
  are removable today. Three owner decisions were folded with it: step 7
  takes `runner-ending/<run>.lock` before it moves a journal pair, because
  `Runner.finish` commits the release and only then persists inside that
  lock; `apply` derives `pid` and `ppid` with `os.getpid()` and
  `os.getppid()`, with a test; and criterion 6 says structured references
  instead of text references, because note bodies are never searched. Two
  citations were repointed, and `skills_catalog.request` joined the writer
  set with its I8 refusal.
