"""The fleet snapshot (sd:TBD): a slow read the server keeps and refreshes in the background.

What this slice promises: a request gets the last fleet document at once, with the UTC time its read began
(`observed`); it waits only when there is none, when it is older than `KEPT_SECONDS`, or after a write. A read past
`FRESH_SECONDS` starts one refresh in a thread and does not wait on it. Two refreshes of one read never overlap. A
refresh that fails keeps the last document, marked `stale` with the reason; with no document there is the failure.
A write the server answers invalidates the snapshot, so the next read waits for a refresh begun after it.
"""

from __future__ import annotations

import json
import threading
import unittest

from sd_dashboard import fleet, metrics_screen, now_screen, server, snapshot

from support import NOW, ScreenCase
from test_workflow_actions import BrowserSession


class Clock:
    """A monotonic clock and a wall clock the test moves."""

    def __init__(self):
        self.now, self.wall = 1000.0, 1_788_000_000.0

    def move(self, seconds):
        self.now += seconds
        self.wall += seconds


class Source:
    """A compute that answers `value`, raises `failure`, or blocks until `release` is set, and counts its calls."""

    def __init__(self):
        self.calls, self.value, self.failure = 0, {"n": 0}, None
        self.release, self.entered = threading.Event(), threading.Event()
        self.release.set()
        self.running = 0
        self.most = 0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.calls += 1
            self.running += 1
            self.most = max(self.most, self.running)
        self.entered.set()
        try:
            self.release.wait(10)
            if self.failure:
                raise self.failure
            return dict(self.value, n=self.calls)
        finally:
            with self.lock:
                self.running -= 1


class TheSnapshot(unittest.TestCase):
    def setUp(self):
        self.clock, self.source = Clock(), Source()
        self.kept = snapshot.Snapshot(self.source, clock=lambda: self.clock.now, wall=lambda: self.clock.wall)

    def wait_idle(self):
        self.assertTrue(self.kept.idle(10), "the refresh never finished")

    def test_the_first_read_waits_for_its_document_and_says_when_it_was_observed(self):
        value, observed, stale = self.kept.read()
        self.assertEqual((value, stale, self.source.calls), ({"n": 1}, None, 1))
        self.assertEqual(observed, "2026-08-29T10:40:00Z")

    def test_a_fresh_document_is_answered_without_a_refresh(self):
        self.kept.read()
        self.clock.move(snapshot.FRESH_SECONDS - 1)
        self.assertEqual(self.kept.read()[0], {"n": 1})
        self.wait_idle()
        self.assertEqual(self.source.calls, 1)

    def test_a_request_does_not_wait_on_a_running_refresh(self):
        self.kept.read()
        self.clock.move(snapshot.FRESH_SECONDS + 1)
        self.source.release.clear()
        self.source.entered.clear()
        value, observed, _ = self.kept.read()  # starts the refresh, which blocks
        self.assertTrue(self.source.entered.wait(10))
        self.assertEqual((value, observed), ({"n": 1}, "2026-08-29T10:40:00Z"))
        self.assertEqual(self.kept.read()[0], {"n": 1})  # the refresh still runs; this read did not wait on it
        self.source.release.set()
        self.wait_idle()
        self.assertEqual((self.kept.read()[0], self.source.calls), ({"n": 2}, 2))

    def test_a_failed_refresh_keeps_the_last_document_marked_stale_with_the_reason(self):
        self.kept.read()
        self.clock.move(snapshot.FRESH_SECONDS + 1)
        self.source.failure = ValueError("fleet collection was stopped at its budget")
        self.kept.read()
        self.wait_idle()
        value, observed, stale = self.kept.read()
        self.assertEqual((value, observed), ({"n": 1}, "2026-08-29T10:40:00Z"))
        self.assertEqual(stale, "fleet collection was stopped at its budget")
        self.source.failure = None
        self.clock.move(snapshot.FRESH_SECONDS + 1)
        self.kept.read()
        self.wait_idle()
        self.assertEqual(self.kept.read()[2], None)

    def test_with_no_document_a_failed_read_is_the_failure(self):
        self.source.failure = ValueError("fleet collector exited 1 without a reason")
        with self.assertRaisesRegex(ValueError, "exited 1"):
            self.kept.read()

    def test_a_document_older_than_kept_waits_for_a_new_read(self):
        self.kept.read()
        self.clock.move(snapshot.KEPT_SECONDS + 1)
        value, observed, _ = self.kept.read()
        self.assertEqual((value["n"], observed), (2, "2026-08-29T10:45:01Z"))

    def test_an_old_document_whose_refresh_fails_is_answered_stale_not_dropped(self):
        self.kept.read()
        self.clock.move(snapshot.KEPT_SECONDS + 1)
        self.source.failure = ValueError("git did not answer")
        value, _, stale = self.kept.read()
        self.assertEqual((value, stale), ({"n": 1}, "git did not answer"))
        # The failure is recent, so the next read does not wait on another try.
        self.assertEqual(self.kept.read()[2], "git did not answer")
        self.assertEqual(self.source.calls, 2)

    def test_two_reads_never_run_two_refreshes_at_once(self):
        self.source.release.clear()
        answers = []
        readers = [threading.Thread(target=lambda: answers.append(self.kept.read())) for _ in range(4)]
        for reader in readers:
            reader.start()
        self.assertTrue(self.source.entered.wait(10))
        self.source.release.set()
        for reader in readers:
            reader.join(10)
        self.assertEqual((self.source.calls, self.source.most, len(answers)), (1, 1, 4))

    def test_after_a_write_the_next_read_waits_for_a_refresh_begun_after_it(self):
        self.kept.read()
        self.clock.move(snapshot.FRESH_SECONDS + 1)
        self.source.release.clear()
        self.source.entered.clear()
        self.kept.read()  # a background refresh that began before the write
        self.assertTrue(self.source.entered.wait(10))
        self.kept.invalidate()
        threading.Timer(0.2, self.source.release.set).start()
        value, _, _ = self.kept.read()
        self.assertEqual((value["n"], self.source.calls, self.source.most), (3, 3, 1))


    def test_a_refresh_thread_that_cannot_start_is_a_failed_read_not_a_wait_forever(self):
        from unittest import mock

        with mock.patch.object(snapshot.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")):
            with self.assertRaisesRegex(RuntimeError, "can't start new thread"):
                self.kept.read()
        self.clock.move(snapshot.FRESH_SECONDS + 1)
        self.assertEqual((self.kept.read()[0], self.source.calls), ({"n": 1}, 1))

    def test_a_compute_that_exits_its_thread_still_ends_the_read(self):
        self.source.failure = SystemExit(3)
        reader = threading.Thread(target=lambda: self.assertRaises(SystemExit, self.kept.read), daemon=True)
        reader.start()
        reader.join(10)
        self.assertFalse(reader.is_alive(), "the read waited on a refresh whose thread exited")


class TheFleet(unittest.TestCase):
    def test_each_area_answers_its_document_with_when_it_was_observed_and_why_it_is_stale(self):
        clock = Clock()
        calls = []

        def collect(area):
            calls.append(area)
            if len(calls) > 2:
                raise ValueError("fleet collection was stopped at its budget")
            return {"area": area}

        kept = snapshot.Fleet(collect, clock=lambda: clock.now, wall=lambda: clock.wall)
        self.assertEqual(kept("repos"), {"area": "repos", "observed": "2026-08-29T10:40:00Z"})
        self.assertEqual(kept("sessions")["area"], "sessions")
        clock.move(snapshot.FRESH_SECONDS + 1)
        kept("repos")
        self.assertTrue(kept.idle(10))
        self.assertEqual(kept("repos"), {"area": "repos", "observed": "2026-08-29T10:40:00Z",
                                         "stale": "fleet collection was stopped at its budget"})
        with self.assertRaisesRegex(ValueError, "no kept read is named 'health'"):
            kept("health")

    def test_the_areas_are_the_fleets(self):
        self.assertEqual(tuple(snapshot.Fleet(lambda area: {})._kept), fleet.AREAS)


class TheToday(ScreenCase):
    def test_today_says_when_each_fleet_area_was_observed_and_why_it_is_stale(self):
        documents = {"repos": {"repos": [], "observed": "2026-09-06T11:59:00Z", "stale": "git did not answer"},
                     "sessions": {"worktrees": [], "processes": [], "observed": "2026-09-06T11:58:30Z"}}

        class NoJobs:
            def names(self):
                return []

        doc = now_screen.document(self.connection, now=NOW, fleet=documents.__getitem__, jobs=NoJobs())
        self.assertEqual(doc["observed"], {"repos": {"at": "2026-09-06T11:59:00Z", "stale": "git did not answer"},
                                           "sessions": {"at": "2026-09-06T11:58:30Z", "stale": None}})


class Spy:
    """A kept-read seam that answers `answer` for every name and counts invalidations."""

    def __init__(self, answer):
        self.invalidated, self.answer = 0, answer

    def __call__(self, name):
        return dict(self.answer)

    def invalidate(self):
        self.invalidated += 1


class TheServer(BrowserSession):
    fleet_backend = Spy({"repos": [], "worktrees": [], "processes": []})
    metrics_backend = Spy({"error": "the kept part", "observed": "2026-09-06T11:59:00Z"})

    def test_a_write_invalidates_the_fleet_and_metrics_snapshots(self):
        before = (self.fleet_backend.invalidated, self.metrics_backend.invalidated)
        status, _, answer = self.post("/api/items", {"title": "a new item"})
        self.assertEqual(status, 201, answer)
        self.assertEqual((self.fleet_backend.invalidated, self.metrics_backend.invalidated), (before[0] + 1, before[1] + 1))

    def test_metrics_reads_its_slow_parts_from_the_kept_reads(self):
        status, _, body = self.request("/api/metrics", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        doc = json.loads(body)
        self.assertEqual((doc["usage"], doc["numbers"]), ({"error": "the kept part", "observed": "2026-09-06T11:59:00Z"},) * 2)

    def test_the_kept_metrics_read_opens_its_own_connection_and_closes_it(self):
        kept = server._metrics_kept(self.path)
        usage = kept("usage")
        self.assertEqual((usage["error"], usage["month"]), ("", server._now()[:7]))
        self.assertIn("observed", kept("numbers"))

    def test_main_serves_the_fleet_from_the_snapshot(self):
        built = {}

        def build(*args, **options):
            built.update(options)
            raise SystemExit(0)

        from unittest import mock

        with mock.patch.object(server, "build", build), mock.patch.object(server.runtime, "installed_library"):
            with self.assertRaises(SystemExit):
                server.main(["--port", "0"])
        self.assertIsInstance(built["fleet_backend"], snapshot.Fleet)
        self.assertIsInstance(built["metrics_backend"], snapshot.Kept)
        self.assertEqual(tuple(built["metrics_backend"]._kept), metrics_screen.SLOW)


if __name__ == "__main__":
    unittest.main()
