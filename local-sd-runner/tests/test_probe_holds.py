"""Bounded probe failures use temporary state and never inspect host processes."""

import io
import json
import sqlite3
import subprocess
import unittest
from unittest.mock import Mock, patch

from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_db.operations import control_gate
from sd_db.workflow import WorkflowError
from sd_runner import runtime

from tests import test_runtime


class LoopFinished(Exception):
    """Stop a daemon fixture after exactly two completed iterations."""


class LiveChild:
    pid = 424242

    def __init__(self):
        self.stdout = io.StringIO('{"ok": true}\n')
        self.waits = []

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.waits.append(timeout)
        raise AssertionError("a live owned child must not be joined during a probe hold")


class ProbeHolds(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.db = self.fixture.db
        self.request = self.fixture.claim()
        self.run = self.runner.persist(self.db, self.request["run"]["id"])
        self.journal_path = journal.directory(self.fixture.database) / f"{self.run['id']}.json"
        self.report = {"ok": True, "problems": [], "database_below_floor": False,
                       "dispatch_allowed": False}
        self.real_preflight = runtime.storage.preflight
        self.preflight = self.enterContext(patch.object(runtime.storage, "preflight", return_value=self.report))
        self.capacity = self.enterContext(patch.object(runtime.storage, "capacity", side_effect=OSError("fixture capacity unavailable")))
        self.terminate = self.enterContext(patch.object(runtime.processes, "terminate_owned", return_value=[]))
        self.enterContext(patch.object(runtime.processes, "table", side_effect=AssertionError("no host process inventory")))
        self.head = self.enterContext(patch.object(runtime.gitops, "head", return_value="fixture-head"))
        self.enterContext(patch.object(self.runner, "refresh_archives"))
        self.enterContext(patch.object(self.runner, "watch_deliveries"))
        self.claim = self.enterContext(patch.object(store, "claim", side_effect=AssertionError("probe fixtures must not dispatch")))
        self.queue_merges = self.enterContext(patch.object(store, "queue_automatic_merges"))

    def assert_owned_unchanged(self, before, journal_bytes, child):
        self.assertEqual(store.run_state(self.db, self.run["id"]), before)
        self.assertEqual(self.journal_path.read_bytes(), journal_bytes)
        self.assertIs(self.runner.supervisors[self.run["id"]], child)
        self.assertEqual(child.waits, [])
        self.terminate.assert_not_called()
        self.claim.assert_not_called()
        self.queue_merges.assert_not_called()

    def assert_transient_probe_hold(self, error, *, pack_probe=False):
        child = LiveChild()
        self.runner.supervisors[self.run["id"]] = child
        before = store.run_state(self.db, self.run["id"])
        journal_bytes = self.journal_path.read_bytes()
        if pack_probe:
            self.head.side_effect = [error, "fixture-head"]
        else:
            self.preflight.side_effect = [self.report, error, self.report]
        iterations = []

        def pause(seconds):
            iterations.append(seconds)
            heartbeat = store.heartbeat_state(self.db)
            self.assert_owned_unchanged(before, journal_bytes, child)
            self.assertEqual(heartbeat.get("owner"), self.runner.owner)
            # A held tick must retain the daemon lock, not leave restart recovery
            # to reinterpret live workers as abandoned attempts.
            with self.assertRaisesRegex(store.RunnerRefused, "ownership lock held"):
                with journal.lock(self.fixture.database.parent / "runner.lock", blocking=False):
                    self.fail("runner ownership was dropped")
            if len(iterations) == 1:
                self.assertIs(heartbeat.get("healthy"), False)
                self.assertIn(str(error), json.dumps(heartbeat))
                self.assertTrue(heartbeat.get("probe_holds"))
                self.assertIs(heartbeat["storage"]["dispatch_allowed"], False)
                if pack_probe:
                    self.assertIsNone(heartbeat["pack_commit"])
                else:
                    self.assertEqual(heartbeat["storage"]["observation"], "unavailable")
                    self.assertIsNone(heartbeat["storage"]["database"])
                    self.assertIsNone(heartbeat["storage"]["database_below_floor"])
            else:
                self.assertIs(heartbeat.get("healthy"), True)
                self.assertFalse(heartbeat.get("probe_holds"))
                raise LoopFinished

        with patch.object(self.runner, "recover", return_value=[]) as recover, \
             patch.object(runtime.time, "sleep", side_effect=pause):
            try:
                with self.assertRaises(LoopFinished):
                    self.runner.serve()
            except (OSError, subprocess.TimeoutExpired) as escaped:
                self.fail(f"bounded probe escaped serve before a visible hold: {escaped}")
        self.assertEqual(iterations, [self.runner.config.interval] * 2)
        self.assertEqual(self.preflight.call_count, 3)
        recover.assert_called_once()

    def test_statvfs_failure_holds_owner_then_recovers_without_replay(self):
        self.assert_transient_probe_hold(OSError("fixture statvfs temporarily unavailable"))

    def test_subprocess_error_from_preflight_holds_owner_then_recovers_without_replay(self):
        # A diskutil timeout no longer reaches `pulse` as an exception (sd:970,
        # `preflight` returns it as a problem); any other subprocess failure
        # raised out of the probe still takes this transient hold.
        self.assert_transient_probe_hold(subprocess.SubprocessError("fixture diskutil unavailable"))

    def test_pack_head_probe_timeout_holds_owner_then_recovers_without_replay(self):
        self.assert_transient_probe_hold(subprocess.TimeoutExpired(["git", "rev-parse", "HEAD"], 120), pack_probe=True)

    def assert_startup_journal_diagnostic(self, name, content, reason):
        target = journal.directory(self.fixture.database) / name
        target.write_bytes(content)
        preserved = {path.name: path.read_bytes() for path in target.parent.iterdir()}
        with patch.object(self.runner, "tick") as tick:
            with self.assertRaises(store.RunnerRefused):
                self.runner.serve(once=True)
        self.assertEqual({path.name: path.read_bytes() for path in target.parent.iterdir()}, preserved)
        self.assertEqual(store.run_state(self.db, self.run["id"]), self.run)
        self.terminate.assert_not_called()
        tick.assert_not_called()
        heartbeat = store.heartbeat_state(self.db)
        self.assertIs(heartbeat.get("healthy"), False, heartbeat)
        self.assertFalse(heartbeat["ok"])
        self.assertIn(reason, json.dumps(heartbeat.get("restore_holds")))

    def test_partial_startup_journal_has_diagnostic_without_repair(self):
        self.assert_startup_journal_diagnostic(self.run["id"] + ".partial", b"interrupted", "interrupted journal write")

    def test_unknown_startup_journal_has_diagnostic_without_repair(self):
        self.assert_startup_journal_diagnostic("unexpected-entry", b"preserve", "unknown run journal entry")

    def test_corrupt_startup_journal_has_diagnostic_without_repair(self):
        self.assert_startup_journal_diagnostic(self.run["id"] + ".json", b"{}", "runner journal is unreadable")

    def test_worker_waits_do_not_repeat_global_preflight_or_heartbeat(self):
        selector = Mock()
        selector.select.side_effect = [[], [], [object()], [], [], [object()]]
        with patch.object(runtime.selectors, "DefaultSelector", return_value=selector), \
             patch.object(store, "heartbeat", wraps=store.heartbeat) as heartbeat:
            self.runner.pulse(self.db)
            for _ in range(2):
                self.assertEqual(self.runner._response(self.db, LiveChild(), self.request, deadline=float("inf")), {"ok": True})
        self.assertEqual(selector.select.call_count, 6)
        self.assertEqual((self.preflight.call_count, heartbeat.call_count), (1, 1),
                         "worker response waits must not repeat the global storage/heartbeat cycle")
        self.terminate.assert_not_called()

    def assert_worker_refusal(self, reason, *, deadline=float("inf")):
        selector = Mock()
        with patch.object(runtime.selectors, "DefaultSelector", return_value=selector):
            with self.assertRaisesRegex(store.RunnerRefused, reason):
                self.runner._response(self.db, LiveChild(), self.request, deadline=deadline)
        selector.select.assert_not_called()
        selector.close.assert_called_once()
        self.preflight.assert_not_called()
        self.terminate.assert_called_once()
        self.assertEqual(self.terminate.call_args.args[0]["id"], self.run["id"])
        self.assertIsNone(store.run_state(self.db, self.run["id"])["released_at"])

    def test_worker_cancellation_remains_prompt_without_global_probe(self):
        current = store.queue_state(self.db, self.request["id"])
        store.request_cancel(self.db, current["id"], expected_revision=current["revision"], who="fixture operator")
        self.assert_worker_refusal("fixture operator")

    def test_worker_deadline_remains_prompt_without_global_probe(self):
        self.assert_worker_refusal("time budget exceeded", deadline=float("-inf"))

    def test_worker_restore_marker_still_stops_owned_attempt(self):
        (self.fixture.database.parent / "runner-restore-intent.json").write_text("{}")
        self.assert_worker_refusal("database restore interrupted")

    def test_confirmed_low_database_floor_still_signals_owned_group(self):
        self.preflight.return_value = {**self.report, "database_below_floor": True}
        heartbeat = self.runner.pulse(self.db)
        self.assertIs(heartbeat["healthy"], False)
        self.terminate.assert_called_once()
        self.assertEqual(self.terminate.call_args.args[0]["id"], self.run["id"])

    def test_confirmed_low_database_floor_still_signals_when_pack_probe_fails(self):
        self.preflight.return_value = {**self.report, "database_below_floor": True}
        self.head.side_effect = subprocess.TimeoutExpired(["git", "rev-parse", "HEAD"], 120)
        heartbeat = self.runner.pulse(self.db)
        self.assertIs(heartbeat["healthy"], False)
        self.assertIs(heartbeat["storage"]["database_below_floor"], True)
        self.assertIsNone(heartbeat["pack_commit"])
        self.assertTrue(heartbeat["probe_holds"])
        self.terminate.assert_called_once()
        self.assertEqual(self.terminate.call_args.args[0]["id"], self.run["id"])

    def test_missing_pack_head_prevents_dispatch_with_available_storage(self):
        self.preflight.return_value = {**self.report, "dispatch_allowed": True}
        self.head.return_value = ""
        heartbeat = self.runner.pulse(self.db)
        self.assertIsNone(heartbeat["pack_commit"])
        self.assertIs(heartbeat["healthy"], False)
        self.assertIs(heartbeat["storage"]["ok"], True)
        self.assertIs(heartbeat["storage"]["dispatch_allowed"], False)
        self.assertEqual(heartbeat["probe_holds"], [{"probe": "pack_head", "reason": "pack HEAD is unavailable"}])

    def test_once_owner_monitors_while_worker_waits_without_more_dispatch(self):
        worker = Mock()
        worker.alive = True
        worker.is_alive.side_effect = lambda: worker.alive

        def join(timeout=None):
            self.assertFalse(worker.alive, "once joined an active worker before owner monitoring")

        worker.join.side_effect = join
        self.runner.threads[self.run["id"]] = worker
        child = LiveChild()
        self.runner.supervisors[self.run["id"]] = child
        before = store.run_state(self.db, self.run["id"])
        journal_bytes = self.journal_path.read_bytes()

        def preflight(*args, **kwargs):
            if self.preflight.call_count == 3:
                self.assertTrue(worker.alive)
                self.assert_owned_unchanged(before, journal_bytes, child)
                with self.assertRaisesRegex(store.RunnerRefused, "ownership lock held"):
                    with journal.lock(self.fixture.database.parent / "runner.lock", blocking=False):
                        self.fail("once wait dropped its owner lock")
                worker.alive = False
                return {**self.report, "database_below_floor": True}
            return self.report

        self.preflight.side_effect = preflight
        with patch.object(self.runner, "recover", return_value=[]), patch.object(runtime.time, "sleep") as sleep:
            self.runner.serve(once=True)
        self.assertEqual(self.preflight.call_count, 3)
        sleep.assert_called_once_with(self.runner.config.interval)
        self.terminate.assert_called_once()
        worker.join.assert_called_once()
        self.claim.assert_not_called()
        self.queue_merges.assert_not_called()

    def test_a_login_probe_that_exits_late_adds_no_sleep_to_the_monitor(self):
        # `serve` probes the login shell on its own thread (sd:1762). With a
        # timeout, `Popen.wait` polls a child that closed its pipes but has not
        # exited: time.sleep(0.001), then 0.002 and on. `runtime.time.sleep` is
        # the global `time.sleep`, so the test above counted those polls as
        # monitor ticks (sd:1778). This probe always exits late; the fixture
        # must keep `serve` from reaching it.
        def late_exit():
            subprocess.run(["/bin/sh", "-c", "exec >&- 2>&-; sleep 0.01"], capture_output=True, timeout=5, check=False)
            return []

        with patch.dict(runtime.Runner.resolve_tools.__kwdefaults__, {"probe": late_exit}):
            self.test_once_owner_monitors_while_worker_waits_without_more_dispatch()

    def assert_once_database_hold(self, error):
        worker = Mock()
        worker.alive = True
        worker.is_alive.side_effect = lambda: worker.alive
        worker.join.side_effect = lambda: self.assertFalse(worker.alive)
        self.runner.threads[self.run["id"]] = worker
        child = LiveChild()
        self.runner.supervisors[self.run["id"]] = child
        before = store.run_state(self.db, self.run["id"])
        journal_bytes = self.journal_path.read_bytes()
        write = store.heartbeat
        writes = []

        def heartbeat(connection, body):
            writes.append(body)
            if len(writes) == 2:
                raise error
            if len(writes) == 3:
                self.assert_owned_unchanged(before, journal_bytes, child)
                with self.assertRaisesRegex(store.RunnerRefused, "ownership lock held"):
                    with journal.lock(self.fixture.database.parent / "runner.lock", blocking=False):
                        self.fail("busy monitor dropped its owner lock")
                worker.alive = False
            return write(connection, body)

        with patch.object(self.runner, "recover", return_value=[]) as recover, \
             patch.object(store, "heartbeat", side_effect=heartbeat), \
             patch.object(runtime, "SQL_RETRY_SECONDS", 0), patch.object(runtime.time, "sleep") as sleep:
            self.runner.serve(once=True)
        self.assertEqual(len(writes), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assert_owned_unchanged(before, journal_bytes, child)
        recover.assert_called_once()

    def test_once_busy_monitor_keeps_owner_and_child_until_retry_succeeds(self):
        self.assert_once_database_hold(sqlite3.OperationalError("database is locked"))

    def test_once_locked_monitor_keeps_owner_and_child_until_retry_succeeds(self):
        self.assert_once_database_hold(sqlite3.OperationalError("database table is locked"))

    def test_once_corrupt_database_is_not_retried_as_a_busy_monitor(self):
        with self.assertRaisesRegex(sqlite3.DatabaseError, "malformed"):
            self.assert_once_database_hold(sqlite3.DatabaseError("database disk image is malformed"))

    def assert_once_partial_tick_hold(self, error):
        worker = Mock()
        worker.alive = False
        worker.start.side_effect = lambda: setattr(worker, "alive", True)
        worker.is_alive.side_effect = lambda: worker.alive
        worker.join.side_effect = lambda: self.assertFalse(worker.alive)
        self.runner.watcher = Mock()
        self.runner.watcher.is_alive.return_value = True
        self.runner.last_archive_watch = float("inf")
        child = LiveChild()
        self.runner.supervisors[self.run["id"]] = child
        before = store.run_state(self.db, self.run["id"])
        journal_bytes = self.journal_path.read_bytes()
        self.claim.side_effect = [self.request, error]

        def preflight(*args, **kwargs):
            if self.preflight.call_count == 3:
                self.assertTrue(worker.alive)
                self.assertEqual(self.claim.call_count, 2)
                self.assertEqual(store.run_state(self.db, self.run["id"]), before)
                self.assertEqual(self.journal_path.read_bytes(), journal_bytes)
                self.assertEqual(child.waits, [])
                with self.assertRaisesRegex(store.RunnerRefused, "ownership lock held"):
                    with journal.lock(self.fixture.database.parent / "runner.lock", blocking=False):
                        self.fail("partially dispatched tick dropped its owner lock")
                worker.alive = False
            return {**self.report, "dispatch_allowed": True}

        self.preflight.side_effect = preflight
        with patch.object(self.runner, "recover", return_value=[]) as recover, \
             patch.object(runtime.threading, "Thread", return_value=worker), \
             patch.object(store, "queued", return_value=[{"id": self.request["id"]}, {"id": 999}]) as queued, \
             patch.object(runtime, "SQL_RETRY_SECONDS", 0), patch.object(runtime.time, "sleep"):
            self.runner.serve(once=True)
        self.assertEqual(self.claim.call_count, 2)
        queued.assert_called_once()
        self.queue_merges.assert_called_once()
        worker.start.assert_called_once()
        worker.join.assert_called_once()
        recover.assert_called_once()
        self.terminate.assert_not_called()

    def test_once_busy_later_claim_monitors_started_worker_without_more_claims(self):
        self.assert_once_partial_tick_hold(sqlite3.OperationalError("database is locked"))

    def test_once_locked_later_claim_monitors_started_worker_without_more_claims(self):
        self.assert_once_partial_tick_hold(sqlite3.OperationalError("database table is locked"))

    def test_once_busy_before_worker_start_remains_a_visible_refusal(self):
        with patch.object(self.runner, "recover", return_value=[]), \
             patch.object(self.runner, "pulse", side_effect=sqlite3.OperationalError("database is locked")), \
             patch.object(runtime.time, "sleep") as sleep:
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                self.runner.serve(once=True)
        self.assertEqual(self.runner.threads, {})
        sleep.assert_not_called()
        self.terminate.assert_not_called()

    def test_known_low_database_floor_survives_later_diskutil_timeout(self):
        self.runner.config.work.mkdir()
        self.runner.config.retention.mkdir()
        database = {"device": 1, "free": 0, "total": 1000000000000}
        work = {"device": 2, "free": 100000000000, "total": 1000000000000}
        self.capacity.side_effect = [database, work, work, database]
        self.preflight.side_effect = self.real_preflight
        # diskutil runs only on macOS; the platform is patched so the same
        # path runs on the Linux CI runner, with diskutil stood in below.
        with patch("sd_runner.storage.sys.platform", "darwin"), \
             patch.object(runtime.storage.subprocess, "run", side_effect=subprocess.TimeoutExpired(["diskutil", "info"], 20)):
            heartbeat = self.runner.pulse(self.db)
        self.assertIs(heartbeat["healthy"], False)
        self.assertEqual(heartbeat["storage"]["database"], database)
        self.assertIs(heartbeat["storage"]["database_below_floor"], True)
        self.assertIs(heartbeat["storage"]["dispatch_allowed"], False)
        # A diskutil timeout is a preflight problem since sd:970, not a raised
        # probe hold: the report carries the observed capacities and names it.
        self.assertIn("diskutil timed out after 20s", heartbeat["storage"]["problems"][0])
        self.assertEqual(self.capacity.call_count, 3)
        self.terminate.assert_called_once()
        self.assertEqual(self.terminate.call_args.args[0]["id"], self.run["id"])

    def test_low_work_floor_pauses_dispatch_without_signalling(self):
        self.runner.tick(self.db)
        self.assertFalse(store.heartbeat_state(self.db)["storage"]["dispatch_allowed"])
        self.terminate.assert_not_called()
        self.claim.assert_not_called()

    def test_corrupt_database_is_not_reclassified_as_transient_probe(self):
        with patch.object(self.runner, "recover", return_value=[]), \
             patch.object(self.runner, "pulse", side_effect=sqlite3.DatabaseError("database disk image is malformed")) as pulse, \
             patch.object(runtime.time, "sleep") as sleep:
            with self.assertRaisesRegex(sqlite3.DatabaseError, "malformed"):
                self.runner.serve()
        pulse.assert_called_once()
        sleep.assert_not_called()
        self.terminate.assert_not_called()

    def test_existing_process_probe_timeout_retains_cleanup_hold(self):
        error = subprocess.TimeoutExpired(["ps", "fixture-only"], 10)
        store.begin_ending(self.db, self.run["id"], outcome="blocked", detail="fixture completed")
        with patch.object(self.runner, "observer", side_effect=error):
            held = self.runner.finish(self.db, self.run["id"])
        self.assertIsNone(held["released_at"])
        self.assertIn("cleanup held", held["detail"])
        self.assertIn(str(error), held["detail"])
        self.terminate.assert_not_called()

    def test_control_gate_stays_exclusive_without_blocking_heartbeat_sql(self):
        with control_gate(self.db):
            self.assertTrue(self.runner.pulse(self.db)["healthy"])
            with self.assertRaisesRegex(WorkflowError, "another control or restore"):
                with control_gate(self.db):
                    self.fail("the control lock must remain exclusive")
        with control_gate(self.db):
            self.assertFalse(self.db.in_transaction)


if __name__ == "__main__":
    unittest.main()
