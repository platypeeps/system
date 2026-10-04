"""Real HTTP controls, with disposable registry/files and no provider processes."""
import html
import json
import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

from sd_db import (
    connect,
    create_assignment,
    provider_controls,
    reads,
    registry,
    reporting,
    runner,
    skills_catalog,
    workflow,
)
from sd_db.writes import add_note, create_item, set_item_fields, upsert_repo
from sd_dashboard import controls
from sd_dashboard.runner_screen import item_controls as run_controls

from .test_capture_screen import Fields
from .test_workflow_actions import BrowserSession

REGISTRY = """bills:
  a: {cost: subscription}
  b: {cost: subscription}
  c: {cost: plan}
providers:
  claude: {start: "claude -p", vendor: anthropic, bill: a, roles: [author, reviewer], reader: claude-json}
  codex: {start: "codex exec", vendor: openai, bill: b, roles: [author, reviewer], reader: codex-json}
  minimax: {url: "https://example.invalid/v1", model: fixture, vendor: minimax, bill: c, roles: [reviewer]}
roles:
  author: [claude, codex]
  reviewer: [codex, claude, minimax]
"""

class WorkflowControls(BrowserSession):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name).resolve()
        provider_file = root / "providers.yaml"; provider_file.write_text(REGISTRY)
        original = registry.read
        replacement = patch.object(registry, "read", side_effect=lambda path=None, **kw: original(path or provider_file, **kw))
        replacement.start(); self.addCleanup(replacement.stop)
        self.pack = root / "pack"; (self.pack / "skills").mkdir(parents=True)
        (self.pack / "skills/paths.json").write_text(json.dumps({"paths": {"build": {"skills": []}}}))
        (self.pack / "contrib/sd-fixture").mkdir(parents=True)
        (self.pack / "contrib/sd-fixture/SKILL.md").write_text("---\nname: sd-fixture\ndescription: A fixture skill.\n---\nUse for testing.\n")
        replacement = patch.object(skills_catalog, "location", return_value=(self.pack, {}))
        replacement.start(); self.addCleanup(replacement.stop)

    def test_every_new_route_refuses_bad_session_before_work(self):
        before = self.snapshot()
        for path in ("/api/run", "/api/providers/configure", "/api/bills/c/cap", "/api/skills/sd-fixture/try", "/api/items/1/prepare", "/api/runner/1/cancel", "/api/runner/1/requeue", "/api/runner/1/resume", "/api/runner/1/restore", "/api/skill-reviews/1/apply", "/api/reports/1/acknowledge", "/api/reports/acknowledge-clean"):
            self.assertEqual(self.post(path, {}, **{"X-SD-CSRF": "0" * 64})[0], 403, path)
            self.assertEqual(self.request(path)[0], 404, path)
        self.assertEqual(before, self.snapshot())

    def test_provider_atomic_valid_alternate_and_stale_refusal(self):
        state = provider_controls.snapshot(self.connection)
        enabled = {entry["name"]: entry["enabled"] for entry in state["providers"]}; enabled["codex"] = False
        payload = {"revision": state["revision"], "enabled": enabled, "orders": state["orders"]}
        before = self.snapshot()
        self.assertEqual(self.post("/api/providers/configure", payload)[0], 400)
        self.assertEqual(before, self.snapshot())
        payload["orders"]["reviewer"] = ["minimax", "codex", "claude"]
        self.assertEqual(self.post("/api/providers/configure", payload)[0], 200)
        before = self.snapshot()
        self.assertEqual(self.post("/api/providers/configure", payload)[0], 409)
        self.assertEqual(before, self.snapshot())

    def test_provider_subset_round_trips_without_disabling_explicit_provider(self):
        state = provider_controls.snapshot(self.connection)
        payload = {"revision": state["revision"],
            "enabled": {entry["name"]: entry["enabled"] for entry in state["providers"]},
            "orders": {**state["orders"], "reviewer": ["codex", "claude"]}}
        status, _, result = self.post("/api/providers/configure", payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["orders"], payload["orders"])
        minimax = next(entry for entry in result["providers"] if entry["name"] == "minimax")
        self.assertTrue(minimax["enabled"])
        self.assertEqual(minimax["roles"], ["reviewer"])
        self.assertEqual(minimax["ranks"], {})
        self.assertEqual(provider_controls.snapshot(self.connection), result)
        before = self.snapshot()
        self.assertEqual(self.post("/api/providers/configure", payload)[0], 409)
        self.assertEqual(self.snapshot(), before)

    def test_provider_page_explains_automatic_order_without_hiding_enabled_entries(self):
        from sd_dashboard.operations_screen import _provider_controls

        state = provider_controls.snapshot(self.connection)
        provider_controls.configure(self.connection,
            enabled={entry["name"]: entry["enabled"] for entry in state["providers"]},
            orders={**state["orders"], "reviewer": ["codex", "claude"]},
            expected_revision=state["revision"], who="operator")
        page = str(_provider_controls(self.connection))
        self.assertIn("Automatic author order", page)
        self.assertIn("Automatic reviewer order", page)
        self.assertIn("Omitted providers remain available for explicit requests when enabled.", page)
        self.assertIn('name="enabled.minimax"', page)
        self.assertIn('value="codex, claude"', page)

    START_BILL_SENTENCE = "provider 'claude' is a 'start' entry on the capped bill 'a'"
    # `tag` escapes quotes (`markup.escape`, `quote=True`), so the page carries the sentence this way.
    START_BILL_SENTENCE_HTML = html.escape(START_BILL_SENTENCE, quote=True)

    def cap_of(self, state, name):
        return next(bill["cap_usd_month"] for bill in state["bills"] if bill["name"] == name)

    def test_a_cap_posted_on_the_url_bill_is_the_row_and_null_clears_it(self):
        """Slice 12b (sd:234): the route writes through `set_cap` with the
        dashboard's name; the answer is the snapshot, the row carries the
        number, the file's hash is unchanged, and `null` clears it."""
        state = provider_controls.snapshot(self.connection)
        self.assertIsNone(self.cap_of(state, "c"))
        status, _, result = self.post("/api/bills/c/cap", {"revision": state["revision"], "cap_usd_month": 12.5})
        self.assertEqual(status, 200)
        self.assertEqual(self.cap_of(result, "c"), 12.5)
        current = provider_controls.snapshot(self.connection)
        self.assertEqual(self.cap_of(current, "c"), 12.5)
        self.assertEqual(current["revision"], result["revision"])
        self.assertEqual(current["configuration_sha256"], state["configuration_sha256"])
        self.assertEqual(self.connection.execute("SELECT cap_usd_month FROM bill WHERE name='c'").fetchone()[0], 12.5)
        status, _, result = self.post("/api/bills/c/cap", {"revision": current["revision"], "cap_usd_month": None})
        self.assertEqual(status, 200)
        self.assertIsNone(self.cap_of(result, "c"))
        self.assertIsNone(self.connection.execute("SELECT cap_usd_month FROM bill WHERE name='c'").fetchone()[0])

    def test_a_cap_on_a_start_bill_is_the_readers_refusal_and_writes_nothing(self):
        """Clause 15.15 over HTTP: 400, the reader's sentence in the body,
        the snapshot's revision unmoved."""
        state = provider_controls.snapshot(self.connection)
        before = self.snapshot()
        status, _, result = self.post("/api/bills/a/cap", {"revision": state["revision"], "cap_usd_month": 5.0})
        self.assertEqual(status, 400)
        self.assertIn(self.START_BILL_SENTENCE, result["error"])
        self.assertIn("nothing enforces", result["error"])
        self.assertEqual(provider_controls.snapshot(self.connection)["revision"], state["revision"])
        self.assertEqual(before, self.snapshot())

    def test_a_stale_revision_an_unknown_bill_and_a_bad_number_are_refused_by_status(self):
        state = provider_controls.snapshot(self.connection)
        self.assertEqual(self.post("/api/bills/c/cap", {"revision": state["revision"], "cap_usd_month": 1.0})[0], 200)
        before = self.snapshot()
        status, _, result = self.post("/api/bills/c/cap", {"revision": state["revision"], "cap_usd_month": 2.0})
        self.assertEqual(status, 409)
        self.assertTrue(result["reload"])
        current = provider_controls.snapshot(self.connection)
        status, _, result = self.post("/api/bills/d/cap", {"revision": current["revision"], "cap_usd_month": 2.0})
        self.assertEqual(status, 400)
        self.assertIn("no bill 'd'", result["error"])
        for bad in (-1.0, "12", True, {"usd": 1}):
            status, _, result = self.post("/api/bills/c/cap", {"revision": current["revision"], "cap_usd_month": bad})
            self.assertEqual(status, 400, bad)
            self.assertIn("finite, non-negative number", result["error"])
        self.assertEqual(self.post("/api/bills/c/cap", {"revision": current["revision"]})[0], 400)
        self.assertEqual(self.post("/api/bills/c/cap", {"revision": current["revision"], "cap_usd_month": 2.0, "who": "x"})[0], 400)
        self.assertEqual(self.post("/api/bills/c%2F..%2Fx/cap", {"revision": current["revision"], "cap_usd_month": 2.0})[0], 404)
        # The bound is the services route's, 200 characters (#435's verification
        # round): at it the name reaches the library, which has no such bill;
        # past it the route answers 404 before any connection is opened.
        status, _, result = self.post("/api/bills/" + "x" * 200 + "/cap", {"revision": current["revision"], "cap_usd_month": 2.0})
        self.assertEqual(status, 400)
        self.assertIn("no bill 'xxx", result["error"])
        self.assertEqual(self.post("/api/bills/" + "x" * 201 + "/cap", {"revision": current["revision"], "cap_usd_month": 2.0})[0], 404)
        self.assertEqual(before, self.snapshot())

    def test_the_usage_area_renders_one_cap_form_per_url_bill_with_the_current_cap_and_revision(self):
        state = provider_controls.snapshot(self.connection)
        self.assertEqual(self.post("/api/bills/c/cap", {"revision": state["revision"], "cap_usd_month": 12.5})[0], 200)
        current = provider_controls.snapshot(self.connection)
        page = self.request("/operations?area=usage")[2]
        forms = re.findall(r'<form\b[^>]*action="/api/bills/([^"]+)/cap"[^>]*>.*?</form>', page, re.S)
        self.assertEqual(forms, ["c"], "one form, for the one bill no start entry is billed to")
        form = re.search(r'<form\b[^>]*action="/api/bills/c/cap"[^>]*>.*?</form>', page, re.S).group(0)
        self.assertIn(f'<input type="hidden" name="revision" value="{current["revision"]}">', form)
        self.assertIn("$12.50", form)
        self.assertRegex(form, r'<input[^>]*name="cap_usd_month"[^>]*type="number"')
        self.assertRegex(form, r'<input[^>]*name="cap_usd_month"[^>]*value="12.5"')
        self.assertIn('data-workflow-form', form)
        self.assertNotIn(self.START_BILL_SENTENCE_HTML, page)
        for bill in ("a", "b"):
            self.assertRegex(page, rf"<p[^>]*>{bill}: no cap can be set here; a &#x27;start&#x27; entry is billed to it\.</p>")

    def test_the_script_sends_the_cap_as_a_number_or_null_and_returns_to_the_usage_area(self):
        """A form field is a string and the route takes a number or `null`;
        the configure form's handling covers neither, so the script carries
        two lines for this form. Pinned by text, as `test_criterion_7` pins
        the palette's selector: no browser runs here.

        #435's review: `Number("abc")` is `NaN`, which JSON carries as `null`,
        and `null` is the clear -- so a tampered field would have deleted the
        cap. Only a blank field becomes `null`; a value that is not a finite
        decimal goes as its string, and the library refuses it (below)."""
        script = self.request("/static/dashboard.js")[2]
        self.assertNotIn('null : Number(values.cap_usd_month)', script)
        # The number control's own grammar (`.5`, `+1`, `1.`, `1e3`; #435's verification round).
        self.assertIn('values.cap_usd_month = cap === "" ? null : /^[-+]?(\\d+\\.?\\d*|\\.\\d+)([eE][-+]?\\d+)?$/.test(cap) && Number.isFinite(Number(cap)) ? Number(cap) : cap;', script)
        self.assertIn('action.startsWith("/api/bills/") ? "/operations?area=usage"', script)

    def test_a_string_cap_is_the_ledgers_refusal_and_the_cap_stays(self):
        """The route hands a string straight to `set_cap`, and `ledger._money`
        refuses it with 400; nothing is cleared. This is what the script's
        raw-string fallback relies on."""
        state = provider_controls.snapshot(self.connection)
        self.assertEqual(self.post("/api/bills/c/cap", {"revision": state["revision"], "cap_usd_month": 12.5})[0], 200)
        current = provider_controls.snapshot(self.connection)
        before = self.snapshot()
        status, _, result = self.post("/api/bills/c/cap", {"revision": current["revision"], "cap_usd_month": "abc"})
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "a cap must be a finite, non-negative number; bill 'c' was given 'abc'")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.cap_of(provider_controls.snapshot(self.connection), "c"), 12.5)

    def test_a_legacy_row_cap_on_a_start_bill_renders_the_readers_warning(self):
        provider_controls.snapshot(self.connection)  # seeds the rows the bare UPDATE below needs
        with self.connection:
            self.assertEqual(self.connection.execute("UPDATE bill SET cap_usd_month = 5.0 WHERE name = 'a'").rowcount, 1)
        page = self.request("/operations?area=usage")[2]
        self.assertIn(self.START_BILL_SENTENCE_HTML, page)
        self.assertRegex(page, r'<ul class="notice[^"]*">\s*<li>[^<]*' + re.escape(self.START_BILL_SENTENCE_HTML))
        # The repair the library names, `set_cap(..., None)`, is the one save a
        # start bill's row gets: a clear-only form, no number field.
        self.assertEqual(re.findall(r'action="/api/bills/([^"]+)/cap"', page), ["a", "c"])
        clear = re.search(r'<form\b[^>]*action="/api/bills/a/cap"[^>]*>.*?</form>', page, re.S).group(0)
        self.assertIn('<input type="hidden" name="cap_usd_month" value="">', clear)
        self.assertNotIn('type="number"', clear)
        self.assertIn("Clear the legacy cap for a", clear)
        state = provider_controls.snapshot(self.connection)
        self.assertIn(f'name="revision" value="{state["revision"]}"', clear)
        status, _, result = self.post("/api/bills/a/cap", {"revision": state["revision"], "cap_usd_month": None})
        self.assertEqual(status, 200)
        self.assertEqual(result["warnings"], [])
        self.assertIsNone(self.connection.execute("SELECT cap_usd_month FROM bill WHERE name='a'").fetchone()[0])
        page = self.request("/operations?area=usage")[2]
        self.assertNotIn(self.START_BILL_SENTENCE_HTML, page)
        self.assertEqual(re.findall(r'action="/api/bills/([^"]+)/cap"', page), ["c"])

    def test_stale_batch_creates_nothing_then_parallel_cancel_and_requeue(self):
        self.repo()
        items = [self.item(str(n), kind="task", repo="/repos/system", branch=f"task/{n}", status="ready") for n in range(2)]
        payload = {"items": items, "parallel": True, "revisions": {str(item): workflow.item_state(self.connection, item)["revision"] for item in items}}
        set_item_fields(self.connection, items[1], title="new")
        before = self.snapshot()
        self.assertEqual(self.post("/api/run", payload)[0], 400)
        self.assertEqual(before, self.snapshot())
        payload["revisions"] = {str(item): workflow.item_state(self.connection, item)["revision"] for item in items}
        status, _, result = self.post("/api/run", payload)
        self.assertEqual(status, 200); self.assertEqual(len(result["assignments"]), 2)
        assignment = result["assignments"][0]
        self.assertEqual(self.post(f"/api/runner/{assignment['id']}/cancel", {"revision": assignment["revision"]})[0], 200)
        current = runner.queue_state(self.connection, assignment["id"])
        self.assertEqual(current["status"], "cancelled")
        self.assertEqual(self.post(f"/api/runner/{assignment['id']}/requeue", {"revision": current["revision"]})[0], 200)
        self.assertEqual(runner.queue_state(self.connection, assignment["id"])["status"], "queued")

    def prepared_followup(self):
        """sd:809. A task prepared for a run and then reclassified keeps its
        repository and branch, so only its kind can refuse the run."""
        self.repo()
        prepared = self.item("Prepared then reclassified", kind="task", repo="/repos/system", branch="task/prepared", status="planning")
        workflow.edit_item(self.connection, prepared, {"kind": "followup"}, who="operator")
        return prepared

    def test_a_followup_with_a_repository_and_a_branch_is_not_offered_a_run(self):
        prepared = self.prepared_followup()
        runnable = self.item("Still a task", kind="task", repo="/repos/system", branch="task/runnable", status="planning")
        backlog = self.request("/classic/backlog")[2]
        boxes = {item: re.search(rf'<input[^>]*id="run-item-{item}"[^>]*>', backlog) for item in (prepared, runnable)}
        self.assertIsNotNone(boxes[prepared])
        self.assertIn(" disabled", boxes[prepared].group(0))
        self.assertIsNotNone(boxes[runnable])
        self.assertNotIn(" disabled", boxes[runnable].group(0))
        self.assertIn(f"item {prepared} is a followup item; an agent runs only work, task, report and skill-review items", backlog)

    def test_the_run_route_refuses_a_followup_with_a_repository_and_a_branch(self):
        prepared = self.prepared_followup()
        revision = workflow.item_state(self.connection, prepared)["revision"]
        before = self.snapshot()
        status, _, result = self.post("/api/run", {"items": [prepared], "revisions": {str(prepared): revision}})
        self.assertEqual(status, 400)
        self.assertIn(f"item {prepared} is a followup item", json.dumps(result))
        self.assertEqual(before, self.snapshot())

    def test_catalog_and_new_operations_reads_do_not_write(self):
        before = self.snapshot()
        with patch("subprocess.run", side_effect=AssertionError("catalog invoked a process")):
            self.assertEqual(self.request("/classic/skills")[0], 200)
            self.assertIn("Start 30-day trial", self.request("/classic/skills")[2])
            self.assertEqual(self.request("/operations?area=reports")[0], 200)
            self.assertEqual(self.request("/operations?area=progress")[0], 200)
        self.assertEqual(self.request("/favicon.ico")[0], 204)
        self.assertEqual(before, self.snapshot())
        skill = skills_catalog.catalog(self.connection)["skills"][0]
        status, _, _ = self.post("/api/skills/sd-fixture/try", {"revision": skill["revision"]})
        self.assertEqual(status, 200)
        before = self.snapshot()
        self.assertEqual(self.post("/api/skills/sd-fixture/try", {"revision": skill["revision"]})[0], 409)
        self.assertEqual(before, self.snapshot())

    def test_offline_service_control_binds_revision_run_and_refuses_missing_install(self):
        self.repo()
        item = self.item("Owned", kind="task", repo="/repos/system", branch="main", status="ready")
        assignment = runner.enqueue(self.connection, [item], who="operator")[0]["id"]
        root = Path(self.tmp.name)
        runner.claim(self.connection, assignment, owner="fixture", work_root=root / "work", retention_root=root / "retained")
        current = runner.queue_state(self.connection, assignment)
        calls = []
        def backend(installation, verb, identity, **options):
            calls.append((verb, identity, options))
            return {"id": identity, "control": {"signalled_owned_group": True}}
        self.listening.RequestHandlerClass.runner_backend = staticmethod(backend)
        before = self.snapshot()
        with patch("sd_db.runner_controls.service_installation", return_value={"database": str(self.path)}):
            self.assertEqual(self.post(f"/api/runner/{assignment}/cancel", {"revision": "0" * 64})[0], 409)
            self.assertEqual(calls, [])
            status, _, result = self.post(f"/api/runner/{assignment}/cancel", {"revision": current["revision"]})
        self.assertEqual(status, 200); self.assertTrue(result["control"]["signalled_owned_group"])
        self.assertEqual(calls[0][2]["run"], current["run"]["id"])
        self.assertEqual(before, self.snapshot())
        with patch("sd_db.runner_controls.service_installation", side_effect=workflow.WorkflowError("Runner controls are unavailable until installed")):
            status, _, result = self.post(f"/api/runner/{assignment}/cancel", {"revision": current["revision"]})
        self.assertEqual(status, 400); self.assertIn("unavailable", result["error"])
        self.assertEqual(len(calls), 1)

    def test_resume_and_restore_bind_owned_attempt_and_new_destination(self):
        self.repo()
        item = self.item("Kept", kind="task", repo="/repos/system", branch="main", status="ready")
        assignment = runner.enqueue(self.connection, [item], who="operator")[0]["id"]
        root = Path(self.tmp.name)
        held = runner.claim(self.connection, assignment, owner="fixture", work_root=root / "work", retention_root=root / "retained")["run"]
        runner.begin_ending(self.connection, held["id"], outcome="blocked", detail="fixture")
        runner.update_run(self.connection, held["id"], end_step="kept")
        current = runner.queue_state(self.connection, assignment)
        calls = []
        self.listening.RequestHandlerClass.runner_backend = staticmethod(lambda installation, verb, identity, **options: calls.append((verb, options)) or {"accepted": True})
        with patch("sd_db.runner_controls.service_installation", return_value={"database": str(self.path)}):
            self.assertEqual(self.post(f"/api/runner/{assignment}/resume", {"revision": current["revision"]})[0], 200)
            self.assertEqual(calls[0][0], "resume")
            runner.update_run(self.connection, held["id"], end_step="retained", quarantine=None)
            runner.release(self.connection, held["id"])
            current = runner.queue_state(self.connection, assignment)
            self.assertEqual(self.post(f"/api/runner/{assignment}/restore", {"revision": current["revision"], "destination": str(root)})[0], 400)
            destination = str(root / "restored")
            self.assertEqual(self.post(f"/api/runner/{assignment}/restore", {"revision": current["revision"], "destination": destination})[0], 200)
        self.assertEqual(len(calls), 2); self.assertEqual(calls[1][1]["destination"], destination)
        self.assertEqual(calls[1][1]["run"], held["id"])

    def test_usage_does_not_treat_missing_costs_as_measured_zero(self):
        item = self.item("Delivered", kind="task", status="done")
        self.connection.execute("UPDATE item SET shipped_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?", (item,))
        page = self.request("/operations?area=usage")[2]
        self.assertIn("No bills yet.", page)
        self.assertIn("not recorded", page)
        self.assertNotIn("$0.00", page)

    def test_report_followups_gate_acknowledgement(self):
        state = reporting.ingest(self.connection, job="fixture", run_id="one", started="2026-09-08T00:00:00Z", ended="2026-09-08T00:01:00Z", exit_code=1, text="Failed", source_path="/fixture/log", attention=True)
        item = state["item"]["id"]
        before = self.snapshot()
        self.assertEqual(self.post(f"/api/reports/{item}/acknowledge", {"revision": state["revision"]})[0], 400)
        self.assertEqual(before, self.snapshot())
        self.assertIn("Report source", self.request(f"/item/{item}")[2])

    def test_a_folded_report_shows_its_repeats_and_a_single_one_does_not(self):
        """sd:920 -- the count and the last run are what the operator needs
        from a job that fails the same way on a schedule; one `Repeats` row
        in the provenance list, only when `fields.repeats` exists."""
        runs = dict(started="2026-09-08T00:00:00Z", exit_code=1, text="Failed", source_path="/fixture/log", attention=True)
        state = reporting.ingest(self.connection, job="fixture", run_id="one", ended="2026-09-08T00:01:00Z", **runs)
        item = state["item"]["id"]
        page = self.request(f"/item/{item}")[2]
        self.assertIn("Report source", page)
        self.assertNotIn("Repeats", page)
        for run, ended in (("two", "2026-09-08T00:16:00Z"), ("three", "2026-09-08T00:31:00Z")):
            self.assertEqual(reporting.ingest(self.connection, job="fixture", run_id=run, ended=ended, **runs)["item"]["id"], item)
        page = self.request(f"/item/{item}")[2]
        self.assertIn("<dt>Repeats</dt><dd>3 runs, last 2026-09-08T00:31:00+00:00</dd>", page)
        self.assertIn("<dt>Ended</dt><dd>2026-09-08T00:01:00+00:00</dd>", page)
        self.assertEqual(page.count("Report source"), 1)

    def test_a_report_whose_fields_are_not_json_renders_with_its_controls_disabled(self):
        """sd:775 -- `item.fields` carries no `json_valid` check, so the page
        must survive a row it cannot parse.

        Unguarded, `report_controls` raised `JSONDecodeError` through
        `screens.item`; `do_GET` catches `MissingItem`, `NotFound` and the
        database errors and not `ValueError`, so the reader got no response at
        all. Fail closed: the page renders, the reason is on it, and the
        acknowledge form is withheld rather than offered against a source
        nothing could read. `reporting.acknowledge` never parses `fields`, so
        the command line the notice names still finishes the report.
        """
        state = reporting.ingest(self.connection, job="fixture", run_id="two", started="2026-09-08T00:00:00Z", ended="2026-09-08T00:01:00Z", exit_code=0, text="Fine", source_path="/fixture/log")
        item = state["item"]["id"]
        self.connection.execute("UPDATE item SET fields=? WHERE id=?", ("{not json", item))
        self.connection.commit()
        status, _, page = self.request(f"/item/{item}")
        self.assertEqual(status, 200)
        self.assertIn("Report source", page)
        self.assertIn("not valid JSON", page)
        self.assertNotIn(f'action="/api/reports/{item}/acknowledge"', page)
        self.assertNotIn("Acknowledge report", page)
        revision = workflow.item_state(self.connection, item)["revision"]
        finished = reporting.acknowledge(self.connection, item, expected_revision=revision, who="fixture")
        self.assertEqual(finished["item"]["status"], "done")

    def test_absent_empty_and_malformed_fields_withhold_the_acknowledge_alike(self):
        """sd:873 -- one meaning for a `fields` that cannot say the report is
        clean: NULL, '' and `{not json` are one case, here as at retention,
        the bulk clean and the backlog (`reporting.UNREADABLE_FIELDS`).

        Before, `json.loads(row["fields"] or "{}")` turned NULL and '' into an
        empty document, so the page rendered every provenance field as "Not
        recorded" and offered the one-click acknowledge -- the control that
        would close, unexamined, the row retention and the bulk clean had
        declined so that a person would look at it. This page is that
        person's surface: it renders the row, says why it is here, and leaves
        the finishing to the command line, whose acknowledge names a `who`.

        `NaN` and `{"a": Infinity}` are the review's case: `json.loads`
        accepts those constants and SQLite's `json_valid` does not, so a row
        holding either was unreadable to retention and the bulk clean while
        the page parsed it and offered the form. The two nested arrays are
        the verification review's: SQLite's `json_valid` stops at its depth
        limit (1000 in 3.53), Python's decoder reads on to its own recursion
        limit, so 1100 deep the page parsed what retention refused and 10000
        deep it raised `RecursionError` past `do_GET`, and the reader got no
        response. The page now asks SQLite first, so its verdict is theirs.
        """
        for index, value in enumerate((None, "", "{not json", "NaN", '{"a": Infinity}',
                                       "[" * 1100 + "]" * 1100, "[" * 10000 + "]" * 10000)):
            with self.subTest(fields=value[:24] if isinstance(value, str) else value):
                state = reporting.ingest(self.connection, job="fixture", run_id=f"absent-{index}",
                                         started="2026-09-08T00:00:00Z", ended="2026-09-08T00:01:00Z",
                                         exit_code=0, text="Fine", source_path="/fixture/log")
                item = state["item"]["id"]
                self.connection.execute("UPDATE item SET fields=? WHERE id=?", (value, item))
                self.connection.commit()
                status, _, page = self.request(f"/item/{item}")
                self.assertEqual(status, 200)
                self.assertIn("Report source", page)
                self.assertIn("not valid JSON", page)
                self.assertIn(f"sd reports acknowledge {item}", page)
                self.assertNotIn("Not recorded", page)
                self.assertNotIn(f'action="/api/reports/{item}/acknowledge"', page)
                self.assertNotIn("Acknowledge report", page)
                revision = workflow.item_state(self.connection, item)["revision"]
                finished = reporting.acknowledge(self.connection, item, expected_revision=revision, who="fixture")
                self.assertEqual(finished["item"]["status"], "done")


class ItemRepository(BrowserSession):
    """sd:452 -- the item screen's change set matches `sd task edit`'s.

    `repo` was the one `workflow.USER_FIELDS` member the screen never offered,
    so a task could only be attached to or detached from a checkout from the
    CLI. The write path already existed; what is new is the widget.
    """

    def details_form(self, item):
        """The `Save details` form only.

        `runner_screen.item_controls` renders a second `name="repo"` select on
        the same page, from a *different* option set, so a whole-page parse
        would read whichever came last and prove nothing about this field.
        """
        panel = str(controls.item_controls(self.connection, item))
        return re.search(rf'<form[^>]*action="/api/items/{item}"[^>]*>.*?</form>', panel).group(0)

    def test_the_change_set_matches_sd_task_edit_and_the_blank_choice_is_first(self):
        remote_less = "/repos/no-remote"
        upsert_repo(self.connection, remote_less)
        self.repo("/repos/system")
        item = self.item("Unattached", kind="task")
        markup = self.details_form(item)
        parsed = Fields(markup)

        # The emitted field list, which is the deliverable. `revision` is the
        # hidden stale-edit guard; the rest are `workflow.USER_FIELDS`.
        self.assertEqual(sorted(parsed.fields), ["body", "due", "kind", "priority", "repo", "revision", "title"])
        self.assertEqual(sorted(workflow.USER_FIELDS | {"revision"}), sorted(parsed.fields))
        self.assertIn("data-changed-fields", markup)

        options = parsed.options["repo"]
        self.assertEqual([entry["attributes"]["value"] for entry in options],
                         ["", remote_less, "/repos/system"])
        self.assertEqual(options[0]["text"], "No repository")
        # A checkout with no remote is still a checkout a task can sit in, so
        # this set is wider than the runner's. The runner filters on `remote`
        # because a run has to clone; belonging does not.
        self.assertIn(remote_less, [entry["attributes"]["value"] for entry in options])
        run_repo = Fields(str(run_controls(self.connection, reads.item_by_id(self.connection, item))))
        self.assertNotIn(remote_less, [entry["attributes"]["value"] for entry in run_repo.options["repo"]])

        # Nothing is marked selected, so the browser selects the first option
        # and `dataset.initialValue` becomes "" -- an unchanged field, which
        # `data-changed-fields` omits rather than sending as a null.
        self.assertEqual([entry for entry in options if "selected" in entry["attributes"]], [])

    def test_set_then_clear_then_an_unrelated_edit_are_three_different_payloads(self):
        self.repo("/repos/system")
        item = self.item("Moves house", kind="task")
        self.assertIsNone(workflow.item_state(self.connection, item)["item"]["repo"])

        # 1. Set: the value the widget submits when a path is chosen.
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "repo": "/repos/system"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["repo"], "/repos/system")
        self.assertIn('value="/repos/system" selected', self.details_form(item))

        # 2. Clear: `dashboard.js` turns the blank option's "" into JSON null,
        # the same write the CLI's `--no-repo` makes.
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "repo": None})
        self.assertEqual(status, 200)
        self.assertIsNone(state["item"]["repo"])

        # 3. Leave it alone: an *absent* key, which is not a null. An omitted
        # key never reaches `workflow._fields`, so a title edit on a task that
        # has a repository must not detach it.
        revision = workflow.item_state(self.connection, item)["revision"]
        self.assertEqual(self.post(f"/api/items/{item}",
            {"revision": revision, "repo": "/repos/system"})[0], 200)
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "title": "Renamed"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["title"], "Renamed")
        self.assertEqual(state["item"]["repo"], "/repos/system")

    def test_the_option_set_already_contains_every_path_a_task_row_can_hold(self):
        """Why there is no `No longer registered` fallback option.

        `priority` needs one -- `item.priority` has no constraint, so the
        column holds values the form never offered and `item_controls` appends
        the stored one. `item.repo` is `REFERENCES repo(path) ON DELETE
        RESTRICT`: the database refuses to strand a task on a path it does not
        hold, so `_checkouts` enumerating the table is already complete.
        """
        self.repo("/repos/gone")
        item = self.item("Filed against a checkout", kind="task", repo="/repos/gone")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM repo WHERE path = '/repos/gone'")
        self.connection.rollback()
        options = Fields(self.details_form(item)).options["repo"]
        self.assertEqual([entry["attributes"]["value"] for entry in options], ["", "/repos/gone"])
        self.assertEqual([entry["attributes"]["value"] for entry in options
                          if "selected" in entry["attributes"]], ["/repos/gone"])
        # Selected, so `initialValue` is the stored path: an unrelated edit
        # leaves the field unchanged and sends no `repo` key.
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "title": "Renamed"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["repo"], "/repos/gone")

    def test_a_work_item_is_offered_no_repository_field_because_the_library_refuses_one(self):
        first = self.repo("/repos/system")
        upsert_repo(self.connection, first, status_source="row")
        other = self.repo("/repos/other")
        upsert_repo(self.connection, other, status_source="row")
        item = self.item("Work with a source identity", kind="work", repo=first, path="docs/work/x/prd.md")
        markup = self.details_form(item)
        self.assertNotIn("repo", Fields(markup).fields)
        # Why the field is absent rather than present: the library refuses the
        # reassignment, and the refusal takes the whole save with it.
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, result = self.post(f"/api/items/{item}",
            {"revision": revision, "title": "Renamed", "repo": other})
        self.assertEqual(status, 400)
        self.assertIn("source identity", result["error"])
        self.assertEqual(workflow.item_state(self.connection, item)["item"]["title"],
                         "Work with a source identity")


class ItemKind(BrowserSession):
    """sd:743 -- the item screen reclassifies a task through `edit_item`."""

    details_form = ItemRepository.details_form

    def test_a_task_offers_the_hand_kinds_and_a_work_item_offers_none(self):
        item = self.item("Errand", kind="task")
        options = Fields(self.details_form(item)).options["kind"]
        self.assertEqual([entry["attributes"]["value"] for entry in options], list(workflow.HAND_KINDS))
        self.assertEqual([entry["attributes"]["value"] for entry in options
                          if "selected" in entry["attributes"]], ["task"])
        self.repo("/repos/system")
        self.connection.execute("UPDATE repo SET status_source = 'row' WHERE path = '/repos/system'")
        self.connection.commit()
        work = self.item("Work", kind="work", repo="/repos/system")
        self.assertNotIn('name="kind"', str(controls.item_controls(self.connection, work)))

    def test_a_store_older_than_migration_009_offers_only_the_kinds_its_check_admits(self):
        """The page reads through `connect(write=False)`, which accepts a store
        that predates the migration widening `item.kind`; on one, `edit_item`
        refuses the four kinds it added, so the select must not offer them.
        The store here is narrowed the way migration 009 widened it: the CHECK
        text edited in place under `writable_schema`, with a schema-cookie
        bump so the next connection reads the narrow list. Only the CHECK is
        older; the property under test is the CHECK and nothing else. The
        edit goes through the library's own `connect`, because
        `local-sd-db/tests/test_one_store.py` is the one place that may open
        this file any other way.
        """
        item = self.item("Errand", kind="task")
        narrower = connect(self.path)
        current = narrower.execute("SELECT sql FROM sqlite_master WHERE name = 'item'").fetchone()[0]
        narrow = re.sub(r"'dep',\s*'personal', 'followup',\s*'work-idea', 'personal-idea'\)\)", "'dep'))", current)
        self.assertNotEqual(narrow, current, "the CHECK did not carry the four kinds, so this proves nothing")
        cookie = narrower.execute("PRAGMA schema_version").fetchone()[0]
        narrower.execute("PRAGMA writable_schema = ON")
        narrower.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'item'", (narrow,))
        # The cookie tells every other connection its cached schema is stale.
        narrower.execute(f"PRAGMA schema_version = {cookie + 1}")
        narrower.execute("PRAGMA writable_schema = RESET")
        narrower.commit()
        narrower.close()
        reader = connect(self.path, write=False)
        self.addCleanup(reader.close)
        self.assertEqual(workflow.schema_kinds(reader), tuple(kind for kind in workflow.schema_kinds(reader)
                                                             if kind not in ("personal", "followup", "work-idea", "personal-idea")))
        panel = str(controls.item_controls(reader, item))
        form = re.search(rf'<form[^>]*action="/api/items/{item}"[^>]*>.*?</form>', panel).group(0)
        options = Fields(form).options["kind"]
        self.assertEqual([entry["attributes"]["value"] for entry in options], ["task"])
        # What the select offers is what the library accepts, and nothing
        # more: the same store, through a writer, refuses a withheld kind.
        writer = connect(self.path)
        self.addCleanup(writer.close)
        with self.assertRaisesRegex(workflow.WorkflowError, "no item kind 'personal'"):
            workflow.edit_item(writer, item, {"kind": "personal"}, who="test",
                               expected_revision=workflow.item_state(writer, item)["revision"])

    def test_a_kind_change_is_written_and_noted_and_a_produced_kind_is_refused(self):
        item = self.item("Errand", kind="task")
        revision = workflow.item_state(self.connection, item)["revision"]
        before = self.snapshot()
        self.assertEqual(self.post(f"/api/items/{item}", {"revision": revision, "kind": "report"})[0], 400)
        self.assertEqual(before, self.snapshot())
        status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "kind": "personal"})
        self.assertEqual(status, 200)
        self.assertEqual(state["item"]["kind"], "personal")
        self.assertEqual((state["notes"][-1]["body"], state["notes"][-1]["session"]),
                         ("Changed kind task -> personal by dashboard", "dashboard"))


class RunPanelKinds(BrowserSession):
    """sd:824 (N-a) -- the run panel draws for `runner.RUNNABLE_KINDS` and no other kind.

    Nothing in the panel can queue a run for another kind: `runner._item`
    refuses it, so `readiness` reports the refusal and `Queue assignment` is
    never rendered. What a kind that slipped into this check would still draw
    is a "Run with an agent" heading and, for a `followup`, the run setup form
    -- an offer over a row that can never take it. Until this test, a mutation
    adding `followup` to the check passed every dashboard test.

    The kinds come from `item.kind`'s CHECK and not from a list written here,
    so a kind a migration adds is a kind this already covers. The CHECK is
    read by `workflow.schema_kinds`, the library's own parser, and not by a
    second one here: a copy assumed one spelling of the clause and answered
    `AttributeError` on any other, where the library refuses by name (PR
    #370 review).
    """

    def schema_kinds(self):
        return sorted(workflow.schema_kinds(self.connection))

    def panel(self, kind):
        item = self.item(f"A {kind} item", kind=kind, repo=self.repo(), branch="work/panel")
        return str(run_controls(self.connection, reads.item_by_id(self.connection, item)))

    def test_only_the_runnable_kinds_draw_the_run_panel(self):
        kinds = self.schema_kinds()
        # The CHECK is the whole population, so every runnable kind is in it.
        self.assertEqual(sorted(set(runner.RUNNABLE_KINDS)), sorted(set(runner.RUNNABLE_KINDS) & set(kinds)))
        self.assertIn("followup", kinds)
        for kind in kinds:
            with self.subTest(kind=kind):
                markup = self.panel(kind)
                if kind in runner.RUNNABLE_KINDS:
                    self.assertIn("Run with an agent", markup)
                else:
                    self.assertEqual(markup, "")


class ItemStatusOnly(BrowserSession):
    """sd:772 -- a personal item gets the status control alone.

    `workflow.allowed_statuses` offers a personal item the task statuses
    (sd:768) and `workflow.edit_item` refuses its details, so the panel splits:
    the status form renders and the `Save details` form does not.

    A followup shared this branch until sd:816. `edit_item` stopped refusing
    its details at sd:809, so the panel now renders both forms for it and
    `FollowupDetails` below owns what the details form holds.
    """

    def panel(self, item):
        return str(controls.item_controls(self.connection, item))

    def status_form(self, item):
        found = re.search(rf'<form[^>]*action="/api/items/{item}/status"[^>]*>.*?</form>', self.panel(item))
        return found.group(0) if found else None

    def details_form(self, item):
        return re.search(rf'<form[^>]*action="/api/items/{item}"[^>]*>', self.panel(item))

    def test_followup_and_personal_offer_the_task_statuses(self):
        for kind in ("followup", "personal"):
            with self.subTest(kind=kind):
                item = self.item(f"A {kind} item", kind=kind)
                markup = self.status_form(item)
                self.assertIsNotNone(markup)
                options = Fields(markup).options["status"]
                self.assertEqual([entry["attributes"]["value"] for entry in options],
                                 list(workflow.TASK_STATUSES))
                self.assertEqual([entry["attributes"]["value"] for entry in options
                                  if "selected" in entry["attributes"]], ["planning"])

    def test_a_personal_item_gets_no_details_form_and_a_followup_now_does(self):
        """sd:816 -- the hint that named `sd task edit ID` is gone, because
        the form it stood in for is on the panel. A personal item keeps the
        hint: `edit_item` still refuses its details.
        """
        personal = self.item("A personal item", kind="personal")
        self.assertIsNone(self.details_form(personal))
        self.assertNotIn('name="title"', self.panel(personal))
        self.assertNotIn("sd task edit", self.panel(personal))
        self.assertIn("Its other fields keep their own editing workflow.", self.panel(personal))

        followup = self.item("A followup item", kind="followup")
        self.assertIsNotNone(self.details_form(followup))
        self.assertIn('name="title"', self.panel(followup))
        self.assertNotIn("with sd task edit", self.panel(followup))

    def test_the_rendered_status_form_closes_and_reopens_a_followup(self):
        item = self.item("Come back to this", kind="followup")
        for target in ("done", "planning"):
            revision = Fields(self.status_form(item)).fields["revision"]["value"]
            status, _, state = self.post(f"/api/items/{item}/status", {"revision": revision, "status": target})
            self.assertEqual((status, state["item"]["status"]), (200, target))
            selected = [entry["attributes"]["value"] for entry in Fields(self.status_form(item)).options["status"]
                        if "selected" in entry["attributes"]]
            self.assertEqual(selected, [target])

    def test_a_followup_with_a_queued_assignment_shows_the_hint_and_no_select(self):
        """The queued run owns the status, not the details. A task with a
        queued assignment keeps its `Save details` form for the same reason:
        `workflow.edit_item` consults no assignment, so withholding the form
        would hide a save the library still accepts.
        """
        item = self.item("Queued", kind="followup")
        create_assignment(self.connection, item=item, role="author", status="queued")
        panel = self.panel(item)
        self.assertIn("Status is controlled by the active workflow.", panel)
        self.assertIsNone(self.status_form(item))
        self.assertIsNotNone(self.details_form(item))

    def test_a_personal_item_a_run_owns_is_not_also_told_it_can_finish_here(self):
        # The personal branch printed "can finish or reopen here" beside the
        # line saying the workflow owns the status (PR #344 review).
        item = self.item("Owned by a run", kind="personal")
        create_assignment(self.connection, item=item, role="author", status="queued")
        panel = self.panel(item)
        self.assertIn("Status is controlled by the active workflow.", panel)
        self.assertNotIn("can finish or reopen here", panel)
        self.assertIn("Its other fields keep their own editing workflow.", panel)
        free = self.item("Nobody's run", kind="personal")
        self.assertIn("A personal item can finish or reopen here.", self.panel(free))

    def test_task_and_work_keep_both_forms(self):
        task = self.item("Errand", kind="task")
        self.assertIsNotNone(self.details_form(task))
        self.assertEqual([entry["attributes"]["value"] for entry in Fields(self.status_form(task)).options["status"]],
                         list(workflow.TASK_STATUSES))
        self.repo("/repos/system")
        upsert_repo(self.connection, "/repos/system", status_source="row")
        work = self.item("Work", kind="work", repo="/repos/system")
        self.assertIsNotNone(self.details_form(work))
        self.assertEqual([entry["attributes"]["value"] for entry in Fields(self.status_form(work)).options["status"]],
                         workflow.allowed_statuses(self.connection, work))
        self.assertNotIn("done", workflow.allowed_statuses(self.connection, work))

    def test_kinds_with_their_own_workflow_still_render_no_item_controls(self):
        for kind in ("idea", "work-idea", "personal-idea", "report"):
            with self.subTest(kind=kind):
                self.assertEqual(self.panel(self.item(f"A {kind} item", kind=kind)), "")


class HostileItemStorage(BrowserSession):
    """sd:870 and sd:874 -- the item screen survives what the store permits.

    `item.body` and `item.fields` are plain `TEXT` with no `json_valid` check
    (`sd_db/schema/001_initial.sql:51-52`), so a row neither column can be read
    from is a row the store accepts. sd:775 guarded `fields` on the report
    panel; `screens.artifact` and `controls.item_controls` read `body` the same
    way and were not guarded, and `workflow.item_state` hashes the whole row
    before either of them runs.

    The consequence each time is worse than an error page. `do_GET`
    (`source:local-project-dashboard/sd_dashboard/server.py::do_GET`) catches `MissingItem`, `NotFound`, `SdDbError` and
    `sqlite3.Error`. `json.JSONDecodeError` and `TypeError` are none of those,
    so the failure raised through the handler and the reader got no response at
    all -- `RemoteDisconnected`, with the traceback on the server's stderr.
    """

    def panel(self, item):
        return str(controls.item_controls(self.connection, item))

    def details_form(self, item):
        found = re.search(rf'<form[^>]*action="/api/items/{item}"[^>]*>.*?</form>',
                          self.panel(item), re.S)
        return found.group(0) if found else None

    def unreadable(self, kind="task"):
        item = self.item("Unreadable", kind=kind, body={"text": "was readable once"})
        self.connection.execute("UPDATE item SET body = ? WHERE id = ?", ("{not json", item))
        self.connection.commit()
        return item

    def test_an_item_whose_body_is_not_json_renders_instead_of_dropping_the_reader(self):
        """sd:870 -- `screens.artifact` parsed `body` with no guard."""
        for kind in ("task", "followup"):
            with self.subTest(kind=kind):
                item = self.unreadable(kind)
                status, _, page = self.request(f"/item/{item}")
                self.assertEqual(status, 200)
                self.assertIn("not valid JSON", page)
                self.assertNotIn("was readable once", page)

    def test_an_unreadable_body_withholds_the_details_field_and_keeps_the_rest(self):
        """sd:870 -- an empty textarea here would offer a save that overwrites
        the stored body with the blank the reader never typed. Withhold the
        field: `data-changed-fields` omits what is not rendered, so the save
        that stays available cannot touch `body`.
        """
        item = self.unreadable()
        markup = self.details_form(item)
        self.assertIsNotNone(markup)
        self.assertEqual(sorted(Fields(markup).fields),
                         ["due", "kind", "priority", "repo", "revision", "title"])
        self.assertNotIn('name="body"', markup)
        self.assertIn("not valid JSON", markup)

    def test_a_save_beside_an_unreadable_body_leaves_the_stored_body_alone(self):
        """The withheld field is not a silent clear: the stored bytes stay."""
        item = self.unreadable()
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "title": "Renamed"})
        self.assertEqual((status, state["item"]["title"]), (200, "Renamed"))
        self.assertEqual(state["item"]["body"], "{not json")

    def test_a_blob_in_fields_renders_the_item_page(self):
        """sd:874 -- TEXT affinity converts every numeric storage class to text,
        so `json.loads` never sees an `int` from these columns. BLOB is the one
        class affinity leaves alone: it comes back as `bytes`, and
        `workflow.item_state` json.dumps-es the row to compute the revision.
        Valid JSON in the BLOB dies there too, so this is a type the revision
        computation cannot serialise, not malformed data.
        """
        item = self.binary()
        status, _, page = self.request(f"/item/{item}")
        self.assertEqual(status, 200)
        self.assertIn("Binary fields", page)

    def binary(self):
        item = self.item("Binary fields", kind="task")
        self.connection.execute("UPDATE item SET fields = ? WHERE id = ?", (b'{"report": {}}', item))
        self.connection.commit()
        return item

    def test_a_post_on_a_blob_backed_item_answers_instead_of_dropping_the_reader(self):
        """sd:874 again, on the other verb. `item_state` is what every mutation
        returns and `server.Dashboard._json` dumps that whole state, so making
        the revision hash survive a BLOB only moved the `TypeError` from the
        GET to the POST beside it -- `do_POST` names no more of them than
        `do_GET` does, and the reader lost the connection either way. The
        encoding the hash uses is the encoding the response uses.
        """
        item = self.binary()
        revision = workflow.item_state(self.connection, item)["revision"]
        status, _, state = self.post(f"/api/items/{item}/status",
                                     {"revision": revision, "status": "ready"})
        self.assertEqual((status, state["item"]["status"]), (200, "ready"))
        self.assertEqual(state["item"]["fields"], {"sd_db_bytes_hex": b'{"report": {}}'.hex()})


class FollowupDetails(BrowserSession):
    """sd:816 -- a followup gets the details the library already accepts.

    sd:809 (system PR #360) put `followup` in `workflow.DETAIL_KINDS`, so
    `edit_item` takes its title, body, priority, due date and repository. The
    panel still offered the status control alone and a hint naming
    `sd task edit ID`, and `screens.artifact` decoded `{"text": ...}` for
    `task`, `report` and `skill-review` only -- so every followup filed with
    `sd task add --kind followup` showed its body as raw JSON.

    `kind` stays out of the form. `edit_item` would take it, but reclassifying
    a followup is not one of the five fields #360 named, and the select would
    be a widget sd:816 did not ask for.
    """

    def panel(self, item):
        return str(controls.item_controls(self.connection, item))

    def details_form(self, item):
        found = re.search(rf'<form[^>]*action="/api/items/{item}"[^>]*>.*?</form>',
                          self.panel(item), re.S)
        return found.group(0) if found else None

    def test_a_followup_body_renders_as_text_not_as_its_stored_json(self):
        item = self.item("Ask the vendor", kind="followup", body={"text": "Call **them** back"})
        status, _, page = self.request(f"/item/{item}")
        self.assertEqual(status, 200)
        self.assertNotIn("&quot;text&quot;", page)
        self.assertIn("<strong>them</strong>", page)

    def test_a_task_reclassified_to_any_hand_kind_still_shows_its_text(self):
        # The kind select keeps the `{"text": ...}` body; the artifact decode
        # named four kinds and showed the other three their braces (PR #332
        # review). Every hand kind, so a kind added to `HAND_KINDS` is covered.
        for kind in workflow.HAND_KINDS:
            with self.subTest(kind=kind):
                item = self.item("Errand", kind="task", body={"text": "Buy **milk**"})
                if kind != "task":
                    revision = workflow.item_state(self.connection, item)["revision"]
                    status, _, state = self.post(f"/api/items/{item}", {"revision": revision, "kind": kind})
                    self.assertEqual((status, state["item"]["kind"]), (200, kind))
                page = self.request(f"/item/{item}")[2]
                self.assertNotIn("&quot;text&quot;", page)
                self.assertIn("<strong>milk</strong>", page)

    def test_the_followup_details_form_offers_what_the_library_accepts_and_saves(self):
        self.repo("/repos/system")
        item = self.item("Ask the vendor", kind="followup", body={"text": "Call them"})
        markup = self.details_form(item)
        self.assertIsNotNone(markup)
        parsed = Fields(markup)
        self.assertEqual(sorted(parsed.fields),
                         ["body", "due", "priority", "repo", "revision", "title"])
        status, _, state = self.post(f"/api/items/{item}", {
            "revision": parsed.fields["revision"]["value"],
            "title": "Ask the vendor twice", "priority": 2, "due": "2026-09-30",
            "repo": "/repos/system", "body": "Called **twice**"})
        self.assertEqual((status, state["item"]["title"]), (200, "Ask the vendor twice"))
        self.assertEqual(json.loads(state["item"]["body"])["text"], "Called **twice**")
        self.assertEqual(state["item"]["repo"], "/repos/system")
        self.assertIn("<strong>twice</strong>", self.request(f"/item/{item}")[2])


#: The cutoff the bulk-acknowledge tests preview at, as `reporting.cutoff`
#: stamps the date `2026-09-10`, and the instant every clean fixture report
#: was filed: before it. The server's clock is the real one, later than both.
CUTOFF = "2026-09-10T00:00:00+00:00"
FILED = "2026-09-09T00:00:00+00:00"
ACKNOWLEDGE_CLEAN = "/api/reports/acknowledge-clean"


class BulkAcknowledge(BrowserSession):
    """sd:755, the dashboard half of the bulk acknowledge: a GET preview that
    writes nothing and issues a plan, and one POST that moves exactly that
    plan or nothing, naming the person who typed `who`, the principal the
    listener authenticated and `program="dashboard"`.

    Two clean reports and one that needs attention, all filed before the
    cutoff through `create_item`, which takes a `created_at`; `ingest` stamps
    the real clock, which is after every cutoff a test can name.
    """

    def setUp(self):
        super().setUp()
        self.clean = [self.report("alpha"), self.report("beta")]
        self.flagged = self.report("gamma", attention=True)

    def report(self, job, *, attention=False):
        fields = {"attention": attention, "report": {"job": job, "ended": FILED}}
        item = create_item(self.connection, kind="report", title=f"{job}: run report", status="planning",
                           source="cron-report", external_id=f"{job}:run", fields=fields, created_at=FILED)
        if attention:
            add_note(self.connection, item, "followup", f"Review {job} findings.", session="cron")
        return item

    def plan(self):
        return reporting.clean_reports(self.connection, before=CUTOFF)["plan"]

    def payload(self, **overrides):
        return {"before": CUTOFF, "plan": self.plan(), "who": "operator", **overrides}

    def status_of(self, item):
        return self.connection.execute("SELECT status FROM item WHERE id=?", (item,)).fetchone()[0]

    def fields_of(self, item):
        return json.loads(self.connection.execute("SELECT fields FROM item WHERE id=?", (item,)).fetchone()[0])

    def test_the_apply_moves_the_previewed_reports_and_records_who_from_where(self):
        status, _, result = self.post(ACKNOWLEDGE_CLEAN, self.payload())
        self.assertEqual(status, 200, result)
        batch = result["item"]["id"]
        self.assertEqual(result["item"]["status"], "done")
        self.assertEqual(result["acknowledged"], self.clean)
        for item in self.clean:
            self.assertEqual(self.status_of(item), "done")
        self.assertEqual(self.status_of(self.flagged), "planning")
        actor = self.fields_of(batch)["report"]["actor"]
        self.assertEqual(actor["who"], "operator")
        # The loopback listener's principal (`auth.access_context`): the
        # server recorded what its channel authenticated, not what was typed.
        self.assertEqual(actor["principal"], "local")
        self.assertEqual(actor["program"], "dashboard")
        self.assertEqual(self.fields_of(batch)["record"], "reports-acknowledge")

    def test_the_record_names_the_servers_session_as_the_cli_names_its_own(self):
        """sd:1168: the route passed no `session`, so a batch filed while
        `SD_SESSION` was set recorded `null`. The server runs in this process,
        so its environment is the one patched here."""
        with patch.dict("os.environ", {"SD_SESSION": "dashboard-session"}):
            status, _, result = self.post(ACKNOWLEDGE_CLEAN, self.payload())
        self.assertEqual(status, 200, result)
        self.assertEqual(self.fields_of(result["item"]["id"])["report"]["actor"]["session"], "dashboard-session")

    def test_a_replay_of_an_applied_plan_is_stale_and_writes_nothing(self):
        payload = self.payload()
        self.assertEqual(self.post(ACKNOWLEDGE_CLEAN, payload)[0], 200)
        before = self.snapshot()
        status, _, result = self.post(ACKNOWLEDGE_CLEAN, payload)
        self.assertEqual(status, 409, result)
        self.assertIs(result["reload"], True)
        self.assertEqual(before, self.snapshot())

    def test_a_body_the_route_does_not_recognise_is_refused_before_any_write(self):
        plan = self.plan()
        before = self.snapshot()
        for payload in (self.payload(plan=plan, extra="x"),
                        self.payload(plan="not-a-plan"),
                        self.payload(plan=plan.upper()),
                        {"before": CUTOFF, "plan": plan},
                        self.payload(plan=plan, who=7)):
            with self.subTest(payload=payload):
                status, _, result = self.post(ACKNOWLEDGE_CLEAN, payload)
                self.assertEqual(status, 400, result)
                self.assertIn("Preview the clean reports again", result["error"])
        self.assertEqual(before, self.snapshot())

    def test_a_cutoff_that_is_not_a_date_is_refused_with_a_json_answer(self):
        """`reporting.cutoff` raises `WorkflowError`, which the `try` around
        `action_route` does not catch: run in that body it leaves `do_POST`
        with no response and this client raises `RemoteDisconnected`. Inside
        the returned function the second `try` answers 400 with the message."""
        before = self.snapshot()
        status, _, result = self.post(ACKNOWLEDGE_CLEAN, self.payload(before="2026-09-10T01:00:00+00:00"))
        self.assertEqual(status, 400, result)
        self.assertEqual(result["error"], "give the cutoff as a date, YYYY-MM-DD, meaning 00:00 UTC")
        self.assertEqual(before, self.snapshot())

    def test_an_emptied_selection_is_stale_not_a_second_kind_of_error(self):
        plan = self.plan()
        for item in self.clean:
            reporting.acknowledge(self.connection, item, expected_revision=workflow.item_state(self.connection, item)["revision"], who="operator")
        self.assertIsNone(self.plan())
        before = self.snapshot()
        status, _, result = self.post(ACKNOWLEDGE_CLEAN, self.payload(plan=plan))
        self.assertEqual(status, 409, result)
        self.assertIs(result["reload"], True)
        self.assertEqual(before, self.snapshot())

    def test_a_blank_who_is_refused_with_the_library_message(self):
        before = self.snapshot()
        status, _, result = self.post(ACKNOWLEDGE_CLEAN, self.payload(who="  "))
        self.assertEqual(status, 400, result)
        self.assertEqual(result["error"], "who must not be blank")
        self.assertEqual(before, self.snapshot())

    def test_a_followup_added_after_the_preview_makes_the_plan_stale(self):
        plan = self.plan()
        add_note(self.connection, self.clean[0], "followup", "look at this first", session="operator")
        before = self.snapshot()
        status, _, result = self.post(ACKNOWLEDGE_CLEAN, self.payload(plan=plan))
        self.assertEqual(status, 409, result)
        self.assertIs(result["reload"], True)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.status_of(self.clean[1]), "planning")

    def test_the_preview_stamps_the_date_and_the_apply_takes_the_stamp_back(self):
        """The one `before` contract, end to end: the page is asked with a
        date, the hidden field and the pasted command carry the stamp
        `cutoff` made of it, and the POST of those fields succeeds. A bare
        date in the hidden field would fail at `stamp`; a stamp computed
        differently would not match the plan."""
        before = self.snapshot()
        status, _, page = self.request("/operations?area=reports&clean_before=2026-09-10")
        self.assertEqual(status, 200)
        self.assertEqual(before, self.snapshot())
        self.assertIn("Would acknowledge (2)", page)
        self.assertIn("Declined (1)", page)
        self.assertIn("it needs attention, which waits for a person", page)
        forms = re.findall(r'<form[^>]*action="/api/reports/acknowledge-clean"[^>]*>.*?</form>', page, re.S)
        self.assertEqual(len(forms), 1, page)
        form = forms[0]
        stamped = re.search(r'name="before" value="([^"]*)"', form).group(1)
        self.assertEqual(stamped, CUTOFF)
        token = re.search(r'name="plan" value="([^"]*)"', form).group(1)
        self.assertEqual(token, reporting.clean_reports(self.connection, before=CUTOFF)["plan"])
        self.assertRegex(form, r'<input name="who" id="field-who" value="" type="text" required maxlength="200"')
        self.assertIn(f"--before {CUTOFF} --apply --if-plan {token} --who NAME", form)
        self.assertIn('data-reload-label="Preview again"', form)
        status, _, result = self.post(ACKNOWLEDGE_CLEAN, {"before": stamped, "plan": token, "who": "operator"})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["acknowledged"], self.clean)
