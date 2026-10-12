"""The single report's controls (`reports_screen.report_controls`).

The classic Operations > Reports area and its bulk-acknowledge preview were
retired in sd:3402; the v2 Reports page previews through `/api/reports/clean`
(`test_v2_reports.py`), and the POST is in `test_controls_actions.py`.
"""

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
