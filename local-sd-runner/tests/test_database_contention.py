"""Real SQLite writer contention with fake owned processes and no providers."""
import io
import sqlite3
import unittest
from unittest.mock import patch

from sd_db import runner as store
from sd_db.database import connect, transaction
from sd_db.writes import record_state
from sd_runner import controls, runtime

from tests import test_runtime


class FakeChild:
    pid = 424242

    def __init__(self):
        self.stdin, self.stdout, self.stderr = io.StringIO(), io.StringIO(), io.StringIO()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return 0

    def poll(self):
        return 0


class DatabaseContention(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.runner.observer = lambda run: []
        self.db = self.fixture.db
        self.db.execute("PRAGMA busy_timeout=20")
        self.holder = connect(self.fixture.database)
        self.addCleanup(self.holder.close)
        self.request = self.fixture.claim()
        self.runner.persist(self.db, self.request["run"]["id"])

    def release(self, seconds=None):
        if self.holder.in_transaction:
            self.holder.execute("ROLLBACK")

    def execute(self, *, release=False):
        child = FakeChild()
        self.actions = []

        def action(connection, process, selected, name, deadline, **kwargs):
            self.actions.append(name)
            if name == "clone":
                self.holder.execute("BEGIN IMMEDIATE")
            return {"exit_code": 0} if name == "provider" else {}

        with patch("sd_runner.runtime.subprocess.Popen", return_value=child), \
             patch("sd_runner.runtime.processes.start_identity", return_value="fixture-start"), \
             patch("sd_runner.runtime.gitops.check_attribution", return_value="f" * 40), \
             patch.object(self.runner, "action", side_effect=action), \
             patch("sd_runner.runtime.time.sleep", side_effect=self.release if release else lambda seconds: None):
            result = self.runner.execute(self.db, self.request, command=["fixture-only"])
        return result, child

    def tick(self):
        with patch.object(self.runner, "pulse", return_value={"storage": {"dispatch_allowed": False}}):
            return self.runner.tick(self.db)

    def test_busy_progress_retries_atomic_write_without_repeating_action(self):
        result, child = self.execute(release=True)
        self.assertEqual(result["end_step"], "released")
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(self.actions, ["clone", "branch", "merge", "provider"])
        self.assertEqual(child.waits, [2])
        self.assertEqual(self.db.execute("PRAGMA busy_timeout").fetchone()[0], 20)

    def test_exhaustion_joins_child_and_later_reconciles_without_replay(self):
        with patch.object(runtime, "SQL_RETRY_SECONDS", 0, create=True):
            result, child = self.execute()
        self.assertTrue(result["database_hold"])
        self.assertIn("stopped", result["detail"])
        self.assertEqual(child.waits, [2])
        before = store.run_state(self.db, self.request["run"]["id"])
        self.assertIsNone(before["released_at"])
        self.assertIsNone(before["outcome"])
        self.assertEqual(store.queue_state(self.db, self.request["id"])["status"], "running")
        self.release()
        self.tick()
        after = store.run_state(self.db, before["id"])
        self.assertEqual(after["outcome"], "blocked")
        self.assertEqual(after["end_step"], "released")
        self.assertEqual(self.actions, ["clone"])
        self.assertEqual(self.runner.pending_endings, {})

    def test_restore_hold_prevents_deferred_ending(self):
        with patch.object(runtime, "SQL_RETRY_SECONDS", 0, create=True):
            result, _ = self.execute()
        self.assertTrue(result["database_hold"])
        self.release()
        record_state(self.db, "restore", key="concurrent-restore", body={})
        self.tick()
        self.assertIsNone(store.run_state(self.db, self.request["run"]["id"])["released_at"])
        self.assertIn(self.request["run"]["id"], self.runner.pending_endings)
        self.assertEqual(self.actions, ["clone"])

    def cancel_held_ending(self, completed):
        with patch.object(runtime, "SQL_RETRY_SECONDS", 0):
            result, _ = self.execute()
        self.assertTrue(result["database_hold"])
        self.release()
        current = store.queue_state(self.db, self.request["id"])
        with patch("sd_runner.controls.processes.terminate_owned", return_value=[]):
            controls.cancel(self.runner.config, current["id"], expected_revision=current["revision"],
                            expected_run=current["run"]["id"], who="fixture operator")
        with patch.object(self.runner, "execution_result", return_value=completed):
            self.tick()
        ended = store.run_state(self.db, current["run"]["id"])
        self.assertEqual(ended["outcome"], "cancelled")
        self.assertEqual(ended["end_step"], "released")
        self.assertIn("fixture operator", ended["detail"])
        self.assertEqual(store.queue_state(self.db, current["id"])["status"], "cancelled")
        self.assertEqual(self.actions, ["clone"])

    def test_current_cancellation_wins_over_cached_blocked_ending(self):
        self.cancel_held_ending(None)

    def test_current_cancellation_wins_over_completion_receipt(self):
        self.cancel_held_ending({"exit_code": 0})

    def test_fatal_database_error_does_not_retry(self):
        with patch.object(store, "update_run", side_effect=sqlite3.OperationalError("disk I/O error")), \
             patch("sd_runner.runtime.time.sleep") as sleep:
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
                self.runner.persist(self.db, self.request["run"]["id"], start_step="supervised")
        sleep.assert_not_called()

    def test_an_open_transaction_is_never_replayed(self):
        with transaction(self.db):
            with patch.object(store, "update_run", side_effect=sqlite3.OperationalError("database is locked")), \
                 patch("sd_runner.runtime.time.sleep") as sleep:
                with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                    self.runner.persist(self.db, self.request["run"]["id"], start_step="supervised")
            sleep.assert_not_called()

    def test_busy_heartbeat_keeps_daemon_and_existing_child_alive(self):
        self.holder.execute("BEGIN IMMEDIATE")
        ticks = []
        child = FakeChild()
        self.runner.supervisors["existing-owned"] = child
        original_tick = self.runner.tick

        class ProbeFinished(Exception):
            pass

        def tick(connection):
            ticks.append(True)
            if len(ticks) == 1:
                return original_tick(connection)
            self.assertEqual(child.waits, [])
            raise ProbeFinished

        def fast_connect(path):
            connection = connect(path)
            connection.execute("PRAGMA busy_timeout=20")
            return connection

        with patch.object(runtime, "SQL_RETRY_SECONDS", 0, create=True), \
             patch("sd_runner.runtime.connect", side_effect=fast_connect), \
             patch("sd_runner.runtime.storage.preflight", return_value={"ok": True, "problems": [], "database_below_floor": False}), \
             patch("sd_runner.runtime.gitops.head", return_value="fixture"), \
             patch.object(self.runner, "recover", return_value=[]) as recover, \
             patch.object(self.runner, "refresh_archives"), patch.object(self.runner, "watch_deliveries"), \
             patch.object(self.runner, "tick", side_effect=tick), \
             patch("sd_runner.runtime.time.sleep", side_effect=self.release):
            with self.assertRaises(ProbeFinished):
                self.runner.serve()
        self.assertEqual(len(ticks), 2)
        self.assertEqual(recover.call_count, 1)
        self.assertEqual(child.waits, [])

    def test_retry_budget_is_monotonic_and_restores_connection_timeout(self):
        self.holder.execute("BEGIN IMMEDIATE")
        clock = [0.0]
        calls = []

        def write(connection):
            calls.append(True)
            return store.heartbeat(connection, {"healthy": True})

        def sleep(seconds):
            clock[0] += seconds

        with patch.object(runtime, "SQL_RETRY_SECONDS", 0.03), \
             patch("sd_runner.runtime.time.monotonic", side_effect=lambda: clock[0]), \
             patch("sd_runner.runtime.time.sleep", side_effect=sleep):
            with self.assertRaises(sqlite3.OperationalError):
                runtime.database_write(self.db, write)
        self.assertEqual(clock[0], 0.03)
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.db.execute("PRAGMA busy_timeout").fetchone()[0], 20)
        self.assertEqual(self.db.execute("SELECT count(*) FROM state WHERE kind='heartbeat'").fetchone()[0], 0)
