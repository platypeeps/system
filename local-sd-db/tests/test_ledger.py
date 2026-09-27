"""The reservation ledger: the cap reserves, the claim is the only road to
the wire, and settling happens once. Criterion 15's ledger clauses, the
library half: what the client and the runner will call, asserted on rows.
"""

import json
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sd_db
from sd_db import connect, create_assignment, create_item, record_cost, seed, set_bill_cap
from sd_db import ledger
from sd_db.ledger import (
    LedgerRefused,
    Released,
    claim,
    exposure,
    lose,
    release_orphans,
    reserve,
    set_budget,
    settle,
)
from sd_db.migrate import initialise
from sd_db.registry import parse

#: Two bills: one with a cap the ledger enforces, one without. Every author
#: the tests need: a `url` entry on each bill, and a `start` entry, which is
#: the one a budget must refuse. The reviewer list is `url` only, so a
#: budget on a `url` author is accepted; `SPAWNED_REVIEWER` puts the `start`
#: entry there for the test that refuses it by that role.
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

SPAWNED_REVIEWER = REGISTRY.replace("  reviewer: [mini, kimi]", "  reviewer: [mini, claude]")

CAP = 10.0
ME = os.getpid()


def dead_pid() -> int:
    """A pid that was a process and is one no longer."""
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        state = self.home / ".local/share/sd"
        state.mkdir(parents=True)
        self.registry_path = state / "providers.yaml"
        self.registry_path.write_text(REGISTRY, encoding="utf-8")
        initialise(home=self.home)
        self.db = connect(home=self.home)
        self.addCleanup(self.db.close)
        seed(self.db, parse(REGISTRY, "providers.yaml"))
        self.item = create_item(self.db, kind="work", title="a capped item")

    def assignment(self, *, provider="kimi", budget_usd=None):
        return create_assignment(
            self.db, role="author", status="queued", item=self.item,
            provider=provider, budget_usd=budget_usd,
        )

    def row(self, call_id):
        return self.db.execute("SELECT * FROM cost WHERE call_id = ?", (call_id,)).fetchone()

    def rows(self):
        return self.db.execute("SELECT call_id, source, usd FROM cost ORDER BY id").fetchall()


class TheCapReserves(LedgerCase):
    def test_two_concurrent_reservations_with_room_for_one_dispatch_exactly_one(self):
        """Two callers, one bill with room for one bound, and the check runs
        under the write lock: the second reads the first's row, or waits."""
        results = {}
        gate = threading.Barrier(2)
        real = ledger.exposure

        def slow(connection, **scope):
            spent = real(connection, **scope)
            time.sleep(0.2)
            return spent

        def caller(call_id):
            connection = connect(home=self.home)
            try:
                gate.wait(timeout=5)
                results[call_id] = reserve(
                    connection, bill="capped", bound=8.0, call_id=call_id, owner_pid=ME,
                )
            except LedgerRefused as refused:
                results[call_id] = refused
            finally:
                connection.close()

        with patch.object(ledger, "exposure", slow):
            threads = [threading.Thread(target=caller, args=(name,)) for name in ("a", "b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
        self.assertEqual(sorted(results), ["a", "b"])
        dispatched = [name for name, got in results.items() if isinstance(got, int)]
        refused = [got for got in results.values() if isinstance(got, LedgerRefused)]
        self.assertEqual(len(dispatched), 1, results)
        self.assertEqual(len(refused), 1, results)
        self.assertEqual(refused[0].scope, "bill")
        self.assertEqual([r["source"] for r in self.rows()], ["reserved"])
        self.assertEqual(exposure(self.db, bill="capped"), 8.0)

    def test_a_bound_alone_over_the_remaining_room_is_refused(self):
        reserve(self.db, bill="capped", bound=5.0, call_id="first", owner_pid=ME,
                now="2026-09-10T10:00:00+00:00")
        claim(self.db, "first", now="2026-09-10T10:00:01+00:00")
        settle(self.db, "first", usd=5.0)
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="capped", bound=6.0, call_id="second", owner_pid=ME,
                    now="2026-09-10T10:01:00+00:00")
        message = str(raised.exception)
        # The refusal names the bill, the month, its total so far and the
        # cap (sd:788 reads this message from `bin/sd-review`).
        self.assertIn("bill 'capped'", message)
        self.assertIn("for 2026-09", message)
        self.assertIn("(5.00 spent or held)", message)
        self.assertIn("5.00 of its 10.00 cap", message)
        self.assertIn("bound is 6.00", message)
        self.assertEqual((raised.exception.exposure, raised.exception.limit), (5.0, CAP))
        self.assertIsNone(self.row("second"))
        # Exactly the room is not over it.
        reserve(self.db, bill="capped", bound=5.0, call_id="third", owner_pid=ME,
                now="2026-09-10T10:02:00+00:00")
        self.assertEqual(self.row("third")["source"], "reserved")

    def test_an_uncapped_bill_reserves_without_a_limit(self):
        for index in range(3):
            reserve(self.db, bill="open", bound=100.0, call_id=f"c{index}", owner_pid=ME)
        self.assertEqual(exposure(self.db, bill="open"), 300.0)

    def test_a_call_id_is_one_attempt(self):
        reserve(self.db, bill="open", bound=1.0, call_id="once", owner_pid=ME)
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="open", bound=1.0, call_id="once", owner_pid=ME)
        self.assertIn("'once' is already in the ledger as 'reserved'", str(raised.exception))
        self.assertEqual(len(self.rows()), 1)

    def test_an_unknown_bill_and_a_bad_owner_are_refused_before_any_row(self):
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="nowhere", bound=1.0, call_id="x", owner_pid=ME)
        self.assertIn("no bill 'nowhere'", str(raised.exception))
        with self.assertRaises(LedgerRefused):
            reserve(self.db, bill="open", bound=1.0, call_id="y", owner_pid=0)
        with self.assertRaises(LedgerRefused):
            reserve(self.db, bill="open", bound=-1.0, call_id="z", owner_pid=ME)
        self.assertEqual(self.rows(), [])


class SettlingHappensOnce(LedgerCase):
    def test_settling_twice_changes_nothing(self):
        reserve(self.db, bill="capped", bound=4.0, call_id="call", owner_pid=ME,
                now="2026-09-10T10:00:00+00:00")
        claim(self.db, "call", now="2026-09-10T10:00:01+00:00")
        self.assertEqual(self.row("call")["source"], "sending")
        self.assertTrue(settle(self.db, "call", usd=2.5, tokens_in=1000, tokens_out=200))
        before = dict(self.row("call"))
        self.assertEqual((before["source"], before["usd"]), ("run", 2.5))
        self.assertEqual((before["tokens_in"], before["tokens_out"]), (1000, 200))
        self.assertFalse(settle(self.db, "call", usd=99.0, tokens_in=1, tokens_out=1))
        self.assertFalse(lose(self.db, "call"))
        self.assertEqual(dict(self.row("call")), before)
        self.assertEqual(exposure(self.db, bill="capped", month="2026-09"), 2.5)

    def test_a_lost_response_settles_to_bound_at_the_full_bound_and_still_counts(self):
        reserve(self.db, bill="capped", bound=4.0, call_id="lost", owner_pid=ME,
                now="2026-09-10T10:00:00+00:00")
        claim(self.db, "lost", now="2026-09-10T10:00:01+00:00")
        self.assertTrue(lose(self.db, "lost"))
        row = self.row("lost")
        self.assertEqual((row["source"], row["usd"]), ("bound", 4.0))
        self.assertFalse(settle(self.db, "lost", usd=1.0))
        self.assertEqual(self.row("lost")["source"], "bound")
        with self.assertRaises(LedgerRefused):
            reserve(self.db, bill="capped", bound=7.0, call_id="next", owner_pid=ME,
                    now="2026-09-10T10:01:00+00:00")

    def test_settling_what_was_never_claimed_is_refused(self):
        reserve(self.db, bill="open", bound=4.0, call_id="held", owner_pid=ME)
        with self.assertRaises(LedgerRefused) as raised:
            settle(self.db, "held", usd=1.0)
        self.assertIn("was never claimed", str(raised.exception))
        with self.assertRaises(LedgerRefused) as raised:
            lose(self.db, "nobody")
        self.assertIn("holds no row", str(raised.exception))
        self.assertEqual(self.row("held")["source"], "reserved")


class TheClaimIsTheOnlyRoad(LedgerCase):
    def test_a_claim_refuses_a_row_that_is_not_reserved(self):
        reserve(self.db, bill="open", bound=1.0, call_id="call", owner_pid=ME)
        claim(self.db, "call")
        with self.assertRaises(LedgerRefused) as raised:
            claim(self.db, "call")
        self.assertIn("its row is 'sending', not 'reserved'", str(raised.exception))
        with self.assertRaises(LedgerRefused) as raised:
            claim(self.db, "released")
        self.assertIn("holds no row for it", str(raised.exception))
        self.assertEqual(self.row("call")["source"], "sending")

    def test_the_month_boundary(self):
        """Reserved a minute before the month ends, claimed a minute after:
        the open row counts against the new month's cap, the `run` row
        carries the claim month, and the old month never held it."""
        reserve(self.db, bill="capped", bound=4.0, call_id="late", owner_pid=ME,
                now="2026-09-30T23:59:00+00:00")
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="capped", bound=7.0, call_id="october", owner_pid=ME,
                    now="2026-10-01T00:00:30+00:00")
        self.assertEqual(raised.exception.exposure, 4.0)
        claim(self.db, "late", now="2026-10-01T00:01:00+00:00")
        self.assertTrue(settle(self.db, "late", usd=3.0))
        row = self.row("late")
        self.assertEqual(row["source"], "run")
        self.assertEqual(row["timestamp"][:7], "2026-10")
        self.assertEqual(exposure(self.db, bill="capped", month="2026-10"), 3.0)
        self.assertEqual(exposure(self.db, bill="capped", month="2026-09"), 0.0)
        reserve(self.db, bill="capped", bound=7.0, call_id="october", owner_pid=ME,
                now="2026-10-01T00:02:00+00:00")


class DeadOwners(LedgerCase):
    def test_a_dead_owner_reservation_is_released_by_the_next_reservation(self):
        gone = dead_pid()
        reserve(self.db, bill="capped", bound=8.0, call_id="orphan", owner_pid=gone,
                now="2026-09-10T10:00:00+00:00")
        # The next reservation, on any bill, releases it and the room is free.
        reserve(self.db, bill="open", bound=1.0, call_id="elsewhere", owner_pid=ME,
                now="2026-09-10T10:01:00+00:00")
        self.assertIsNone(self.row("orphan"))
        reserve(self.db, bill="capped", bound=CAP, call_id="whole", owner_pid=ME,
                now="2026-09-10T10:02:00+00:00")
        self.assertEqual(self.row("whole")["source"], "reserved")
        with self.assertRaises(LedgerRefused) as raised:
            claim(self.db, "orphan")
        self.assertIn("holds no row for it", str(raised.exception))

    def test_a_dead_owner_sending_row_settles_to_bound_and_still_counts(self):
        gone = dead_pid()
        reserve(self.db, bill="capped", bound=8.0, call_id="wire", owner_pid=gone,
                now="2026-09-10T10:00:00+00:00")
        claim(self.db, "wire", now="2026-09-10T10:00:01+00:00")
        with self.assertRaises(LedgerRefused):
            reserve(self.db, bill="capped", bound=3.0, call_id="next", owner_pid=ME,
                    now="2026-09-10T10:01:00+00:00")
        row = self.row("wire")
        self.assertEqual((row["source"], row["usd"]), ("bound", 8.0))
        self.assertEqual(exposure(self.db, bill="capped", month="2026-09"), 8.0)

    def test_a_live_owner_is_left_alone(self):
        reserve(self.db, bill="capped", bound=8.0, call_id="mine", owner_pid=ME,
                now="2026-09-10T10:00:00+00:00")
        reserve(self.db, bill="open", bound=1.0, call_id="other", owner_pid=ME,
                now="2026-09-10T10:01:00+00:00")
        self.assertEqual(self.row("mine")["source"], "reserved")
        self.assertEqual(release_orphans(self.db), Released())

    def test_the_sweep_is_exported_for_sd_usage(self):
        gone = dead_pid()
        # The second reservation's own sweep would release the first; hold
        # the process check open so both rows are there for `sd usage`.
        reserve(self.db, bill="open", bound=1.0, call_id="a", owner_pid=gone)
        reserve(self.db, bill="open", bound=2.0, call_id="b", owner_pid=gone,
                is_alive=lambda pid: True)
        claim(self.db, "b")
        swept = sd_db.release_orphans(self.db)
        self.assertEqual(swept, Released(deleted=("a",), bound=("b",)))
        self.assertEqual([tuple(r) for r in self.rows()], [("b", "bound", 2.0)])
        self.assertEqual(sd_db.release_orphans(self.db), Released())

    def test_the_process_check_is_replaceable(self):
        reserve(self.db, bill="open", bound=1.0, call_id="a", owner_pid=ME)
        swept = release_orphans(self.db, is_alive=lambda pid: False)
        self.assertEqual(swept, Released(deleted=("a",)))


class TheBudget(LedgerCase):
    def test_sending_at_eight_of_a_budget_of_ten_refuses_a_second_eight(self):
        """Against the assignment and against a cap-ten bill alike, through
        the one exposure function: a call on the wire counts."""
        budgeted = self.assignment(provider="mini", budget_usd=10.0)
        reserve(self.db, bill="open", bound=8.0, call_id="first", owner_pid=ME,
                assignment=budgeted, now="2026-09-10T10:00:00+00:00")
        claim(self.db, "first", now="2026-09-10T10:00:01+00:00")
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="open", bound=8.0, call_id="second", owner_pid=ME,
                    assignment=budgeted, now="2026-09-10T10:01:00+00:00")
        self.assertEqual(raised.exception.scope, "assignment")
        message = str(raised.exception)
        self.assertIn("budget spent", message)
        self.assertIn(f"assignment {budgeted}", message)
        # When the budget is the limit, the message names it and the total.
        self.assertIn("2.00 of its 10.00 budget", message)
        self.assertIn("(8.00 spent or held)", message)
        self.assertEqual((raised.exception.exposure, raised.exception.limit), (8.0, 10.0))

        reserve(self.db, bill="capped", bound=8.0, call_id="third", owner_pid=ME,
                now="2026-09-10T10:02:00+00:00")
        claim(self.db, "third", now="2026-09-10T10:02:01+00:00")
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="capped", bound=8.0, call_id="fourth", owner_pid=ME,
                    now="2026-09-10T10:03:00+00:00")
        self.assertEqual(raised.exception.scope, "bill")
        self.assertEqual((raised.exception.exposure, raised.exception.limit), (8.0, CAP))
        self.assertEqual([r["source"] for r in self.rows()], ["sending", "sending"])

    def test_one_sum_over_cost_rows(self):
        """Both limits read `exposure`: the grep clause 15.10 asks for. The
        one sum is Python's since sd:1176 (exact decimals), SQL's before."""
        source = Path(ledger.__file__).read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"\bsum\(", source, re.IGNORECASE)), 1)

    def test_a_budget_bounds_the_assignment_on_an_uncapped_bill_across_months(self):
        budgeted = self.assignment(provider="mini", budget_usd=5.0)
        reserve(self.db, bill="open", bound=3.0, call_id="a", owner_pid=ME,
                assignment=budgeted, now="2026-09-30T23:00:00+00:00")
        claim(self.db, "a", now="2026-09-30T23:00:01+00:00")
        settle(self.db, "a", usd=3.0)
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="open", bound=3.0, call_id="b", owner_pid=ME,
                    assignment=budgeted, now="2026-10-01T00:00:00+00:00")
        self.assertIn("budget spent", str(raised.exception))
        reserve(self.db, bill="open", bound=2.0, call_id="c", owner_pid=ME,
                assignment=budgeted, now="2026-10-01T00:01:00+00:00")
        self.assertEqual(exposure(self.db, assignment=budgeted), 5.0)
        # Another assignment without a budget on the same bill is not bounded.
        other = self.assignment(provider="mini")
        reserve(self.db, bill="open", bound=50.0, call_id="d", owner_pid=ME, assignment=other)

    def test_budget_usd_on_a_start_author_is_refused_naming_the_entry(self):
        spawned = self.assignment(provider="claude")
        with self.assertRaises(LedgerRefused) as raised:
            set_budget(self.db, spawned, 5.0)
        message = str(raised.exception)
        self.assertIn("author 'claude' is a 'start' entry", message)
        self.assertIn(f"assignment {spawned}", message)
        row = self.db.execute("SELECT budget_usd FROM assignment WHERE id = ?", (spawned,)).fetchone()
        self.assertIsNone(row["budget_usd"])

    def test_budget_usd_on_a_url_author_is_accepted_and_cleared(self):
        called = self.assignment(provider="kimi")
        set_budget(self.db, called, 5.0)
        row = self.db.execute("SELECT budget_usd FROM assignment WHERE id = ?", (called,)).fetchone()
        self.assertEqual(row["budget_usd"], 5.0)
        set_budget(self.db, called, None)
        row = self.db.execute("SELECT budget_usd FROM assignment WHERE id = ?", (called,)).fetchone()
        self.assertIsNone(row["budget_usd"])

    def test_budget_usd_is_refused_when_a_reviewer_it_can_resolve_is_a_start_entry(self):
        self.registry_path.write_text(SPAWNED_REVIEWER, encoding="utf-8")
        called = self.assignment(provider="kimi")
        with self.assertRaises(LedgerRefused) as raised:
            set_budget(self.db, called, 5.0)
        self.assertIn("reviewer 'claude' is a 'start' entry", str(raised.exception))
        # A registry the caller passes is the one checked.
        set_budget(self.db, called, 5.0, registry=parse(REGISTRY, "providers.yaml"))
        row = self.db.execute("SELECT budget_usd FROM assignment WHERE id = ?", (called,)).fetchone()
        self.assertEqual(row["budget_usd"], 5.0)

    def test_budget_usd_needs_an_author_to_stand_in_front_of(self):
        unassigned = self.assignment(provider=None)
        with self.assertRaises(LedgerRefused) as raised:
            set_budget(self.db, unassigned, 5.0)
        self.assertIn("has no provider yet", str(raised.exception))
        with self.assertRaises(LedgerRefused):
            set_budget(self.db, self.assignment(), -1.0)


class Exposure(LedgerCase):
    def test_exposure_takes_exactly_one_scope(self):
        with self.assertRaises(ValueError):
            exposure(self.db)
        with self.assertRaises(ValueError):
            exposure(self.db, bill="open", assignment=1)

    def test_open_rows_count_in_every_month_and_settled_rows_in_theirs(self):
        reserve(self.db, bill="capped", bound=1.0, call_id="a", owner_pid=ME,
                now="2026-08-15T10:00:00+00:00")
        reserve(self.db, bill="capped", bound=2.0, call_id="b", owner_pid=ME,
                now="2026-08-15T10:00:00+00:00")
        claim(self.db, "b", now="2026-08-15T10:00:01+00:00")
        reserve(self.db, bill="capped", bound=4.0, call_id="c", owner_pid=ME,
                now="2026-08-15T10:00:00+00:00")
        claim(self.db, "c", now="2026-08-15T10:00:01+00:00")
        settle(self.db, "c", usd=3.0)
        self.assertEqual(exposure(self.db, bill="capped", month="2026-08"), 6.0)
        self.assertEqual(exposure(self.db, bill="capped", month="2026-09"), 3.0)
        self.assertEqual(exposure(self.db, bill="capped"), 6.0)
        set_bill_cap(self.db, "capped", 6.0)
        with self.assertRaises(LedgerRefused):
            reserve(self.db, bill="capped", bound=0.5, call_id="d", owner_pid=ME,
                    now="2026-08-20T10:00:00+00:00")
        reserve(self.db, bill="capped", bound=3.0, call_id="e", owner_pid=ME,
                now="2026-09-20T10:00:00+00:00")


class WhatTheReviewFound(LedgerCase):
    """#406's first review: three inline findings and two suppressed ones
    that held, each pinned here."""

    def test_a_bound_or_a_budget_that_is_not_a_finite_number_is_refused(self):
        """SQLite stores `nan` as NULL and a NULL `usd` sums to nothing, so
        a `nan` bound would pass every limit; `inf`, a string and a bool
        are not amounts either."""
        for bad in (float("nan"), float("inf"), "8", True, None):
            with self.assertRaises(LedgerRefused) as raised:
                reserve(self.db, bill="capped", bound=bad, call_id="bad", owner_pid=ME)
            self.assertIn("finite, non-negative number", str(raised.exception))
        self.assertEqual(self.rows(), [])
        called = self.assignment(provider="kimi")
        for bad in (float("nan"), float("inf"), "5", -1.0):
            with self.assertRaises(LedgerRefused):
                set_budget(self.db, called, bad)
        row = self.db.execute("SELECT budget_usd FROM assignment WHERE id = ?", (called,)).fetchone()
        self.assertIsNone(row["budget_usd"])
        reserve(self.db, bill="capped", bound=8, call_id="int", owner_pid=ME)
        self.assertEqual(self.row("int")["usd"], 8.0)

    def test_a_settlement_cost_is_finite_and_non_negative_and_tokens_are_counts(self):
        """A negative `run` cost would reopen room the call already spent."""
        reserve(self.db, bill="capped", bound=4.0, call_id="call", owner_pid=ME)
        claim(self.db, "call")
        for usd, tokens in ((-1.0, {}), (float("nan"), {}), ("2", {}),
                            (2.0, {"tokens_in": -1}), (2.0, {"tokens_out": 1.5}),
                            (2.0, {"tokens_in": True})):
            with self.assertRaises(LedgerRefused):
                settle(self.db, "call", usd=usd, **tokens)
        row = self.row("call")
        self.assertEqual((row["source"], row["usd"]), ("sending", 4.0))
        self.assertTrue(settle(self.db, "call", usd=0, tokens_in=0, tokens_out=None))
        self.assertEqual(self.row("call")["usd"], 0.0)

    def test_a_reservation_inside_an_open_transaction_is_refused(self):
        """`transaction` nests as a savepoint inside an open one and takes
        no lock, so the cap check would read under the caller's lock or
        none. The library's own writes all begin `IMMEDIATE`; a caller's
        deferred `BEGIN` is the case refused."""
        self.db.execute("BEGIN")
        try:
            with self.assertRaises(LedgerRefused) as raised:
                reserve(self.db, bill="capped", bound=1.0, call_id="nested", owner_pid=ME)
            self.assertIn("inside an open transaction", str(raised.exception))
        finally:
            self.db.execute("ROLLBACK")
        self.assertEqual(self.rows(), [])

    def test_set_budget_checks_and_writes_under_one_write_lock(self):
        """The runner can move an assignment's provider between a read and
        a write; the check must hold the lock the write takes."""
        called = self.assignment(provider="kimi")
        seen = []
        real = ledger.update_assignment

        def observed(connection, assignment, **columns):
            seen.append(connection.in_transaction)
            return real(connection, assignment, **columns)

        with patch.object(ledger, "update_assignment", observed):
            set_budget(self.db, called, 5.0)
        self.assertEqual(seen, [True])
        self.assertFalse(self.db.in_transaction)

    def test_the_pid_refusal_says_what_it_checks(self):
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="open", bound=1.0, call_id="pid", owner_pid=-4)
        self.assertIn("positive process id", str(raised.exception))
        self.assertNotIn("live", str(raised.exception))

    def test_a_call_id_that_is_not_a_non_empty_string_is_refused_before_any_row(self):
        """`cost.call_id` is nullable, and NULL never equals NULL: a row with
        no call id could never be claimed, settled or refused as a repeat."""
        for call_id in (None, "", 7, b"x"):
            with self.subTest(call_id=call_id):
                with self.assertRaises(LedgerRefused) as raised:
                    reserve(self.db, bill="open", bound=1.0, call_id=call_id, owner_pid=ME)
                self.assertIn("non-empty string", str(raised.exception))
        self.assertEqual(self.rows(), [])

    def test_the_moment_is_taken_under_the_write_lock(self):
        """A reservation or a claim that waits for the lock across month end
        belongs to the month it lands in; the clock is read after the lock."""
        seen = []
        real = ledger._now

        def observed():
            seen.append(self.db.in_transaction)
            return real()

        with patch.object(ledger, "_now", observed):
            reserve(self.db, bill="open", bound=1.0, call_id="clocked", owner_pid=ME)
            claim(self.db, "clocked")
        self.assertEqual(seen, [True, True])
        self.assertFalse(self.db.in_transaction)


class MoneyCompares(LedgerCase):
    """sd:965 (a), #406's R4/R5: `0.1 + 0.2 > 0.3` is True on binary floats,
    so a reservation that exactly fills the cap or the budget was refused.
    Both limit checks compare at micro-dollar precision, the smallest unit
    any caller writes."""

    def test_an_exact_fill_of_the_cap_is_admitted_and_a_micro_dollar_over_is_refused(self):
        set_bill_cap(self.db, "capped", 0.30)
        reserve(self.db, bill="capped", bound=0.10, call_id="first", owner_pid=ME,
                now="2026-09-10T10:00:00+00:00")
        claim(self.db, "first", now="2026-09-10T10:00:01+00:00")
        settle(self.db, "first", usd=0.10)
        self.assertEqual(exposure(self.db, bill="capped", month="2026-09"), 0.10)
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="capped", bound=0.200001, call_id="over", owner_pid=ME,
                    now="2026-09-10T10:01:00+00:00")
        self.assertEqual(raised.exception.scope, "bill")
        self.assertEqual((raised.exception.exposure, raised.exception.limit), (0.10, 0.30))
        self.assertIsNone(self.row("over"))
        # Exactly the room: 0.10 + 0.20 against 0.30 is not over it.
        reserve(self.db, bill="capped", bound=0.20, call_id="fill", owner_pid=ME,
                now="2026-09-10T10:02:00+00:00")
        self.assertEqual(self.row("fill")["source"], "reserved")

    def test_an_exact_fill_of_the_budget_is_admitted_and_a_micro_dollar_over_is_refused(self):
        budgeted = self.assignment(provider="mini", budget_usd=0.30)
        reserve(self.db, bill="open", bound=0.10, call_id="first", owner_pid=ME,
                assignment=budgeted, now="2026-09-10T10:00:00+00:00")
        claim(self.db, "first", now="2026-09-10T10:00:01+00:00")
        settle(self.db, "first", usd=0.10)
        self.assertEqual(exposure(self.db, assignment=budgeted), 0.10)
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="open", bound=0.200001, call_id="over", owner_pid=ME,
                    assignment=budgeted, now="2026-09-10T10:01:00+00:00")
        self.assertEqual(raised.exception.scope, "assignment")
        self.assertIn("budget spent", str(raised.exception))
        self.assertEqual((raised.exception.exposure, raised.exception.limit), (0.10, 0.30))
        self.assertIsNone(self.row("over"))
        reserve(self.db, bill="open", bound=0.20, call_id="fill", owner_pid=ME,
                assignment=budgeted, now="2026-09-10T10:02:00+00:00")
        self.assertEqual(self.row("fill")["source"], "reserved")

    def test_a_fraction_of_a_micro_dollar_over_is_still_over(self):
        """#423's review: a six-place rounding admitted `bound=0.2000004`
        over 0.10 against 0.30. The tolerance is float noise only, so
        anything a caller can write above the limit is refused."""
        set_bill_cap(self.db, "capped", 0.30)
        reserve(self.db, bill="capped", bound=0.10, call_id="first", owner_pid=ME,
                now="2026-09-10T10:00:00+00:00")
        claim(self.db, "first", now="2026-09-10T10:00:01+00:00")
        settle(self.db, "first", usd=0.10)
        budgeted = self.assignment(provider="mini", budget_usd=0.30)
        reserve(self.db, bill="open", bound=0.10, call_id="budgeted", owner_pid=ME,
                assignment=budgeted, now="2026-09-10T10:00:02+00:00")
        claim(self.db, "budgeted", now="2026-09-10T10:00:03+00:00")
        settle(self.db, "budgeted", usd=0.10)
        # 5e-10 over is under the old MONEY_NOISE of 1e-9, which admitted it (sd:1176).
        for bound in (0.2000004, 0.2000005, 0.2000000005):
            with self.subTest(bound=bound, limit="cap"):
                with self.assertRaises(LedgerRefused) as raised:
                    reserve(self.db, bill="capped", bound=bound, call_id=f"cap{bound}",
                            owner_pid=ME, now="2026-09-10T10:01:00+00:00")
                self.assertEqual((raised.exception.exposure, raised.exception.limit), (0.10, 0.30))
            with self.subTest(bound=bound, limit="budget"):
                with self.assertRaises(LedgerRefused) as raised:
                    reserve(self.db, bill="open", bound=bound, call_id=f"budget{bound}",
                            owner_pid=ME, assignment=budgeted, now="2026-09-10T10:01:00+00:00")
                self.assertIn("budget spent", str(raised.exception))
        self.assertEqual([r["source"] for r in self.rows()], ["run", "run"])


class WhatTheThirdReviewFound(LedgerCase):
    """#406's third Copilot pass, carried on sd:965 (note 2574) and taken
    here with slice 8b: R2 the overshoot, R8 the nested transaction, R9 the
    overflowing int, R10 the fresh store, R11 the row with no call id."""

    def test_r2_a_settlement_above_the_bound_settles_at_the_actual_cost_and_files_cap_overshot(self):
        """C-59's attention row: the bound was what the cap admitted, so a
        cost above it is money the cap did not stand in front of. The row
        is `run` at the actual cost, and one attention report names the
        call, the bound and the actual; a settlement within the bound
        files nothing."""
        reserve(self.db, bill="capped", bound=2.0, call_id="over", owner_pid=ME)
        claim(self.db, "over")
        self.assertTrue(settle(self.db, "over", usd=2.5, tokens_in=10, tokens_out=20))
        self.assertEqual(self.row("over")["usd"], 2.5)
        (report,) = self.db.execute(
            "SELECT title, external_id, fields, body FROM item WHERE kind = 'report'"
        ).fetchall()
        fields = json.loads(report["fields"])
        self.assertEqual(report["external_id"], f"{ledger.OVERSHOOT_JOB}:over")
        self.assertIs(fields["attention"], True)
        self.assertEqual(fields["report"]["attention_basis"], "cap overshot on call over")
        text = json.loads(report["body"])["text"]
        self.assertIn("call over", text)
        self.assertIn("2.500000", text)
        self.assertIn("2.000000", text)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM note WHERE kind = 'followup'").fetchone()[0], 1)
        # Within the bound, and the exact fill: nothing filed.
        reserve(self.db, bill="capped", bound=2.0, call_id="under", owner_pid=ME)
        claim(self.db, "under")
        settle(self.db, "under", usd=2.0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM item WHERE kind = 'report'").fetchone()[0], 1)

    def test_r8_claim_settle_and_lose_are_refused_inside_an_open_transaction(self):
        """Nested, `transaction` is a savepoint under the caller's rollback,
        which would un-claim a request already on the wire or un-settle a
        response already read. Each is refused as `reserve` is, and the
        row does not move."""
        reserve(self.db, bill="open", bound=1.0, call_id="nested", owner_pid=ME)

        def refused(step, stays):
            self.db.execute("BEGIN")
            try:
                with self.assertRaises(LedgerRefused) as raised:
                    step()
                self.assertIn("inside an open one", str(raised.exception))
            finally:
                self.db.execute("ROLLBACK")
            self.assertEqual(self.row("nested")["source"], stays)

        refused(lambda: claim(self.db, "nested"), "reserved")
        claim(self.db, "nested")
        refused(lambda: settle(self.db, "nested", usd=0.5), "sending")
        refused(lambda: lose(self.db, "nested"), "sending")
        self.assertFalse(self.db.in_transaction)

    def test_r9_an_int_too_large_for_a_float_is_refused_as_not_finite(self):
        """`math.isfinite(10**1000)` raises `OverflowError`; `_money` answers
        with the refusal rather than the traceback. Landed with #415; pinned
        here on the ledger's three money doors."""
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="open", bound=10**1000, call_id="huge", owner_pid=ME)
        self.assertIn("finite, non-negative number", str(raised.exception))
        called = self.assignment(provider="kimi")
        with self.assertRaises(LedgerRefused):
            set_budget(self.db, called, 10**1000)
        reserve(self.db, bill="open", bound=1.0, call_id="huge", owner_pid=ME)
        claim(self.db, "huge")
        with self.assertRaises(LedgerRefused):
            settle(self.db, "huge", usd=10**1000)
        self.assertEqual(self.row("huge")["source"], "sending")

    def test_r11_a_row_with_no_call_id_is_released_by_its_row_id(self):
        """`record_cost` can write a `reserved` or `sending` row with a NULL
        call id, which `WHERE call_id = ?` never matches: the sweep reported
        it released and left it open. Released by row id, and named `None`
        in `Released`, since it has no other name."""
        reserve(self.db, bill="open", bound=1.0, call_id="live", owner_pid=ME)
        gone = dead_pid()
        record_cost(self.db, source="reserved", bill="open", owner_pid=gone, usd=1.0)
        record_cost(self.db, source="sending", bill="open", owner_pid=gone, usd=2.0)
        released = release_orphans(self.db)
        self.assertEqual(released, Released(deleted=(None,), bound=(None,)))
        self.assertEqual([tuple(row) for row in self.rows()], [("live", "reserved", 1.0), (None, "bound", 2.0)])


class WhatSliceEightBsReviewFound(LedgerCase):
    """#426's Copilot pass on R2: the overshoot report's run id is the call
    id, and `reporting.ingest` takes at most 160 characters of it."""

    def test_a_call_id_longer_than_the_report_run_id_is_refused_at_reserve_not_at_settle(self):
        """A 161-character id reserved and claimed fine, then the overshoot
        report's `WorkflowError` rolled the settlement back and the row
        stayed `sending`. Refused at `reserve`, before any row; at the limit
        the report is filed."""
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="capped", bound=1.0, call_id="c" * 161, owner_pid=ME)
        self.assertIn("non-empty string of at most 160 characters", str(raised.exception))
        self.assertIn("got 161 characters", str(raised.exception))
        self.assertEqual(self.rows(), [])
        longest = "c" * 160
        reserve(self.db, bill="capped", bound=1.0, call_id=longest, owner_pid=ME)
        claim(self.db, longest)
        self.assertTrue(settle(self.db, longest, usd=1.5))
        self.assertEqual(self.row(longest)["source"], "run")
        (external_id,) = self.db.execute("SELECT external_id FROM item WHERE kind = 'report'").fetchone()
        self.assertEqual(external_id, f"{ledger.OVERSHOOT_JOB}:{longest}")
        self.assertEqual(ledger.MAX_CALL_ID, 160)


class AFreshStoreSeedsOnTheFirstReservation(unittest.TestCase):
    """R10: a store `init` made has no `bill` rows until something reads the
    registry through a writable connection; the first `reserve` must not say
    `no bill` for a bill the file beside the database names."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.state = home / ".local/share/sd"
        self.state.mkdir(parents=True)
        (self.state / "providers.yaml").write_text(REGISTRY, encoding="utf-8")
        initialise(home=home)
        self.db = connect(home=home)
        self.addCleanup(self.db.close)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bill").fetchone()[0], 0)

    def test_the_first_reservation_reads_the_registry_and_holds_against_the_files_cap(self):
        reserve(self.db, bill="capped", bound=8.0, call_id="first", owner_pid=ME)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bill").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM provider").fetchone()[0], 3)
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="capped", bound=8.0, call_id="second", owner_pid=ME)
        self.assertEqual(raised.exception.limit, CAP)

    def test_a_bill_the_file_does_not_name_is_still_no_bill_and_a_missing_file_says_so(self):
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="nowhere", bound=1.0, call_id="x", owner_pid=ME)
        self.assertIn("no bill 'nowhere'", str(raised.exception))
        (self.state / "providers.yaml").unlink()
        with self.assertRaises(LedgerRefused) as raised:
            reserve(self.db, bill="elsewhere", bound=1.0, call_id="y", owner_pid=ME)
        self.assertIn("could not seed", str(raised.exception))
        self.assertIn("providers.yaml", str(raised.exception))


class ThePackReaderKeepsItsName(unittest.TestCase):
    def test_money_noise_stays_for_the_pack_reader(self):
        # The pack's sd-review reads ledger.MONEY_NOISE in capped_bills. Removing
        # the name in sd:1176 broke every review on a machine that installed it.
        self.assertEqual(ledger.MONEY_NOISE, 1e-9)


if __name__ == "__main__":
    unittest.main()
