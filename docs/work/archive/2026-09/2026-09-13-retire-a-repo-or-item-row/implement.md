# Implement — retire a repo or item row

The work lands as three system pull requests in `local-sd-db`, in this order.
There is no pack change and no migration (`design.md` D2, D8, section 9).

| PR | Steps | What it adds | Writes to the store | Size |
|---|---|---|---|---|
| 1 | 1-3 | `ingest` keywords, `plan_item`, `plan_repo`, every plan refusal except R1-R3 | none (read-only plans) | M+L+M, about 600 lines |
| 2 | 4-5 | the record with R1-R3 in both plans, `apply` with the journal move, `records`, one runner test module | the apply; the journal move after its commit | M+L, about 800 lines |
| 3 | 6-7 | the CLI, usage text, README, whole-suite and backup check | through PR 2 | M+S, about 300 lines |

**PR 1 is merged.** System #350 landed as `c7809f63` on main: new
`local-sd-db/sd_db/removal.py` (+497 lines),
`local-sd-db/tests/test_removal.py` (+741) and
`local-sd-db/tests/test_removal_ingest.py` (+50). Steps 1, 2 and 3 are ticked
below, and their notes say what the code settled. Steps 4 to 7 are unchanged.

**Why three.** The code review cap is 1 (the pack's
`.claude/rules/sd-planning-adversarial-review.md`). One pull request of about
1,500 lines is too large for one careful pass (review round 1, C-10). The
split follows the risk: PR 1 only reads, so a reviewer can check the refusal
list against the schema alone. PR 2 holds every write and the transaction.
PR 3 is the operator surface. Each PR is usable on its own: PR 1's plans are
library functions with tests, and PR 2 needs nothing from PR 3.

**D4 is option (a) (decision note 1879).** Steps 2, 3 and 6 implement I6
and P4 as `design.md` section 3 states them: refuse a retained clone on disk
and print `chflags -R nouchg <path>` then `rm -rf <path>` for it, and refuse
an absent retention root with "retained volume is not mounted". No step runs
`chflags` or deletes a file. A runner `prune apply` is followup sd:770, not
this item.

**The journal files are option (c) (decision note 1900).** It replaces the
"leave the journal files" clause of note 1846 Q4. Steps 2 and 3 list each
removed run's `runner-journal/<run>.json` and `<run>.lock` under `move`.
Step 5 moves them into `runner-recovery-evidence/removed-<fingerprint>/`
after the commit (`design.md` section 4, step 7). Step 6 prints the
`mkdir` and `mv` commands and exits 4 when the move fails. No step deletes a
journal file.

## Conventions for every step

Environment:

- `PYTHONDONTWRITEBYTECODE=1`.
- `SD_ACCEPTANCE_PACK` points at a `git archive` extraction of the pack, not at
  the live checkout.
- Nothing opens `~/.local/share/sd/sd.db`. Every test builds its own store in a
  temporary `home`.

**Running one module.** `sd-db.sh test` passes its arguments to
`unittest discover -s tests` (`local-sd-db/sd-db.sh:221`). A module name such
as `tests.test_removal` is ignored there, and the whole suite runs (review
round 1, C-6). Use the pattern flag:

    sh local-sd-db/sd-db.sh test -p test_removal.py -v

Checked on `0b5394ae`: `sd-db.sh test -p test_report_replay.py` printed
`Ran 10 tests`, and `grep -c "def test_" local-sd-db/tests/test_report_replay.py`
gives 10. Every Verify line below states the count check the same way: the
`Ran N tests` line must equal `grep -c "def test_"` over the named files. A
larger N means the pattern did not narrow the run.

**Failing first.** A new module that does not exist yet fails with
`ImportError`, and that proves nothing about a guard. So each step first
commits a stub: the new functions exist with their final signatures, and
return an empty result (a plan with no rows and no refusals, a record with no
chunks, an apply that returns without writing). The new tests then run and
fail on assertions. Quote the `FAILED (failures=N)` line from the stub, then
the `OK` line from the finished step. The mutation checks are the proof for
each guard.

**Mutation checks.** For each refusal, delete or bypass exactly that guard,
run the module, and quote the failing test name. Then restore the file and
confirm with `git diff --exit-code local-sd-db/sd_db`. A mutation that leaves
the module green means the test is missing.

## PR 1 — `ingest` keywords and the read-only plans

- [x] **1. `ingest` takes `actor` and `record`, or the step confirms sd:755
      already added them.** Size: S. Done in #350: both keywords were there,
      so `reporting.py` was not touched and only
      `local-sd-db/tests/test_removal_ingest.py` was added.
  - The rule for sd:754 and sd:755 alike: add `actor` and `record` to
    `reporting.ingest` only if absent (sd:755 round 2, C-18).
  - Check first, with two commands on the branch base, one per keyword:
    `grep -c "actor=None" local-sd-db/sd_db/reporting.py` and
    `grep -c "record=None" local-sd-db/sd_db/reporting.py`. Each prints 1
    when its keyword is there and 0 when it is not. Both keywords can sit
    on one line, so a line count of one grep for both does not tell the
    cases apart (review round 5, C-71). On main `e377da74` (sd:755's #347)
    both print 1.
    - If both are there, sd:755 landed first. Keep them unchanged, and
      confirm they match this step. Their tests are in
      `local-sd-db/tests/test_report_bulk_acknowledge.py`, class
      `IngestTakesAnActorAndARecord` (`:417-449`): a bad `actor` or
      `record`, an 11-key dict and a 201-character value refused with 0
      rows written; an `actor` with a `reason` key stored as given; and
      `fields` unchanged without them. Verify and mutate there: run
      `sh local-sd-db/sd-db.sh test -p test_report_bulk_acknowledge.py -v`,
      and check that dropping the `actor` size check or the `record`
      pattern check fails `test_a_bad_actor_or_record_is_refused_and_writes_nothing`.
      Restore the file. Add only the `item-remove` marker test, in the new
      module `local-sd-db/tests/test_removal_ingest.py`: `record="item-remove"`
      and `record="repo-remove"` are stored as `fields.record`, and an
      `actor` with the seven keys of `design.md` section 4.1 is stored as
      given. No library code changes.
    - If only one is there, stop and record it on sd:754. The contract is
      broken, and the two plans need to agree again (review round 2, C-28).
    - If neither is there, add both, as below.
  - When neither is there, files: `local-sd-db/sd_db/reporting.py`,
    `local-sd-db/tests/test_report_replay.py`. `ingest` gains `actor=None`,
    with sd:755's validation: a dict of at most 10 `str` keys, each value a
    `str` of at most 200 characters, an `int`, or `None`. It is stored as
    `provenance["actor"]` beside `removed` (`reporting.py:82-87` on
    `e377da74`). `ingest`
    also gains `record=None`. When given, it must match the job-name pattern
    (`reporting.py:31`). It is stored as top-level `fields["record"]`, next to
    `attention` and `report` (`reporting.py:88-90`).
  - Tests: a non-dict `actor`, an 11-key dict and a 201-character value each
    raise `WorkflowError` and write 0 rows. An `actor` with the seven keys of
    `design.md` section 4.1, `reason` included, is stored as given.
    `record="item-remove"` is stored as `fields.record == "item-remove"`.
    `record="Not A Job"` raises `WorkflowError` and writes 0 rows.
  - Mutations: drop the `actor` size check (the 201-character test fails);
    drop the `record` pattern check (the `Not A Job` test fails).
  - Verify, when neither was there:
    `sh local-sd-db/sd-db.sh test -p test_report_replay.py -v` ends in `OK`,
    and its `Ran N tests` equals `grep -c "def test_"
    local-sd-db/tests/test_report_replay.py`. The existing tests pass
    unchanged, so a call without `actor` or `record` gives the same `fields`.
    When both were there: `sh local-sd-db/sd-db.sh test -p
    test_report_bulk_acknowledge.py -v` and `sh local-sd-db/sd-db.sh test -p
    test_removal_ingest.py -v` each end in `OK`, with `Ran N tests` equal to
    the `grep -c "def test_"` count of that file.

- [x] **2. The plan for an item: rows, refusals, fingerprint.** Size: L.
      Done in #350.
  - Files: new `local-sd-db/sd_db/removal.py` with `check_actor(*, who,
    reason, session, principal, program)` and `plan_item(connection, item,
    *, home)`. New `local-sd-db/tests/test_removal.py`.
  - The plan reads, never writes. It returns the rows by table in insert
    order, each row as a dict of every column; the refusals (G1, G2, G6,
    A1, I1-I11) each with a table and a key; the `move` list (each removed run's
    journal `.json` and `.lock` that exist, and the quarantine directory,
    `design.md` section 4.1); the `left` references (`design.md`
    section 1, second table); the warning for each row a writer can key
    again (`design.md` section 3, the D6 paragraph); and the
    fingerprint, sha256 over the canonical JSON of the rows and the refusal
    keys. G6 is checked after the fingerprint and is not part of it
    (`design.md` section 3, C-64).
  - Tests, one per refusal, each with its mutation:

    | refusal | test builds | mutation that must fail it |
    |---|---|---|
    | G1 | a store with one orphan `note` (foreign keys off while building) | skip the `foreign_key_check` read |
    | G2 | an unresolved `restore` state row | skip the restore-row read |
    | G4, raised by `check_actor`, which the CLI calls before the preview and `apply` calls first; the plans take no actor values (C-61) | `who` blank, `reason` blank, each over 200 characters, and `session`, `principal` and `program` each over 200 characters (seven cases) | drop the length check; drop the blank check; drop `program` from the checked values |
    | G6 | a report filed with `reporting.ingest`, job `item-remove`, `run_id` the plan's fingerprint; control: `run_id` another 64-hex value is not refused | skip the G6 read |
    | A1 | the item's assignment has the largest id; control: a newer assignment on another item exists; a store whose only assignments are the item's (no survivor) | skip the `max(id)` comparison; treat a `NULL` survivor maximum as "not refused" (the no-survivor case fails) |
    | I1 | an id with no row | skip the existence check |
    | I2 | a `publication_claim` on the item, once as `item` and once as `active_item` | drop the `active_item` clause |
    | I3 | one assignment each `queued`, `running`, `ending` and `blocked` | allow `blocked` |
    | I4 | a `cost` row on the item's assignment | skip the `cost` read |
    | I5 | an assignment on another item with `after`, then `parent`, pointing at it | drop the `parent` clause |
    | I6 | an unreleased run; an open lease; a retained clone present (the refusal text holds the path, `chflags -R nouchg <path>` and `rm -rf <path>`, in that order); a clone path with a space (printed as one `shlex.quote` argument); a `retained_path` that ends in `<other assignment>/<run>/clone`, and one with a symlink component (refused with "unexpected shape", no command printed); after the clone directory is removed, no I6; the retention root absent (message names the unmounted volume) | skip the retention-root check; print the commands in the other order; drop `shlex.quote` (the space test fails); skip the shape check |
    | I7 | an open `followup`; an open `question`; control: a resolved one | drop the `question` kind |
    | I8 | the `docs/work` file present; `piece` set; `fields.contribution`; `fields.skill_review` | drop the `skill_review` clause |
    | I9 | reports marked `item-remove`, `repo-remove` and `reports-acknowledge`; a report whose `fields` is `{"record": null}`, set by hand SQL; control: the same report with `{}`. The predicate is `CASE WHEN json_valid(fields) THEN json_type(fields,'$.record') END IS NOT NULL` (C-19) | test the value instead of presence; use `json_extract(fields,'$.record') IS NOT NULL` (the `null` case fails) |
    | I10 | another item's `depends_on` names it; a pending queue entry names it as `item_id`; one names it as a dependency | skip the queue read |
    | I11 | the item's `fields` is `{not json`; another item's `fields` is `{not json`; the queue checkpoint body is `{not json`; a blob in a text column, which `_unreadable` refuses by an `isinstance` test on each column value (`removal.py:317`, added by #350) | skip the `json_valid` check (the plan then reads the item as removable); accept a value that is not `str`, `int`, `float` or `None` |

  - Also:
    - A fixture item with an opening note, a `decision` note, an `exec` note
      and a `done` assignment lists exactly those rows.
    - The schema test: every `pragma_foreign_key_list` row whose parent is
      `repo`, `item`, `assignment` or `runner_run` is handled by a named rule
      in `removal.py`. It fails when a fixture migration adds a child table of
      any of the four without a rule. (The `repo` and `runner_run` parents
      are used by step 3; the test covers all four from the start.)
    - A note added after a plan changes the fingerprint.
    - Verify (the record predicate): a report whose `fields` is
      `{"attention": false, "record": null, "report": {...}}`, written by
      hand SQL, is refused with I9. A `json_extract(...) IS NOT NULL`
      predicate would pass it, so this test fails on that form.
    - Verify: a report whose `fields` is `{not json` is refused with I11,
      with no exception (review round 2, C-31).
  - Verify: `sh local-sd-db/sd-db.sh test -p test_removal.py -v` ends in
    `OK`, and `Ran N tests` equals `grep -c "def test_"
    local-sd-db/tests/test_removal.py`. Every mutation in the table fails its
    test.

- [x] **3. The plan for a repo, with and without `--with-items`.** Size: M.
      Done in #350.
  - Files: `local-sd-db/sd_db/removal.py` (`plan_repo(connection, path, *,
    with_items, home)`), `local-sd-db/tests/test_removal.py`.
  - `plan_repo` adds P1-P6, `repo_protection`, and runs and leases on the
    repo. With `with_items`, it runs the item plan on each item and merges
    rows and refusals, so one refusal on any item refuses the repo. A1 is
    computed over the merged plan: every removed assignment id must be
    smaller than the largest surviving id, and at least one assignment must
    survive when any is removed. Test: `repo remove --with-items` over a
    store whose every assignment is on the repo's items is refused with A1.
  - Tests, one per refusal, each with its mutation:

    | refusal | test builds | mutation that must fail it |
    |---|---|---|
    | G6 | a report with job `repo-remove` and the plan's fingerprint as `run_id` | skip the G6 read in `plan_repo` |
    | P1 | a path with no row | skip the existence check |
    | P2 | `status_source` `retiring`; `pieces_source` `retiring` | drop the `pieces_source` clause |
    | P3 | a repo with one item, no `with_items` | ignore `with_items` |
    | P4 | a run on the repo with its clone present (path, quoted, and both commands printed); a `retained_path` of the wrong shape (refused, no command); with the retention root absent (unmounted message) | skip the retention-root check; drop the printed commands; skip the shape check |
    | P5 | a temporary `repos.personal.conf` and a checkout directory under `home` | skip the disk check |
    | P6 | a run on the repo whose assignment's item has `repo` NULL | skip the item-in-plan check |

  - Also:
    - A released run whose `runner-journal/<run>.json` and `<run>.lock`
      exist beside the fixture store lists both under `move`, with
      `runner-recovery-evidence/removed-<fingerprint>/` as the target, and
      the plan changes neither file (sha256 and mtime). The run's
      `retention.json` and a `runner-reconciliation` receipt naming the run
      are listed under `left`. Mutation: read the journal directory from
      `home` instead of the connection's file (the test's store lives
      outside `default_path(home)`, so the list is empty).
    - The sd:744 probe fixture, rebuilt from the rows listed in
      `design.md` section 7, lists exactly 15 rows with `with_items=True`.
      With assignments 1, 2 and 3 as in the snapshot, it is refused with A1.
      With a fourth assignment on another item, A1 clears.
    - With the clone directories present, the plan has two P4 refusals that
      name the clone paths, each with its two commands. The test removes the
      two directories (the fixture never sets `uchg`), plans again, and finds
      no P4 refusal.
  - Verify: `sh local-sd-db/sd-db.sh test -p test_removal.py -v` ends in
    `OK`, and `Ran N tests` equals `grep -c "def test_"
    local-sd-db/tests/test_removal.py`. Every mutation in the table fails its
    test.

## PR 2 — the record and the apply

**Code review of PR 2.** The review must also show that step 7 takes
`runner-ending/<run>.lock` before it moves a run's journal pair, and that a
held one fails the move: `Runner.finish` commits `store.release` and only
then calls `journal.persist` inside that lock (`runtime.py:630`,
`:671-672`), so the journal lock alone leaves a window where `persist`
recreates `<run>.json` for a removed run (Copilot on #336). It must
reproduce the round 5 probes
against the code and show both fixed: C-63 (a second apply after a put-back,
whose `ingest` raises "this report identity already has different evidence"
inside the transaction, is not reported as committed and prints no lines)
and C-65 (a second signal raised inside the first handler of
`signal_stop` does not hang, and the process still exits).

- [x] **4. The record: manifest, chunks and size refusals.** Size: M.
  - Files: `local-sd-db/sd_db/removal.py` (`MAX_RECORD_NOTES = 16`, a
    `_record(plan, *, who, reason, backup, note_limit=MAX_REPORT,
    max_notes=MAX_RECORD_NOTES)` helper that returns the manifest and the
    chunk bodies, and R1-R3 in both plans), `local-sd-db/tests/test_removal.py`.
  - Format as `design.md` section 4.1: manifest lines in the stated order;
    chunk header `removed rows <n> of <N>, sha256 <hex>`; one row per line
    with `sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`.
  - Tests, each with its mutation:

    | case | mutation that must fail it |
    |---|---|
    | With `note_limit` lowered to fit two rows, a four-row plan gives chunks `1 of 2` and `2 of 2`; no line is split; each sha256 matches its lines | hash the whole plan instead of the chunk |
    | A row with a newline and a non-ASCII character in `body` round-trips to equal column values | `ensure_ascii=True` plus a byte comparison of the stored text |
    | R1: a row larger than `note_limit` | skip the single-row check |
    | R2: a plan needing more than `max_notes` | skip the note-count check |
    | R3: a manifest over its limit | skip the manifest check |

  - Also correct the docstring at `removal.py:310-312`. It names
    `json.dumps` raising as the mechanism, where the guard is an `isinstance`
    test (`removal.py:317`), and it says the value is refused before the
    fingerprint reads it, where `_finish` canonicalises the same rows with
    `default=repr` (`removal.py:395-396`) and hashes the value as its `repr`.
    The refusal is right; the two sentences are wrong (Copilot on #336). No
    behaviour changes, so no test changes.
  - Verify: `sh local-sd-db/sd-db.sh test -p test_removal.py -v` ends in
    `OK`, and `Ran N tests` equals `grep -c "def test_"
    local-sd-db/tests/test_removal.py`.

- [x] **5. The apply.** Size: L.
  - Files: `local-sd-db/sd_db/removal.py` (`apply(connection, plan_kind,
    target, *, fingerprint, who, reason, principal, program, home,
    with_items=False, session=None, stop=None)`, `records(connection,
    table, key)` and the context manager `signal_stop()`),
    `local-sd-db/tests/test_removal.py`, and new
    `local-sd-runner/tests/test_removal_journal.py`. The move uses only
    `sd_db.runner_journal` (`lock`, `directory`, `fsync_directory`), so
    `sd_db` does not import `sd_runner`. The runner module imports
    `sd_runner.reconciliation` and `sd_runner.archive_refresh` to check the
    result, as `local-sd-runner/tests/test_archive_refresh.py` does.
  - Order, the same as `design.md` section 4:
    1. G4 with `check_actor` (`who`, `reason`, `session`, `principal`, `program`), before
       anything else, then G5 for an in-memory connection (an empty `main`
       file name), before `control_gate` (C-52);
    2. the read-only plan and G3, with every refusal (G6 included), outside
       any transaction; then `stop()`;
    3. `control_gate`;
    4. `backup.run(home=home, database=<main file from PRAGMA database_list>,
       keep=None)`, and G5; then `stop()`;
    5. one `transaction`: re-plan and G3; `ingest` with job and `record`
       `item-remove` or `repo-remove`; the chunk notes; `transition` of the
       record to `done` with `reason="removal record"`; the deletes,
       children-first, with all assignments in one statement;
       `PRAGMA foreign_key_check`; then `stop()`, whose
       `RemovalInterrupted` rolls the transaction back (C-57). The id that
       `ingest` returned is kept in a local variable, `None` until then;
    6. commit;
    7. still inside `control_gate`, the journal move (`design.md` section 4,
       step 7): the `.lock` check, the non-blocking run lock, the `.json`
       check, `os.mkdir` of the quarantine directory, `receipt.json` opened
       with `x` and fsynced, a check that no target name exists, `os.rename`
       of `.json` then `.lock`, the directory `fsync`s, and on any failure
       (including `KeyboardInterrupt`) the returned `move_error` and
       `move_commands`, with no exception. After the commit `stop()` is not
       called, and the move always runs to its end (C-66). Around the
       transaction, a handler for `KeyboardInterrupt` only returns
       `move_commands` when the kept id is set, `connection.in_transaction`
       is false, and a row with that id and the record's `external_id`
       exists; otherwise it re-raises (C-57, C-63).
  - `signal_stop()`: handlers for SIGINT, SIGTERM and SIGHUP that store
    `True` in a one-element list, with no lock and no raise, installed on
    entry and restored in `finally` (C-65, C-66, C-69).
  - Tests:
    - The probe fixture (clones absent, retention root present, a newer
      assignment present): after the apply, `foreign_key_check` gives 0
      rows, `integrity_check` gives `ok`, and the 15 rows are gone. Exactly
      one new report exists, with `external_id` `repo-remove:<fingerprint>`,
      `repo` NULL, `fields.report.removed` equal to the table counts, and
      `fields.report.actor.who` and `.reason` equal to the arguments.
      `fields.report.actor.pid` and `.ppid` equal `os.getpid()` and
      `os.getppid()` of the test process, because `apply` derives them
      (`design.md` section 4.1). Mutation: leave `pid` and `ppid` out of the
      actor dict.
    - The record marker and status: `fields.record == "repo-remove"`,
      `fields.attention` is false, `status` is `done`, the report has no
      `followup` note, and its last `status_change` note reads
      `planning -> done by <who>: removal record`. An `item remove` apply
      gives `fields.record == "item-remove"`.
    - Nothing sweeps the record: `retention.settle_clean_reports` with `now`
      30 days later leaves the record's revision unchanged. If sd:755's
      `reporting.clean_candidates` exists on the branch base, it does not
      return the record's id either.
    - Parsing the report's chunk notes gives rows equal to the fixture rows,
      column by column. `records(connection, "item", 72)` returns the
      report's id.
    - Id reuse: after the apply, an assignment inserted on another item gets
      an id larger than every removed assignment id (A1 in effect). The
      removed item had the largest `item.id`, and the report's id is larger
      than it.
    - An assignment chain inside the plan (assignment 3 has `after` = 2, both
      on the item) is removed without a constraint error.
    - A note added between plan and apply: G3, and every table count is
      unchanged, the report included.
    - A forced foreign key violation inside the transaction (a test-only
      child table with a NO ACTION key on `item`, created in the fixture and
      not known to the plan) rolls back the record and the deletes. The
      journal files stay in `runner-journal/`, unchanged.
    - Another connection holds `BEGIN IMMEDIATE` for 1 second, then commits.
      The apply waits, then completes, and `foreign_key_check` is empty.
    - `backup.run` patched to raise: G5, and nothing is written. Then the
      same apply, with `backup.run` restored, completes: a retry after G5 is
      safe.
    - `backup.run` patched to return a `Snapshot` whose `violations` holds
      one row: G5, and nothing is written (review round 2, C-26).
    - A stale fingerprint takes no backup: a note added after the preview,
      `backup.run` patched to record calls, and the apply raises G3 with 0
      calls and no new snapshot directory (C-25). The same for a refusal
      that appears after the preview (an open `followup` added).
    - The backup is of the apply's store: the fixture store lives outside
      `default_path(home)`, and `backup.run` patched to record its
      arguments receives that file as `database` (C-29). An in-memory
      connection is refused with the G5 message, and `control_gate`
      (patched to record calls) is never entered. The test checks the
      message and the call count, not the exception class (C-52, C-60).
    - G4 on `program` of 201 characters: refused, and `backup.run` is never
      called.
    - G4 before the backup: with `session` of 201 characters, `backup.run`
      (patched to record calls) is never called, and no snapshot directory
      is created.
    - The apply's snapshot directory exists, and the manifest's `backup:`
      line names it. `restore` of it into a second temporary home (never the
      home under test) gives back the 15 rows and passes `_check_restore`.
    - Calling `apply` without `who` or without `reason` raises `TypeError`.
    - A second apply with the same fingerprint is refused (I1 or P1), and no
      second report is filed.
    - An `item remove` of the new record is refused with I9.
  - Journal move tests, in `local-sd-runner/tests/test_removal_journal.py`.
    Each builds a temporary home with a `Config` from `sd_runner.runtime`, a
    store, a retention root, and the probe fixture's two runs, released,
    with their journal `.json` and `.lock` written by
    `runner_journal.persist`:
    - Before the apply, `reconciliation.plan(config)["entries"]` is empty.
      After the apply, it has no entry whose `run` is a removed run, its
      `journal_issues` is empty, and `archive_refresh._safe_state(config)`
      returns without raising. `runner-journal/` holds neither file.
      `runner-recovery-evidence/removed-<fingerprint>/` holds both, with
      sha256 equal to the originals, and `receipt.json` names the record id.
      The returned `move_commands` is empty.
    - Control for that test: copying the two `.json` files back into
      `runner-journal/` gives one blocked entry per run, "assignment is
      absent", and `_safe_state` raises "archive refresh held". So the test
      can see the hold.
    - A held `runner-ending` lock (`design.md` section 4, step 7.1): a
      child process holds `runner_journal.lock` on
      `runner-ending/<run>.lock` of the first run in plan order, with that
      run's journal `.lock` free. The apply commits, returns a `move_error`
      that names the ending lock's path and both possible holders, an ending
      sequence and an archive refresh (`design.md` section 4, step 7.1), and
      leaves both files in `runner-journal/`. Mutation: take the journal lock
      alone (the move then finishes, and the files leave while the runner's
      ending sequence is still open); name one holder in the message (the
      test reads both).
    - A held lock: a child process started by the test holds
      `runner_journal.lock` on the `.lock` of the first run in plan order.
      The apply commits: the rows are gone and the record is filed. It
      returns a `move_error` that names the lock. The move stops at that
      run, so on this fixture, which has no `runner-recovery-evidence`,
      `move_commands` holds two `mkdir -m 700` lines,
      `runner-recovery-evidence` first, then the quarantine directory
      (C-58). Then it holds `mv -n` lines for all four unmoved files, both
      runs, `.json` before `.lock` (C-53), each ending in
      `&& test ! -e <source> && test ! -L <source>` (C-62, C-70). Parsing each line with `shlex.split`
      gives the exact paths. Both `.json` files are still in
      `runner-journal/`, and `plan` has two blocked entries. The test kills
      the child by pid, runs each line with
      `subprocess.run(["sh", "-c", line], check=True)`, and `plan` then has no entry and
      `_safe_state` does not raise.
    - An interrupt after the commit (C-48): `os.rename` in `removal`,
      patched to raise `KeyboardInterrupt` on its first call. The rows are
      gone, `move_error` names the interrupt, and `move_commands` lists all
      four files. Control: the same patch on a call before the commit (the
      `ingest` call) lets `KeyboardInterrupt` propagate, and every table
      count is unchanged.
    - An interrupt as `COMMIT` returns (C-57): the `transaction` that
      `removal` uses, wrapped so that it raises `KeyboardInterrupt` right
      after the wrapped block has committed. `apply` returns `move_error`
      "interrupted" and `move_commands` for all four files. On a fresh
      connection the rows are gone and the record row exists. Control: the
      same wrapper raising before the commit lets `KeyboardInterrupt`
      propagate, and every table count is unchanged.
    - The stop points (C-57, C-67), one test each. They use the real
      `backup.run` unless a test says otherwise. Counts are every table's
      row count. Whenever `backup.run` ran, `state` gains exactly its one
      `checkpoint` row (`backup.py:692`), and every other count is
      unchanged.
      - Before `control_gate`: `stop` is true from its first call. The
        apply raises `RemovalInterrupted`, `control_gate` (patched to record
        calls) is never entered, `backup.run` is never called, and no count
        changes, `state` included.
      - After `backup.run`: `stop` turns true once `backup.run` has
        returned. The apply raises `RemovalInterrupted`, `ingest` (patched
        to record calls) is never called, the snapshot directory is
        complete, and `state` gains one row.
      - Inside the transaction: `stop` turns true once `ingest` has been
        called. The apply raises `RemovalInterrupted`, `ingest` was called
        once, no record row exists, and `state` gains one row.
      - After the last check: `stop` returns false on its first three calls
        and true after. The apply commits, finishes the move, returns an
        empty `move_commands`, and calls `stop` exactly three times (C-66).
    - The round 5 probe (C-63): after an apply, the test puts the rows back
      from the chunk notes (`design.md` section 4.3 steps 2-4), so the
      record row with `external_id` `item-remove:<fingerprint>` and the rows
      are both there. With G6 patched off, the same apply runs. `ingest`
      raises "this report identity already has different evidence" inside
      the transaction. `apply` raises that `WorkflowError`, returns no
      `move_commands`, and every count except `state` is unchanged.
    - A `KeyboardInterrupt` before the commit beside an old record (C-63):
      the same store, G6 patched off, and the in-transaction re-plan patched
      to raise `KeyboardInterrupt`. `apply` re-raises it, and every count
      except `state` is unchanged.
    - A second apply after a put-back (C-64): the same store, G6 not
      patched. The apply is refused with G6, `backup.run` (patched to record
      calls) is never called, and no snapshot directory is created.
    - `signal_stop` does not hang on a nested signal (C-65): a child Python
      process started by the test enters `signal_stop`, patches
      `threading.Condition.notify_all` to raise SIGTERM once, then raises
      SIGTERM, and prints `stop()`. Inside the block, `signal.getsignal`
      for SIGINT, SIGTERM and SIGHUP gives the new handler. The child
      prints `True` and exits 0
      within 10 seconds. On a timeout the test kills the child by pid and
      fails. After the `with` block, `signal.getsignal` gives back the old
      handler for each of the three signals, also when the block exits by
      an exception (C-69).
    - A dangling symlink (C-70): the first run's `.lock` is a symlink to a
      path that does not exist, so step 7.2 refuses it, and `move_commands`
      lists it. With a file of the same name placed in the quarantine
      directory, its line exits non-zero, and the symlink is still in
      `runner-journal/`.
    - An existing target (C-62): after the held-lock apply, the test kills
      the child, runs the two `mkdir` lines, and places a file named like
      the first `.json` in the quarantine directory. The `mv -n` line for
      that file exits non-zero, and the placed file's sha256 is unchanged.
    - The step 7 checks (C-51): a `<run>.lock` that is a symlink to a path
      that does not exist is refused before `lock`, and the symlink target
      is not created. An existing `removed-<fingerprint>/` is refused, and
      nothing is renamed. `receipt.json` exists in the quarantine directory
      before the first rename (`os.rename` patched to assert it).
    - The put-back (C-45), in the same module: after an apply, the test
      follows `design.md` section 4.3 step by step. It inserts the rows from
      the chunk notes under `control_gate`, commits, then lists
      `removed-<fingerprint>/`, checks each listed file against
      `receipt.json`, and moves each `.json` and `.lock` back (C-59).
      `plan` has no entry, `_safe_state` does not raise, and `backup.run`
      completes. Control: the same put-back without the move leaves `plan`
      with no entry and `_safe_state` not raising, and `backup.run` raises
      "runner row ... differs from the backup journal" (C-55).
    - A home path with a space: every `move_commands` line splits back into
      the exact paths.
    - The quarantine is not read: a store with no removed run, and a valid
      journal `.json` for a run with no row placed in
      `runner-recovery-evidence/removed-<64 hex>/`. `plan` has no entry and
      no journal issue, and `backup.run` on that home completes with
      `runner-recovery-evidence` in its copied list.
    - The leftovers (C-3): the fixture also has each run's
      `retention.json`, a run `kept.tar`, an `archives/<generation>/` with
      `kept.tar` and `manifest.json`, and a
      `runner-reconciliation/<64 hex>.json` whose `run` is a removed run.
      After the apply, each file's sha256 is unchanged, the manifest lists
      each under `left:`, `plan` has no entry, and `_safe_state` does not
      raise.
  - Mutations, each must fail a named test above:
    - move the `ingest` call after the deletes (the id-reuse test on
      `item.id`);
    - drop A1 from the re-plan (the assignment id-reuse test);
    - drop `record=` from the `ingest` call (the marker test and the I9
      self-removal test);
    - drop the `transition` (the status test);
    - split the assignment delete into one statement per id in id order
      (the chain test);
    - remove the `foreign_key_check` (the forced-violation test);
    - move the G4 check after `backup.run` (the G4-before-backup test);
    - skip the read-only fingerprint compare before the backup (the
      stale-fingerprint-takes-no-backup test);
    - skip the in-transaction fingerprint compare, keeping the first one
      (the note-added-between-plan-and-apply test, with the note added
      after the first compare by a patched `control_gate`) (G3, C-26);
    - ignore `Snapshot.violations` (the violations test) (G5, C-26);
    - call `backup.run` without `database` (the backup-of-the-apply's-store
      test);
    - skip the journal move (the journal move test: `plan` has two entries
      and `_safe_state` raises);
    - move the journal files before the commit (the forced-violation test:
      the files leave `runner-journal/` while the rows stay);
    - move only `<run>.lock` (the journal move test);
    - drop `shlex.quote` from `move_commands` (the space test);
    - raise after the commit instead of returning `move_commands` (the
      held-lock test: the apply returns no record id and no lines);
    - put the quarantine directory inside `runner-journal/` (the journal
      move test: `plan` reports a journal issue, "unsafe, unowned, linked or
      oversized journal entry", because the entry is a directory);
    - catch only `Exception` after the commit (the interrupt test: the
      `KeyboardInterrupt` escapes and no lines are returned);
    - take the lock before the `.lock` check (the step 7 checks test: the
      symlink target is created);
    - use `os.makedirs(..., exist_ok=True)` for the quarantine directory
      (the step 7 checks test: the existing directory is accepted);
    - write `receipt.json` after the renames (the step 7 checks test);
    - check the G5 file name after `control_gate` (the in-memory test:
      `control_gate` is entered, and the message is "service controls
      require a file-backed database", not G5);
    - treat any exception out of the transaction as before the commit (the
      `COMMIT` interrupt test: `KeyboardInterrupt` escapes and the rows are
      gone);
    - restore the round 4 handler: catch `BaseException` and decide by
      `in_transaction` and the `external_id` row (the round 5 probe test:
      `apply` returns `move_commands`) (C-63);
    - decide by the `external_id` row without the kept id (the
      `KeyboardInterrupt`-beside-an-old-record test returns
      `move_commands`) (C-63);
    - skip G6 in the read-only plan (the second-apply test: `backup.run` is
      called) (C-64);
    - skip each of the three `stop()` checks, one at a time (its stop point
      test: `control_gate` is entered, `ingest` is called, or the apply
      commits) (C-67);
    - call `stop()` before each run's move (the after-the-last-check test:
      `move_commands` is not empty) (C-66);
    - set a `threading.Event` in the handler (the nested signal test: the
      child hangs and is killed) (C-65);
    - restore the handlers after the `yield` without `finally` (the nested
      signal test: the exception case keeps the new handlers) (C-69);
    - leave SIGHUP out of `signal_stop` (the nested signal test: SIGHUP still has
      its old handler inside the block) (C-66);
    - print the lines without `test ! -L <source>` (the dangling symlink
      test: the line exits 0) (C-70);
    - print `mv` without `-n` (the existing-target test: the placed file is
      replaced);
    - print the quarantine `mkdir` line before the
      `runner-recovery-evidence` line (the held-lock test: the first line
      fails).
  - Verify: `sh local-sd-db/sd-db.sh test -p test_removal.py -v` ends in
    `OK`, and `Ran N tests` equals `grep -c "def test_"
    local-sd-db/tests/test_removal.py`. Then
    `sh local-sd-db/sd-db.sh test -p test_controls.py -v` ends in `OK`, with
    `NoVerbNamesItsOperatorForTheCaller` among the passing tests. Then
    `sh local-sd-runner/runner.sh test -p test_removal_journal.py -v` ends in
    `OK`, and `Ran N tests` equals `grep -c "def test_"
    local-sd-runner/tests/test_removal_journal.py`. Against `origin/main`
    code (no `sd_db/removal.py`) the runner module fails to import, and with
    the skip-the-move mutation its journal move test fails on
    `_safe_state`.

## PR 3 — the CLI and the whole-suite check

- [x] **6. The CLI: `sd-db.sh item remove` and `sd-db.sh repo remove`.**
      Size: M.
  - Files: `local-sd-db/sd_db/jobs/cli.py` (`remove` in `command_repo`, a new
    `item` entry in `COMMANDS`), `local-sd-db/sd-db.sh` (add `item` to the
    arm that execs `sd_db.jobs.cli`, `sd-db.sh:211`; without it `item` falls
    to `*) usage; exit 1` at `sd-db.sh:250-252` (C-46); and the usage text of
    `usage()` at `sd-db.sh:64`),
    `local-sd-db/README.md` (one section: the verbs, the record, how to find
    a removed row, restore into a scratch home only, and the manual put-back
    path of `design.md` section 4.3: parent-first inserts in one transaction
    under `control_gate`, `foreign_key_check` before commit, and a note on
    the record; there is no re-insert verb; the journal files moved back
    after that commit, listed from the quarantine directory and checked
    against `receipt.json`, with `os.rename` in the same session, as in
    section 4.3 step 5 (C-45); and the recovery for an apply killed after
    its commit: create `runner-recovery-evidence` when it is absent, then
    the record's `to` directory, each with `mkdir -m 700`; then, for each
    listed file still in `runner-journal/`, `.json` first, run
    `mv -n <file> <to>/<name> && test ! -e <file> && test ! -L <file>`
    with each path quoted (C-70). A
    line that fails means the target exists: nothing is replaced, and the
    operator compares sha256 before anything else (C-48, C-58, C-62)). Also `local-sd-db/README.md:548-557`: one sentence
    that the remove verb moves a removed run's journal pair into
    `runner-recovery-evidence/removed-<fingerprint>/`, and that those
    directories are diagnostic, like the others. Also
    `local-sd-runner/README.md` (C-47):
    - `:153` "their external journals remain unchanged": add that
      `sd-db.sh item remove` and `repo remove` move a removed run's pair;
    - `:160` "Quarantine requires the daemon lock": say this is
      `recovery-quarantine`, and that the remove verb moves a released run's
      healthy pair under the per-run lock and `control_gate` without the
      daemon lock, because the live runner holds that lock
      (`runtime.py:814`) and nothing writes a released run's journal;
    - `:162` "Healthy JSON and lock entries cannot be quarantined": limit it
      to `recovery-quarantine`;
    - `:163`: name the second producer, `removed-<fingerprint>/` with its
      `receipt.json`.
    Also `local-sd-db/tests/test_cli.py`.
  - The CLI refuses a missing `--who` or `--reason`, and `--apply` without
    `--if-fingerprint`, before it opens the store. The preview prints rows,
    refusals, the `move` list of journal files moved after the commit (C-53),
    `left`, the note count and the fingerprint. Its last line is
    the apply command. It exits 0, 3, 4 or 1 (`design.md` section 6). After
    an apply, it prints the moved journal files, or the `move_error` and
    each `move_commands` line and exits 4. The apply
    passes `principal=getpass.getuser()`, `program="sd-db.sh item remove"`
    or `"sd-db.sh repo remove"`, and `session=os.environ.get("SD_SESSION")`.
    During `--apply` it wraps `apply` and the printing of its output in
    `with removal.signal_stop() as stop:` and passes `stop=stop` (C-48,
    C-57, C-65, C-66, C-69). `main` gives exit 1 for `RemovalInterrupted`,
    an `SdDbError`. The CLI calls `check_actor` before the preview (C-61).
  - Correct the warning's source set before the CLI prints it (the #350
    review, N-1). `removal.py:38` holds `IMPORTED = ("register", "drafts",
    "cron-report")`. The writers that can key an `item` row again are
    `docs/work`, `vault` and `register`, each a `SOURCE` constant in
    `local-sd-db/sd_db/sources/`, plus `cron-report` from `reporting.ingest`
    (`design.md` section 3, the D6 paragraph). `github-issues` and
    `index.sqlite` write `shadow` rows only, and `drafts` has no importer.
    Derive the importer names from the source modules rather than retyping
    them, so a new source cannot leave the list stale. Test: a fixture item
    on each of `docs/work`, `vault`, `register` and `cron-report` is
    reported; one on `drafts` and one on `github-issues` are not. Mutation:
    restore the literal tuple (the `docs/work` and `vault` cases fail).
  - Tests:
    - `item remove ID` without `--who` exits non-zero with a message naming
      `--who`, and the store file's mtime is unchanged.
    - A preview with a refusal exits 3. A clean preview exits 0 and prints
      the fingerprint. The printed apply command, run as given, exits 0 and
      files the report.
    - `SD_SESSION` of 201 characters: the apply exits 1 with the G4 message,
      and no snapshot directory is created. The preview with the same value
      also exits 1 with the G4 message, and prints no plan (C-67).
    - `repo remove PATH --with-items` on the probe fixture prints 15 rows.
    - With a retained clone directory present, the preview exits 3 and
      prints `chflags -R nouchg <path>` and `rm -rf <path>` for it. The test
      does not run them.
    - `sd-db.sh repo` with no verb names `add, seed, list, remove`.
    - `sd-db.sh item remove ID --who W --reason R`, run through the
      `sd-db.sh` entrypoint on a clean fixture, exits 0 and prints the
      fingerprint. `sd-db.sh item` with no verb names `remove` (C-46).
    - The signal handlers (C-48, C-57, C-66, C-69). Each case runs `main`
      in a child Python process that the test starts, with the patches
      applied inside the child and a 20-second timeout; on a timeout the
      test kills the child by pid. For each of SIGINT, SIGTERM and SIGHUP,
      `signal.raise_signal` from a patched `backup.run` makes `main` return
      1 with "interrupted before the commit", and every count except
      `state` is unchanged. The same signal from the `transaction` wrapper
      of step 5, right after the commit, makes `main` return 0: the rows are
      gone and the move finished. Raising the signal twice changes neither
      result. After `main` returns, in the exit-1 case, the exit-0 case and
      a case where a patched `ingest` raises `sqlite3.OperationalError`,
      the child prints the old handlers from `signal.getsignal`.
    - An interrupt as `COMMIT` returns: the step 5 wrapper raises
      `KeyboardInterrupt` right after the commit. `main` returns 4 and
      prints the lines (C-57).
    - The preview lists the journal files under `move`, before `left`
      (C-53).
    - An apply whose journal move fails (a child process holds the run's
      `.lock`) exits 4, prints the lock reason and the `mv` lines, and the
      rows are gone. The test does not run the lines.
  - Mutations: drop the early `--who` check (the mtime test fails); map
    refusals to exit 0 (the exit-3 test fails); map a non-empty
    `move_commands` to exit 0 (the exit-4 test fails); leave `item` out of
    the `sd-db.sh` arm (the entrypoint test exits 1 with usage); install
    handlers that raise `KeyboardInterrupt` instead of setting the flag (the
    before-commit signal case gets `KeyboardInterrupt` instead of 1); drop
    the `check_actor` call before the preview (the preview G4 test exits 0)
    (C-67).
  - Verify: `sh local-sd-db/sd-db.sh test -p test_cli.py -v` ends in `OK`,
    and `Ran N tests` equals `grep -c "def test_" local-sd-db/tests/test_cli.py`
    (33 on the merged head `a9b3f22f`, unchanged since `0b5394ae`, plus the
    new tests).

- [x] **7. Whole-suite and backup check.** Size: S (no new code).
  - Files: none, unless a test from an earlier step needs a fix.
  - Verify:
    - `sh local-sd-db/sd-db.sh test` on `origin/main` and on the branch head.
      The failing test names are the same, and the branch adds only the new
      tests. On the merged head `a9b3f22f` the whole suite printed
      `Ran 1111 tests in 139.532s`, then `OK`, in the `system-native
      (shared)` CI job (job 103997655345). The step reads the number again
      on its own base, because `main` keeps moving.
    - `sh local-sd-runner/runner.sh test` on `origin/main` and on the branch
      head: the same failing names, and the branch adds only
      `test_removal_journal.py`.
    - On a fixture home after an apply, `sd-db.sh backup` exits 0 and its
      summary line has no `BROKEN:` clause (`sd_db/jobs/backup.py:69-72`).
    - Pinned pack `sd-docs-lint` (the commit in
      `.github/workflows/system-native.yml`) is clean on the branch.
  - Record on sd:754: the PR number, head sha, the decisive `Ran N tests`
    lines from both suite runs, and NOT VERIFIED for anything not run.

## Rollback

- Revert the pull requests in reverse order: PR 3, then PR 2, then PR 1. No
  migration was added, so no schema step is needed. Records already filed
  stay as `done` report items.
- PR 1, with one precondition. First run
  `grep -rn "record=" local-sd-db/sd_db --include=*.py`, the same command as
  sd:755's Rollback. If a caller other than `sd_db/removal.py` passes
  `record=` (sd:755's `reporting.acknowledge_clean`), keep the `actor` and
  `record` keywords in `ingest`, their validation and their tests, and
  revert everything else PR 1 added. Otherwise revert PR 1 in full. Neither
  item's rollback removes a keyword the other uses (sd:755 round 2, C-18,
  C-28). If PR 1 added no `ingest` code (step 1 found both keywords
  present), there is nothing in `reporting.py` to revert.
- Journal files moved by an apply stay in
  `runner-recovery-evidence/removed-<fingerprint>/` after a revert. Move them
  back into `runner-journal/` only together with their rows (a full restore
  or the put-back path of `design.md` section 4.3). A file moved back
  without its row makes the blocked entry again.
- Verify: `sh local-sd-db/sd-db.sh test` has the same failing names as
  before PR 1, and while sd:755 is on `main`,
  `grep -c "actor=None" local-sd-db/sd_db/reporting.py` and
  `grep -c "record=None" local-sd-db/sd_db/reporting.py` each still print
  1, and `sh local-sd-db/sd-db.sh test -p test_report_bulk_acknowledge.py -v`
  ends in `OK` (C-71).

## Verification

Named before the work starts:

- **Correctness of the rows removed.** The probe fixture test (steps 3 and
  5) lists exactly 15 rows and removes exactly those rows. Failure: any other
  count, or any row left.
- **The record is complete.** The round-trip test (step 5) rebuilds every
  removed row from the chunk notes, equal column by column. Failure: any
  difference.
- **All or nothing.** The forced-violation and stale-fingerprint tests
  (step 5) leave every table count unchanged. Failure: any count change.
- **No id is reused.** The A1 tests (steps 2, 3 and 5). Failure: an
  assignment inserted after an apply gets a removed id.
- **Each refusal is real.** Every refusal in `design.md` section 3 has a row
  in a mutation table above, and each mutation fails its test. Failure: a
  mutation that leaves the module green.
- **The run was narrowed.** Every `Ran N tests` line equals the
  `grep -c "def test_"` count for the files it names. Failure: a larger N.
- **Size limits.** The chunking tests (step 4). Failure: a split line, a
  wrong `n of N`, or a plan over a limit that is not refused.
- **No recovery hold after an apply.** The journal move tests (step 5).
  Failure: a `reconciliation.plan` entry for a removed run,
  `_safe_state` raising, a failed move with no `mv` line, or exit 0 after a
  failed move (step 6).

Not verifiable in this item:

- The operator's hand removal of a retained clone (D4, option a). Tests
  check the printed commands and the refusal after a directory is removed.
  They never run `chflags` on a real retained volume.
- The journal move against the live `runner-journal/` with the runner
  running, and the next real archive refresh after it. Tests use a
  temporary home and call `_safe_state` directly.
- A remove against the live store with the dashboard and runner running.
  The live store is not touched by tests or lanes. Its first real use is an
  owner act, and the note on sd:754 must say so.
- The runner and dashboard holding no in-memory copy of `repo` or `item`
  rows. That rests on a grep (`prd.md` Assumptions).
- The lock time of a 16-note apply on a store the size of the live one. The
  round 1 reviewer measured 0.010-0.013 s for a 579-row simulated apply on a
  copy, not for a 16-note record.

## PR 2 landed: steps 4 and 5

Branch `feat/sd-754-pr2-apply` in system. Steps 4 and 5 are ticked above;
the code settled these points, each read against the plan text:

- `_record` in `local-sd-db/sd_db/removal.py` is one helper for both
  callers. A plan passes `who`, `reason` and `backup` as `None`, so their
  manifest lines are left out and R3 reserves `MANIFEST_HEADROOM`
  (two `MAX_ACTOR` plus 4096 bytes) for them; the apply passes the real
  values, and that manifest is what `reporting.ingest` stores. R1 to R3 are
  read in `_finish` after the fingerprint, like G6, because they derive from
  the same rows.
- `apply` reaches `backup.run` through
  `importlib.import_module("sd_db.backup")` (`backups` in `removal.py`),
  because `sd_db/__init__` binds the name `backup` to the function
  `backup.run`.
- Step 7 takes `runner-ending/<run>.lock` before the run's journal lock, and
  a held one fails the move with both possible holders named. Taking it
  creates that lock file for each removed run when it was absent; the runner
  ignores a `runner-ending/` entry with no row. Note 1966 Q3 is a refusal:
  a `<run>.partial` or any other `<run>.*` entry beside the pair stops the
  move before anything is renamed, and the lines are printed.
- The `KeyboardInterrupt` handler around the transaction is the round 5
  form: a commit only when this call's kept record id is set, no
  transaction is open, and a row with that id and the record's
  `external_id` exists. The runner module reproduces C-63 (a second apply
  after a put-back raises `WorkflowError` from `ingest` and prints no
  lines) and C-65 (a nested signal in `signal_stop` does not hang, in a
  child process with `threading.Condition.notify_all` patched to send the
  second signal).
- Plan divergences, each measured: the forced-violation test uses a
  `DEFERRABLE INITIALLY DEFERRED` child and patches `_unknown_children` off,
  because G7 (system #399) refuses an unknown child at plan time and an
  immediate constraint fails the delete before `PRAGMA foreign_key_check`
  runs; the dangling-symlink and `.partial` tests place the bad entry after
  the backup, because `backup.run` validates every journal entry and refuses
  before step 7 otherwise; the take-the-lock-first mutation fails the
  step 7 checks test on the `lock` call, not on a created target, because
  `runner_journal.lock` opens with `O_NOFOLLOW`; the id-reuse test holds the
  probe as the largest item id, so the newest assignment sits on an older
  item; A1 from the re-plan has its own test, with the newest assignment
  removed inside a patched `control_gate`.
- Every mutation in the step 4 table and the step 5 list fails its named
  test. Three of them (raise after the commit; catch only `Exception`; treat
  any exception as before the commit) let the `KeyboardInterrupt` escape the
  test runner, which aborts the run at the named test.
- Code review of PR 2 (system #407, Copilot, one pass): six findings held
  and are fixed with a test each. `_plan` refuses a kind other than `item`
  or `repo`; `_commands` gives no line when the evidence or quarantine path
  exists as anything but a directory of its own, and none when the plan
  moves nothing (the interrupt handler of an apply with no runs called it
  with `to` of `None`); the move takes only the planned members of a pair,
  and a `.lock` that `runner_journal.lock` created for a `.json`-only
  journal is unlinked under the flock; the apply checks its own manifest
  with `_record` before `ingest` and raises R3 inside the transaction, and
  `MANIFEST_HEADROOM` counts the three line prefixes; the `unittest.main()`
  guard of `test_removal.py` is at the end of the file (a direct run gave
  `Ran 74` before, `Ran 102` after). One finding is rebutted: `call` in the
  step 7 checks test is the comprehension's own variable, the test runs and
  passes, and mutation 5.20 fails on that assertion.

## PR 3 landed: steps 6 and 7

- System PR 3 (`feat/sd-754-pr3-verbs`, base `20c94970`, after #405 and
  #407) adds `repo remove` to `command_repo` and `item` to `COMMANDS` in
  `local-sd-db/sd_db/jobs/cli.py` (`_remove_options`, `_print_preview`,
  `_print_apply`, `_remove`), `item` to the dispatch arm and both verbs to
  `usage()` in `local-sd-db/sd-db.sh`, the `Retiring a row` section and the
  journal-pair sentence in `local-sd-db/README.md`, the C-47 sentences in
  `local-sd-runner/README.md`, and thirteen tests in
  `local-sd-db/tests/test_cli.py` (`RemoveCase`, `TheRemoveVerbs`,
  `TheSignalHandlers`, `InterruptAsCommitReturns`; `Ran 46`, equal to the
  file's `def test_` count, 33 before).
- Fail-first: the two verb arms were stubbed as usage-only exits first
  (`FAILED (failures=15)` for `test_cli.py`), then made green (`OK`).
  Every mutation in the step 6 list fails its named test; the mutation
  table is in the pull request body.
- Three residues of the #407 review round are fixed here, each with a
  test that failed first: `MANIFEST_HEADROOM` in `local-sd-db/sd_db/removal.py`
  reserves `4 * MAX_ACTOR` bytes for each of `who` and `reason`, because
  `check_actor` counts characters and R3 counts bytes
  (`test_the_headroom_holds_a_who_and_a_reason_of_200_non_ascii_characters`);
  `_commands` applies `_private` to an existing evidence or quarantine
  path and gives no line when it fails
  (`test_a_world_writable_evidence_directory_is_refused_and_gives_no_line`);
  `_move` takes `runner-ending/<run>.lock` for every removed run before
  anything else and lists each pair under those locks instead of taking
  the plan's list, so a released run whose pair is absent at re-plan time
  still waits for its ending sequence, and a pair `Runner.finish` persists
  after the re-plan is moved
  (`test_a_held_ending_lock_fails_the_apply_for_a_pair_less_run_too`,
  `test_a_pair_persisted_after_the_plan_is_listed_under_the_ending_lock_and_moved`).
  The receipt now names the files found under the locks; the failure lines
  name the plan's files until that listing is whole.
- Plan divergences, each measured: the step 6 `IMPORTED` sub-bullet was
  delivered by system #399 (`a24b6a7`) before this PR, and its derived
  tuple includes `github-issues` and `index.sqlite`, because their
  importers do upsert `item` rows by `(source, external_id)`; nothing here
  changes it. The pair-less-run tests unlink the pairs after the backup,
  because `backup.run` refuses a released run with no journal. The step 6
  list cites `local-sd-db/README.md:548-557` and `local-sd-runner/README.md`
  lines `:160`, `:162` and `:163` as they stood at `c7809f63` and `92efdbbf`;
  on this base those paragraphs sit later in each file, and the sentences
  went beside the same text. The `OperationalError` handler case lets the
  error out of `main` as every other verb does; the child records that the
  old handlers came back all the same. One sentence beside the printed
  `chflags` and `rm` lines names `runner.sh prune-apply` for a clone at
  least 30 days old (note 2135); the refusal itself is unchanged.
- Step 7: `sh local-sd-db/sd-db.sh test` and `sh local-sd-runner/runner.sh
  test` on the base and on the branch head, and the `backup` verb after
  an apply through the entrypoint
  (`test_the_backup_verb_passes_on_a_store_that_holds_a_record`). The
  `Ran N` lines are on sd:754 with the PR number and head.
- What is left after this PR is the closing paragraph's own list: the
  first remove against the live store is an owner act, and nothing in
  this item runs one.
- Code review of PR 3 (system #414, Copilot, one pass): two findings held
  and are fixed with a test each. A line break in `--who`, `--reason` or
  the target is refused before the store opens, because `shlex.quote`
  keeps it and the preview's last line would no longer be one line
  (`test_a_line_break_in_who_reason_or_the_target_is_refused_before_the_store_opens`);
  the preview opens the store with `connect(path, write=False)`, since a
  writable open sets the journal mode
  (`test_the_preview_opens_the_store_read_only_and_the_apply_for_write`;
  `Ran 48` for `test_cli.py`). Two README nits are taken: the
  `prune-apply` sentence names the `runner.sh prune` plan and the
  `--fingerprint`/`--who` arguments, and a failed `mv` line is read for
  its own error before the target collision is assumed.
- 2026-09-16, sd:968 (the #414 residue past the review cap): `_move` no
  longer holds every removed run's ending-lock fd for the whole move. The
  listing is `source:local-sd-db/sd_db/removal.py::_listed`, one ending
  lock taken and let go per run in plan order, and the move takes each
  pair's journal lock alone; nothing persists a pair for a run whose row
  is gone, because the ending sequence and the archive refresh read
  `run_state` first under that lock. A repo with more runs than the fd
  soft limit admits now moves every pair
  (`test_more_runs_than_the_fd_limit_admits_are_all_moved`, the limit
  lowered with `resource.setrlimit` inside the test). The post-commit
  `KeyboardInterrupt` branch of `apply` lists under the ending locks too,
  so a pair persisted between the re-plan and the interrupt gets its line
  (`test_an_interrupt_as_commit_returns_lists_a_pair_persisted_after_the_re_plan`);
  when that listing fails the lines name the plan's files as before. The
  `command_repo` docstring names `repo remove` beside the three verbs it
  listed.
