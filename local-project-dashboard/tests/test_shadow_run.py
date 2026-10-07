"""`sd shadow sync` from the dashboard (sd:2207, ruling #8765).

What this promises: POST /api/shadow/sync starts one run in a server thread and answers 202 at once; the run calls
`sd_db.sync_shadow` once per tracker in `sd_db.TRACKERS` inside one 120-second deadline; a second start while one is live is
refused with 409; GET /api/shadow/state says how the run went. `sync_shadow` is stubbed throughout: nothing reaches gh or
the network. Whether gh is authenticated under the LaunchAgent is a live check these tests do not make.
"""

from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import sd_db
from sd_db.shadow_sync import Synced

from sd_dashboard import shadow_run

from support import ScreenCase
from test_workflow_actions import BrowserSession


def synced(ok=True, reason="", configured=True):
    return Synced(ok=ok, reason=reason, written=2, truncated=[], window_start="2026-10-05T00:00:00Z",
                  watermark_moved=ok, configured=configured)


class Stub:
    """`sync_shadow` stand-in: records each call, and waits on `gate` when one is set."""

    def __init__(self, answers=None, gate=None, raises=None, spend=None):
        self.calls, self.answers, self.gate, self.raises, self.spend = [], answers or {}, gate, raises, spend

    def __call__(self, connection, *, tracker, max_seconds, runner=None):
        self.calls.append((threading.current_thread().name, tracker, max_seconds,
                           connection.execute("PRAGMA database_list").fetchone()[2], runner))
        if self.spend:
            self.spend(tracker)
        if self.gate is not None:
            self.gate.wait(5)
        if self.raises:
            raise self.raises
        return self.answers.get(tracker, synced())


class TheRun(ScreenCase):
    def start(self, run, stub):
        with patch.object(sd_db, "sync_shadow", stub):
            status, answer = run.start(self.connection)
            if status == 202:
                run.thread.join(5)
        return status, answer

    def test_a_run_syncs_every_tracker_in_its_own_thread_with_the_interactive_budget(self):
        run, stub = shadow_run.Run(), Stub(answers={"jira": synced(ok=False, reason="JIRA_URL is not set", configured=False)})
        status, answer = self.start(run, stub)
        self.assertEqual((status, answer["running"], answer["max_seconds"]), (202, True, 120))
        self.assertEqual([call[:2] for call in stub.calls], [("sd-shadow-sync", tracker) for tracker in sd_db.TRACKERS])
        self.assertTrue(all(100 < call[2] <= 120 for call in stub.calls), [call[2] for call in stub.calls])
        self.assertEqual([callable(call[4]) for call in stub.calls], [tracker == "jira" for tracker in sd_db.TRACKERS])
        self.assertTrue(all(Path(call[3]).samefile(self.path) for call in stub.calls), "the run opened another database")
        state = run.status()
        self.assertFalse(state["running"])
        self.assertIsNone(state["error"])
        self.assertIsNotNone(state["finished"])
        self.assertEqual([(t["tracker"], t["ok"], t["configured"]) for t in state["trackers"]],
                         [("github", True, True), ("jira", False, False)])
        self.assertEqual(state["trackers"][1]["reason"], "JIRA_URL is not set")

    def test_the_run_has_one_deadline_and_jira_gets_what_github_left(self):
        # Review round 1: 120 s per tracker let a run hold the slot for minutes. GitHub here spends 100 of the 120 s.
        clock = [1000.0]

        def spend(tracker):
            if tracker == "github":
                clock[0] += 100

        stub = Stub(spend=spend)
        with patch.object(shadow_run.time, "monotonic", lambda: clock[0]):
            self.start(shadow_run.Run(), stub)
        self.assertEqual([call[2] for call in stub.calls], [120, 20])
        transport = stub.calls[1][4]
        with patch("sd_db.shadow_jira._http", lambda *a: ({"ok": 1}, "")) as _:
            with patch.object(shadow_run.time, "monotonic", lambda: 1119.0):
                self.assertEqual(transport("https://jira.example.test/x", {}, None), ({"ok": 1}, ""))
            with patch.object(shadow_run.time, "monotonic", lambda: 1120.0):
                self.assertEqual(transport("https://jira.example.test/x", {}, None),
                                 (None, "the dashboard's 120-second sync budget ran out"))

    def test_a_start_answers_running_even_when_the_run_ends_at_once(self):
        # Review round 1: the answer was read after the thread started, so a fast run answered its start "finished".
        run = shadow_run.Run()
        with patch.object(sd_db, "sync_shadow", Stub()), patch.object(shadow_run.threading.Thread, "start", lambda t: t.run()):
            status, answer = run.start(self.connection)
        self.assertEqual((status, answer["running"], answer["finished"]), (202, True, None))
        self.assertFalse(run.status()["running"])

    def test_a_start_while_a_run_is_live_is_refused_and_the_next_one_after_it_runs(self):
        run, gate = shadow_run.Run(), threading.Event()
        stub = Stub(gate=gate)
        with patch.object(sd_db, "sync_shadow", stub):
            self.assertEqual(run.start(self.connection)[0], 202)
            status, answer = run.start(self.connection)
            self.assertEqual(status, 409)
            self.assertIn("is still running", answer["error"])
            self.assertTrue(run.status()["running"])
            gate.set()
            run.thread.join(5)
        self.assertEqual(len(stub.calls), len(sd_db.TRACKERS), "the refused start ran a sync")
        self.assertEqual(self.start(run, Stub())[0], 202)

    def test_a_run_that_breaks_names_the_error_and_frees_the_slot(self):
        run = shadow_run.Run()
        self.start(run, Stub(raises=RuntimeError("gh: not logged in")))
        state = run.status()
        self.assertFalse(state["running"])
        self.assertEqual(state["error"], "RuntimeError: gh: not logged in")
        self.assertEqual(self.start(run, Stub())[0], 202)


class TheRoute(BrowserSession):
    def setUp(self):
        super().setUp()
        self.run = shadow_run.Run()
        patcher = patch.object(shadow_run, "RUN", self.run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_post_starts_a_run_get_reads_it_and_a_second_post_is_refused_while_it_is_live(self):
        gate = threading.Event()
        stub = Stub(gate=gate)
        with patch.object(sd_db, "sync_shadow", stub):
            status, _, answer = self.post("/api/shadow/sync", {})
            self.assertEqual((status, answer["running"]), (202, True))
            status, _, again = self.post("/api/shadow/sync", {})
            self.assertEqual(status, 409)
            self.assertIn("is still running", again["error"])
            status, _, body = self.request("/api/shadow/state", headers={"Cookie": self.cookie})
            self.assertEqual((status, json.loads(body)["running"]), (200, True))
            gate.set()
            self.run.thread.join(5)
        status, _, body = self.request("/api/shadow/state", headers={"Cookie": self.cookie})
        state = json.loads(body)
        self.assertEqual((status, state["running"], [t["tracker"] for t in state["trackers"]]), (200, False, list(sd_db.TRACKERS)))
        self.assertTrue(all(100 < call[2] <= 120 for call in stub.calls), [call[2] for call in stub.calls])

    def test_a_start_with_arguments_is_refused_before_anything_runs(self):
        stub = Stub()
        with patch.object(sd_db, "sync_shadow", stub):
            status, _, answer = self.post("/api/shadow/sync", {"max_seconds": 600})
        self.assertEqual(status, 400)
        self.assertEqual(answer["error"], "Start the sync without arguments.")
        self.assertEqual((stub.calls, self.run.thread), ([], None))


if __name__ == "__main__":
    unittest.main()
