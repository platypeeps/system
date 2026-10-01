"""The Usage area's month (sd:234 slice 8d): the four numbers per bill, the
`bound` rows, the burn line, the gauges, and `/api/usage` as the verb's bytes.
Criterion 12's chart clauses are asserted from the SVG the server returns."""

import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, provider_controls, reads, usage
from sd_db.migrate import initialise
from sd_dashboard import operations_screen

from support import NOW, ScreenCase, record_cost
from test_workflow_actions import BrowserSession

SD_DB = Path(__file__).resolve().parents[2] / "local-sd-db"


class Seeds:
    def bill(self, name, cap=None, basis="api"):
        self.connection.execute("INSERT OR IGNORE INTO bill VALUES (?, ?, ?)", (name, basis, cap))
        self.connection.commit()

    def cost(self, source, *, at, **columns):
        row = record_cost(self.connection, source=source, **columns)
        self.connection.execute("UPDATE cost SET timestamp = ? WHERE id = ?", (at, row))
        self.connection.commit()
        return row

    def month(self):
        self.provider("kimi")
        self.bill("capped", 100)
        self.bill("open")
        self.cost("run", at="2026-09-01T10:00:00Z", provider="kimi", bill="capped", role="author", usd=10)
        self.cost("bound", at="2026-09-05T10:00:00Z", provider="kimi", bill="capped", role="author",
                  call_id="lost", usd=30)
        self.cost("sending", at="2026-09-06T09:00:00Z", provider="kimi", bill="capped", role="author",
                  call_id="wire", usd=5, owner_pid=os.getpid())
        self.cost("run", at="2026-09-03T10:00:00Z", provider="kimi", bill="open", role="reviewer", usd=1)



class UsageScreen(Seeds, ScreenCase):
    def test_the_area_renders_the_four_numbers_and_the_bound_row(self):
        self.month()
        page = self.render("/operations", {"area": ["usage"], "month": ["2026-09"]})
        for expected in ('data-number="spent">$40.00', 'data-number="estimated">$30.00',
                         'data-number="held">$5.00', 'data-number="cap">$100.00', "room $55.00",
                         '<span data-bound="lost">$30.00</span>', "<td>lost</td>",
                         "2026-09: $41.00 spent, $5.00 held, against the invoice line.",
                         'data-gauge="capped" data-percent="45"',
                         "<td>capped</td><td>kimi</td><td>author</td><td>3</td><td>$40.00</td><td>$5.00</td>",
                         'value="2026-09"', "sd-db.sh usage --month 2026-09"):
            self.assertIn(expected, page)
        self.assertNotIn(" title=", page)
        empty = self.render("/operations", {"area": ["usage"], "month": ["2026-08"]})
        self.assertIn("No bound rows this month.", empty)
        # The open `sending` row counts against whichever month asks.
        self.assertIn("2026-08: $0.00 spent, $5.00 held", empty)
        self.assertIn('data-number="spent">$0.00', empty)
        self.assertIn("a month is YYYY-MM, not &#x27;soon&#x27;", self.render("/operations", {"area": ["usage"], "month": ["soon"]}))

    def test_the_burn_line_its_cap_rule_and_the_day_the_projection_crosses(self):
        """A month of rows against a capped bill: the points, the cap, and the
        projection crossing on the day the rate implies -- 40 by day 8 is 5 a
        day, and 100 is passed on day 20; a day earlier the rate is 40/7 and
        the crossing, 17.5, is reported as the day it is passed, 18."""
        self.month()
        self.now = "2026-09-08T12:00:00Z"
        page = self.render("/operations", {"area": ["usage"]})
        self.assertIn('data-points="1:10.00;5:40.00"', page)
        self.assertIn('class="chart-cap" data-cap="100.00"', page)
        self.assertIn('class="chart-projection" data-end="150.00" data-cross="20"', page)
        # The drawn line meets the cap rule on the day `data-cross` names,
        # not at the month's end: its endpoint is x(20) on the cap's y.
        # September has 30 days, and the axis runs from day 1 to day 30.
        line = re.search(r'<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)" class="chart-projection"', page)
        self.assertIsNotNone(line, page)
        cap_rule = re.search(r'<line x1="([\d.]+)" x2="([\d.]+)" y1="([\d.]+)" y2="[\d.]+" class="chart-cap"', page)
        left, right = float(cap_rule.group(1)), float(cap_rule.group(2))
        self.assertAlmostEqual(float(line.group(3)), left + (right - left) * (20 - 1) / (30 - 1), places=1)
        self.assertEqual(line.group(4), cap_rule.group(3))
        self.now = "2026-09-07T12:00:00Z"
        self.assertIn('data-end="171.43" data-cross="18"', self.render("/operations", {"area": ["usage"]}))
        self.assertNotIn("chart-projection", self.render("/operations", {"area": ["usage"], "month": ["2026-08"]}))

    def test_the_cost_tile_and_the_month_card_read_one_snapshot(self):
        """A cap committed after the tile's read and before the card's put
        one cap on the tile and another on the card (PR #438 review). The
        write lands between the two through a second connection, inside
        `usage.bills`, which is the tile's read; WAL lets it commit while the
        page's read transaction holds its snapshot. Both numbers are the
        cap the page started with.
        """
        self.month()
        writer = connect(self.path)
        self.addCleanup(writer.close)
        real = usage.bills

        def bills_then_raise_the_cap(connection, **arguments):
            rows = real(connection, **arguments)
            writer.execute("UPDATE bill SET cap_usd_month = 250 WHERE name = 'capped'")
            writer.commit()
            return rows

        with patch.object(usage, "bills", bills_then_raise_the_cap):
            page = self.render("/operations", {"area": ["usage"], "month": ["2026-09"]})
        self.assertIn("capped: $40.00 of $100.00", page)
        self.assertIn('data-number="cap">$100.00', page)
        self.assertNotIn("$250.00", page)
        # The page left no transaction open on the connection it read with.
        self.assertFalse(self.connection.in_transaction)
        # And the next read sees the new cap: the snapshot ended with the page.
        self.assertIn('data-number="cap">$250.00', self.render("/operations", {"area": ["usage"], "month": ["2026-09"]}))

    def test_the_snapshot_ends_before_the_git_scans_begin(self):
        # The snapshot wrapped the whole Usage render, and the render runs
        # git across every registered repository, twice, each with its own
        # timeout. A read transaction held across that keeps the WAL from
        # checkpointing past it for as long as the slowest checkout takes
        # (Codex review of PR #501). The two reads that must agree are
        # inside it; the scans are not.
        self.month()
        inside = []
        real = reads.weekly_numbers

        def watch(connection, **arguments):
            inside.append(connection.in_transaction)
            return real(connection, **arguments)

        with patch.object(reads, "weekly_numbers", watch):
            page = self.render("/operations", {"area": ["usage"], "month": ["2026-09"]})
        self.assertEqual(inside, [False])
        self.assertIn('data-number="cap">$100.00', page)

    def test_the_operations_subtitle_names_every_area_the_nav_draws(self):
        # A written sentence named ten of eleven areas (PR #427 review).
        page = self.render("/operations", {"area": ["usage"]})
        subtitle = re.search(r'<p class="subtitle">([^<]*)</p>', page).group(1)
        for key, label in operations_screen.AREAS:
            with self.subTest(area=key):
                self.assertIn(label.lower(), subtitle.lower())
        self.assertEqual(subtitle, "Jobs, services, ports, workflow progress, usage, reports, resources, "
                                   "trackers, repos, sessions and commands")

    def test_the_bound_rows_are_a_listing_with_the_filter_and_the_pager(self):
        """The `bound` rows grow all month, so they go through `Listing`:
        the filter narrows them, the URL carries it, and the month rides along."""
        self.month()
        for index in range(60):
            self.cost("bound", at=f"2026-09-{10 + index % 19:02d}T10:00:00Z", provider="kimi", bill="open",
                      role="reviewer", call_id=f"lost-{index}", usd=1)
        page = self.render("/operations", {"area": ["usage"], "month": ["2026-09"]})
        self.assertIn('data-listing="usage-bound"', page)
        self.assertIn("Page 1 of 2", page)
        self.assertNotIn('data-bound="lost-56"', page)  # the last row by timestamp, page two
        filtered = self.render("/operations", {"area": ["usage"], "month": ["2026-09"], "q": ["lost-56"]})
        self.assertIn('data-bound="lost-56"', filtered)
        self.assertNotIn('data-bound="lost">', filtered)
        self.assertIn('name="month" value="2026-09"', filtered)

    def test_a_zero_cap_is_a_cap_and_a_plan_bill_draws_its_windows_not_a_burn(self):
        """`cap_usd_month = 0` is a limit, not an open bill: the gauge is full
        and the burn is drawn. A `plan` bill draws its meter windows and no
        cap burn, whatever `cap_usd_month` says."""
        self.provider("kimi")
        self.provider("claude")
        self.bill("zero", 0, basis="company")
        self.bill("plan", 50, basis="plan")
        self.cost("run", at="2026-09-02T10:00:00Z", provider="kimi", bill="zero", role="author", usd=1)
        self.cost("run", at="2026-09-02T10:00:00Z", provider="claude", bill="plan", role="author", usd=1)
        self.cost("meter", at="2026-09-05T12:00:00Z", provider="claude", bill="plan", window_minutes=300, used_percent=40)
        page = self.render("/operations", {"area": ["usage"], "month": ["2026-09"]})
        zero = page[page.index('data-bill="zero"'):page.index("</article>", page.index('data-bill="zero"'))]
        self.assertIn('data-number="cap">$0.00', zero)
        self.assertIn('data-gauge="zero" data-percent="100"', zero)
        self.assertIn("chart-burn-line", zero)
        plan = page[page.index('data-bill="plan"'):page.index("</article>", page.index('data-bill="plan"'))]
        self.assertIn('data-gauge="claude:300" data-percent="40"', plan)
        self.assertNotIn("chart-burn-line", plan)
        self.assertNotIn('data-gauge="plan"', plan)

    def test_a_plan_bill_with_meter_rows_at_forty_and_ninety_draws_two_gauges(self):
        self.provider("claude")
        self.bill("plan", basis="plan")
        self.cost("meter", at="2026-09-05T08:00:00Z", provider="claude", bill="plan", window_minutes=300, used_percent=20)
        self.cost("meter", at="2026-09-05T12:00:00Z", provider="claude", bill="plan", window_minutes=300, used_percent=40)
        self.cost("meter", at="2026-09-05T12:00:00Z", provider="claude", bill="plan", window_minutes=10080, used_percent=90)
        page = self.render("/operations", {"area": ["usage"]})
        self.assertIn('data-gauge="claude:300" data-percent="40"', page)
        self.assertIn('data-gauge="claude:10080" data-percent="90"', page)
        self.assertNotIn('data-percent="20"', page)


def _home_store(self):
    """`ScreenCase.setUp` with the store where `sd-db.sh` looks for it."""
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.home = Path(self.tmp.name)
    self.path = self.home / ".local/share/sd/sd.db"
    self.path.parent.mkdir(parents=True)
    initialise(self.path)
    self.connection = connect(self.path)
    self.addCleanup(self.connection.close)
    self.now = NOW


class UsageApi(Seeds, BrowserSession):
    def setUp(self):
        with patch.object(ScreenCase, "setUp", _home_store):
            super().setUp()

    def test_the_api_serves_the_read_and_the_verb_sweeps_first(self):
        """The dashboard's GET connection is read-only (`mode=ro`,
        `query_only`), so `/api/usage` and the panel show the read as it
        stands: a dead owner's `sending` row is still held until the next
        reservation or `sd usage` sweeps it. After the verb, the API's bytes
        are the verb's."""
        self.month()
        child = subprocess.Popen(["true"])
        child.wait()
        self.cost("sending", at="2026-09-07T09:00:00Z", provider="kimi", bill="capped", role="author",
                  call_id="orphan", usd=7, owner_pid=child.pid)
        before = self.request("/api/usage?month=2026-09", headers={"Cookie": self.cookie})[2]
        self.assertIn('"held": 12.0', before)
        self.assertEqual(self.connection.execute("SELECT source FROM cost WHERE call_id = 'orphan'").fetchone()[0],
                         "sending")
        environment = {**os.environ, "HOME": str(self.home), "PYTHONPATH": str(SD_DB)}
        environment.pop("XDG_STATE_HOME", None)
        printed = subprocess.run([str(SD_DB / "sd-db.sh"), "usage", "--month", "2026-09", "--json"],
                                 capture_output=True, text=True, input="", env=environment)
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertIn("bound 1 sending row(s)", printed.stderr)
        self.assertNotEqual(printed.stdout, before)
        self.assertEqual(self.request("/api/usage?month=2026-09", headers={"Cookie": self.cookie})[2], printed.stdout)
        self.assertIn('"held": 5.0', printed.stdout)

    def test_the_api_serves_the_verbs_json_byte_for_byte(self):
        self.month()
        environment = {**os.environ, "HOME": str(self.home), "PYTHONPATH": str(SD_DB)}
        environment.pop("XDG_STATE_HOME", None)
        verb = subprocess.run([str(SD_DB / "sd-db.sh"), "usage", "--month", "2026-09", "--json"],
                              capture_output=True, text=True, input="", env=environment)
        self.assertEqual(verb.returncode, 0, verb.stdout + verb.stderr)
        status, headers, body = self.request("/api/usage?month=2026-09", headers={"Cookie": self.cookie})
        self.assertEqual((status, headers["Content-Type"]), (200, "application/json; charset=utf-8"))
        self.assertEqual(body, verb.stdout)
        self.assertIn('"estimated": 30.0', body)
        self.assertEqual(self.request("/api/usage?month=2026-09")[0], 403)
        self.assertEqual(self.request("/api/usage?month=soon", headers={"Cookie": self.cookie})[0], 400)
        self.assertEqual(self.request("/api/usage?week=1", headers={"Cookie": self.cookie})[0], 400)


#: One `start` entry on `a`, one `url` entry on the capped `c`: the shape
#: `registry.merge` warns about once a row cap lands on `a`.
REGISTRY = """bills:
  a: {cost: subscription}
  c: {cost: api, cap_usd_month: 20}
providers:
  claude: {start: "claude -p", vendor: anthropic, bill: a, roles: [author, reviewer], reader: claude-json}
  minimax: {url: "https://example.invalid/v1", model: fixture, vendor: minimax, bill: c, roles: [author, reviewer]}
roles:
  author: [claude, minimax]
  reviewer: [minimax, claude]
"""


class MergedCaps(Seeds, BrowserSession):
    """#435's residue (sd:234 slice 12h): the cost tile and the month card read
    a bill's cap the way `snapshot` does, so a legacy row cap on a `start` bill
    -- the one the panel above the cap forms warns about -- is not printed as
    the cap, while the `url` bill's cap is unchanged."""

    START_BILL_SENTENCE = "provider &#x27;claude&#x27; is a &#x27;start&#x27; entry on the capped bill &#x27;a&#x27;"

    def setUp(self):
        with patch.object(ScreenCase, "setUp", _home_store):
            super().setUp()
        (self.path.parent / "providers.yaml").write_text(REGISTRY)
        provider_controls.snapshot(self.connection)  # seeds the rows the bare UPDATE below needs
        with self.connection:
            self.assertEqual(self.connection.execute("UPDATE bill SET cap_usd_month = 5.0 WHERE name = 'a'").rowcount, 1)
        # The served page reads the wall clock, and its cost tile sums the current month, so the costs fall at the start
        # of the current UTC month: a pinned month fails every run outside it (sd:2288).
        self.this_month = datetime.now(timezone.utc).strftime("%Y-%m")
        self.cost("run", at=f"{self.this_month}-01T00:00:00Z", provider="claude", bill="a", role="author", usd=2)
        self.cost("run", at=f"{self.this_month}-01T00:00:01Z", provider="minimax", bill="c", role="reviewer", usd=4)

    def page(self):
        return self.request(f"/operations?area=usage&month={self.this_month}")[2]

    def card(self, page, bill):
        return re.search(r'<article\b[^>]*data-bill="' + bill + r'"[^>]*>.*?</article>', page, re.S).group(0)

    def test_a_warned_bills_cap_is_on_neither_the_tile_nor_the_card_and_the_url_bills_is_on_both(self):
        page = self.page()
        self.assertIn(self.START_BILL_SENTENCE, page)
        self.assertIn('<p class="cost-line">a: $2.00</p>', page)
        self.assertNotIn("a: $2.00 of $5.00", page)
        self.assertIn('<p class="cost-line">c: $4.00 of $20.00</p>', page)
        warned, control = self.card(page, "a"), self.card(page, "c")
        self.assertIn('data-number="cap">\u2014<', warned)
        self.assertNotIn("room", warned)
        self.assertNotIn("$5.00", warned)
        self.assertIn('data-number="cap">$20.00<', control)
        self.assertIn("room $16.00", control)
        self.assertIn('data-cap="20.00"', control)

    def test_the_api_and_the_verb_carry_the_merged_caps_in_the_same_bytes(self):
        body = self.request(f"/api/usage?month={self.this_month}", headers={"Cookie": self.cookie})[2]
        environment = {**os.environ, "HOME": str(self.home), "PYTHONPATH": str(SD_DB)}
        environment.pop("XDG_STATE_HOME", None)
        verb = subprocess.run([str(SD_DB / "sd-db.sh"), "usage", "--month", self.this_month, "--json"],
                              capture_output=True, text=True, input="", env=environment)
        self.assertEqual(verb.returncode, 0, verb.stdout + verb.stderr)
        self.assertEqual(body, verb.stdout)
        self.assertIn('"name": "a",\n      "room": null', body)
        self.assertIn('"cap": null,\n      "cost_basis": "subscription"', body)
        self.assertIn('"cap": 20.0,\n      "cost_basis": "api"', body)

    def test_the_clear_leaves_the_row_as_the_cap_and_a_cap_set_on_the_url_bill_prints(self):
        state = provider_controls.snapshot(self.connection)
        cleared = provider_controls.set_cap(self.connection, "a", None, expected_revision=state["revision"], who="test")
        self.assertEqual(cleared["warnings"], [])
        page = self.page()
        self.assertNotIn(self.START_BILL_SENTENCE, page)
        self.assertIn('<p class="cost-line">a: $2.00</p>', page)
        self.assertIn('data-number="cap">\u2014<', self.card(page, "a"))
        provider_controls.set_cap(self.connection, "c", 30.0, expected_revision=cleared["revision"], who="test")
        page = self.page()
        self.assertIn('<p class="cost-line">c: $4.00 of $30.00</p>', page)
        control = self.card(page, "c")
        self.assertIn('data-number="cap">$30.00<', control)
        self.assertIn("room $26.00", control)
