"""The collector that keeps `shadow` from freezing on the day of the switch.

A one-time import of `index.sqlite` would leave the table correct at midnight
and wrong by lunch. These assert the two rules a windowed collector cannot get
wrong: an hour of overlap, and a watermark that moves only on success.
"""

import json
import os
import socket
import socketserver
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sd_db import connect
from sd_db.migrate import initialise
from sd_db.progress import tracker_freshness
from sd_db.shadow_sync import (
    FIRST_RUN_WINDOW,
    NO_AUTH,
    OVERLAP,
    TRACKER,
    SyncBusy,
    _sync,
    iso,
    parse_iso,
    read_watermark,
    search,
    sync,
    sync_lock,
    window_start,
    write_watermark,
)
from tests.test_shadow_jira import (BROKEN_ANSWERS, JIRA_ENV, NON_ASCII_HOST, NON_ASCII_HOST_REASON, NOT_A_HOST,
                                    NOT_A_HOST_REASON, SECRET, UNSPLITTABLE,
                                    RawServer, jira_issue, jira_transport, no_socket, without_jira_environment)


def node(number, *, url=None, kind="Issue", state="OPEN", title="a title"):
    return {
        "__typename": kind,
        "number": number,
        "title": title,
        "url": url or f"https://github.com/example-org/benchmark/issues/{number}",
        "state": state,
        "updatedAt": "2026-09-05T10:00:00Z",
        "author": {"login": "octocat"},
        "repository": {"nameWithOwner": "example-org/benchmark"},
    }


class Gh:
    """A `gh` seam: it records every argv and answers from a script.

    Not a patch of `subprocess`. A patched suite proves a call was made; this
    proves it was made and spelled correctly, which is the harness's own rule.
    """

    def __init__(self, *, nodes=None, auth=True, fail=None, count=None):
        self.calls: list[list[str]] = []
        self.nodes = nodes if nodes is not None else []
        self.auth = auth
        self.fail = fail
        self.count = count

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["gh", "auth"]:
            return (0, "", "") if self.auth else (1, "", "not logged in")
        if self.fail:
            return 1, "", self.fail
        payload = {
            "data": {
                "search": {
                    "issueCount": self.count if self.count is not None else len(self.nodes),
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": self.nodes,
                }
            }
        }
        return 0, json.dumps(payload), ""

    @property
    def queries(self):
        out = []
        for argv in self.calls:
            for index, value in enumerate(argv):
                if value == "-f" and argv[index + 1].startswith("q="):
                    out.append(argv[index + 1][2:])
        return out


class SyncCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        database = Path(self.tmp.name) / "sd.db"
        initialise(database)
        self.connection = connect(database)
        self.addCleanup(self.connection.close)
        self.now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    def shadow(self):
        return {row["url"]: row for row in self.connection.execute("SELECT * FROM shadow")}


class TheWindow(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    def test_a_first_run_reads_ninety_days(self):
        self.assertEqual(window_start(None, self.now), self.now - FIRST_RUN_WINDOW)

    def test_a_watermark_is_re_read_with_an_hour_of_overlap(self):
        watermark = iso(self.now - timedelta(days=1))
        self.assertEqual(
            window_start(watermark, self.now),
            self.now - timedelta(days=1) - OVERLAP,
        )

    def test_a_watermark_it_cannot_parse_widens_the_window(self):
        """Narrowing on garbage is how a windowed collector stops collecting."""
        self.assertEqual(
            window_start("not a timestamp", self.now), self.now - FIRST_RUN_WINDOW
        )


class TheWatermark(SyncCase):
    def test_a_first_run_has_none(self):
        self.assertIsNone(read_watermark(self.connection))

    def test_only_a_resolved_row_counts(self):
        """A cursor written half-way is a cursor nothing may resume from."""
        from sd_db.writes import record_state

        record_state(
            self.connection, "watermark", key=TRACKER,
            body={"collected_at": "2026-09-01T00:00:00Z"},
        )
        self.assertIsNone(read_watermark(self.connection))
        write_watermark(self.connection, TRACKER, "2026-09-02T00:00:00Z", "x")
        self.assertEqual(read_watermark(self.connection), "2026-09-02T00:00:00Z")

    def test_the_newest_resolved_row_wins(self):
        write_watermark(self.connection, TRACKER, "2026-09-02T00:00:00Z", "x")
        write_watermark(self.connection, TRACKER, "2026-09-03T00:00:00Z", "x")
        self.assertEqual(read_watermark(self.connection), "2026-09-03T00:00:00Z")

    def test_a_watermark_row_leaves_no_unresolved_state_behind(self):
        """`sd-db.sh status` prints unresolved records; a nightly cursor is
        not something the operator has to reconcile."""
        write_watermark(self.connection, TRACKER, "2026-09-02T00:00:00Z", "x")
        rows = list(self.connection.execute(
            "SELECT * FROM state WHERE resolved_at IS NULL"
        ))
        self.assertEqual(rows, [])


class TheSync(SyncCase):
    def test_collected_issues_land_as_shadow_rows(self):
        from tests.test_contribution_github import URL, Api

        runner = Gh(nodes=[node(11), node(7, kind="PullRequest", url=URL)])
        result = sync(self.connection, now=self.now, runner=runner,
                      contribution_client=Api().client(), notifier=lambda claim: "sent")
        self.assertTrue(result.ok)
        rows = self.shadow()
        self.assertEqual(len(rows), 2)
        kinds = {row["kind"] for row in rows.values()}
        self.assertEqual(kinds, {"issue", "pull"})
        self.assertEqual({row["tracker"] for row in rows.values()}, {TRACKER})

    def test_the_watermark_moves_on_success(self):
        sync(self.connection, now=self.now, runner=Gh(nodes=[node(11)]))
        self.assertEqual(read_watermark(self.connection), iso(self.now))

    def test_a_failed_collect_holds_the_watermark(self):
        """A failed run costs a repeated window, never a skipped one."""
        write_watermark(self.connection, TRACKER, "2026-09-01T00:00:00Z", "x")
        result = sync(
            self.connection, now=self.now, runner=Gh(fail="the API said no")
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(read_watermark(self.connection), "2026-09-01T00:00:00Z")

    def test_a_missing_credential_is_reported_by_name_and_not_by_secret(self):
        result = sync(self.connection, now=self.now, runner=Gh(auth=False))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, NO_AUTH)
        self.assertEqual(self.shadow(), {})

    def test_the_four_buckets_are_searched_separately(self):
        """`involves:@me` would collapse *why* an issue reached you."""
        runner = Gh(nodes=[])
        sync(self.connection, now=self.now, runner=runner)
        self.assertEqual(len(runner.queries), 4)
        self.assertTrue(any("review-requested:@me" in one for one in runner.queries))
        expected = f"updated:{iso(self.now - FIRST_RUN_WINDOW)}..{iso(self.now)}"
        self.assertTrue(all(expected in one for one in runner.queries))

    def test_a_second_sync_updates_the_row_rather_than_adding_one(self):
        sync(self.connection, now=self.now, runner=Gh(nodes=[node(11)]))
        result = sync(
            self.connection,
            now=self.now + timedelta(hours=1),
            runner=Gh(nodes=[{**node(11, state="CLOSED"),
                              "updatedAt": iso(self.now + timedelta(hours=1))}]),
        )
        self.assertTrue(result.ok, result.reason)
        rows = self.shadow()
        self.assertEqual(len(rows), 1)
        self.assertEqual(list(rows.values())[0]["state"], "closed")

    def test_first_seen_survives_and_last_seen_moves(self):
        sync(self.connection, now=self.now, runner=Gh(nodes=[node(11)]))
        first = list(self.shadow().values())[0]
        result = sync(
            self.connection,
            now=self.now + timedelta(hours=1),
            runner=Gh(nodes=[{**node(11, title="renamed"),
                              "updatedAt": iso(self.now + timedelta(hours=1))}]),
        )
        self.assertTrue(result.ok, result.reason)
        second = list(self.shadow().values())[0]
        self.assertEqual(first["first_seen"], second["first_seen"])
        self.assertEqual(second["title"], "renamed")

    def test_a_truncated_bucket_is_reported_rather_than_swallowed(self):
        runner = Gh(nodes=[node(11)], count=900)
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertEqual(len(result.truncated), 4)
        self.assertFalse(result.ok)
        self.assertFalse(result.watermark_moved)
        self.assertIsNone(read_watermark(self.connection))
        self.assertIn("truncated buckets", "\n".join(result.report()))


class WindowedGh:
    """A capped, paginated search with real timestamp filtering and URL identities."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv == ["gh", "auth", "status"]:
            return 0, "", ""
        query = next(value[2:] for value in argv if value.startswith("q="))
        lower, upper = map(parse_iso, query.split("updated:", 1)[1].split(".."))
        selected = [value for value in self.nodes
                    if lower <= parse_iso(value["updatedAt"]) <= upper]
        if not query.startswith("author:@me "):
            selected = []
        offset = next((int(value[6:]) for value in argv if value.startswith("after=")), 0)
        visible = selected[:1000]
        page = visible[offset:offset + 100]
        more = offset + len(page) < len(visible)
        block = {"issueCount": len(selected), "nodes": page,
                 "pageInfo": {"hasNextPage": more, "endCursor": str(offset + len(page)) if more else None}}
        return 0, json.dumps({"data": {"search": block}}), ""


class CompleteCoverage(SyncCase):
    def heartbeat(self):
        row = self.connection.execute(
            "SELECT body FROM state WHERE kind='heartbeat' AND key='tracker-sync:github' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return json.loads(row[0])

    def test_more_than_one_thousand_results_cover_each_boundary_and_page(self):
        start = self.now - timedelta(seconds=1100)
        nodes = [{**node(number), "updatedAt": iso(start + timedelta(seconds=number))}
                 for number in range(1101)]
        runner = WindowedGh(nodes)

        result = sync(self.connection, now=self.now, since=start, runner=runner)

        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.watermark_moved)
        self.assertEqual(result.truncated, [])
        self.assertEqual(set(self.shadow()), {value["url"] for value in nodes})
        self.assertEqual(result.written, 1101)
        self.assertEqual(result.requests, len(runner.calls) - 1)
        self.assertLess(result.requests, 20)
        self.assertTrue(any(value.startswith("after=") for call in runner.calls for value in call))
        for bucket in ("assigned", "mentioned", "review-requested", "author"):
            windows = sorted((entry for entry in result.coverage if entry["bucket"] == bucket),
                             key=lambda entry: entry["start"])
            cursor = start
            for entry in windows:
                self.assertTrue(entry["complete"])
                self.assertEqual(parse_iso(entry["start"]), cursor)
                cursor = parse_iso(entry["end"])
            self.assertEqual(cursor, self.now)
        # One shared endpoint is counted in both leaves but stored once.
        self.assertEqual(sum(entry["count"] for entry in result.coverage if entry["bucket"] == "author"), 1102)
        self.assertEqual(self.heartbeat()["coverage"], result.coverage)

    def test_fractional_timestamp_between_boundary_seconds_is_not_lost(self):
        start = self.now - timedelta(seconds=4)
        nodes = [{**node(number), "updatedAt": iso(start if number < 550 else self.now)}
                 for number in range(1100)]
        fractional = {**node(1100), "updatedAt": (start + timedelta(seconds=2.5)).isoformat()}
        result = sync(self.connection, now=self.now, since=start,
                      runner=WindowedGh([*nodes, fractional]))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.written, 1101)
        self.assertIn(fractional["url"], self.shadow())

    def test_a_late_page_error_preserves_observed_rows_but_holds_the_cursor(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            if argv == ["gh", "auth", "status"]:
                return 0, "", ""
            if any(value.startswith("after=") for value in argv):
                return 1, "", "fixture second-page failure"
            block = {"issueCount": 2, "nodes": [node(11)],
                     "pageInfo": {"hasNextPage": True, "endCursor": "next"}}
            return 0, json.dumps({"data": {"search": block}}), ""

        result = sync(self.connection, now=self.now, runner=runner)
        self.assertFalse(result.ok)
        self.assertEqual(result.written, 1)
        self.assertIn(node(11)["url"], self.shadow())
        self.assertIn("second-page failure", result.reason)
        self.assertIsNone(read_watermark(self.connection))

    def test_a_result_outside_the_frozen_interval_refuses_coverage(self):
        result = sync(self.connection, now=self.now, since=self.now, runner=Gh(nodes=[node(11)]))
        self.assertFalse(result.ok)
        self.assertIn("outside the requested interval", result.reason)
        self.assertIsNone(read_watermark(self.connection))

    def test_same_second_saturation_holds_cursor_and_marks_heartbeat_failed(self):
        previous = iso(self.now - timedelta(days=1))
        write_watermark(self.connection, TRACKER, previous, "earlier")
        nodes = [{**node(number), "updatedAt": iso(self.now)} for number in range(1001)]
        result = sync(self.connection, now=self.now, since=self.now, runner=WindowedGh(nodes))
        self.assertFalse(result.ok)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(result.truncated, ["author"])
        self.assertIn("within second", result.reason)
        self.assertEqual(read_watermark(self.connection), previous)
        self.assertGreater(result.written, 0)
        self.assertFalse(self.heartbeat()["ok"])
        self.assertEqual(self.heartbeat()["truncated"], ["author"])

    def test_request_limit_preserves_partial_rows_without_advancing(self):
        runner = WindowedGh([{**node(number), "updatedAt": iso(self.now)} for number in range(1001)])
        result = sync(self.connection, now=self.now, runner=runner, max_requests=4)
        self.assertFalse(result.ok)
        self.assertIn("request limit exhausted", result.reason)
        self.assertEqual(result.requests, 4)
        self.assertEqual(len(runner.calls), 5)
        self.assertGreater(result.written, 0)
        self.assertIsNone(read_watermark(self.connection))

    def test_time_limit_stops_before_another_external_request(self):
        clock = [0.0]
        calls = []

        def runner(argv):
            calls.append(argv)
            if argv == ["gh", "auth", "status"]:
                return 0, "", ""
            clock[0] = 2.0
            return Gh(nodes=[])(argv)

        with patch("sd_db.shadow_sync.time.monotonic", side_effect=lambda: clock[0]):
            result = sync(self.connection, now=self.now, runner=runner, max_seconds=1)
        self.assertFalse(result.ok)
        self.assertIn("time limit exhausted", result.reason)
        self.assertEqual(result.requests, 1)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(read_watermark(self.connection))

    def test_historical_recovery_does_not_lower_a_later_cursor(self):
        later = iso(self.now + timedelta(days=1))
        write_watermark(self.connection, TRACKER, later, "earlier")
        result = sync(self.connection, now=self.now, since=self.now - timedelta(days=2), runner=Gh())
        self.assertTrue(result.ok)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(read_watermark(self.connection), later)
        self.assertEqual(result.window_end, iso(self.now))

    def test_complete_recovery_cannot_skip_an_uncovered_prefix(self):
        previous = iso(self.now - timedelta(days=8))
        write_watermark(self.connection, TRACKER, previous, "earlier")
        result = sync(self.connection, now=self.now, since=self.now - timedelta(days=1), runner=Gh())
        self.assertTrue(result.ok)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(read_watermark(self.connection), previous)

    def test_overlapping_collector_cannot_replace_a_newer_completed_cursor(self):
        other = connect(Path(self.tmp.name) / "sd.db")
        self.addCleanup(other.close)
        newer = self.now + timedelta(hours=1)
        completed = []

        # `_sync`, under the lock `sync` takes: the cursor recheck still guards a collect the lock does not cover.
        def runner(argv):
            if not completed:
                completed.append(_sync(other, now=newer, runner=Gh()))
            return Gh()(argv)

        result = _sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(completed[0].ok)
        self.assertTrue(completed[0].watermark_moved)
        self.assertTrue(result.ok)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(read_watermark(self.connection), iso(newer))

    def test_historical_attempt_health_uses_actual_time_and_keeps_coverage_separate(self):
        recent = self.now + timedelta(days=3)
        with patch("sd_db.shadow_sync.time.time", return_value=recent.timestamp()):
            sync(self.connection, now=recent, runner=Gh())
        attempt = recent + timedelta(minutes=1)
        with patch("sd_db.shadow_sync.time.time", return_value=attempt.timestamp()):
            result = sync(self.connection, now=self.now, since=self.now - timedelta(days=1),
                          runner=Gh(fail="historical recovery failed"))
        self.assertFalse(result.ok)
        health = tracker_freshness(self.connection, now=attempt)
        self.assertEqual(health["state"], "degraded")
        self.assertEqual(parse_iso(health["last_attempt_at"]), attempt)
        self.assertEqual(health["last_success_at"], iso(recent))
        self.assertIn("historical recovery failed", health["reason"])
        self.assertEqual(self.heartbeat()["window_end"], iso(self.now))
        attempt += timedelta(minutes=1)
        with patch("sd_db.shadow_sync.time.time", return_value=attempt.timestamp()):
            result = sync(self.connection, now=self.now, since=self.now - timedelta(days=1), runner=Gh())
        self.assertTrue(result.ok)
        self.assertFalse(result.watermark_moved)
        health = tracker_freshness(self.connection, now=attempt)
        self.assertEqual(health["state"], "fresh")
        self.assertEqual(parse_iso(health["last_attempt_at"]), attempt)
        self.assertEqual(health["last_success_at"], iso(recent))
        self.assertEqual(self.heartbeat()["window_end"], iso(self.now))

    def test_changed_parent_count_during_splitting_refuses_coverage(self):
        start = self.now - timedelta(seconds=2)
        base = WindowedGh([{**node(number), "updatedAt": iso(start if number < 550 else self.now)}
                           for number in range(1100)])

        def runner(argv):
            code, out, error = base(argv)
            if argv == ["gh", "auth", "status"]:
                return code, out, error
            query = next(value[2:] for value in argv if value.startswith("q="))
            if f"updated:{iso(start)}..{iso(self.now)}" in query:
                payload = json.loads(out)
                payload["data"]["search"]["issueCount"] += 1
                out = json.dumps(payload)
            return code, out, error

        result = sync(self.connection, now=self.now, since=start, runner=runner)
        self.assertFalse(result.ok)
        self.assertIn("count changed while splitting", result.reason)
        self.assertIn("author", result.truncated)
        self.assertIsNone(read_watermark(self.connection))

    def test_timestamp_offset_overflow_is_invalid(self):
        for value in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"):
            with self.subTest(value=value):
                self.assertIsNone(parse_iso(value))
                result = sync(self.connection, now=self.now,
                              runner=Gh(nodes=[{**node(1), "updatedAt": value}]))
                self.assertFalse(result.ok)
                self.assertIsNone(read_watermark(self.connection))

    def test_invalid_bounds_and_limits_refuse_before_any_call_or_write(self):
        before = self.connection.total_changes
        for options in ({"since": self.now + timedelta(seconds=1)}, {"since": datetime(2026, 1, 1)},
                        {"max_requests": 0}, {"max_requests": True}, {"max_requests": 1.5},
                        {"max_seconds": 0}, {"max_seconds": float("nan")}, {"max_seconds": float("inf")}):
            with self.subTest(options=options):
                runner = Gh()
                with self.assertRaises(ValueError):
                    sync(self.connection, now=self.now, runner=runner, **options)
                self.assertEqual(runner.calls, [])
                self.assertEqual(self.connection.total_changes, before)


class TheLock(SyncCase):
    """One collect at a time per database, whichever caller starts first (sd:2207 review)."""

    def other(self):
        other = connect(Path(self.tmp.name) / "sd.db")
        self.addCleanup(other.close)
        return other

    def test_a_sync_while_another_caller_holds_the_lock_is_refused_before_it_collects(self):
        gh = Gh()
        with sync_lock(self.other()):
            with self.assertRaisesRegex(SyncBusy, "already running"):
                sync(self.connection, now=self.now, runner=gh)
        self.assertEqual(gh.calls, [])
        self.assertIsNone(read_watermark(self.connection))

    def test_a_second_caller_during_a_sync_is_refused_and_the_first_completes(self):
        other, refused = self.other(), []

        def runner(argv):
            if not refused:
                with self.assertRaises(SyncBusy) as busy:
                    sync(other, now=self.now + timedelta(hours=1), runner=Gh())
                refused.append(str(busy.exception))
            return Gh()(argv)

        result = sync(self.connection, now=self.now, runner=runner)
        self.assertIn("already running", refused[0])
        self.assertTrue(result.ok)
        self.assertEqual(read_watermark(self.connection), iso(self.now))

    def test_the_lock_is_free_again_after_a_sync_ends_well_or_badly(self):
        self.assertFalse(sync(self.connection, now=self.now, runner=Gh(fail="boom")).ok)
        self.assertTrue(sync(self.other(), now=self.now, runner=Gh()).ok)
        with sync_lock(self.connection):
            pass

    def test_a_satellite_refuses_before_it_locks_or_collects(self):
        from sd_db.remote import HubOnly

        gh = Gh()
        with patch("sd_db.database.served_by", return_value="hub.example.test:8770"):
            with self.assertRaises(HubOnly):
                sync(self.connection, now=self.now, runner=gh)
        self.assertEqual(gh.calls, [])
        self.assertFalse((Path(self.tmp.name) / "operation-locks").exists())

    def test_an_in_memory_database_takes_no_lock(self):
        import sqlite3

        memory = sqlite3.connect(":memory:")
        self.addCleanup(memory.close)
        with patch("sd_db.runner_journal.lock") as lock, sync_lock(memory):
            pass
        lock.assert_not_called()


class TheTrackerDispatch(SyncCase):
    """`tracker` chooses the collector, and each tracker keeps its own cursor.

    The Jira transport is the seam `shadow_jira` already takes, so nothing
    here opens a socket; the environment is replaced whole, so the operator's
    own `JIRA_*` values never reach a test.
    """

    def environment(self, *, without=()):
        values = {**os.environ, **JIRA_ENV}
        for name in ("JIRA_JQL", *without):
            values.pop(name, None)
        return patch.dict(os.environ, values, clear=True)

    def jira_heartbeats(self):
        return [json.loads(row["body"]) for row in self.connection.execute(
            "SELECT body FROM state WHERE kind = 'heartbeat' AND key = 'tracker-sync:jira' ORDER BY id"
        )]

    def watermark_keys(self):
        return [row["key"] for row in self.connection.execute(
            "SELECT key FROM state WHERE kind = 'watermark' ORDER BY id"
        )]

    def test_each_tracker_moves_only_its_own_cursor(self):
        """Seeded, not empty: on an empty database a cross-tracker overwrite
        would leave the other cursor non-`None` and pass."""
        seeded = "2026-09-01T00:00:00Z"
        write_watermark(self.connection, "github", seeded, "earlier")
        transport = jira_transport([{"issues": [jira_issue("ABC-1")], "isLast": True}])
        with self.environment():
            result = sync(self.connection, tracker="jira", now=self.now, runner=transport)
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.configured)
        self.assertTrue(result.watermark_moved)
        self.assertEqual(result.written, 1)
        self.assertEqual({row["tracker"] for row in self.shadow().values()}, {"jira"})
        self.assertEqual(self.watermark_keys(), ["github", "jira"])
        self.assertEqual(read_watermark(self.connection, "jira"), iso(self.now))
        self.assertEqual(read_watermark(self.connection, "github"), seeded)
        self.assertTrue(self.jira_heartbeats()[-1]["ok"])

        later = self.now + timedelta(hours=1)
        result = sync(self.connection, now=later, runner=Gh(nodes=[node(11)]))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(read_watermark(self.connection, "github"), iso(later))
        self.assertEqual(read_watermark(self.connection, "jira"), iso(self.now))

    def test_an_unconfigured_jira_is_not_collected_and_writes_only_its_heartbeat(self):
        transport = jira_transport([{"issues": [jira_issue("ABC-1")], "isLast": True}])
        with self.environment(without=("JIRA_BASE_URL",)):
            result = sync(self.connection, tracker="jira", now=self.now, runner=transport)
        self.assertFalse(result.ok)
        self.assertFalse(result.configured)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(result.reason, "JIRA_BASE_URL not set")
        self.assertEqual(transport.seen["calls"], 0, "a request was made with no host configured")
        self.assertEqual(self.shadow(), {})
        self.assertEqual(self.watermark_keys(), [])
        heartbeats = self.jira_heartbeats()
        self.assertEqual(len(heartbeats), 1)
        self.assertFalse(heartbeats[0]["ok"])
        self.assertEqual(heartbeats[0]["reason"], "JIRA_BASE_URL not set")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM state").fetchone()[0], 1)

    def test_a_secret_in_the_jira_base_url_reaches_no_heartbeat(self):
        """Before sd:771 `http.client.InvalidURL` and `urllib.parse`'s
        `ValueError` left `sync` quoting it, with no heartbeat written."""
        cases = {
            f"https://user:{SECRET}@jira.example.invalid": "JIRA_BASE_URL must not carry a user name or password",
            **{base: "JIRA_BASE_URL is not a URL urllib can split" for base in UNSPLITTABLE},
            **{base: NOT_A_HOST_REASON for base in NOT_A_HOST},
            **{base: NON_ASCII_HOST_REASON for base in NON_ASCII_HOST},
        }
        for count, (base, reason) in enumerate(cases.items(), start=1):
            with self.subTest(base=base), self.environment(), no_socket() as connect, patch.dict(
                    os.environ, {"JIRA_BASE_URL": base}):
                result = sync(self.connection, tracker="jira", now=self.now, runner=None)
                self.assertFalse(result.ok)
                self.assertFalse(result.configured)
                self.assertEqual(result.reason, reason)
                self.assertEqual((result.requests, connect.call_count), (0, 0))
                heartbeats = self.jira_heartbeats()
                self.assertEqual(len(heartbeats), count)
                self.assertEqual((heartbeats[-1]["ok"], heartbeats[-1]["requests"]), (False, 0))
                self.assertNotIn(SECRET, json.dumps(heartbeats))

    def test_an_answering_resolver_or_an_echoing_proxy_gets_no_secret_from_the_host(self):
        """Before sd:784 `[v1.user:password]` and its kin reached the resolver.

        Here the resolver answers every name with a loopback server that hangs
        up, and then the only proxy is a loopback one that echoes its CONNECT
        target in its reason phrase, which the heartbeat kept. Nothing leaves
        127.0.0.1 in either mode.
        """
        asked, tunnels = [], []
        real = socket.getaddrinfo

        class Loopback(socketserver.TCPServer):
            allow_reuse_address = True

            def __init__(self, handle):
                super().__init__(("127.0.0.1", 0), type("Handler", (socketserver.StreamRequestHandler,),
                                                        {"handle": handle}))
                threading.Thread(target=self.serve_forever, daemon=True).start()

        def hang_up(inner):
            pass

        def echo_target(inner):
            line = inner.rfile.readline().decode("latin-1").strip()
            tunnels.append(line)
            while inner.rfile.readline() not in (b"\r\n", b"\n", b""):
                pass
            target = line.split(" ")[1] if " " in line else line
            inner.wfile.write(f"HTTP/1.1 502 cannot reach {target}\r\n\r\n".encode("latin-1"))

        closer, proxy = Loopback(hang_up), Loopback(echo_target)
        for server in (closer, proxy):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

        def resolver(host, port, *rest, **named):
            name = host.decode() if isinstance(host, bytes) else host
            if name in ("127.0.0.1", "::1", "localhost"):
                return real(host, port, *rest, **named)
            asked.append(name)
            return real("127.0.0.1", closer.server_address[1], *rest, **named)

        proxied = {"https_proxy": f"http://127.0.0.1:{proxy.server_address[1]}", "no_proxy": "", "NO_PROXY": ""}
        for mode, extra in (("resolver", {}), ("proxy", proxied)):
            for base in NOT_A_HOST:
                with self.subTest(mode=mode, base=base), without_jira_environment(), patch.dict(
                        os.environ, {**JIRA_ENV, "JIRA_BASE_URL": base, **extra}), patch(
                        "socket.getaddrinfo", side_effect=resolver):
                    del asked[:], tunnels[:]
                    result = sync(self.connection, tracker="jira", now=self.now, runner=None)
                    self.assertFalse(result.ok)
                    self.assertNotIn(SECRET, json.dumps(self.jira_heartbeats()[-1]))
                    self.assertNotIn(SECRET, " ".join(asked + tunnels))

    def test_a_broken_jira_answer_writes_the_heartbeat_and_its_count(self):
        """The real transport against loopback servers; proxies are removed."""
        from sd_db import shadow_jira

        for name, reply in BROKEN_ANSWERS.items():
            with self.subTest(answer=name):
                server = RawServer(reply)
                self.addCleanup(server.close)
                config = {**shadow_jira.settings(JIRA_ENV), "base": server.url}
                with without_jira_environment(), patch("sd_db.shadow_jira.settings", return_value=config):
                    result = sync(self.connection, tracker="jira", now=self.now, runner=None)
                self.assertFalse(result.ok)
                self.assertTrue(result.configured)
                self.assertFalse(result.watermark_moved)
                self.assertEqual((result.requests, server.connections), (1, 1))
                heartbeat = self.jira_heartbeats()[-1]
                self.assertEqual(heartbeat["reason"], f"the request to Jira failed: {name}")
                self.assertEqual((heartbeat["ok"], heartbeat["requests"]), (False, 1))
                self.assertNotIn(SECRET, json.dumps(heartbeat))

    def test_a_truncated_jira_walk_stores_its_rows_and_holds_the_cursor(self):
        transport = jira_transport([{"issues": [jira_issue("ABC-1")], "isLast": False}])
        with self.environment():
            result = sync(self.connection, tracker="jira", now=self.now, runner=transport)
        self.assertFalse(result.ok)
        self.assertTrue(result.configured)
        self.assertEqual(result.truncated, ["jql"])
        self.assertIn("https://example.invalid/browse/ABC-1", self.shadow())
        self.assertFalse(result.watermark_moved)
        self.assertIsNone(read_watermark(self.connection, "jira"))
        self.assertEqual(self.jira_heartbeats()[-1]["truncated"], ["jql"])

    def test_a_tracker_it_does_not_know_is_refused_before_any_call_or_write(self):
        """Before this, an unknown name ran GitHub's buckets under that key."""
        runner = Gh()
        before = self.connection.total_changes
        with self.assertRaises(ValueError) as raised:
            sync(self.connection, tracker="nope", now=self.now, runner=runner)
        self.assertIn("jira", str(raised.exception))
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.connection.total_changes, before)

    def test_a_recovery_window_is_refused_for_jira(self):
        """`since` is GitHub's; accepting it silently would promise a re-walk."""
        transport = jira_transport([])
        before = self.connection.total_changes
        with self.environment(), self.assertRaises(ValueError):
            sync(self.connection, tracker="jira", now=self.now, runner=transport,
                 since=self.now - timedelta(days=1))
        self.assertEqual(transport.seen["calls"], 0)
        self.assertEqual(self.connection.total_changes, before)


class PaginationIntegrity(unittest.TestCase):
    def pages(self, blocks):
        calls = []

        def runner(argv):
            calls.append(argv)
            block = blocks[min(len(calls) - 1, len(blocks) - 1)]
            return 0, json.dumps({"data": {"search": block}}), ""

        return runner, calls

    def block(self, numbers, *, count=2, more=False, cursor=None):
        return {"issueCount": count, "nodes": [node(number) for number in numbers],
                "pageInfo": {"hasNextPage": more, "endCursor": cursor}}

    def test_duplicate_pages_cannot_satisfy_the_advertised_identity_count(self):
        runner, calls = self.pages([self.block([1], more=True, cursor="next"), self.block([1])])
        nodes, cut, error = search("author:@me", runner)
        self.assertEqual(len(nodes), 1)
        self.assertTrue(cut)
        self.assertFalse(error)
        self.assertEqual(len(calls), 2)

    def test_missing_or_repeated_cursors_refuse_without_looping(self):
        for cursor in (None, "same"):
            with self.subTest(cursor=cursor):
                runner, calls = self.pages([self.block([1], more=True, cursor=cursor)])
                _, _, error = search("author:@me", runner)
                self.assertIn("pagination cursor", error)
                self.assertLessEqual(len(calls), 2)

    def test_changed_counts_and_malformed_pages_cannot_claim_complete(self):
        for blocks in ([self.block([1], more=True, cursor="next"), self.block([2], count=3)],
                       [{"nodes": [], "pageInfo": {"hasNextPage": False}}],
                       [self.block([1], count=True)],
                       [{"issueCount": 1, "nodes": [None], "pageInfo": {"hasNextPage": False}}]):
            with self.subTest(blocks=blocks):
                runner, _ = self.pages(blocks)
                _, _, error = search("author:@me", runner)
                self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
