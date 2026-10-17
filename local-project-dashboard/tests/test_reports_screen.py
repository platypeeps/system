"""The single report's controls (`reports_screen.report_controls`).

The classic Operations > Reports area and its bulk-acknowledge preview were
retired in sd:3402, and the v2 Reports page has no clean preview or batch:
a clean run files no report since the quiet-run rule.
"""

import json

from sd_db import reporting
from sd_dashboard.reports_screen import report_controls

from support import ScreenCase

class ReportControls(ScreenCase):
    """The single report's form, which the Item page keeps."""

    def test_the_single_report_form_posts_to_the_reports_api(self):
        state = reporting.ingest(self.connection, job="fixture", run_id="one", started="2026-09-08T00:00:00Z",
                                 ended="2026-09-08T00:01:00Z", exit_code=0, text="Fine", source_path="/fixture/log",
                                 attention=False)
        row = self.connection.execute("SELECT * FROM item WHERE id=?", (state["item"]["id"],)).fetchone()
        markup = str(report_controls(self.connection, row, state["revision"]))
        self.assertIn('action="/api/reports/', markup)

    def test_the_truncation_notice_reads_the_kept_body_not_the_first_run(self):
        # sd:4652: `repeats.body_truncated` says what the kept body is once a later run replaced it (pack sd:4641).
        state = reporting.ingest(self.connection, job="fixture", run_id="one", started="2026-09-08T00:00:00Z",
                                 ended="2026-09-08T00:01:00Z", exit_code=0, text="Fine", source_path="/fixture/log",
                                 attention=False, truncated=True)
        item = state["item"]["id"]
        fields = json.loads(self.connection.execute("SELECT fields FROM item WHERE id=?", (item,)).fetchone()[0])
        for first, kept, shown in ((True, None, True), (True, False, False), (False, True, True)):
            with self.subTest(first=first, kept=kept):
                fields["report"]["truncated"] = first
                fields["repeats"] = {"count": 2} if kept is None else {"count": 2, "body_truncated": kept}
                self.connection.execute("UPDATE item SET fields = ? WHERE id = ?", (json.dumps(fields), item))
                row = self.connection.execute("SELECT * FROM item WHERE id=?", (item,)).fetchone()
                markup = str(report_controls(self.connection, row, state["revision"]))
                self.assertIs("Output was truncated." in markup, shown)

    def test_the_attention_evidence_is_the_current_set_not_the_first_run(self):
        # sd:4664: `repeats.basis` is the set a smaller run left (pack sd:4658); `attention_basis` is the first run's.
        state = reporting.ingest(self.connection, job="fixture", run_id="one", started="2026-09-08T00:00:00Z",
                                 ended="2026-09-08T00:01:00Z", exit_code=0, text="Fine", source_path="/fixture/log",
                                 attention=True, attention_basis="finding one\nfinding two")
        item = state["item"]["id"]
        fields = json.loads(self.connection.execute("SELECT fields FROM item WHERE id=?", (item,)).fetchone()[0])
        fields["repeats"] = {"count": 2, "basis": "finding one"}
        self.connection.execute("UPDATE item SET fields = ? WHERE id = ?", (json.dumps(fields), item))
        row = self.connection.execute("SELECT * FROM item WHERE id=?", (item,)).fetchone()
        markup = str(report_controls(self.connection, row, state["revision"]))
        self.assertIn("<dd>finding one</dd>", markup)
        self.assertNotIn("finding two", markup)
