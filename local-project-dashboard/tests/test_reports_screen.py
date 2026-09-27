"""Operations > Reports: the bulk-acknowledge preview, a GET that writes nothing (sd:755).

The POST that moves the previewed reports, and the round trip from this page
to it, are in `test_controls_actions.py`, where a real listener answers. Here
the router is called as a function, with the clock frozen, so a page can be
asserted without a socket.
"""

import re
from unittest.mock import patch

from sd_db import add_note, create_item, reporting
from sd_dashboard import controls
from sd_dashboard.reports_screen import report_controls

from support import ScreenCase

CUTOFF = "2026-09-10T00:00:00+00:00"
FILED = "2026-09-09T00:00:00+00:00"
APPLY_FORM = re.compile(r'<form[^>]*action="/api/reports/acknowledge-clean"[^>]*>.*?</form>', re.S)


class ReportsPreview(ScreenCase):

    def setUp(self):
        super().setUp()
        # Two days after the cutoff every test previews at. The fixture clock
        # is 2026-09-06, before it, and a cutoff later than now is refused.
        self.now = "2026-09-12T00:00:00Z"

    def report(self, job, *, attention=False, created_at=FILED):
        fields = {"attention": attention, "report": {"job": job, "ended": created_at}}
        item = create_item(self.connection, kind="report", title=f"{job}: run report", status="planning",
                           source="cron-report", external_id=f"{job}:run", fields=fields, created_at=created_at)
        if attention:
            add_note(self.connection, item, "followup", f"Review {job} findings.", session="cron")
        return item

    def preview(self, clean_before=None):
        parameters = {"area": ["reports"]}
        if clean_before is not None:
            parameters["clean_before"] = [clean_before]
        return self.render("/operations", parameters)

    def snapshot(self):
        return tuple(self.connection.iterdump())

    def test_the_page_asks_for_a_date_and_offers_no_apply_until_one_is_previewed(self):
        page = self.preview()
        self.assertIn('name="clean_before"', page)
        self.assertIn('type="date"', page)
        self.assertNotIn("/api/reports/acknowledge-clean", page)
        self.assertNotIn('name="plan"', page)

    def test_a_preview_lists_both_sides_with_the_plan_and_writes_nothing(self):
        clean = [self.report("alpha"), self.report("beta")]
        flagged = self.report("gamma", attention=True)
        self.report("epsilon", created_at="2026-09-10T00:00:01+00:00")
        before = self.snapshot()
        page = self.preview("2026-09-10")
        self.assertEqual(before, self.snapshot())
        self.assertIn("Would acknowledge (2)", page)
        self.assertIn("Declined (1)", page)
        for item in clean:
            self.assertIn(f'href="/item/{item}"', page)
        self.assertIn(f'href="/item/{flagged}"', page)
        self.assertIn("it needs attention, which waits for a person", page)
        forms = APPLY_FORM.findall(page)
        self.assertEqual(len(forms), 1)
        expected = reporting.clean_reports(self.connection, before=CUTOFF)["plan"]
        self.assertIn(f'name="before" value="{CUTOFF}"', forms[0])
        self.assertIn(f'name="plan" value="{expected}"', forms[0])
        self.assertIn("Acknowledge 2 reports", forms[0])
        self.assertIn('data-reload-label="Preview again"', forms[0])

    def test_a_date_that_has_not_begun_or_is_not_a_date_is_a_notice_and_no_form(self):
        self.report("alpha")
        for value, message in (("2099-01-01", "is later than now"),
                               ("nonsense", "give the cutoff as a date, YYYY-MM-DD, meaning 00:00 UTC"),
                               ("2026-09-10T01:00:00+00:00", "give the cutoff as a date"),
                               ("", "give the cutoff as a date")):
            with self.subTest(clean_before=value):
                page = self.preview(value)
                self.assertIn(message, page)
                self.assertNotIn("/api/reports/acknowledge-clean", page)
                self.assertNotIn('name="plan"', page)

    def test_nothing_clean_renders_the_reason_and_no_form(self):
        self.report("gamma", attention=True)
        page = self.preview("2026-09-10")
        self.assertIn("Would acknowledge (0)", page)
        self.assertIn("Declined (1)", page)
        self.assertIn("Nothing is clean before 2026-09-10T00:00:00+00:00", page)
        self.assertNotIn("/api/reports/acknowledge-clean", page)
        self.assertNotIn('name="plan"', page)

    def test_more_than_the_batch_limit_renders_the_lists_and_no_form(self):
        self.report("alpha"); self.report("beta")
        with patch.object(reporting, "MAX_BATCH", 1):
            page = self.preview("2026-09-10")
        self.assertIn("Would acknowledge (2)", page)
        self.assertIn("more than 1 clean reports", page)
        self.assertNotIn("/api/reports/acknowledge-clean", page)
        self.assertNotIn('name="plan"', page)

    def test_each_list_shows_its_first_two_hundred_rows_and_counts_the_rest(self):
        items = [self.report(f"job{index:03d}") for index in range(203)]
        page = self.preview("2026-09-10")
        self.assertIn("Would acknowledge (203)", page)
        self.assertIn("and 3 more", page)
        # The listing below the preview links the newest 200 reports too, so
        # the count is taken inside the preview's own list.
        selected = re.search(r"<h3>Would acknowledge \(203\)</h3><ul>(.*?)</ul>", page, re.S).group(1)
        self.assertEqual([int(found) for found in re.findall(r'href="/item/([0-9]+)"', selected)], items[:200])
        self.assertEqual(len(APPLY_FORM.findall(page)), 1)
        self.assertIn("Acknowledge 203 reports", page)


class ReloadLabel(ScreenCase):
    """`controls.form` gains `reload_label`; a form that omits it renders as before."""

    def test_the_label_is_an_attribute_only_when_given(self):
        plain = str(controls.form("/api/x", label="Go", command="sd x"))
        labelled = str(controls.form("/api/x", label="Go", command="sd x", reload_label="Preview again"))
        self.assertNotIn("data-reload-label", plain)
        self.assertIn(' data-reload-label="Preview again"', labelled)
        self.assertEqual(plain, labelled.replace(' data-reload-label="Preview again"', ""))

    def test_the_single_report_form_does_not_carry_it(self):
        state = reporting.ingest(self.connection, job="fixture", run_id="one", started="2026-09-08T00:00:00Z",
                                 ended="2026-09-08T00:01:00Z", exit_code=0, text="Fine", source_path="/fixture/log",
                                 attention=False)
        row = self.connection.execute("SELECT * FROM item WHERE id=?", (state["item"]["id"],)).fetchone()
        markup = str(report_controls(self.connection, row, state["revision"]))
        self.assertIn('action="/api/reports/', markup)
        self.assertNotIn("data-reload-label", markup)
