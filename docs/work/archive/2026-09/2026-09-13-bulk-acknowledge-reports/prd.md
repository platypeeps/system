---
title: an attributable bulk acknowledge for clean reports
created: 2026-09-13
status: ready
item: sd:755
---

# PRD — bulk-acknowledge-reports

Item: sd:755. Filed by the owner's decision on sd:743 (note 1762), which split
the missing bulk verb out of the kind-editing item.

## Problem

Reports can only be acknowledged one at a time. `sd reports acknowledge ID`
(pack `bin/sd_controls.py:37-39`) and the dashboard's
`/api/reports/<id>/acknowledge` (`local-project-dashboard/sd_dashboard/server.py:210-216`)
both call `reporting.acknowledge` for one item. Retention settles clean reports
in bulk, but only after seven days and only inside the nightly prune
(`retention.settle_clean_reports`, `local-sd-db/sd_db/retention.py:191-219`).

On 2026-09-13 an operator wanted the backlog cleared sooner. At
`2026-09-13T16:49:32+00:00`, 281 report items moved `planning -> done` in one
second. A direct library call did it, and it let `acknowledge` default `who`
to `"user"`. Every one of those status notes reads `planning -> done by user`
with `session` `user`. No record in the store names the process (sd:747, notes
1719 and 1734).

The batch itself was correct. It touched only clean reports. The 28 attention
reports were closed earlier by a named actor. The defect is that an unwanted
batch would look the same.

sd:747 removed the default (system #319), and sd:749 removed the other 44
(system #322). The same script now fails. The need behind it remains, and
nothing supported meets it. The same pattern appears in sd:743 and sd:744:
when a verb is missing, someone writes an unattributable script.

### What the store shows today

Measured read-only on 2026-09-14 at about 01:50Z. The queries are in
design.md, section "Evidence".

- Reports: 310. All 310 are `done`, and none are `planning`, so at that
  moment every selection rule below selected 0 reports. The figure is a
  snapshot of a store the cron jobs keep writing to, not a standing claim;
  the next bullet re-measures it.
- Re-measured at 13:28Z the same day, and again at 14:48Z: 312 reports, of
  which 2 are `planning`. #781 (`sd-db-prune`, created
  `2026-09-14T08:10:06+00:00`) is clean. #798 (`secret-scan-weekly`, created
  `2026-09-14T13:10:29+00:00`) has attention and an open followup. So
  `clean_reports` with `before` equal to now selects 1 report, and
  `--all-clean --before 2026-09-14` selects 0. The backlog this item answers
  is still the 281-row batch below, not the daily rate.
- Since sd:739, a quiet tick writes a heartbeat and no item. Reports arrive
  only for a failure, a declared attention marker or a recovery. At 01:50Z the
  newest report was created `2026-09-13T16:15:05+00:00`, and the oldest
  `cron-report:` heartbeat was `2026-09-13T16:30:04+00:00`. Compaction has
  since removed those heartbeat rows. At 14:48Z the oldest is
  `2026-09-13T18:35:02+00:00`.
- No report has yet been settled by retention (`by retention` status notes: 0).

At the moment of the batch the backlog was real. The store rebuilt at
`16:49:32` shows 281 `planning` reports. All were clean, with no open
followup. 176 were created before that UTC day began, and 140 were more than
24 hours old. None were more than 7 days old, so retention would not have
settled any of them before 2026-09-16.

## Requirements

The owner decided the scope on 2026-09-13. design.md, section "Decided", gives
D1 to D6.

1. One supported verb acknowledges many reports in one call. It lives in
   `sd_db`, and two callers use it: the pack CLI (D1) and a two-step form on
   the dashboard's Operations > Reports (D5).
2. The verb selects only reports that meet all of these conditions:
   `kind='report'`, `status='planning'`, `fields.attention` exactly false
   (JSON `false` or the number `0`, which `json_extract(...) = 0` reads the
   same way),
   no unresolved `followup` note, no queued, running or ending assignment,
   `report.ended` not later than the `ended` of the job's newest heartbeat by
   id (when the job has one), no `fields.record` marker, and `created_at`
   earlier than a cutoff. The caller must state the cutoff; there is no
   default (D2). The cutoff may not be in the future. A report outside that
   set is never moved. That includes a report whose `attention` key is
   missing or whose `fields` cannot be read. A report whose `fields` cannot be
   read is declined; it never makes the read fail.

2a. A person gives the cutoff as a date, `YYYY-MM-DD`, meaning 00:00 UTC on
   that date, on both surfaces. One library helper, `reporting.cutoff`,
   converts it to `YYYY-MM-DDT00:00:00+00:00`. The CLI, the dashboard preview,
   the dashboard apply form, its `data-cli` command and the plan token all
   carry that stamped value (design.md 2.1).

3. The first step writes nothing. It lists every report it would move and
   every report it declines, with a reason for each. "Declined" means a
   `planning` report created before the cutoff that is not selected. Reports
   created at or after the cutoff, and reports that are not `planning`, are in
   neither list (design.md 3). It also gives a plan
   token that covers the exact list and each listed item's current revision.
   On the CLI this step is the command without `--apply`. On the dashboard it
   is a GET preview.
4. The apply writes only when the caller passes that plan token. If the
   current selection, or any listed report's revision, no longer matches the
   token, the apply writes nothing and says so. On the dashboard, the refusal
   stays on the page, and a reload shows the preview again with the current
   selection.
5. A report that arrives or changes between the preview and the apply is not
   swept.
6. The caller must name `who`. The library has no default and refuses a blank
   value. On the CLI, `--who` is required with `--apply` (D4). On the
   dashboard, the apply form has a `who` field that a person types and that
   is required (design.md 7.5).
7. Each run also records the authenticated principal, the program, the pid,
   the parent pid and `SD_SESSION` when it is set. The principal is the login
   account on the CLI, and the dashboard's authenticated principal on the
   dashboard.
8. The apply is one transaction, and every report moves or none do. It refuses
   an empty selection and a selection of more than 1000 reports. At apply
   time, both refusals come back as a stale plan, because the preview for
   such a selection issues no token (design.md 5, step 3).
9. The run files one report item for the batch, as job `reports-acknowledge`,
   with the plan token as its run id (D3). That item lists every id that
   moved, `who`, the principal and the process. It carries
   `fields.record = "reports-acknowledge"`, so a later bulk run declines it,
   and the apply moves it to `done` in the same transaction, as sd:754 does
   with its records, so retention never settles it (design.md 5, 8). Each
   moved report's `status_change` note names that item.
10. Dashboard POSTs pass the existing same-origin and CSRF gate unchanged, and
    a POST without a valid token is refused with 403.
11. Retention keeps its current behaviour, with one deliberate change: a
    report whose `fields` is not valid JSON is skipped instead of failing the
    whole settle. The nightly prune report then names such reports, in a
    bounded list, and is filed with attention, so a person sees it
    (design.md 2). The verb never
    selects a report that
    retention's predicate would refuse at the same cutoff.

11a. There is no schema migration. `SCHEMA_VERSION` stays 9.

12. A replay of a cron run (`ingest_log` and `ingest`, as changed by system
    #326) never moves an acknowledged report out of `done`. It never adds a
    report to a plan that has already been issued.
13. The 281 `by user` rows of 2026-09-13T16:49:32Z are not written to (D6).

## Assumptions

These can be checked. None of them is a requirement.

- One transaction is small enough. On a copy of the live store, the 281-row
  apply held the write lock for 20 to 25 ms over three runs, and 23 to 33 ms
  over three runs of the shipped `reporting.acknowledge_clean` (design.md,
  section Evidence). The connection
  `busy_timeout` is 5000 ms (`local-sd-db/sd_db/database.py:68`). No run
  above 281 rows was measured.
- `created_at` is the right field for the cutoff, because retention uses the
  same field (`reporting.clean_candidates`, `reporting.py:280`).
- Reports stay rare while the sd:739 heartbeat shape holds.

## Acceptance criteria

- [ ] A new `sd_db` test builds a store with 7 reports, all created before
      the cutoff unless stated: 2 clean `planning`, 1 `planning` attention, 1
      clean `planning` with an open followup, 1 clean `planning` record report,
      1 clean `planning` created after the cutoff, and 1 clean `done`.
      `clean_reports` selects exactly the 2 clean ids. It declines exactly the
      attention, followup and record ids, with their reasons. The after-cutoff
      and `done` ids are in neither list. The store dump
      (`tuple(connection.iterdump())`) is unchanged.
- [ ] The same `clean_reports` call on a `sd_db.connect(path, write=False)`
      connection returns the same plan and raises nothing.
- [ ] `reporting.cutoff("2026-09-10")` returns `2026-09-10T00:00:00+00:00`.
      A bare date passed straight to `clean_reports` raises "carries no
      timezone". The CLI given `--before 2026-09-10` exits 0.
- [ ] An apply with that token moves exactly the 2 reports to `done`. Each has
      one `status_change` note ending `bulk acknowledge, report #<batch>`. The
      batch report's `fields.report.actor` holds `who`, `principal`,
      `program`, the pid, the parent pid and the session, and its body lists
      both ids. The batch report is `done` when the apply returns.
- [ ] Three changes after the preview each make the apply with the old token
      raise `StaleItem` and write 0 rows: a listed report gains an open
      followup; a listed report is acknowledged singly; a listed report is
      settled by `retention.settle_clean_reports`. A second apply of a plan
      that already succeeded also raises `StaleItem` and writes 0 rows.
- [ ] A report created after the preview (with `created_at` after the cutoff)
      is still `planning` after the apply.
- [ ] Omitting `who` or `principal` raises `TypeError`. `who=""` and
      `who="  "` each raise `WorkflowError`. A `before` later than now raises
      `WorkflowError`. `NoVerbNamesItsOperatorForTheCaller` still passes, and
      its walk includes the new functions and checks `principal` as well as `who`.
- [ ] A property test runs over random report fixtures with one cutoff. Every
      id the verb selects is also allowed by an independent oracle: a Python
      predicate in the test, and retention run on a copy at the cutoff plus 7
      days.
- [ ] A replay test bulk-acknowledges a recovery report and then replays its
      run through `ingest_log`. The report is still `done`, and no new item or
      followup is written.
- [ ] Dashboard: `GET /operations?area=reports&clean_before=<date>` returns 200
      and renders the selected count, the declined reasons and an apply form
      with hidden `before` (the stamped value `<date>T00:00:00+00:00`) and
      `plan`, and a required `who`. The store dump
      (`tuple(connection.iterdump())`) is unchanged across the request.
- [ ] Dashboard: `POST /api/reports/acknowledge-clean` with a valid CSRF token,
      `{before, plan, who}` returns 200, and its `item` is the batch report.
      The same POST with a stale plan, or with a selection that became empty,
      returns 409 with `"reload": true`. A blank `who` returns 400. A `before`
      that `cutoff` refuses returns 400 with a JSON error body. A POST without the token returns 403. Every
      refused POST leaves the store dump unchanged.
- [ ] `retention.prune` on a store with one `planning` report whose `fields`
      is not valid JSON, next to one clean report, settles only the clean
      report. The prune report has `fields.attention == True`, one open
      followup, and a text line that names the unreadable report's id. With
      300 such reports the prune still files its report: `attention_basis`
      names 20 ids and ends `and 280 more`, and the text names all 300. With
      no such report, the prune report's `fields` and text are as on the base
      commit.
- [ ] Each new test fails against origin/main code and passes on the branch.
- [ ] Pack: `sd reports acknowledge --all-clean --before DATE` exits 0 and
      leaves the store unchanged. With `--apply --if-plan TOKEN --who NAME`
      the reports move. `--apply` without `--who`, or without `--if-plan`,
      exits nonzero with the store unchanged.
- [ ] `sd-db.sh test`, `dashboard.sh test` and the pack `run-tests.sh` report no
      failing test names beyond those that already fail on their base
      commits.

## Out of scope

- Recording the process on every `transition`. That is sd:747 recommendation
  2 in general. This item records the process only for the bulk verb.
- A bulk verb for attention reports. Those wait for a person, which is the
  line that sd:739 drew.
- Any write to the 281 `by user` rows (D6).
- Retire paths (sd:744) and kind editing (sd:743, system #332).

## References

- `sd:755` — this item. The owner's decision note is dated 2026-09-13.
- `sd:747` — the 281-row batch and the `who` default. Notes 1719, 1725 and
  1734 hold the census and what the batch touched.
- `sd:743` — note 1762, the owner decision that filed this item.
- `sd:749` — no default `who` on any operator verb (system #322). Pack
  callers are #908 and #912.
- `sd:757` — report replay (system #326, `a75693d`). Its follow-ups are
  note 1807.
- `sd:739` — the quiet-tick heartbeat (system #315, `ac7afd0`). Note 1374
  drew the line between clean and attention reports.
- `local-sd-db/sd_db/reporting.py` — `ingest`, `_beat`, `ingest_log`,
  `acknowledge`.
- `local-sd-db/sd_db/retention.py` — `settle_clean_reports`.
- `local-project-dashboard/sd_dashboard/server.py:210-216`,
  `reports_screen.py:121-131` (the reports panel) and
  `reports_screen.py:134-147` (the single acknowledge form).
- Pack `bin/sd_controls.py` — `reports list`, `ingest` and `acknowledge`.

## Log

- 2026-09-13 created (planning only)
- 2026-09-13 owner decided D1 to D6; the dashboard form is in scope (D5)
- 2026-09-14 planning review round 1 (C-1 to C-14) addressed: one `before`
  contract, one `declined` definition, record reports, rollback
- 2026-09-14 planning review round 2 converged; C-15 to C-21 addressed: the
  batch record is filed `done`, the `cutoff` refusal maps to 400, and unreadable
  reports are named by the prune
- 2026-09-14 planning review round 3 converged; C-40 to C-44 addressed: the
  prune's id lists are bounded, and the sd:754 and `workflow.py` citations
  are re-anchored
- 2026-09-14 planning review round 4 found B-1 to B-3 after system PR 1
  landed: citations into `reporting.py` and `retention.py` re-anchored at
  head, the prune predicate matched to the shipped `coalesce` form, and
  section 3's "Today" column re-measured
- 2026-09-14 planning review round 5 (the last automatic pass) found B-1: the
  dashboard imports `sd_db` from the pack `.venv`, so the deploy re-provisions
  it from the system checkout HEAD before the restart, and the rollback
  re-provisions last
