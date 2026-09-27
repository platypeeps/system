# Implement — bulk-acknowledge-reports

The work lands as three pull requests and one deploy, in the order from
design.md section 10:

1. System PR 1: the library verb.
2. System PR 2: the dashboard form. It depends on PR 1.
3. System deploy: the operator pulls the system checkout, re-provisions the
   pack `.venv` from it, and then restarts the dashboard. It follows PR 2.
4. Pack PR 3: the CLI and the pin move. It depends on PR 1 being on system
   `main`.

Every step has a Verify line. A step is done only when its Verify line has
run and produced the stated result. Record the decisive output line on
sd:755.

**Steps 1 to 11 and 13 are delivered.** System PR 1 merged as #347
(`e377da7`) on 2026-09-14; PR 2 is #405. Step 12 is open (its manual browser
check), as are step 14 (the deploy) and steps 15 to 18 (the pack).

Environment for every run:

- `PYTHONDONTWRITEBYTECODE=1`.
- `SD_ACCEPTANCE_PACK` points at a `git archive` extraction of the pack, not
  at the live checkout.
- Nothing touches `~/.local/share/sd/sd.db`, except the read-only dry run in
  step 18.

**No schema migration.** No step adds or changes a table, column, index or
trigger, and `SCHEMA_VERSION` stays 9 (design.md section 1). Step 8 and step
15 check this.

Fixture rule for every test below: a report with a chosen `created_at` is
made with `writes.create_item(connection, kind="report", ..., created_at=...)`.
`ingest` takes no `created_at`. A test that files through `ingest` or
`ingest_log` and needs a cutoff between two reports passes stamps that differ
by at least one second, because `created_at < before` compares whole seconds
as text.

## PR 1 — system: the library verb (`local-sd-db`)

1. Move retention's clean-report query out of `retention.settle_clean_reports`
   (`local-sd-db/sd_db/retention.py:203-207` at `b420bea`, before PR 1) and
   into one function,
   `reporting.clean_candidates(connection, *, before)`.
   - The function returns ids in id order. It opens no transaction and no
     savepoint. Its SQL is retention's SQL, with the attention term written as
     `CASE WHEN json_valid(fields) THEN json_extract(fields,'$.attention') END = 0`
     (design.md section 2). The `CASE` form guarantees that `json_extract`
     never sees malformed text. An `AND json_valid(fields)` term would depend
     on SQLite's evaluation order.
   - `settle_clean_reports` keeps its `with transaction(connection)` block and
     calls `clean_candidates(connection, before=_cutoff(now, CLEAN_REPORT_AGE))`
     inside it. Its docstring (`retention.settle_clean_reports`,
     `retention.py:208-209`) gains one sentence: a
     report whose `fields` is not valid JSON is not settled, and the prune
     report names it.
   - `retention.prune`, after the settle, reads the ids of `planning`
     reports whose `fields` is unreadable, in id order. The predicate is
     `coalesce(json_valid(fields), 0) = 0`, not `NOT json_valid(fields)`:
     `json_valid(NULL)` is `NULL`, so the second form does not count a report
     with no `fields` at all (design.md section 2). When there are
     none, the call to `ingest` is unchanged. Otherwise `ingest` gets
     `attention=True` (design.md section 2):
     - the report text gains the line `N report(s) in planning have
       unreadable fields: #id, ...`, with at most the first
       `UNREADABLE_TEXT_IDS = 1000` ids and then `and M more`;
     - `attention_basis` is the same line with at most the first
       `UNREADABLE_BASIS_IDS = 20` ids and then `and M more`. `ingest`
       refuses a basis over 2000 characters (`reporting.ingest`,
       `reporting.py:61`), and
       `jobs/backup.py:91-105` turns that refusal into exit 1.
   - Verify: `sh local-sd-db/sd-db.sh test -k test_retention -v > retention.txt 2>&1`.
     `sd-db.sh test` passes its arguments to `unittest discover`, and `-k`
     narrows the run, where a dotted module name does not. The `Ran N tests`
     line must equal `grep -c "^    def test_" local-sd-db/tests/test_retention.py`
     (20 at `0b5394ae`, before any new test), and the last line must be `OK`.
     Run it before and after the move, and quote both lines.
   - Verify: a new `test_retention` test stores one planning report whose
     `fields` is the text `{not json`, created 8 days before `now`, next to one
     clean report. `settle_clean_reports` returns 1 and settles only the clean
     one. On the base commit the same test raises `sqlite3.OperationalError`
     ("malformed JSON").
   - Verify: a new `test_retention` test runs `retention.prune` on a store with
     one `planning` report whose `fields` is `{not json`. The prune report has
     `fields.attention == True`, one open followup, and a text line that names
     the report's id. A second test with no such report checks that the prune
     report's `fields` and text equal the base commit's output for the same
     fixture.
   - Verify (the cap): a new `test_retention` test runs `retention.prune` on
     a store with 300 `planning` reports whose `fields` is `{not json`. The
     prune returns without raising. `fields.attention == True`.
     `fields.report.attention_basis` names exactly 20 ids, ends `and 280
     more`, and is at most 2000 characters. The report text names all 300
     ids. With 20 such reports the basis names 20 ids and has no `and ...
     more`; with 21 it ends `and 1 more`. Then remove the basis cap (pass the
     full line). The test fails with `WorkflowError` "report provenance
     exceeds its size limit". Restore with `git diff --exit-code`.
   - Verify: deleting the `NOT EXISTS (... followup ...)` clause from
     `clean_candidates` makes at least one `test_retention` test fail.
     Restore the file afterwards and confirm with `git diff --exit-code`.
   - Verify: `sed -n '/^def clean_candidates/,/^def /p' local-sd-db/sd_db/reporting.py > body.txt`
     then `grep -cE 'with transaction\(|"(SAVEPOINT|BEGIN|RELEASE)' body.txt`
     prints `0`. The pattern matches code only. A docstring such as "opens no
     transaction" does not match it.

2. Add `actor` and `record` to `ingest` only if they are absent. sd:754 uses
   the same conditional form (system #336, design section 9, head
   `9b78110b`). Check first:
   `grep -n "record=None\|actor=None" local-sd-db/sd_db/reporting.py`.
   - If both are there, sd:754 landed first. Keep them unchanged, confirm they
     match this step, and add only this item's tests below.
   - If neither is there, add both, with these names, this placement and this
     validation, and the tests below.
   - If only one is there, stop and record it on sd:755. The contract is
     broken, and the two plans need to agree again.
   - `actor=None`: when given, it must be a dict with at most 10 keys. Every
     key is a `str`. Every value is a `str` of at most 200 characters, an
     `int`, or `None`. It is stored as `provenance["actor"]`, the same way
     `removed` is stored (`reporting.ingest`, `reporting.py:82-85`).
   - `record=None`: when given, it must be a `str` that matches the job-name
     pattern `[a-z0-9][a-z0-9_-]{0,99}`. It is stored as top-level
     `fields["record"]`, next to `attention` and `report`.
   - Verify: a new test passes a non-dict `actor`, an 11-key `actor`, an
     `actor` value of 201 characters, and `record="Bad Name"`. Each raises
     `WorkflowError` and leaves `tuple(connection.iterdump())` unchanged.
   - Verify: a call with `record="reports-acknowledge"` stores
     `json_extract(fields,'$.record') == "reports-acknowledge"`, and its replay
     with the same arguments returns the same item id.
   - Verify: calls without `actor` or `record` produce byte-identical
     `fields`. The existing `test_report_replay` and `test_controls` report
     tests pass unchanged.

3. Add `reporting.cutoff(value)` and
   `reporting.clean_reports(connection, *, before, now=None)`, as design.md
   sections 2.1, 3 and 4 specify.
   - `cutoff` accepts `YYYY-MM-DD` (a real date) and returns
     `YYYY-MM-DDT00:00:00+00:00`. It returns that exact stamp shape
     unchanged. It refuses everything else with `WorkflowError`.
   - `clean_reports` stamps `before` with `writes.stamp`. It refuses a
     `before` later than `now`. `now` is an aware `datetime`, and `None` means
     `datetime.now(UTC)`.
   - It runs every read between `SAVEPOINT sd_clean_reports` and `RELEASE
     sd_clean_reports`, and it opens no `transaction`.
   - It takes `clean_candidates` and applies the refusals of design.md section
     3, in the table's order. Every `json_extract` and `json_type` sits
     inside `CASE WHEN json_valid(...) THEN ... END`. "`fields.record` is
     set" is exactly `CASE WHEN json_valid(fields) THEN
     json_type(fields,'$.record') END IS NOT NULL`. The heartbeat rule reads
     the newest `cron-report:<job>` row by `id`.
   - `declined` lists every `planning` report with `created_at < before` that
     is not selected, with its first reason. It lists no report created at or
     after `before`, and no report whose status is not `planning`.
   - `plan` is `sha256` over the stamped `before` plus the sorted
     `(id, revision)` pairs. When the selection is empty or larger than
     `MAX_BATCH`, `plan` is `None` and no revision is read.
   - Verify: new `local-sd-db/tests/test_report_bulk_acknowledge.py`. The
     cutoff is `2026-09-10T00:00:00+00:00` and `now` is
     `2026-09-12T00:00:00+00:00`. The fixture has 7 reports, each made with
     `create_item(created_at=...)`:
     - 2 clean `planning` reports created before the cutoff;
     - 1 `planning` attention report created before the cutoff;
     - 1 clean `planning` report with an open followup, created before the
       cutoff;
     - 1 clean `planning` report with `fields.record = "reports-acknowledge"`,
       created before the cutoff;
     - 1 clean `planning` report created at `2026-09-10T00:00:01+00:00`, after
       the cutoff;
     - 1 clean `done` report created before the cutoff.
     The test asserts that `selected` ids equal the 2 clean ids. It asserts
     that `declined` ids equal exactly the attention, followup and record ids,
     with their reasons. It asserts that the after-cutoff id and the `done` id
     are in neither list. It asserts that `tuple(connection.iterdump())` is
     unchanged.
   - Verify (the record predicate): a clean `planning` report whose `fields`
     is `{"attention": false, "record": null, "report": {...}}` is declined
     with the record reason. A `json_extract(...) IS NOT NULL` predicate would
     select it, so this test fails on that form.
   - Verify: the same fixture with one report whose `fields` is `{not json`,
     created before the cutoff, returns normally and lists that id as declined.
   - Verify: a clean report whose `ended` is later than the `ended` of its
     job's newest heartbeat by id is declined. The same report is selected
     when the job has no heartbeat.
   - Verify (newest by id, not by `ended`): the job has two heartbeat rows.
     The older row by id has an `ended` later than the report's `ended`. The
     newer row by id has an `ended` earlier than the report's. The report is
     declined. A `max(ended)` rule would select it, so this test fails on
     that rule.
   - Verify (write=False): the test copies the fixture to a file, opens it with
     `sd_db.connect(path, write=False)` and calls `clean_reports`. The call
     returns the same `plan` as on the writable connection and does not raise
     "attempt to write a readonly database".
   - Verify (the `before` contract): `cutoff("2026-09-10")` and
     `cutoff("2026-09-10T00:00:00+00:00")` both return
     `"2026-09-10T00:00:00+00:00"`. `cutoff` raises `WorkflowError` for
     `"2026-02-30"`, `"2026-09-10T00:00:00Z"`, `"2026-09-10T01:00:00+00:00"`
     and `"nonsense"`. `clean_reports(connection, before="2026-09-10")`, a
     bare date that skipped `cutoff`, raises `SdDbError` "carries no
     timezone".
   - Verify: a `before` one second after the injected `now` raises
     `WorkflowError`.

4. Add `reporting.acknowledge_clean(connection, *, before, expected_plan, who,
   principal, program, session=None, now=None)`. It follows design.md section
   5, steps 1 to 6 with step 5a, inside one `transaction`, with
   `MAX_BATCH = 1000`. It stamps `now` for `ingest` as
   `now.astimezone(UTC).isoformat(timespec="seconds")`.
   - Verify: an apply with the step 3 token moves exactly the 2 reports to
     `done`. Each report's newest note is
     `planning -> done by <who>: bulk acknowledge, report #<batch>`. The batch
     report has `source='cron-report'`,
     `external_id='reports-acknowledge:<plan>'` and
     `fields.record == "reports-acknowledge"`. Its body lists both ids. The
     return value's `item.id` is the batch id.
   - Verify (batch record done, C-17): after the apply, the batch report's
     `status` is `done`, and its newest note is `planning -> done by <who>:
     bulk acknowledge record`. The return value's `item.status` is `done`.
     Then `retention.settle_clean_reports` runs with a `now` 8 days later.
     The batch report's `workflow.item_state` revision after that run equals
     its revision before it. Do not assert the return value: on the step 3
     fixture it is 2, because retention settles the hand-built `planning`
     record and the after-cutoff report (design.md section 8).
   - Verify: the batch report's `fields.report.actor` has exactly the keys
     `who`, `principal`, `program`, `pid`, `ppid` and `session`. `who`,
     `principal` and `program` equal the arguments. `pid == os.getpid()` and
     `ppid == os.getppid()` in the test process.
   - Verify: `session="x" * 250` stores a 200-character `actor.session`, and
     the apply succeeds.
   - Verify: three stale cases each raise `workflow.StaleItem` and leave
     `tuple(connection.iterdump())` equal to the value before the call:
     - a followup added after the preview;
     - one listed report acknowledged through `reporting.acknowledge`;
     - `retention.settle_clean_reports` run with a `now` 8 days later.
   - Verify (replay of an applied plan): a second call with the same
     `expected_plan`, after a successful apply, raises `workflow.StaleItem`
     and leaves the dump unchanged.
   - Verify: `who=""`, `who="  "`, `principal=""` and `program=""` each raise
     `WorkflowError`. A call that omits `who` raises `TypeError`. A call that
     omits `principal` raises `TypeError`.
   - Verify (plan shape): `expected_plan=None`, `expected_plan="abc"` and
     `expected_plan="A" * 64` each raise an exception whose type is exactly
     `WorkflowError` (`type(error) is WorkflowError`, not `StaleItem`), and
     the dump is unchanged.
   - Verify (empty and oversized, design.md section 5 step 3): take the step 3
     token. Then acknowledge both reports singly, so the selection is empty.
     The apply with the old token raises an exception whose type is exactly
     `StaleItem`. In a second fixture, take the token with 2 candidates, then
     patch `MAX_BATCH` to 1. The apply raises exactly `StaleItem`. Both leave
     the dump unchanged. `clean_reports` on those stores returns `plan` None.
   - Verify: a report created after the preview, with `created_at` after the
     cutoff, is still `planning` after the apply.
   - Verify: a report created after the preview with `created_at` before the
     cutoff (through `create_item(created_at=...)`) makes the apply raise
     `StaleItem` (design.md section 4).
   - Verify: a second `clean_reports`, with a cutoff after the batch report's
     `created_at`, lists the batch report in neither `selected` nor
     `declined`, because it is `done`. The step 3 fixture's hand-built
     `planning` record report covers the record decline.

5. Add a property test with an independent oracle. It runs 200 seeded
   random fixtures. Each fixture has reports with `attention` true, false,
   missing and `0`, with and without followups (open and resolved), with
   `planning` and `done` statuses, with and without `fields.record`, and with
   `created_at` values around a cutoff C. Every fixture row is built from a
   Python record, and the test keeps those records.
   - The oracle is a Python function in the test file. It reads the Python
     records, not the store, and it does not import `clean_candidates` or
     `clean_reports`. It returns the ids that retention's rule allows at C:
     kind report, status `planning`, attention `False` or `0`, `created_at`
     before C, and no unresolved followup.
   - A second check runs `retention.settle_clean_reports` on a
     `connection.backup` copy with `now = C + CLEAN_REPORT_AGE`, and reads
     the ids it moved.
   - For each fixture, the ids `clean_reports` selects at C are a subset of
     the oracle's ids, and a subset of the ids retention moved on the copy.
   - Verify: the test passes.
   - Verify: mutate the attention clause inside `clean_candidates` to
     `json_extract(fields,'$.attention') IS NOT 1`. The test fails, and the
     failure names the Python oracle (a report with `attention` missing is
     selected). Restore with `git diff --exit-code`.

6. Add a replay test. It files a failure through `ingest_log` with `ended`
   `2026-09-10T00:00:00+00:00`, and a recovery with `ended`
   `2026-09-10T00:15:00+00:00`, using a patched `writes.now` so the two
   `created_at` values are those stamps. It resolves the failure's followup.
   It bulk-acknowledges the recovery report with cutoff `2026-09-11` through
   `cutoff`, and then replays the recovery run through `ingest_log`.
   - Verify: the replay returns the same report id with `status == "done"`.
     The item count and the note count are unchanged.

7. Class test and fail-before evidence.
   - Extend `NoVerbNamesItsOperatorForTheCaller`
     (`local-sd-db/tests/test_controls.py:357`) so that its parameter walk
     (`test_controls.py:394-399`, `who_parameters`) checks parameters named `who` or
     `principal`.
   - Verify: the test passes. Adding `principal="local"` as a default on
     `acknowledge_clean` makes it fail. Adding `who="dashboard"` as a default
     makes it fail. Restore afterwards with `git diff --exit-code`.
   - Verify: run the new test file against origin/main's `sd_db`, from a
     detached worktree with the test file copied in. Every new test errors
     or fails there, because the new names do not exist. Record the count.

8. Run the full shared suite.
   - Verify: `sh local-sd-db/sd-db.sh check` on the branch and on its base
     commit. The branch's set of failing test names equals the base's set.
     Quote both `Ran N tests` lines.
   - Verify: `git diff --stat <base>..HEAD -- local-sd-db/sd_db/schema local-sd-db/sd_db/schema.py`
     prints nothing.
   - Verify: the pack's `bin/sd-docs-lint`, run on this checkout, prints
     `sd-docs-lint: clean`.

## PR 2 — system: the dashboard form (`local-project-dashboard`)

9. `action_route` (`local-project-dashboard/sd_dashboard/server.py:130`)
   gains a required `principal` keyword. `do_POST` passes
   `principal=context.principal` at its one call site (`server.py:530`).
   - Verify: `grep -n "= action_route(" local-project-dashboard/sd_dashboard/server.py`
     prints exactly one line, and the call on that line (and its
     continuation lines) passes `principal=context.principal`.
   - Verify: `grep -rn "action_route(" local-project-dashboard --include=*.py`
     shows no call site other than that one and the `def` line.
   - Verify: `sh local-project-dashboard/dashboard.sh test` passes with the
     same count as the base before any other PR 2 change.

10. Add the `/api/reports/acknowledge-clean` branch as design.md section 7.3
    specifies. It calls `reporting.cutoff(values["before"])` inside the
    returned function, not in the `action_route` body.
    - Tests in `tests/test_controls_actions.py` take a store dump with a
      `snapshot()` helper, `tuple(self.connection.iterdump())`, as
      `tests/test_workflow_actions.py:53-54` defines it. Every "writes
      nothing" check below compares `snapshot()` before and after the request.
    - Verify: a POST with the session's CSRF token and
      `{before: "<stamped>", plan, who: "operator"}` returns 200.
      `result["item"]["id"]` is the batch report. Both fixture reports are
      `done`. The batch report's `fields.report.actor.principal` equals the
      test context's principal, and `actor.program == "dashboard"`.
    - Verify: the same POST repeated after that success returns 409 with
      `"reload": true`, and `snapshot()` is unchanged.
    - Verify, as a regression guard and not a failing check:
      `/api/reports/acknowledge-clean` is added to the no-token loop
      (`tests/test_controls_actions.py:55-56`), and that POST returns 403.
      `do_POST` returns 403 before any routing (`server.py:516`), so this line
      passes on the base too. It guards the token check for the new path.
    - Verify: a body with an extra key, a plan that is not 64 hex characters,
      or a missing `who` each return 400, and `snapshot()` is unchanged.
    - Verify: `before: "2026-09-10T01:00:00+00:00"` returns status 400, and
      the response body parses as JSON whose `error` is the `cutoff` message.
      `snapshot()` is unchanged. If `cutoff` runs in the `action_route` body,
      `do_POST` sends no response (`server.py:530-535` does not catch
      `WorkflowError`). The test client then raises `RemoteDisconnected`, or
      it reads no status, and the test fails. Run the test once with `cutoff`
      moved into the body, confirm that it fails, and restore the file.
    - Verify: an empty selection at apply time (both fixture reports
      acknowledged singly after the preview) returns 409 with
      `"reload": true`, and `snapshot()` is unchanged.
    - Verify: `who: "  "` returns 400 with the library's message, and
      `snapshot()` is unchanged.
    - Verify: a stale plan, created by adding a followup after the preview,
      returns 409 with `"reload": true`, and `snapshot()` is unchanged.

11. Add the preview to `reports_screen.reports_panel`, as design.md section
    7.1 specifies. These code changes are part of this step:
    - `reports_panel(connection, parameters, *, now)` takes the dashboard
      clock. `operations_screen.operations_page` passes `now=now`
      (`operations_screen.py:274`).
    - The panel converts the `...Z` clock (`server.py:84-85`) with
      `datetime.fromisoformat(now)` into an aware `datetime`.
    - The panel calls `reporting.cutoff(clean_before)` and passes the stamped
      value to `clean_reports`.
    - It renders at most 200 rows of each list, with the full counts.
    - Verify: `GET /operations?area=reports` renders the GET form with
      `name="clean_before"` and no `/api/reports/acknowledge-clean` form.
    - Verify (the `before` round trip): `GET
      /operations?area=reports&clean_before=2026-09-10` returns 200. It
      contains "Would acknowledge (2)" and each declined reason. It has exactly
      one form with `action="/api/reports/acknowledge-clean"`. That form has
      `name="before" value="2026-09-10T00:00:00+00:00"`, `name="plan"
      value="<token>"` where the token equals
      `reporting.clean_reports(connection, before="2026-09-10T00:00:00+00:00", now=...)["plan"]`,
      and a required `name="who"`. Its `data-cli` contains `--before
      2026-09-10T00:00:00+00:00`. The test then POSTs the parsed `before` and
      `plan` with `who` and gets 200. If the bare date reaches the hidden
      field or `writes.stamp`, the GET renders no plan or the POST returns
      400, and the test fails.
    - Verify: `snapshot()` is unchanged across the GET.
    - Verify: `clean_before=2099-01-01` and `clean_before=nonsense` each
      return 200 with a notice and no apply form.
    - Verify: with `MAX_BATCH` patched to 1, the GET renders
      `Would acknowledge (2)`, the reason, no apply form, and no
      `name="plan"`.

12. `controls.form` (`controls.py:33`) gains `reload_label=None`. When it is
    given, the form element gets `data-reload-label`. The form script's
    `result.reload && !capture` branch (`static/dashboard.js`) uses
    `form.dataset.reloadLabel` for the link text when it is set, and "Reload
    current item" otherwise. The apply form passes
    `reload_label="Preview again"`.
    - Verify: the apply form's markup contains
      `data-reload-label="Preview again"`.
    - Verify: no other form's markup changes.
      `reports_screen.report_controls(row, revision)` for a report fixture
      returns the same string on the branch as on the base. The whole page
      is not compared, because each response carries a per-session `sd-csrf`
      meta tag (`server.py:419-421`). And `grep -rn
      "reload_label" local-project-dashboard/sd_dashboard` shows only the
      `form` definition and the apply form.
    - Verify (manual, one browser run; not run by the PR 2 lane): preview two fixture reports.
      Add a followup to one of them in another tab. Submit with a `who`. The
      result region shows the stale message and a "Preview again" link, and
      the link re-renders the preview with one report. Submitting again lands
      on `/item/<batch id>`. Record this on sd:755 as a manual check, not as
      a suite result.

13. Run the full dashboard suite.
    - Verify: `sh local-project-dashboard/dashboard.sh test` on the branch
      and on its base. The branch shows `OK`, or the same failing names as
      the base. Quote both `Ran N tests` lines.
    - Verify: each new dashboard test fails against the PR 1 commit's
      dashboard code (the route and markup are absent) and passes on the
      branch.

## System deploy

14. Deploy PR 2 to the running dashboard, after PR 2 merges.
    - This is an operator step. A planning or implementation lane does not
      run `launchctl`, restart services, pull the main checkouts or install
      into the pack `.venv`. The lane asks the operator and records the
      result.
    - The dashboard does not import `sd_db` from the system checkout. The
      `local.system-tools.sd-dashboard` LaunchAgent sets `SD_DASHBOARD_PYTHON` to the
      pack's `.venv/bin/python`
      (`local-machine-setup/launchagents/local.system-tools.sd-dashboard.plist`), and
      `dashboard.sh serve` runs that interpreter with `-I`
      (`local-project-dashboard/dashboard.sh:56-67`). So the `.venv` must
      hold PR 1's functions before the dashboard runs PR 2's code. If it does
      not, a preview GET raises `AttributeError`, `do_GET` does not catch it
      (`server.py:407-416`), and the request gets no response. The apply POST
      fails the same way. On 2026-09-14 the `.venv` held `sd_db` at
      `b420bead`, from before PR 1, and has none of the functions.
    - 14a. The operator pulls the system checkout that the LaunchAgent runs
      (`~/repos/system`) to the commit where PR 2 merged, or later. PR 1
      (#347, `e377da7`) is already on system `main`, so that HEAD contains
      PR 1.
      - Verify: `git -C ~/repos/system merge-base --is-ancestor e377da7 HEAD`
        exits 0, and the same command with PR 2's merge commit exits 0.
    - 14b. Before the restart, the operator re-provisions the pack `.venv`
      from that checkout:
      `cd ~/repos/platypeeps/sd-ai-command-pack && .venv/bin/python bin/sd_install.py --provision-library`.
      The installer takes no ref. It installs `sd_db` from the HEAD of the
      local system checkout (`SD_SYSTEM_CHECKOUT`, default `~/repos/system`):
      an `sd-db-v*` tag when HEAD stands on one, else `git rev-parse HEAD`
      (pack `bin/sd_install.py:1233-1259`, `library_pin`). It does not read
      the pack's CI pin. Run the command with `--dry-run` first. It
      prints `would install sd_db from ... at <ref>`, or the
      `downgrade_refusal` reason (see Rollback step 4). The same `.venv`
      serves `local.system-tools.sd-runner`, `local-sd-db/sd-db.sh` and
      `local-dependabot/dependabot.sh`, so record the library range they also
      receive: `git -C ~/repos/system log --oneline <commit_id>..HEAD -- local-sd-db/sd_db`,
      where `<commit_id>` is in the `.venv`'s
      `sd_db-*.dist-info/direct_url.json`.
      - Verify: the command prints `sd_db installed from ... at <ref>` and
        exits 0. `<ref>` is the HEAD from 14a, or its `sd-db-v*` tag.
      - Verify: `SD_DASHBOARD_PYTHON=$(plutil -extract EnvironmentVariables.SD_DASHBOARD_PYTHON raw ~/Library/LaunchAgents/local.system-tools.sd-dashboard.plist)`,
        then
        `"$SD_DASHBOARD_PYTHON" -I -c "import sd_db.reporting as r; print(all(hasattr(r, n) for n in ('cutoff', 'clean_candidates', 'clean_reports', 'acknowledge_clean')))"`
        prints `True`. Before this step, on 2026-09-14, it printed `False`.
    - 14c. The operator restarts the dashboard with the command the machine
      setup uses for that LaunchAgent.
      - Verify: `curl -s http://127.0.0.1:8767/health` returns JSON with
        `"ok": true`, `"code_changed": false` and a `pid` different from the
        pid before the restart. The port is `DEFAULT_PORT` (`server.py:75`).
      - Verify: `curl -s "http://127.0.0.1:8767/operations?area=reports"`
        contains `name="clean_before"`. This shows only that PR 2's static
        form is served. The form calls no library function.
      - Verify: `curl -s -o preview.html -w '%{http_code}' "http://127.0.0.1:8767/operations?area=reports&clean_before=$(date -u +%F)"`
        prints `200`, and `preview.html` contains `Would acknowledge (`. A
        `.venv` without PR 1's functions gives `000` and an empty reply,
        because the GET raised before it sent a response.

## PR 3 — pack: the CLI (`bin/sd_controls.py`) and the pin

15. Move the pack's `sd_db` pin in `.github/workflows/tests.yml` to the system
    `main` commit where PR 1 merged. Rewrite the comment above it to say why.
    Name the commits in the range that could reach a pack test, as the current
    comment does.
    - Read the old pin from PR 3's base, not from this plan:
      `OLD=$(git show <PR 3 base>:.github/workflows/tests.yml | sed -n 's/.*ref: \([0-9a-f]\{40\}\).*/\1/p')`.
      The pin has already moved: it was `fd07ea9c` at pack `f15eed88` and is
      `dc03956e` at pack `6c0a7ba2`.
    - Verify: in the system repository,
      `git diff --stat "$OLD"..<new> -- local-sd-db/sd_db/schema local-sd-db/sd_db/schema.py`
      prints nothing, or the PR body names each schema file in the range and
      the `SCHEMA_VERSION` before and after.
    - Verify: the CI job log shows `HEAD is now at <new short sha>` in the
      system checkout step.

16. In `bin/sd_controls.py` `register`:
    - `item` becomes `nargs="?"`.
    - Add `--all-clean`, `--before`, `--apply`, `--if-plan` and `--who`.
    - `run` refuses, before connecting, any call that gives neither or both
      of `item` and `--all-clean`, `--all-clean` without `--before`, and
      `--apply` without `--if-plan` or `--who`.
    - `run` also refuses `--before`, `--apply`, `--if-plan` or `--who` with an
      `item` instead of `--all-clean`, and `--if-plan` or `--who` without
      `--apply`. Without this, `ITEM --apply --if-plan <64hex> --who NAME`
      passes every check above, reaches the single-item branch
      (pack `bin/sd_controls.py:37-39`), and there the three flags are
      ignored: `--if-revision` is absent, so the branch reads the current
      revision and acknowledges unconditionally, and `who` becomes
      `getpass.getuser()`. The run exits 0 and the store records an actor the
      operator did not type, which is the defect this item removes. The
      refusal fails closed and invalidates no shipped command, because no
      shipped form takes these flags.
    - `run` calls `reporting.cutoff(args.before)` before connecting, and
      passes the stamped value on.
    - The dry run connects with `write=False`. The apply passes
      `principal=getpass.getuser()`, `program="sd reports acknowledge"` and
      `session=os.environ.get("SD_SESSION")`.
    - Human output ends with the apply command line, with the stamped
      `before`. `--json` prints the library's dict.
    - Verify: `tests/test_sd_controls.py` gains a test beside
      `test_report_ingestion_is_replayable_and_acknowledged`. It builds a
      store with 2 clean planning reports created before
      `2026-09-10T00:00:00+00:00`.
      - `reports acknowledge --all-clean --before 2026-09-10 --json`, a bare
        date, exits 0 and prints 2 `selected` and
        `"before": "2026-09-10T00:00:00+00:00"`. The store dump is unchanged.
        If the CLI passes the bare date to the library, `writes.stamp` raises
        "carries no timezone", the exit is nonzero, and the test fails.
      - `--before 2026-09-10T00:00:00+00:00 --json` prints the same `plan`.
      - The command with `--apply --if-plan <plan> --who tester --json`
        and `SD_SESSION=test-session` in the environment exits 0, both
        reports are `done`, and the output's `actor.principal` is the login
        name, `actor.program` is `sd reports acknowledge` and
        `actor.session` is `test-session`.
      - The connection call `run` makes (pack `bin/sd_controls.py:21`) is
        wrapped in the test. It gets `write=False` for the dry run and
        `write=True` for the apply. The shipped rule
        `write=args.control_action != "list"` gives `True` for both, so this
        fails if the dry run keeps it.
      - Without `--json`, the dry run's last output line starts with
        `sd reports acknowledge --all-clean --before 2026-09-10T00:00:00+00:00 --apply --if-plan `.
      - `--apply --if-plan <plan>` without `--who` exits nonzero with no
        Traceback and writes nothing.
      - `--apply --who tester` without `--if-plan` does the same.
      - One loop over the refused flag forms: `<item id> --who tester`,
        `<item id> --before 2026-09-10`, `<item id> --apply`,
        `<item id> --if-plan <plan>`,
        `<item id> --apply --if-plan <plan> --who tester`,
        `--all-clean --before 2026-09-10 --who tester` and
        `--all-clean --before 2026-09-10 --if-plan <plan>`. Each exits nonzero
        with no Traceback, `tuple(connection.iterdump())` is unchanged after
        each, and the report named by the id is still `planning`. A refusal
        that fires only when `--apply` is present fails on
        `<item id> --who tester`.
      - `--before 2026-09-10T00:00:00Z` exits nonzero with the `cutoff`
        message and no Traceback.
    - Verify: the existing single-item test still passes unchanged.
    - Verify: the new test fails against pack `main`'s `bin/sd_controls.py`
      (unknown flags, exit 2) and passes on the branch.

17. Run the full pack suite.
    - Verify: `run-tests.sh` with `sd_db` from a `pip --target` of a git
      archive of the new pin, on the branch and on pack `main`. The branch's
      failing test names equal main's. Quote both summary lines.
    - Verify: the pack CI `unittest`, `lint` and `route` jobs report the same
      conclusions as on pack `main`, apart from the new passing test.

18. Deployment of the pack, after PR 3 merges.
    - PR 3 changes only pack files. When step 14 has run, the `.venv`
      already holds PR 1's library from step 14b, and this step needs no
      re-provision. When PR 3 merges before step 14, run step 14b first. The
      installer does not read the pin that step 15 moves.
      `bin/sd_install.py --provision-library` installs `sd_db` from the HEAD
      of the local system checkout (`library_pin`), not from the pin.
    - Verify: `.venv/bin/python -I -c "import inspect, sd_db.reporting as r; print(inspect.signature(r.acknowledge_clean), r.cutoff('2026-09-14'))"`
      prints a signature containing `who` and `principal`, then
      `2026-09-14T00:00:00+00:00`.
    - Verify: `sd reports acknowledge --all-clean --before "$(date -u +%F)"`
      on the live store exits 0 and prints its selection. On 2026-09-14 the
      selection was 0. Do not run `--apply` against the live store as part of
      verification.
    - Verify that the dry run made no write. A dump of the live store cannot
      show this, because cron writes heartbeats all day. The dry run connects
      with `write=False`, which opens SQLite with `mode=ro` and `query_only`
      (`database.py:80-81`), and step 16's test pins that call. On the live
      store, `sqlite3 -readonly "file:$HOME/.local/share/sd/sd.db?mode=ro" "SELECT count(*) FROM item WHERE kind='report' AND CASE WHEN json_valid(fields) THEN json_extract(fields, '\$.record') END = 'reports-acknowledge'"`
      prints the same count before and after the command. It printed 0 on
      2026-09-14.

## Rollback

Design.md section 11 gives the reasons. The steps:

- A mistaken apply is accepted. No step reopens reports.
- Code rollback, in this order. The pack CLI and the dashboard stop calling
  PR 1's functions before the `.venv` loses them, so the `.venv` is
  re-provisioned last.
  1. Revert PR 3. Do not re-provision the `.venv`. The revert moves the CI pin
     back, and the pin does not reach the `.venv`.
     Verify: after the operator's pack checkout is on the revert,
     `sd reports acknowledge --help` does not contain `--all-clean`.
  2. Revert PR 2. The operator pulls `~/repos/system` to the revert and
     restarts the dashboard. Do not re-provision the `.venv` yet.
     Verify: `/health` shows `"code_changed": false`, and
     `/operations?area=reports` returns 200 with no `name="clean_before"`.
  3. Revert PR 1, with one precondition. First run
     `grep -rn "record=" local-sd-db/sd_db --include=*.py`. If a caller other
     than `reporting.acknowledge_clean` passes `record=` (sd:754's remove
     verbs), keep the `actor` and `record` keywords in `ingest`, their
     validation and their tests, and revert everything else PR 1 added.
     Otherwise revert PR 1 in full.
     Verify: `sh local-sd-db/sd-db.sh check` has the same failing names as
     before PR 1. When sd:754 is on `main`, its remove tests also pass.
  4. Roll the library back, after the PR 1 revert merges. The operator pulls
     `~/repos/system` to a HEAD that contains the revert, runs
     `bin/sd_install.py --provision-library` in the pack checkout, and
     restarts the dashboard. The installer has no ref input, so it cannot
     install "the old pin". Before it installs, it calls
     `sd_library_guard.downgrade_refusal` (pack `bin/sd_library_guard.py:27-44`).
     That function refuses only when it cannot read a single positive
     integer `SCHEMA_VERSION` from the candidate's or the installed
     `schema.py`, or when the candidate's `SCHEMA_VERSION` is lower than the
     installed one. PR 1 changed no schema: `SCHEMA_VERSION = 9` at
     `e377da7^`, at `e377da7` and at system `main` on 2026-09-14. A revert
     commit carries `main`'s current version, so the guard lets it through.
     A detached checkout of an older commit is refused when a later change
     has raised `SCHEMA_VERSION`, so do not roll back that way.
     Verify: `bin/sd_install.py --provision-library --dry-run` prints
     `would install sd_db from ... at <ref>` for the pulled HEAD, not
     `preserving installed sd_db`. After the install,
     `"$SD_DASHBOARD_PYTHON" -I -c "import sd_db.reporting as r; print(hasattr(r, 'acknowledge_clean'))"`
     prints `False`, and `/health` shows `"ok": true`.
     Without step 3 the library is not rolled back. The `.venv` keeps
     `cutoff`, `clean_candidates`, `clean_reports` and `acknowledge_clean`,
     and after steps 1 and 2 no shipped caller calls them.

## Not in this plan

- The planning adversarial review. It ran at the development "prd and
  design" point (cap 5). Rounds 1 to 3 ran before PR 1. Rounds 4 and 5 ran
  after PR 1 merged, and round 5 spent the cap.
- Any write to the 281 rows (D6).
- sd:754's own record values (`item-remove`, `repo-remove`) and its `item
  remove` refusal. They are agreed in system #336 at head `9b78110b` (design
  section 3 I9 and the "Coordination with sd:755" paragraph under
  Decisions), and sd:754 implements them.

## Landed: PR 2, the dashboard form

Steps 9 to 13 landed as system PR 2 (branch `feat/sd-755-pr2-dashboard-form`),
on 2026-09-16, on base `a5347185`. What is now true:

- `action_route` in `local-project-dashboard/sd_dashboard/server.py` takes a
  required `principal`, and `do_POST` passes `principal=context.principal`
  at its one call site (step 9). `grep -n "= action_route("` prints one
  line, and the repository has no other call site.
- The exact-path branch for `/api/reports/acknowledge-clean` validates
  `{before, plan, who}` and a 64-hex plan, and calls `reporting.cutoff`
  inside the returned function (step 10). The one deliberate run with
  `cutoff` in the `action_route` body failed with
  `http.client.RemoteDisconnected: Remote end closed connection without
  response`, and the file was restored from a byte copy.
- `reports_panel(connection, parameters, *, now)` in
  `local-project-dashboard/sd_dashboard/reports_screen.py` renders the
  preview through `_clean_preview`, at most `PREVIEW_ROWS` (200) rows per
  list, and `operations_page` passes `now=now` (step 11).
- `controls.form` takes `reload_label=None`, rendered as
  `data-reload-label`; the form script reads `form.dataset.reloadLabel`
  in its `result.reload && !capture` branch (step 12). The single-report
  acknowledge form renders without the attribute, and
  `grep -rn reload_label local-project-dashboard/sd_dashboard` shows the
  `form` definition and the apply form only.
- Step 12's browser run is manual and was not run by the lane; it is
  recorded on sd:755 as a manual check.
- Step 13: the dashboard suite on the base, `Ran 421 tests ... OK`; on the
  branch, `Ran 437 tests ... OK`. The 16 new tests and the widened no-token
  loop, run against the base: `Ran 17 tests`,
  `FAILED (failures=21, errors=1)`, the two regression guards passing by
  design.

Where steps 9 to 13 cite `server.py` by line, the numbers are those of the
head the plan was written against, and this PR inserts lines above `do_POST`.
The gate carries those sites in its ratchet and bans the form for new prose,
so they are not renumbered; at this head the same places are these anchors:
`= action_route(`, in `do_POST` of `local-project-dashboard/sd_dashboard/server.py`
(the one call, was line 530); `except (ValueError, UnicodeDecodeError)`, in
`do_POST` of `local-project-dashboard/sd_dashboard/server.py` (the try
around it, was 530-535); `except workflow.StaleItem`, in `do_POST` of
`local-project-dashboard/sd_dashboard/server.py` (the 409, was 548-549);
`except (workflow.WorkflowError, SdDbError, ValueError)`, in `do_POST` of
`local-project-dashboard/sd_dashboard/server.py` (the 400, was 550-551);
`Open the dashboard again before saving`, in `do_POST` of
`local-project-dashboard/sd_dashboard/server.py` (the 403, was 516); and
`name="sd-csrf"`, in `do_GET` of
`local-project-dashboard/sd_dashboard/server.py` (the meta tag, was 419-421).
