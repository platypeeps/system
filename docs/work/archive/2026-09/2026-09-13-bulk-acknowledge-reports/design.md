# Design — bulk-acknowledge-reports

Item: sd:755. The owner decided the six open questions on 2026-09-13 (note
on sd:755). This design follows those decisions. The planning adversarial
review runs at the "prd and design" point in the pack's
`.claude/rules/sd-planning-adversarial-review.md`. Round 1 (2026-09-14) found
C-1 to C-14. Round 2 converged with C-15 to C-21, and round 3 converged with
C-40 to C-44, all LOW or INFO. Round 4 ran after system PR 1 merged and
found B-1 to B-3. Round 5 found B-1, the deploy order, and spent the cap.
This revision addresses all of them. `implement.md` gives the steps.

## Decided

Decided by the owner on 2026-09-13, on draft PR #334 head `b7b55416`.

- **D1. Build the library verb and the pack CLI now.** (OD1 A.) There are 0
  candidates today (Evidence). The verb exists so that the next backlog has a
  supported path, and the operator does not need a script.
- **D2. `--all-clean --before DATE`, with `--before` required.** (OD2 A.)
  `DATE` is compared with `created_at`, the field retention uses. It must not
  be later than the time of the dry run. There is no default cutoff. Section
  2.1 defines the one accepted format.
- **D3. One report item for the batch.** (OD3 C.) It is filed through
  `reporting.ingest` as job `reports-acknowledge`, and its run id is the plan
  token. Its body lists the ids. Its `fields.report.actor` holds `who` and
  the process. Its `fields.record` is `"reports-acknowledge"`, and the apply
  moves it to `done` in the same transaction (section 8).
  Each moved report's transition reason names that report item:
  `bulk acknowledge, report #<id>`.
- **D4. `--who NAME` is required with `--apply`.** (OD4 A.) The CLI still
  records the login account, but as the `principal`, not as `who`
  (section 5).
- **D5. A two-step dashboard form on Operations > Reports, in this item.**
  (OD5 B. The lane recommended A.) The first step is a preview. The second
  step acknowledges and posts the plan token. Section 7 covers the design.
  The form requires a `who` that a person types, and the server also records
  the authenticated principal. Section 7.5 gives the reason.
- **D6. No new write for the 281 rows of 2026-09-13T16:49:32Z.** (OD6 A.)
  sd:747 notes 1719, 1725 and 1734 stand as the record. That includes the
  selection `note.timestamp='2026-09-13T16:49:32+00:00' AND
  kind='status_change'` (items 43 to 742) and the finding that the actor is
  unknown. No note is rewritten or added.

## 1. Approach

System `local-sd-db/sd_db/reporting.py` gains three public functions, next to
`acknowledge`:

    reporting.cutoff(value: str) -> str
    reporting.clean_reports(connection, *, before: str, now: datetime | None = None) -> dict
    reporting.acknowledge_clean(connection, *, before: str, expected_plan: str, who: str,
                                principal: str, program: str, session: str | None = None,
                                now: datetime | None = None) -> dict

- `cutoff` turns what a person types into the one stamp that both surfaces
  pass on (section 2.1).
- `clean_reports` is the dry run. It reads the store, opens no transaction and
  never writes (section 4).
- `acknowledge_clean` is the apply. Inside `BEGIN IMMEDIATE` it runs the same
  selection again, compares the plan token, files the batch report (D3) and
  transitions each selected report (section 5).
- `now` is an aware `datetime` in both functions, the same type `ingest_log`
  and `retention.settle_clean_reports` take. `None` means
  `datetime.now(UTC)`.

Two callers use them. The pack CLI flags are in section 6. The dashboard's
two-step form is in section 7.

**There is no schema migration.** No table, column, index or trigger changes,
and `SCHEMA_VERSION` stays 9. The new values (`fields.record`,
`fields.report.actor`) are keys inside the existing `item.fields` JSON text.
The transition note keeps its format, and `reason` is an existing argument.

**Rejected: call `acknowledge` in a loop.** `acknowledge` checks followups
but never `attention`, so the loop would depend entirely on the caller's
selection. The bulk verb owns the selection. The apply calls `transition`
directly and adds no second followup check. The open-followup clause of the
selection runs inside the apply's own `BEGIN IMMEDIATE`, so a followup
cannot land between that check and the write. That is the same guarantee
`acknowledge`'s check gives, and the token (section 4) also refuses a
followup that landed after the preview.

**Rejected: shorten `CLEAN_REPORT_AGE`.** That changes the nightly job for
everyone. It is also not an attributable operator act, because those notes
read `by retention`.

## 2. The selection, shared with retention

The base predicate is retention's. System PR 1 (#347, `e377da7`) moved it out
of `retention.settle_clean_reports` and into one function,
`reporting.clean_candidates(connection, *, before)`, that both callers use
(`local-sd-db/sd_db/reporting.py:277-283`). It returns ids in id order:

    kind='report' AND status='planning'
    AND CASE WHEN json_valid(fields)
             THEN json_extract(fields,'$.attention') END = 0   -- NULL is not clean
    AND created_at < :before
    AND NOT EXISTS (open followup note)

- `clean_candidates` opens no transaction and no savepoint. Its caller owns
  the read. `retention.settle_clean_reports` keeps its `with
  transaction(connection)` around the call, so its read still takes the write
  lock first (`retention.settle_clean_reports`, `retention.py:215-218`). The
  dry run reads under one savepoint (section 4).
- `retention.settle_clean_reports` calls it with `before = now - 7 days`.
- **One deliberate change to retention: the `json_valid` guard.** Today
  `json_extract` on malformed `fields` raises "malformed JSON" for the whole
  query, so one bad row stops the nightly settle. With the guard, that row is
  not a candidate, which is the answer retention already gives a row with no
  `attention` key. The guard is the `CASE WHEN json_valid(...) THEN
  json_extract(...) END` form that `_beat` uses (`reporting._beat`,
  `local-sd-db/sd_db/reporting.py:152-153`). A `CASE` evaluates `json_extract`
  only when `json_valid` is true, whatever order SQLite picks for `AND`
  terms, so the guard does not depend on the query plan. The live store has 0
  such report rows (Evidence). The `settle_clean_reports` docstring
  (`retention.settle_clean_reports`, `retention.py:208-209`) gains one
  sentence for the guard. Every other retention behaviour, and every existing
  `local-sd-db/tests/test_retention.py` case, is unchanged.
- **An unreadable report does not sit in `planning` silently.** Today the
  failure is loud: the prune raises, no `sd-db-prune` report is filed, and
  the cron job fails. With the guard, the prune stays loud in a different way.
  After the settle, `retention.prune` reads `kind='report' AND
  status='planning' AND coalesce(json_valid(fields), 0) = 0`
  (`retention.prune`, `retention.py:243-249`). The `coalesce` is not
  decoration: `json_valid` answers `NULL` for a `NULL` `fields`, so
  `NOT json_valid(fields)` is `NULL`
  for that row and counts it out. Measured in SQLite 3.51.0:
  `SELECT json_valid(NULL) IS NULL, (NOT json_valid(NULL)) IS NULL,
  coalesce(json_valid(NULL),0)=0;` gives `1|1|1`. A report with no `fields` at
  all is exactly the row this bullet exists to surface, so the predicate has
  to count it. When the count is above 0, the prune report is filed with
  `attention=True`. So `ingest` adds a
  followup, and the report shows as needing a person. When the count is 0,
  the text, the `removed` counts and `attention` are exactly as today.
- **Both id lists are bounded.** The ids are read in id order.
  - The report text gains one line, `N report(s) in planning have unreadable
    fields: #id, ...`. It names at most the first 1000 ids
    (`UNREADABLE_TEXT_IDS`), and then `and M more`.
  - `attention_basis` is the same line, but it names at most the first 20
    ids (`UNREADABLE_BASIS_IDS`), and then `and M more`.
  - `ingest` refuses an `attention_basis` longer than 2000 characters
    (`reporting.ingest`, `local-sd-db/sd_db/reporting.py:61`), and the
    followup text repeats the basis (`reporting.ingest`,
    `reporting.py:101`). An unbounded list passes that limit at about
    280 four-digit ids. The prune would then raise after its expiry,
    compaction and settle had committed, each in its own transaction
    (`retention.prune`, `retention.py:237-239`). The backup job catches the
    `SdDbError`, prints "the prune did not run" and exits 1, every night
    (`local-sd-db/sd_db/jobs/backup.py:91-105`).
  - 20 ids of up to 19 digits, the prefix and `and M more` stay below 600
    characters. 1000 such ids stay below 25 KB, far inside `MAX_REPORT`
    (200 KB). The bulk preview also
  lists such a report as declined with its reason (section 3).
- The bulk verb calls it with the caller's `before` and then applies the
  extra refusals in section 3. The bulk selection is therefore always a
  subset of what retention would select at the same cutoff. A property test
  against an independent oracle pins this (implement step 5).
- `reporting.reports()` is not used. It caps at 200 rows
  (`reporting.reports`, `reporting.py:233-235`).

### 2.1 The one `before` contract

A person types a date, `YYYY-MM-DD`, and it means 00:00 UTC on that date.
Both surfaces convert it with one library helper, `reporting.cutoff(value)`:

- `YYYY-MM-DD`, a real calendar date, returns `YYYY-MM-DDT00:00:00+00:00`.
- `YYYY-MM-DDT00:00:00+00:00`, exactly that shape, returns itself. This lets
  the stamped value travel through the hidden field and the `data-cli`
  command and come back in unchanged.
- Anything else raises `WorkflowError("give the cutoff as a date, YYYY-MM-DD,
  meaning 00:00 UTC")`. That includes a time other than midnight, another
  offset, `Z`, and a date that does not exist.

Who calls it:

- The CLI calls `cutoff(args.before)` before it connects.
- The dashboard GET calls `cutoff(clean_before)` before it reads.
- The dashboard POST route calls `cutoff(values["before"])`.
- The hidden `before` field, the `data-cli` string and the plan token all
  carry the stamped value, never the bare date.

`clean_reports` and `acknowledge_clean` take the stamped value. They pass
`before` through `writes.stamp`, which refuses a bare date because it
"carries no timezone" (`writes.stamp`, the raise at
`local-sd-db/sd_db/writes.py:70-75`). So a caller that
skips `cutoff` fails loudly, and it cannot get a different cutoff by
accident. Implement steps 3, 11 and 16 each have a verify that fails if a
bare date reaches `writes.stamp`.

## 3. What is declined

`declined` has one definition, used by the prd, this section, section 5 and
implement step 3:

> `declined` lists every report with `kind='report'`, `status='planning'` and
> `created_at < before` that is not in `selected`. Each entry has the first
> reason from the table below that applies.

It never lists these two groups:

- **A report created at or after `before`.** The caller's cutoff already
  excludes it, so there is no decision to show. Listing it would also make the
  list grow with every new report.
- **A report whose status is not `planning`.** Someone or something moved it
  already, and the verb only moves `planning` reports. The live store holds
  310 such rows (all `done`), and listing them would bury the real refusals.

The reasons, in the order they are tested:

"Today" was measured at 2026-09-14T14:13Z by running the shipped
`reporting.clean_reports` on a read-only copy of the live store, with
`before` = now. It selected 1 (#781) and declined 1 (#798, attention). With
`before = 2026-09-14` every row is 0, because both `planning` reports were
created today.

| Declined, with reason | Why | Rows at the batch instant (16:49:32) | Today (14:13Z, cutoff = now) |
|---|---|---|---|
| `fields` is not valid JSON | It cannot say it is clean. It is declined, not an error (section 2). | 0 | 0 |
| `fields.record` is set: `CASE WHEN json_valid(fields) THEN json_type(fields,'$.record') END IS NOT NULL` | An operator record, not a run report: an earlier batch (`"reports-acknowledge"`) or an sd:754 record (`"item-remove"`, `"repo-remove"`). The check is whether the key exists, not its value. Section 8. | 0 | 0 |
| `fields.attention` true | It waits for a person (the sd:739 line). | 0 | 1 (#798 `secret-scan-weekly`) |
| `json_extract(fields,'$.attention')` is not 0: the key is missing, null or another value | It never said it was clean. Matches retention. | 0 | 0 |
| An unresolved `followup` note | Matches `acknowledge`. | 0 | 0 |
| A queued, running or ending assignment on the report | Work is in flight. | 0 (no report has one) | 0 |
| `report.ended` later than the `ended` of the job's newest heartbeat | The job's recorded health has not caught up. A bad stamp or a late older run can cause this. | 0 of the 104 with a heartbeat | 0 |

- The first five reasons and the assignment reason are one SQL read
  (`reporting.py:352-365`) of the reports created before `before`, with `CASE WHEN json_valid(fields) THEN ... END` around every
  `json_extract` and `json_type`, as `_beat` does (`reporting._beat`,
  `reporting.py:152-153`). So no row can fail the query.
- **"The job's newest heartbeat" is the newest row by `id`** for key
  `cron-report:<job>`. That is the row `health` and `_beat` read
  (`reporting.health`, `reporting.py:112-115`; `reporting._beat`,
  `reporting.py:156-157`), so the refusal and the job's recorded health use
  the same row. A heartbeat body that is not valid JSON,
  or has no `ended`, counts as no heartbeat. The Evidence query was re-run
  with this rule and gives the same counts as `max(ended)`: 177, 0 and 104.
- **A job with no heartbeat does not decline its reports.** At the batch
  instant, 177 of the 281 reports belonged to jobs with no `cron-report:`
  heartbeat. At 01:50Z the oldest heartbeat was `16:30:04`. `sd-db-prune` files through
  `ingest` and never writes a heartbeat. Since #326, `ingest_log` writes a
  run's heartbeat in the same transaction as its report, so the heartbeat
  rule catches only the cases in the table.

## 4. The dry run and the plan token

`clean_reports` does this:

1. It stamps `before` with `writes.stamp` and refuses a `before` later than
   `now` with `WorkflowError`.
2. It opens no transaction. It runs every read (candidates, refusals and
   revisions) between `SAVEPOINT sd_clean_reports` and `RELEASE
   sd_clean_reports`, the way `workflow.item_state` reads between
   `SAVEPOINT sd_workflow_state` and its `RELEASE`
   (`local-sd-db/sd_db/workflow.py:110-117` at `b420bea`). On a connection with no open
   transaction, the savepoint starts one read transaction, so all rows come
   from one snapshot. `item_state`'s own savepoint nests inside it. A
   `SAVEPOINT` works on a `write=False` connection, and `BEGIN IMMEDIATE`
   does not (`database.py:125-126`). Both callers of the dry run open
   `write=False`.
3. It returns:

       {"before", "selected": [{"id", "revision", "job", "created_at", "title"}],
        "declined": [{"id", "why"}], "count": N, "declined_count": M,
        "plan": <64 hex> or None, "max_batch": 1000}

When N is 0 or more than `MAX_BATCH`, no apply can succeed. The function then
skips the revision reads, sets `plan` to `None` and lists `selected` without
revisions. That bounds the work of a preview that cannot be applied. The
dashboard renders at most the first 200 rows of each list, with the full
counts (section 7.1).

**The plan token** is `sha256` over the stamped `before` and the sorted list
of `(id, revision)` pairs. `revision` is `workflow.item_state`'s hash of the
row and its notes (`workflow.item_state`, `workflow.py:100-120` at `b420bea`). The token changes when any of these
happens:

- a report joins or leaves the selection;
- a listed report's row or notes change. That covers a new followup, a
  comment, a status move and a retention settle.

A `before` later than `now` is refused. A report filed after the preview
normally has `created_at` equal to the time of filing, which is later than
`before`, so it is outside the selection. A report committed after the
preview with an earlier `created_at` (a direct `create_item(created_at=...)`
call, for example) can join the selection. The token catches that case,
because the membership changed. The token, not the cutoff, is the guarantee.

## 5. The apply

Everything below runs inside one `transaction(connection)`, which is
`BEGIN IMMEDIATE` (`database.py:116-136`).

1. Refuse an `expected_plan` that is not a `str` of 64 lowercase hex
   characters with `WorkflowError("preview the clean reports and pass its
   plan")`. Validate `who`, `principal` and `program` with `workflow._text`. That
   refuses blank values, which closes sd:749's NB2 for this verb. `who`,
   `principal` and `program` are at most 200 characters, and a longer value
   is refused. `session` is optional. It is not a name the caller chooses, so
   a value longer than 200 characters is truncated to 200 and the apply goes
   on. A blank `session` is stored as `None`.
2. Stamp `before`. Recompute the selection and the token with the same code
   as the dry run. Inside the transaction, the dry run's savepoint nests. If
   the token differs from `expected_plan`, raise
   `workflow.StaleItem("the clean-report selection changed since the preview;
   preview it again")`. Nothing has been written at this point.
3. The empty-selection and `MAX_BATCH = 1000` refusals reach the caller
   through step 2, as `StaleItem`. When N is 0 or above `MAX_BATCH`,
   `clean_reports` sets `plan` to `None` (section 4). Step 1 admits only a
   64-hex `expected_plan`, so it never equals `None`, and the apply raises
   `StaleItem` at step 2. The caller previews again, and the preview shows
   "nothing to acknowledge" or "more than 1000" with no apply form. That is
   the intended answer: the store changed since the preview that issued the
   token. After step 2, the apply still checks `1 <= N <= MAX_BATCH` and
   raises `WorkflowError` if not. Only a defect in the token code can reach
   that check.
4. File the batch report (D3):
   `ingest(job="reports-acknowledge", run_id=<plan>, started=stamped_now,
   ended=stamped_now, exit_code=0, text=<ids, one per line>, source_path=program,
   attention=False, actor={...}, record="reports-acknowledge")`.
   - `ingest` builds a fixed provenance dict (`reporting.ingest`,
     `reporting.py:79-81`). It gains two optional keywords. `actor` is validated and stored as
     `fields.report.actor`, the same way `removed` is stored. `record=None`:
     when given, it must be a `str` that matches the job-name pattern. It is
     stored as top-level `fields["record"]`, next to `attention` and
     `report`. Calls without them
     produce byte-identical `fields`.
   - sd:754 needs the same two keywords (system #336, design section 9, head
     `9b78110b`). Both plans use one conditional form: add `actor` and
     `record` only if they are absent. The item that lands first adds both,
     with these names, this placement, this validation and its tests. The
     second item finds them, uses them and does not change them.
   - `stamped_now` is `now.astimezone(UTC).isoformat(timespec="seconds")`.
     `writes.stamp` takes a string, not a `datetime`, and `retention.prune`
     stamps its `now` the same way (`retention.prune`, `retention.py:236`).
   - `actor` is `{who, principal, program, pid: os.getpid(), ppid: os.getppid(), session}`.
5. For each selected id, in id order, call
   `transition(connection, id, "done", who=who, reason=f"bulk acknowledge, report #{batch}")`.
   The note reads `planning -> done by operator: bulk acknowledge, report #812`,
   and its `session` column is `who` (`writes.py:231-236`). There is no second
   followup check here (section 1).
5a. Move the batch report to `done` in the same transaction:
   `transition(connection, batch, "done", who=who, reason="bulk acknowledge record")`.
   This matches sd:754, which files its records `done` in the transaction
   that files them (system #336, head `9b78110b`). So a batch record never
   shows as a `planning` report, and retention does not settle it while it
   is `done` (section 8).
6. Return `{"item": <batch report row>, "revision": <its revision>,
   "acknowledged": [ids], "actor": {...}}`. The batch report row is read after
   step 5a, so its status is `done`. The dashboard's form script
   redirects to `/item/<id>` whenever `result.item` is an object
   (`static/dashboard.js`, the submit handler's `window.location.assign`).
   So a dashboard apply lands on the batch report.

Any exception rolls the whole transaction back (`database.py:129-135`).

**Why all or nothing.** Skipping stale rows would turn the reviewed list into
a different list without showing it. A new preview costs milliseconds.

**Size.** On a copy of the live store, a 281-row apply held the write lock
for 20 to 25 ms with a hand-written probe, and for 23 to 33 ms with the
shipped `acknowledge_clean` (Evidence). The `busy_timeout` is 5000 ms
(`database.py:68`), so a cron `ingest_log` that arrives during an apply
waits and does not fail. `MAX_BATCH = 1000` guards against a mistake. It is
not a measured limit. At 1000 ids the body is about 5 KB, well under
`MAX_REPORT` (200 KB).

**No `control_gate`.** `operations.control_gate` serializes service and
install controls that act outside SQL. This verb writes only SQL rows, and
`BEGIN IMMEDIATE` already serializes it with every other writer. That matches
`reporting.acknowledge` and `retention.settle_clean_reports`, which take no
gate either.

**Who and principal.** `who` is the name the caller states: required, with no
default. `principal` is what the caller's channel authenticated: required,
with no default. The CLI passes the login account (`getpass.getuser()`). The
dashboard passes `Context.principal`: `"local"` on the loopback listener, or
the verified Tailscale login on the remote or direct listener
(`auth.py:129-172`). The two are recorded separately, so a stated name never
replaces the login and the login never replaces the stated name. Recording
the process on every transition is sd:747 recommendation 2 and stays out of
scope.

**Replaying the same plan.** The plan is the batch report's run id. A second
apply of an applied plan fails at step 2 with `StaleItem`. Every listed report
is now `done`, so the selection changed: it is empty (plan `None`) or holds
other reports. On the
dashboard that is a 409. If it somehow reached step 4, `ingest` would refuse
it with "different evidence".

## 6. CLI surface (pack)

`bin/sd_controls.py`, `reports acknowledge`:

    sd reports acknowledge ITEM [--if-revision REV]                                   # unchanged
    sd reports acknowledge --all-clean --before DATE [--json]                         # dry run
    sd reports acknowledge --all-clean --before DATE --apply --if-plan TOKEN --who NAME [--json]

- `DATE` follows section 2.1: `2026-09-13`, or `2026-09-13T00:00:00+00:00`.
  The CLI calls `reporting.cutoff` before it connects. A bad value exits
  nonzero with the helper's message and no traceback.
- `item` becomes optional. The caller gives exactly one of `ITEM` or
  `--all-clean`. Any other combination is refused before the store is opened.
- `--apply` without both `--if-plan` and `--who` is refused before the store
  is opened (D4).
- The dry run opens the store read-only, the way `list` does
  (`bin/sd_controls.py:21`).
- The apply passes `principal=getpass.getuser()`,
  `program="sd reports acknowledge"` and `session=os.environ.get("SD_SESSION")`.
- Errors map to `WorkRefusal` as today (`bin/sd_controls.py:45-46`).
- Human output lists the selected and declined rows. Its last line is the
  exact apply command, with the stamped `before`, the token filled in and
  `--who NAME` left for the caller.

The pack's existing `tests/test_sd_controls.py`
`test_report_ingestion_is_replayable_and_acknowledged` has been updated for
the sd:739 heartbeat shape and covers the single-item verb. The bulk flags get
their own test beside it.

## 7. Dashboard surface (system)

### 7.1 Step one: the preview is a GET

Operations > Reports (`reports_screen.reports_panel`) gains a section,
"Acknowledge clean reports", above the listing. It holds a plain GET form. It
has no `data-workflow-form`, so the form script leaves it alone:

    <form method="get" action="/operations">
      <input type="hidden" name="area" value="reports">
      <label for="clean-before">Created before (00:00 UTC on)</label>
      <input type="date" id="clean-before" name="clean_before" required>
      <button type="submit">Preview clean reports</button>
    </form>

The preview writes nothing. That is why it is a GET and not a POST route. A
GET opens the store with `write=False` (`server.py:357`), and it never
reaches `observed_action`. The page renders the plan from the same
`clean_reports` call the CLI uses, so the plan is never computed separately.

Two code changes carry the clock to the panel:

- `operations_screen.operations_page` passes its `now` to the panel:
  `reports_panel(connection, parameters, now=now)`. Today the call has no
  `now` (`operations_screen.py:274`).
- The dashboard clock is a `...Z` string (`server.py:84-85`). The panel
  converts it once with `datetime.fromisoformat(now)`, which gives an aware
  UTC `datetime`, and passes that to `clean_reports`.

When `clean_before` is present, the panel handles it as follows:

- It calls `reporting.cutoff(clean_before)` (section 2.1). A value that
  `cutoff` refuses, or a date whose midnight is later than `now`, renders a
  notice and no apply form. The notice text is the `WorkflowError` message.
- It calls `reporting.clean_reports(connection, before=<stamped>, now=<datetime>)`.
- It renders two lists. "Would acknowledge (N)" shows id links to
  `/item/<id>`, the job and `created_at`. "Declined (M)" shows the id and the
  why. Each list renders at most its first 200 rows and says how many more
  there are.
- If `plan` is `None` (N is 0 or more than `MAX_BATCH`), it still renders
  both lists, and adds the reason and no apply form.

### 7.2 Step two: the apply form carries the plan in hidden fields

    form("/api/reports/acknowledge-clean",
         tag("input", type="hidden", name="before", value=stamped_before),
         tag("input", type="hidden", name="plan", value=plan),
         field("Acknowledged by", "who", required=True, maxlength=200, autocomplete="off"),
         label=f"Acknowledge {n} report(s)",
         command=f"sd reports acknowledge --all-clean --before {stamped_before} --apply --if-plan {plan} --who NAME",
         reload_label="Preview again")

- `stamped_before` is the value `cutoff` returned, for example
  `2026-09-13T00:00:00+00:00`. The characters `+` and `:` need no shell
  quoting, so the `data-cli` string can be pasted as it is.
- `controls.form` (`controls.py:33-42`) already renders the result region
  and `data-cli`. It gains one optional keyword, `reload_label=None`. When
  it is given, the form element gets `data-reload-label`. Every existing call
  omits it, so their markup does not change.
- The form script posts every `FormData` entry as JSON, so the body is exactly
  `{"before", "plan", "who"}`. This is the same way a single acknowledge
  posts `revision` today (`reports_screen.py:146`). No script change is
  needed to send it.

### 7.3 The route

`action_route` gets one exact-path branch, next to the single-report branch
(`server.py:210-216`), and one new keyword, `principal`:

    if path == "/api/reports/acknowledge-clean":
        from sd_db import reporting

        if (set(values) != {"before", "plan", "who"}
                or not all(isinstance(values[key], str) for key in values)
                or not re.fullmatch(r"[a-f0-9]{64}", values["plan"])):
            raise ValueError("Preview the clean reports again, and name who is acknowledging them.")
        return lambda connection: reporting.acknowledge_clean(connection,
            before=reporting.cutoff(values["before"]), expected_plan=values["plan"],
            who=values["who"], principal=principal, program="dashboard")

- `cutoff` runs inside the returned function, not in the `action_route` body.
  The `try` around the `action_route` call catches only `ValueError`,
  `UnicodeDecodeError` and `NotFound` (`server.py:530-535`), and
  `WorkflowError` is not a `ValueError`. So a `WorkflowError` raised in the
  body would leave `do_POST` with no response. Inside the function, the
  second `try` maps `WorkflowError` to 400 (`server.py:550-551`), and
  `StaleItem` to 409 before it (`server.py:548-549`).
- `do_POST` passes `principal=context.principal` to `action_route`. That is
  the only change to the request path. `do_POST` (`server.py:530`) is the
  only caller of `action_route`, so `principal` is a required keyword with no
  default, matching `who`.
- The path is exact and has no digits, so it cannot collide with
  `/api/reports/([1-9][0-9]*)/acknowledge`.

### 7.4 Same-origin, CSRF and exposure

This work adds no new mechanism. The POST passes through the existing
`do_POST` gate (`server.py:504-520`), which checks all of the following:

- one `Origin` equal to the context origin;
- `Sec-Fetch-Site` `same-origin` when present;
- exactly one `X-SD-CSRF`, equal to the HMAC of the `SameSite=Strict`,
  `HttpOnly` session cookie;
- `application/json`;
- a bounded `Content-Length`.

**The route adds no new exposure.** Every POST route is reachable only through
this gate, and from a Tailscale peer only as the one `operator_login`
(`auth.py:129-172`). Existing writes, such as a service stop or a runner
cancel, are already reachable the same way. The new route writes less than
those do: it moves clean reports that retention would settle within 7 days.

The GET preview writes nothing, so it needs no token. It still renders the
`sd-csrf` meta tag that the apply form's script reads (`server.py:420-421`).
The existing test loop that posts each action path without a token and
expects 403 (`local-project-dashboard/tests/test_controls_actions.py:55-56`)
gains the new path.

### 7.5 How the dashboard names `who`

**Decided in this design: a person types a `who`, and it is required. The
server also records the authenticated principal and `program="dashboard"`.**
This is not a new owner question. It applies D4's reasoning to the second
surface:

- The literal `"dashboard"` names a channel, not a person. D5's decision note
  says it is weaker attribution.
- `Context.principal` alone cannot be `who`. On the loopback listener it is
  `"local"`, which names nobody. On the remote listener it is the one
  `operator_login`, which is the same for the owner and for an agent that
  drives a browser as the owner.
- A typed name is self-declared. Recording it next to the authenticated
  principal, the server pid and `program` makes a false name visible
  afterwards, just as the CLI's login makes one visible.

Rejected: prefill `who` with the principal. That would put `"local"` in the
field on the loopback listener, and a prefilled field gets submitted without
anyone reading it.

### 7.6 A stale plan

- If anything changed between the preview and the apply, `acknowledge_clean`
  raises `workflow.StaleItem`. `do_POST` already maps that to
  `409 {"error": ..., "reload": true}` (`server.py:548-549`).
- The form script writes the error into the form's result region, and it adds
  a reload link to `pathname + search` (`static/dashboard.js`, the
  `result.reload && !capture` branch).
- That URL still carries `area=reports&clean_before=...`, so the reload shows
  the preview again with the current selection and a new token. The refusal
  text stays on screen until the reload.
- One script change: the link text is "Reload current item", which is wrong
  here. The branch reads the form's optional `data-reload-label`, and this
  form sets it to "Preview again" (section 7.2). Every other form keeps its
  text.

An empty selection and a selection above `MAX_BATCH` also return 409 with
`"reload": true`, because the apply raises `StaleItem` for them (section 5,
step 3). "Preview again" then shows the reason and no apply form.

These refusals return 400 and the error text in the same result region:

- a body with other keys, a non-string value or a plan that is not 64 hex
  characters (`ValueError` in the `action_route` body);
- a blank `who` (`WorkflowError`);
- a `before` that `cutoff` refuses (`WorkflowError`, raised inside the
  returned function);
- a `before` in the future (`WorkflowError`).

A 403 from the gate shows the gate's own message, as it does for every form.

## 8. Retention and record reports

- The selection shares retention's predicate (section 2). Retention's
  behaviour and tests do not change, apart from the `json_valid` guard and
  the unreadable-report line in the prune report.
- Both writers take `BEGIN IMMEDIATE`, so they never interleave. A nightly
  settle between a preview and an apply changes a listed revision, so the
  apply refuses as a stale plan.
- Bulk-acknowledged reports read `by <who>: bulk acknowledge, report #N`.
  Settled reports read `by retention`.
- **A record report is marked, and the bulk verb declines it.** A record
  report is a report item that records an operator act instead of a cron run.
  The contract is agreed with sd:754 (system #336 at head `9b78110b`: design
  section 9 for the `ingest` keywords, section 3 I9 and the "Coordination
  with sd:755" paragraph under Decisions for the marker). The marker is a
  top-level `fields["record"]` string, written through
  `ingest(..., record=...)`:
  - this item's batch report: `fields.record = "reports-acknowledge"`;
  - sd:754's records: `fields.record = "item-remove"` for an item remove and
    `"repo-remove"` for a repo remove.
- **Guards check that the marker exists, not its value.** The one predicate
  is `CASE WHEN json_valid(fields) THEN json_type(fields,'$.record') END IS
  NOT NULL`. It is not a job list, which would need an edit for every new
  kind of record.
  - `json_type` and not `json_extract`: the two differ only on a JSON `null`
    marker, `{"record": null}`. `json_type` gives `'null'`, so the row counts
    as a record. `json_extract` gives SQL `NULL`, so the row does not.
    `ingest` cannot write that shape, because `record=None` stores no key, so
    only a hand edit can. For a hand-edited row the safe answer is "record" in
    both verbs: the bulk clean declines it, and `item remove` refuses it.
    Both fail closed.
  - sd:754 uses the same predicate, in the same `json_type` form (system #336
    at head `9b78110b`, section 3 I9 and the "Coordination with sd:755"
    paragraph under Decisions). So the two verbs agree on every row,
    including a hand-edited `null` marker.
  - One form, two literals. As shipped it is written twice:
    `removal.RECORD_MARKER` (`local-sd-db/sd_db/removal.py:66`) and the
    `record` column of `clean_reports` (`reporting.py:357`). They differ by
    one space after the comma, which SQL ignores, so they match the same rows
    today. Nothing shares a definition, so a later edit to one can drift from
    the other. A follow-up should have `clean_reports` read
    `removal.RECORD_MARKER`; this plan changes no shipped code.
- **Any report with a `fields.record` marker is declined** as "an operator
  record, not a run report" (section 3). Without this, a later `--all-clean`
  would sweep every earlier batch record.
- **For every record, the decline is a second guard, not the only one.**
  sd:754 files its record with `attention` false and moves it to `done` in the
  same transaction. This item does the same with its batch report (section 5,
  step 5a). The bulk clean and retention select only `planning` reports, so
  neither selects a record, even without the marker. The marker decline
  makes the bulk verb decline a record that a direct library call moved back
  to `planning`.
- **Retention never settles a record in `done`.** A record is `done` from the
  commit that files it, and `settle_clean_reports` selects only `planning`
  reports. Retention has no record term (prd requirement 11). So a clean
  record that a direct library call moved back to `planning` is settled by
  retention after 7 days, the same as a run report. That returns it to
  `done`, where it was filed. The 7-day settle otherwise applies only to the
  run reports this verb has not acknowledged.
- **How long the record lasts.** Retention never prunes items, notes or
  checkpoints (`retention.py:1-53`). So the batch report and its id list last
  until an explicit sd:754 `item remove`. sd:754's `item remove` refuses any
  item that carries a `fields.record` marker, whatever its value (system #336 at
  head `9b78110b`, section 3 I9 and the "Coordination with sd:755"
  paragraph). So once both items land, an explicit remove cannot take a
  record either. Before sd:754 lands, no supported verb deletes an item row:
  a search for `DELETE FROM item` in `sd_db`, the dashboard and the pack's
  `bin/` finds none (2026-09-14).

## 9. Replay (system #326)

- `ingest` answers a replay by `external_id` and never touches `status`
  (`reporting.ingest`, `reporting.py:92-96`). `ingest_log` finds a filed
  report first (`reporting.ingest_log`, `reporting.py:212-213`). A replay of
  an acknowledged run therefore returns the `done` report and adds no
  followup.
- A first-time late arrival gets `created_at` equal to now, which is later
  than `before`. If it joins a selection some other way, the token refuses
  the apply (section 4).
- A late older attention run files an attention report, and that report is
  declined. A late older clean run writes nothing (`_beat` returns `"older"`).
- sd:757 follow-up V1 (note 1807): a future-stamped heartbeat could make a
  late older run file "the job recovered". Main now bounds that with
  `FUTURE_ALLOWANCE` (`reporting.FUTURE_ALLOWANCE`, `reporting.py:22-28`). A
  report filed in that window is clean, and the heartbeat rule does not
  decline it. This design accepts that.

## 10. Order of landing

The library functions are new. `ingest` gains two optional keywords.
`controls.form` gains one optional keyword. The system-internal `action_route`
gains a required `principal` keyword, and it has one caller. No signature a
pack caller uses changes.

The running dashboard does not import `sd_db` from the system checkout. Its
LaunchAgent runs `dashboard.sh serve` with `SD_DASHBOARD_PYTHON` set to the
pack's `.venv/bin/python`
(`local-machine-setup/launchagents/local.system-tools.sd-dashboard.plist`,
`dashboard.sh:56-67`). The pack installer fills that `.venv` from the HEAD of
the local system checkout, `~/repos/system` unless `SD_SYSTEM_CHECKOUT` names
another: an `sd-db-v*` tag when HEAD stands on one, else
`git rev-parse HEAD` (pack `bin/sd_install.py:1233-1259`, `library_pin`). It
takes no ref, and it does not read the pack's CI pin. So PR 2's dashboard
code needs PR 1 in the `.venv`, not only on system `main`. The order is:

1. **System PR 1:** the library verb. Delivered as #347 (`e377da7`).
2. **System PR 2:** the dashboard form.
3. **Deploy system** (implement step 14), an operator step. The operator
   pulls `~/repos/system` to PR 2's merge or later, re-provisions the pack
   `.venv` with `bin/sd_install.py --provision-library` from that HEAD, and
   then restarts the dashboard. The dashboard then loads PR 2 over a library
   that has PR 1.
4. **Pack PR 3:** the CLI flags and the pack's CI pin moved to a system
   commit that contains PR 1. The `.venv` already holds PR 1 from step 3.
   If PR 3 merges before step 3, the pack deploy runs the re-provision
   first (implement step 18).

A restart on PR 2 before the re-provision breaks the preview and the apply.
The `.venv` has no `reporting.cutoff`, so the GET raises `AttributeError`.
`do_GET` catches only `MissingItem`, `NotFound`, `SdDbError` and
`sqlite3.Error` (`server.py:407-416`), so the request gets no response.
Implement step 14 therefore checks the functions with the dashboard's
interpreter and requests a preview. PR 2 could land in the same system PR as
PR 1. It is kept separate so each review stays small.

## 11. Rollback

**A mistaken apply is accepted, not undone.** The verb moves only clean
`planning` reports with no open followup and no record marker, created
before a past cutoff. Retention would settle each of them within 7 days of
its `created_at`. No supported verb reopens a report: `allowed_statuses`
returns no status for a report (`workflow.allowed_statuses`,
`workflow.py:372-395` at `b420bea`; a report is not in
`TASK_STATUS_KINDS`). This item adds none.
The batch report lists every moved id, so a later reopen verb has what it
needs. If a moved report needed a person, the operator reads it from the
batch report's id list and files a followup item for it.

**A code rollback runs in reverse order of landing, and the library goes
last.** A caller stops calling PR 1's functions before the `.venv` loses
them.

1. Revert PR 3, which moves the pack's CI pin back and removes the CLI flags.
   The `.venv` is not re-provisioned. The CI pin never reaches it.
2. Revert PR 2. The operator pulls `~/repos/system` and restarts the
   dashboard, and `/health` shows `code_changed` false.
3. Revert PR 1, with one precondition. If sd:754 is on `main`, its remove
   verbs call `ingest(actor=..., record=...)`. Then the revert keeps the two
   `ingest` keywords, their validation and their tests, and removes
   everything else PR 1 added. Check first:
   `grep -rn "record=" local-sd-db/sd_db --include=*.py` shows a caller other
   than `reporting.acknowledge_clean`. If sd:754 is not on `main`, revert PR 1
   in full.
4. Roll the `.venv` back after the PR 1 revert merges: pull `~/repos/system`,
   run `bin/sd_install.py --provision-library`, and restart the dashboard.
   The installer calls `downgrade_refusal` first (pack
   `bin/sd_library_guard.py:27-44`). It refuses only an unreadable
   `SCHEMA_VERSION` or a candidate `SCHEMA_VERSION` lower than the installed
   one. PR 1 changed no schema, and a revert on `main` carries `main`'s
   version, so the guard lets it through. Without step 3 the library is not
   rolled back: the `.venv` keeps PR 1's functions, and no shipped caller
   calls them.

Rows written while the code was live stay valid after a rollback. They are
ordinary `done` reports, status notes and one `done` report item per batch
with extra keys in `fields`. No schema changed, so there is nothing to migrate back.

## Decisions made in this design

These can be reversed in review.

- The apply is all or nothing, pinned by a token over ids and revisions
  (section 5).
- A job with no heartbeat does not decline its reports (section 3), based on
  the measured 177 of 281.
- `MAX_BATCH = 1000`.
- The dashboard preview is a GET, and the apply is a POST (section 7.1).
- The dashboard `who` is typed and required, and the principal is recorded
  separately (section 7.5).
- One `before` contract on both surfaces: a date meaning 00:00 UTC, converted
  by `reporting.cutoff` (section 2.1).
- `declined` lists only `planning` reports created before the cutoff
  (section 3).
- Record reports carry `fields.record`, are filed `done`, and are declined
  by key existence with `json_type`, as agreed with sd:754 (section 8).
- An empty or oversized selection at apply time is a stale plan, 409 on the
  dashboard (section 5, step 3).
- The heartbeat rule reads the newest heartbeat by id (section 3).
- An over-long `SD_SESSION` is truncated to 200 characters, not refused
  (section 5).
- Malformed `fields` declines a report instead of failing the read, in
  retention too. The prune report names such reports and asks for a person
  (section 2).

## Risks

- **Accepted: little real use.** 0 reports are candidates today (D1).
- **Accepted: `who` stays self-declared** on both surfaces. The principal and
  the process make a false name visible afterwards. Nothing prevents one.
- **Accepted: the library can still be called directly.** A script can still
  call `transition(..., who="x")`. What changes is that a supported path now
  exists.
- **Mitigated: the predicate drifts between retention and the bulk verb.**
  One function serves both, and a property test against an independent
  oracle pins the subset relation.
- **Open: `created_at` versus `ended`.** The two differ for a report filed by
  a hand recovery. This design follows retention and uses `created_at`.
- **Mitigated, cross-item: the shared `ingest` keywords.** sd:754 and this
  item both add `actor` and `record`. The contract in section 8 fixes the
  names, the placement and the validation. Each item adds `actor` and
  `record` only if they are absent, so the second PR changes nothing in
  `ingest`. A rollback of PR 1 keeps them when sd:754 uses them (section 11).
- **Not tested by the suites: the browser.** The dashboard tests cover the
  rendered markup and the HTTP exchange. The `data-reload-label` link and the
  redirect to the batch report need one manual check in a real browser, and
  `implement.md` names it.

## Evidence

All queries ran against `~/.local/share/sd/sd.db` with `sqlite3 -readonly`,
on 2026-09-14 between 01:45Z and 01:50Z, except where a line gives a later
time.

**Report census.**

    SELECT status, json_extract(fields,'$.attention') AS attention, count(*)
    FROM item WHERE kind='report' GROUP BY 1,2;
    -- done|0|282   done|1|28          (no planning rows)

    SELECT count(*) FROM item WHERE kind='report' AND status!='done';           -- 0
    SELECT max(created_at) FROM item WHERE kind='report';                       -- 2026-09-13T16:15:05+00:00
    SELECT min(timestamp) FROM state WHERE kind='heartbeat' AND key LIKE 'cron-report:%';  -- 2026-09-13T16:30:04+00:00
    SELECT count(*) FROM note WHERE body LIKE '%by retention%';                 -- 2 (both on task items; 0 on reports)

**Malformed rows and record markers** (02:37Z).

    SELECT count(*) FROM item WHERE kind='report' AND NOT json_valid(fields);   -- 0
    SELECT count(*) FROM item WHERE kind='report' AND json_valid(fields)
      AND json_type(fields,'$.record') IS NOT NULL;                             -- 0
    SELECT count(*) FROM state WHERE kind='heartbeat' AND NOT json_valid(body); -- 0

**Status notes on reports.**

    SELECT n.body, n.session, count(*) FROM note n JOIN item i ON i.id=n.item
    WHERE i.kind='report' AND n.kind='status_change' GROUP BY 1,2;
    -- opened as planning|cron|310
    -- planning -> done by user|user|281
    -- planning -> done by operator|operator|28
    -- planning -> done by dashboard|dashboard|1

    SELECT count(*), count(DISTINCT item), min(item), max(item) FROM note
    WHERE timestamp='2026-09-13T16:49:32+00:00' AND kind='status_change';
    -- 281|281|43|742

**Candidate rules at the batch instant.** The store is rebuilt as it was just
before `T = 2026-09-13T16:49:32+00:00`. A report counts as open at T if it was
created before T and has no `planning -> done` note before T. A followup
counts as open at T if it was written before T and is unresolved or was
resolved at or after T.

    WITH open_at_t AS (
      SELECT i.id, i.created_at, json_extract(i.fields,'$.attention') AS att,
             json_extract(i.fields,'$.report.job') AS job, json_extract(i.fields,'$.report.ended') AS ended
      FROM item i WHERE i.kind='report' AND i.created_at < :T
        AND NOT EXISTS (SELECT 1 FROM note n WHERE n.item=i.id AND n.kind='status_change'
                        AND n.body LIKE 'planning -> done%' AND n.timestamp < :T)),
    f AS (
      SELECT o.*, EXISTS (SELECT 1 FROM note n WHERE n.item=o.id AND n.kind='followup' AND n.timestamp < :T
                            AND (n.resolved_at IS NULL OR n.resolved_at >= :T)) AS open_followup,
             (SELECT max(json_extract(s.body,'$.ended')) FROM state s WHERE s.kind='heartbeat'
                 AND s.key='cron-report:'||o.job AND s.timestamp < :T) AS beat_ended
      FROM open_at_t o)
    SELECT count(*), sum(att=1), sum(open_followup), sum(att=0 AND NOT open_followup), sum(att IS NULL),
           sum(att=0 AND NOT open_followup AND created_at < '2026-09-13T00:00:00+00:00'),
           sum(att=0 AND NOT open_followup AND created_at < '2026-09-12T16:49:32+00:00'),
           sum(att=0 AND NOT open_followup AND created_at < '2026-09-06T16:49:32+00:00'),
           sum(att=0 AND NOT open_followup AND beat_ended IS NULL),
           sum(att=0 AND NOT open_followup AND beat_ended IS NOT NULL AND ended > beat_ended),
           sum(att=0 AND NOT open_followup AND beat_ended IS NOT NULL AND ended <= beat_ended)
    FROM f;

The re-run at 02:37Z replaced `max(json_extract(s.body,'$.ended'))` with the
`ended` of the newest row by id (`ORDER BY s.id DESC LIMIT 1`), the rule
section 3 chose. It returned `281|177|0|104`, the same counts.

| Rule, at T | Reports |
|---|---|
| every `planning` report | 281 |
| of which attention | 0 |
| of which with an open followup | 0 |
| of which with `attention` missing | 0 |
| clean with no open followup (the base predicate, `before = T`) | 281 |
| same, `before` = start of the UTC day (2026-09-13T00:00Z) | 176 |
| same, created more than 24 h before T | 140 |
| same, created more than 7 days before T (retention) | 0 |
| clean, and the job has no `cron-report:` heartbeat | 177 |
| clean, and `ended` later than the job's newest heartbeat | 0 |
| clean, and `ended` not later than the job's newest heartbeat | 104 |

The same rules evaluated at 01:50Z today select 0 reports, because no report
is `planning`.

Heartbeats before T cannot be fully rebuilt once compaction has removed rows,
and by 2026-09-14 it had. At 01:50Z no `cron-report:` heartbeat predated
`16:30:04` and `claude-mem-pro-watchdog` still held all 38 of its rows, which
is what the 177 and 104 counts rest on. Re-measured at 14:13Z: 0
`cron-report:` rows predate T, the oldest is `2026-09-13T18:35:02+00:00`, and
the watchdog key holds 25 rows with the oldest at `2026-09-14T08:00:04+00:00`.
So the split is a record of what was measured at 01:50Z and cannot be
reproduced now. No acceptance criterion and no Verify line uses 177 or 104:
the heartbeat rule is pinned by the synthetic fixtures in implement step 3.

**Transaction probe.** This was a scratch script and is not committed. The
live store was copied with `sqlite3 -readonly ~/.local/share/sd/sd.db
".backup copy.db"`. In the copy only, the 281 batch notes were deleted and
the rows reset to `planning`. The probe then ran this worktree's `sd_db`
with `PYTHONDONTWRITEBYTECODE=1`. It read 281 revisions, then in one
`transaction` ran `_checked_state` and
`transition(..., who="probe", reason="bulk acknowledge batch 0 (probe)")` for
each row. Output over three runs:

    dry-run read 281 revisions: 0.009s | 0.011s | 0.009s
    apply lock held (BEGIN IMMEDIATE..COMMIT): 0.020s | 0.025s | 0.022s
    281 notes written

**The same probe with the shipped verb** (2026-09-14 13:31Z, after system PR 1
merged as `e377da7`). The same copy and reset, then `reporting.clean_reports`
and `reporting.acknowledge_clean` themselves, three runs:

    preview 281 rows: 0.012s | 0.011s | 0.011s
    apply 281 rows:   0.033s | 0.025s | 0.023s

The apply now also files and closes the batch report, which the hand-written
probe did not.
