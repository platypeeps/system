---
title: tasks that recur
created: 2026-09-19
item: sd:1099
---
# Implement — tasks that recur

## Order

1. **Settle the dashboard question first.** Trace whether
   `local-project-dashboard` completes an item through `change_status` or
   reaches `transition` directly. This decides the insertion point and nothing
   else can be built confidently before it.
2. Migration `012_recurrence.sql`: two columns, `SCHEMA_VERSION` 11 → 12.
   011 is taken by `local-sd-db/sd_db/schema/011_judgment.sql`. Confirm the
   next free number against the schema folder before writing the file; the
   second-machine item also plans the next free slot. Move the last-migration
   reverse in the backup tests to the new migration.
3. Engine: `local-sd-db/sd_db/recurrence.py`, stdlib only. `parse` validates
   and canonicalises the subset and refuses every other part by name;
   `next_after` computes the next occurrence, skipping a month-end day a month
   lacks.
4. Library: validation of the rule and the anchor value in `_fields`; add both
   keys to `set_item_fields`'s allow-list, to `create_item` and to
   `RECURRENCE_FIELDS`, the set `_fields` accepts beside `USER_FIELDS`;
   `capture_task` takes both.
5. Wire into `change_status`, inside the existing transaction: compute the
   next due from the anchor, create the new row, move the rule to it, write a
   note on each row.
6. Return shape: `next_occurrence` on a completion that spawned.
7. CLI: `--recur` and `--recur-anchor` on `sd task add` and `sd task edit`,
   plus a way to clear them. These verbs live in the pack's `bin/sd_work.py`,
   not in this repository, so this step is a pack change against the library
   this item ships.

## Verification

Named before the work, not chosen after:

- **The late-completion test.** A weekly task due 2026-01-01 completed
  2026-01-22 yields 2026-01-08 under `schedule` and 2026-01-29 under
  `completion`. If both produce the same date, the anchor is not wired.
- **The double-completion test.** `sd task status <id> done` twice yields
  exactly one new row.
- **The dateless refusal.** A recurrence rule on a task with no `due` is
  refused at write time, not silently inert.
- **The subset refusal.** `FREQ=WEEKLY;BYDAY=MO` and `FREQ=DAILY;COUNT=3` are
  refused at write time with an error naming `BYDAY` and `COUNT`.
- **The month-end test.** `FREQ=MONTHLY;BYMONTHDAY=31` from 2026-01-31 yields
  2026-03-31, and `BYMONTHDAY=-1` from the same date yields 2026-02-28.
- **The reopen test.** Completing, reopening and completing again yields
  exactly one new row.
- **End to end, on the real backlog**: express the eleven uncovered
  site compliance obligations, then run `python3 tools/check-compliance-rows.py`
  in the `site` repo. It must report zero uncovered and exit 0. That script
  reads the register and the store independently, so it cannot pass by
  agreeing with the thing it checks.
- `sd-docs-lint` clean; the `local-sd-db` test suite green.

## Rollout order

Migration 012 raises the live store to schema version 12. `connect` in
`local-sd-db/sd_db/database.py` refuses a database newer than its library
with `SchemaTooNew`, and it does so for every open, **reads included**. So an
interpreter still holding an 11 build of `sd_db` stops reading the store the
moment `migrate` runs. The reverse gap is narrower: a 12 build on an 11
store reads, but refuses a write with `SchemaTooOld` until `migrate` runs.

Do these steps in this order:

1. **Back up the database:** `local-sd-db/sd-db.sh backup`.
2. **Merge** the pull request.
3. **Re-provision the pack's virtualenv** from the pack checkout:
   `bin/sd_install.py --provision-library`, which `make setup` runs too. It
   installs `sd_db` into `~/repos/platypeeps/sd-ai-command-pack/.venv`.
   That one interpreter also serves the runner and the dashboard, as found in
   their code:
   - `local-sd-runner/runner.sh` runs `SD_RUNNER_PYTHON`, defaulting to the
     pack's `.venv/bin/python`. Its README's installation boundary says to
     install the current `sd_db` wheel in the command pack's environment.
   - `local-project-dashboard/dashboard.sh` serves under
     `SD_DASHBOARD_PYTHON`, defaulting to the same interpreter.
     `local-project-dashboard/RUNTIME.md` says that interpreter must hold an
     installed `sd_db` whose schema matches the database, and that the
     dashboard installer never provisions or downgrades the library.
   - Neither pins a version of its own. Only an override pins: when
     `SD_RUNNER_PYTHON` or `SD_DASHBOARD_PYTHON` names another interpreter,
     install this build into that interpreter as well.
   - `DASHBOARD_PYTHON`, the interpreter for `tile` and `queue-open`, runs
     `sd_tile.py`. Neither it nor the `collectors.py` it loads imports
     `sd_db`, so it needs nothing here.
4. **Stop the runner and the dashboard, then run**
   `local-sd-db/sd-db.sh migrate`. Its help says to run it with both stopped
   and after `backup`. Restart both afterwards.

Between steps 3 and 4, writes fail with `SchemaTooOld` and reads work.
Reversing steps 3 and 4 makes every reader of the old build fail instead,
which is the worse gap. `sd-db.sh status` names the store's version and the
library's.

## BLOCKING

None. The dashboard's completion route is established:
`local-project-dashboard/sd_dashboard/server.py` calls `workflow.change_status`
and `local-project-dashboard/tests/test_criterion_7.py` asserts it, so
`change_status` is the insertion point. Carry a regression test that a
dashboard completion spawns the next occurrence, so the route cannot move
under the hook unnoticed.

## Log

2026-09-19 — filed. Prompted by the site compliance register, where
eleven of thirteen recurring obligations have no row and therefore cannot
reach any digest. The interim workaround is a checker that reports the gap;
this item removes the need for it.

2026-09-20 — renumbered the planned migration to `011_recurrence.sql`,
`SCHEMA_VERSION` 10 → 11 (sd:1153). The documents were written against
`SCHEMA_VERSION` 9 and claimed slot 010; `010_runner_merge.sql` landed first
(system #471) and took it. Planning documents only — no migration file was
written, renamed or moved, and `SCHEMA_VERSION` is untouched. The number is
still only a plan: confirm it against `local-sd-db/sd_db/schema/` at build
time, because another migration can land before this one does.

2026-09-23 — renumbered the planned migration to `012_recurrence.sql`,
`SCHEMA_VERSION` 11 → 12: `011_judgment.sql` landed and took 011 (sd:1099,
note 3728). Settled the rule engine as a stdlib RRULE subset — `FREQ`,
`INTERVAL`, `BYMONTH`, `BYMONTHDAY` — with every other part refused by name;
month end skips rather than clamps, and `BYMONTHDAY=-1` names the last day.
The rule moves to the new row on completion, which makes a reopen and
re-complete spawn nothing. The second-machine item plans the next free slot
too; whichever lands second renumbers.

2026-09-23 — built steps 2 to 6 in this repository: `012_recurrence.sql`,
`local-sd-db/sd_db/recurrence.py`, the refusals in `_fields` and
`_recurring`, and the spawn in `change_status`, which returns
`next_occurrence`. `local-sd-db/tests/test_recurrence.py` carries the
late-completion, double-completion, reopen, dateless, subset and month-end
tests, and the dashboard suite carries the route regression test. Still
open: step 7, the pack's `--recur` and `--recur-anchor` flags; a recurrence
control on the dashboard's details form; and the end-to-end check in the
`site` repository, which needs both the flags and the migrated live store.

2026-09-23 — review fixes for PR #542. Both anchors now walk one series
phased from the completed row's `due`; the completion anchor takes the first
occurrence after the later of `due` and the completion date. That fixes the
leap-day and `INTERVAL` re-phasing the review found, and stops an early
completion from producing a row due sooner. It also means the completion
anchor no longer drifts, against the PRD's prior-art example; `design.md`
records it and the operator confirms it. Added the rollout order above, and
tests for cancel, a rolled-back spawn and two concurrent completions.

2026-09-23 — reversed the anchor half of the entry above; the PRD wins. The
completion anchor restarts the series at the completion date again and
drifts by design, an early completion included. M1 is fixed at write time
instead: `_recurring` refuses a completion-anchored rule with `INTERVAL`
above 1 and `BYMONTH` or `BYMONTHDAY`, whose restarted phase can produce no
occurrence. The runtime `WorkflowError` stays as the backstop for a row
written around that check. The rollout order and the three tests stay.

2026-09-23 — the re-review found completion-anchored rules with
`INTERVAL=1` that still had no next occurrence, such as `FREQ=YEARLY;BYMONTH=2`
completed on a 30th. The completion date supplies the parts a rule leaves
out, so the check against `due` could not see them. The `INTERVAL` refusal is
replaced by a sweep over every start date in one leap cycle
(`first_barren_start`). It refuses the rules the old check refused, plus
these, and accepts sound rules the old check refused. `design.md` records the
measured cost.

2026-09-23 — the final review found completion-anchored rules that pass the
leap-cycle sweep and still find no occurrence, because 2100 is not a leap
year. The recurrence no longer blocks a completion: with no next occurrence,
`change_status` completes the row, creates nothing, clears the rule and notes
why. It returns `next_occurrence` None and `next_occurrence_reason`. The
runtime `WorkflowError` is gone. The sweep stays as a best-effort refusal at
`add` and `edit`.
