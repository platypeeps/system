"""The meter's row: one `cost` row per provider per window, a percentage and
a window and no money. Criterion 15's `meter` clauses, the library half:
what `local-agent-meter/agent-meter.py` calls on its schedule, asserted on
rows.
"""

import tempfile
import unittest
from pathlib import Path

import sd_db
from sd_db import connect, create_assignment, create_item, seed
from sd_db.ledger import claim, reserve, settle
from sd_db.meter import MeterRefused, latest, sample
from sd_db.migrate import initialise
from sd_db.reads import item_assignments, usage_month
from sd_db.registry import parse

#: The ledger tests' registry, verbatim: a capped bill and an open one, a
#: `url` author on each, and `claude` as a `start` entry on the open bill,
#: which is the shape the sampler's providers have in the shipped file.
REGISTRY = """\
bills:
  capped: { cost: company, cap_usd_month: 10 }
  open:   { cost: subscription }
providers:
  kimi:   { url: "https://moonshot.example/v1", model: kimi-k3, vendor: moonshot, bill: capped,
            roles: [author, reviewer], max_tokens: 16384, price: { in: 3.00, out: 15.00 } }
  mini:   { url: "https://minimax.example/v1", model: MiniMax-M3, vendor: minimax, bill: open,
            roles: [author, reviewer], max_tokens: 16384, price: { in: 0, out: 0 } }
  claude: { start: "claude -p", vendor: anthropic, bill: open, roles: [author, reviewer], reader: claude-json }
roles:
  author:   [kimi, mini, claude]
  reviewer: [mini, kimi]
"""

DISABLED = REGISTRY.replace(
    "reader: claude-json }", "reader: claude-json, enabled: false, reason: \"off\" }"
)

ME = 4242
AT = "2026-09-16T12:00:00+00:00"


class MeterCase(unittest.TestCase):
    registry = REGISTRY

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        state = self.home / ".local/share/sd"
        state.mkdir(parents=True)
        (state / "providers.yaml").write_text(self.registry, encoding="utf-8")
        initialise(home=self.home)
        self.db = connect(home=self.home)
        self.addCleanup(self.db.close)
        seed(self.db, parse(self.registry, "providers.yaml"))

    def meter_rows(self):
        return self.db.execute(
            "SELECT * FROM cost WHERE source = 'meter' ORDER BY id"
        ).fetchall()

    def dump(self):
        return "\n".join(self.db.iterdump())


class OneRowPerProviderPerWindow(MeterCase):
    def test_two_windows_for_one_provider_are_two_rows_at_one_moment(self):
        """Clause 15: one row per provider **per window**, so the two gauges
        read two rows. Same provider, same stamp, different windows."""
        first = sample(self.db, provider="claude", window_minutes=300,
                       used_percent=61, now=AT)
        second = sample(self.db, provider="claude", window_minutes=10080,
                        used_percent=37.5, now=AT)
        self.assertNotEqual(first, second)
        rows = self.meter_rows()
        self.assertEqual([row["id"] for row in rows], [first, second])
        self.assertEqual([row["source"] for row in rows], ["meter", "meter"])
        self.assertEqual([row["timestamp"] for row in rows], [AT, AT])
        self.assertEqual([row["window_minutes"] for row in rows], [300, 10080])
        self.assertEqual([row["used_percent"] for row in rows], [61.0, 37.5])
        self.assertEqual([row["provider"] for row in rows], ["claude", "claude"])

    def test_the_bill_is_the_providers_from_the_registry(self):
        """The row's `bill` is what the registry says the provider is billed
        to, never the provider's own name: `claude` is billed to `open`."""
        sample(self.db, provider="claude", window_minutes=300, used_percent=1, now=AT)
        sample(self.db, provider="kimi", window_minutes=300, used_percent=2, now=AT)
        self.assertEqual(
            [(row["provider"], row["bill"]) for row in self.meter_rows()],
            [("claude", "open"), ("kimi", "capped")],
        )

    def test_a_meter_row_carries_no_money_and_no_call(self):
        """`usd`, tokens, `assignment`, `pass` and `call_id` are NULL: a
        provider-wide sample is nobody's call and nobody's dollars."""
        sample(self.db, provider="claude", window_minutes=300, used_percent=61, now=AT)
        (row,) = self.meter_rows()
        for column in ("usd", "tokens_in", "tokens_out", "assignment", "pass",
                       "call_id", "owner_pid", "role", "repo"):
            self.assertIsNone(row[column], column)

    def test_the_clock_is_the_default_and_is_written_in_the_one_shape(self):
        """No `now` is the wall clock; a `Z` stamp is rewritten to `+00:00`,
        the shape every row in the store is compared in."""
        sample(self.db, provider="claude", window_minutes=300, used_percent=61,
               now="2026-09-16T12:00:00Z")
        sample(self.db, provider="claude", window_minutes=300, used_percent=61)
        stamped, clocked = self.meter_rows()
        self.assertEqual(stamped["timestamp"], AT)
        self.assertRegex(clocked["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")

    def test_a_disabled_provider_is_still_sampled_on_its_bill(self):
        """Disabled means the registry will not choose it for a role; the
        vendor's meter still reads, and the plan is still billed."""
        self.db.execute("UPDATE provider SET enabled = 0, reason = 'off' WHERE name = 'claude'")
        row_id = sample(self.db, provider="claude", window_minutes=300, used_percent=9, now=AT)
        (row,) = self.meter_rows()
        self.assertEqual((row["id"], row["bill"]), (row_id, "open"))


class TheMeterRowIsNobodysCost(MeterCase):
    def test_per_item_cost_is_each_items_own_and_the_meter_row_is_in_neither(self):
        """Clause 15's cross-check: one `meter` row and two `run` rows against
        two assignments inside one four-hour window; the per-item cost query
        gives each item its own sum and the `meter` row counts in neither."""
        items = [create_item(self.db, kind="work", title=f"item {n}") for n in (1, 2)]
        rows = [
            create_assignment(self.db, role="author", status="queued", item=item,
                              provider="mini")
            for item in items
        ]
        for call, row, usd, at in (("a", rows[0], 1.25, "2026-09-16T10:00:00+00:00"),
                                   ("b", rows[1], 2.50, "2026-09-16T11:30:00+00:00")):
            reserve(self.db, bill="open", bound=5.0, call_id=call, owner_pid=ME,
                    assignment=row, provider="mini", now=at, is_alive=lambda pid: True)
            claim(self.db, call, now=at)
            settle(self.db, call, usd=usd)
        sample(self.db, provider="mini", window_minutes=300, used_percent=80,
               now="2026-09-16T12:00:00+00:00")

        by_source = dict(self.db.execute(
            "SELECT source, COUNT(*) FROM cost GROUP BY source"
        ).fetchall())
        self.assertEqual(by_source, {"meter": 1, "run": 2})

        (first,) = item_assignments(self.db, items[0])
        (second,) = item_assignments(self.db, items[1])
        self.assertEqual((first["usd"], first["estimated"]), (1.25, 0))
        self.assertEqual((second["usd"], second["estimated"]), (2.5, 0))
        # The meter row belongs to no assignment, and the one per-item query
        # sums `run` and `bound` rows only: a mutation that wrote it as
        # `run` is caught above by count, and here by the sum staying put.
        self.assertEqual(
            self.db.execute(
                "SELECT COALESCE(SUM(usd), 0) FROM cost WHERE source IN ('run', 'bound')"
            ).fetchone()[0],
            3.75,
        )
        self.assertIsNone(self.meter_rows()[0]["assignment"])


class TheNewestRowForABillAndWindow(MeterCase):
    """`latest`: the newest `meter` row for one bill and one window, whatever
    month it was written in. What the pack's meter step reads to classify a
    bill; `usage_month` is the month's own read and cannot answer it."""

    OLD = "2026-08-31T23:59:00Z"
    NEW = "2026-09-01T00:01:00Z"

    def test_latest_is_the_newest_row_for_the_bill_and_window_across_a_month_end(self):
        """A row two minutes before the month ends and one two minutes after
        it: `latest` answers the later, and answers the earlier while it is
        the only one. `usage_month` cannot: its meter read is month-scoped,
        `substr(timestamp, 1, 7) = ?`, in `_usage_reads` of
        `local-sd-db/sd_db/reads.py`, so for the new month it holds only the
        new month's row, for the old month only the old one, and in the new
        month's first five hours before a fresh sample it holds nothing."""
        old = sample(self.db, provider="claude", window_minutes=300, used_percent=40, now=self.OLD)
        only = latest(self.db, bill="open", window_minutes=300)
        self.assertEqual(only["id"], old)
        self.assertEqual(usage_month(self.db, month="2026-09").meter, ())
        new = sample(self.db, provider="claude", window_minutes=300, used_percent=55, now=self.NEW)

        row = latest(self.db, bill="open", window_minutes=300)
        self.assertEqual(
            (row["id"], row["provider"], row["bill"], row["window_minutes"], row["used_percent"], row["timestamp"]),
            (new, "claude", "open", 300, 55.0, "2026-09-01T00:01:00+00:00"),
        )
        september = usage_month(self.db, month="2026-09").meter
        august = usage_month(self.db, month="2026-08").meter
        self.assertEqual([entry["timestamp"] for entry in september], ["2026-09-01T00:01:00+00:00"])
        self.assertEqual([entry["timestamp"] for entry in august], ["2026-08-31T23:59:00+00:00"])

    def test_latest_is_none_with_no_row(self):
        self.assertIsNone(latest(self.db, bill="open", window_minutes=300))
        sample(self.db, provider="claude", window_minutes=300, used_percent=40, now=AT)
        self.assertIsNone(latest(self.db, bill="open", window_minutes=10080))
        self.assertIsNone(latest(self.db, bill="capped", window_minutes=300))
        self.assertIs(sd_db.latest, latest)

    def test_latest_ignores_another_bills_row_and_another_window(self):
        """Keyed by bill and window: a newer row on the other bill and a
        newer row for the other window on the same bill do not win."""
        mine = sample(self.db, provider="claude", window_minutes=300, used_percent=40, now=self.OLD)
        sample(self.db, provider="kimi", window_minutes=300, used_percent=90, now=self.NEW)
        sample(self.db, provider="claude", window_minutes=10080, used_percent=90, now=self.NEW)
        row = latest(self.db, bill="open", window_minutes=300)
        self.assertEqual((row["id"], row["bill"], row["window_minutes"], row["used_percent"]), (mine, "open", 300, 40.0))

    def test_latest_ignores_a_row_of_another_source(self):
        """Only `meter` rows are readings. The schema lets any `cost` row
        carry `window_minutes` and `used_percent`, so a newer `run` row on
        the same bill and window, written straight to the table, does not
        win; the meter row does."""
        mine = sample(self.db, provider="claude", window_minutes=300, used_percent=40, now=self.OLD)
        self.db.execute(
            "INSERT INTO cost (timestamp, provider, bill, window_minutes, used_percent, usd, source) "
            "VALUES (?, 'mini', 'open', 300, 99, 1.0, 'run')", ("2026-09-01T00:01:00+00:00",))
        row = latest(self.db, bill="open", window_minutes=300)
        self.assertEqual((row["id"], row["used_percent"]), (mine, 40.0))

    def test_latest_orders_by_id_when_timestamps_tie(self):
        """Two samples at one moment: the later row, by id, is the answer,
        the rule `usage_month`'s meter read already applies. Asked twice:
        once as the store is, where the planner walks `cost_by_bill` and
        hands a tie back in id order on its own, and once with that index
        dropped, where only the query's own `id DESC` keeps the answer."""
        sample(self.db, provider="claude", window_minutes=300, used_percent=40, now=AT)
        later = sample(self.db, provider="claude", window_minutes=300, used_percent=41, now=AT)
        row = latest(self.db, bill="open", window_minutes=300)
        self.assertEqual((row["id"], row["used_percent"]), (later, 41.0))
        self.db.execute("DROP INDEX cost_by_bill")
        row = latest(self.db, bill="open", window_minutes=300)
        self.assertEqual((row["id"], row["used_percent"]), (later, 41.0))

    def test_latest_refuses_a_window_that_is_not_a_positive_integer(self):
        """The refusal `sample` gives, the same predicate and the same error."""
        for bad in (0, -300, 300.0, "300", None, True, 2**63):
            with self.subTest(window_minutes=bad):
                with self.assertRaises(MeterRefused) as caught:
                    latest(self.db, bill="open", window_minutes=bad)
                self.assertIn("window_minutes", str(caught.exception))
                self.assertIn("'open'", str(caught.exception))


class WhatTheMeterRefuses(MeterCase):
    def test_an_unknown_provider_is_refused_naming_it_and_nothing_is_written(self):
        before = self.dump()
        with self.assertRaises(MeterRefused) as caught:
            sample(self.db, provider="gemini", window_minutes=300, used_percent=1, now=AT)
        self.assertIn("'gemini'", str(caught.exception))
        self.assertIn("providers.yaml", str(caught.exception))
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.meter_rows(), [])

    def test_a_percentage_outside_zero_to_one_hundred_is_refused_before_the_write(self):
        before = self.dump()
        for bad in (101, -1, 100.5, float("nan"), float("inf"), "61", None, True, 10**1000):
            with self.subTest(used_percent=bad):
                with self.assertRaises(MeterRefused) as caught:
                    sample(self.db, provider="claude", window_minutes=300,
                           used_percent=bad, now=AT)
                self.assertIn("used_percent", str(caught.exception))
                self.assertIn("'claude'", str(caught.exception))
        self.assertEqual(self.dump(), before)
        # The edges are inside the range.
        sample(self.db, provider="claude", window_minutes=300, used_percent=0, now=AT)
        sample(self.db, provider="claude", window_minutes=300, used_percent=100, now=AT)
        self.assertEqual([row["used_percent"] for row in self.meter_rows()], [0.0, 100.0])

    def test_a_non_positive_or_non_integer_window_is_refused_before_the_write(self):
        before = self.dump()
        for bad in (0, -300, 300.0, "300", None, True, 2**63):
            with self.subTest(window_minutes=bad):
                with self.assertRaises(MeterRefused) as caught:
                    sample(self.db, provider="claude", window_minutes=bad,
                           used_percent=50, now=AT)
                self.assertIn("window_minutes", str(caught.exception))
        self.assertEqual(self.dump(), before)
        # SQLite's largest INTEGER is the edge: one past it raised the
        # driver's `OverflowError` at the INSERT, not a refusal (sd:1219).
        sample(self.db, provider="claude", window_minutes=2**63 - 1, used_percent=50, now=AT)
        self.assertEqual([row["window_minutes"] for row in self.meter_rows()], [2**63 - 1])

    def test_the_refusal_is_the_librarys_own_error(self):
        self.assertTrue(issubclass(MeterRefused, sd_db.SdDbError))
        self.assertIs(sd_db.sample, sample)
        self.assertIs(sd_db.MeterRefused, MeterRefused)


class AnUnseededStoreSeedsOnTheSample(unittest.TestCase):
    """A store `init` made has no `provider` or `bill` rows until something
    reads the registry through a writable connection."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        state = home / ".local/share/sd"
        state.mkdir(parents=True)
        (state / "providers.yaml").write_text(REGISTRY, encoding="utf-8")
        initialise(home=home)
        self.db = connect(home=home)
        self.addCleanup(self.db.close)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM provider").fetchone()[0], 0)

    def test_the_first_sample_on_a_fresh_store_seeds_the_registry_rows_it_references(self):
        """`cost.provider` and `cost.bill` are foreign keys; the sample seeds
        them in its own transaction, so the row and its referents land together."""
        sample(self.db, provider="claude", window_minutes=300, used_percent=61, now=AT)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM provider").fetchone()[0], 3)
        (row,) = self.db.execute("SELECT provider, bill FROM cost").fetchall()
        self.assertEqual(tuple(row), ("claude", "open"))

    def test_a_refused_sample_on_a_fresh_store_seeds_nothing_either(self):
        """The refusal is before any write, and the seed is a write: an
        unknown provider leaves a fresh store byte-for-byte as it was."""
        before = "\n".join(self.db.iterdump())
        with self.assertRaises(MeterRefused):
            sample(self.db, provider="gemini", window_minutes=300, used_percent=1, now=AT)
        self.assertEqual("\n".join(self.db.iterdump()), before)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM provider").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
