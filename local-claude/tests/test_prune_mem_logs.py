"""`claude.sh prune-mem-logs`: retention for claude-mem's logs and tool_uses.

Every case runs the real script against a scratch `CLAUDE_MEM_DATA_DIR`,
so nothing under ~/.claude-mem is read or written. File dates are relative
to the local date the script sees; row ages to the clock at the run.
"""

import datetime
import hashlib
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import time
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "claude.sh"


def daily(days_ago):
    day = datetime.date.today() - datetime.timedelta(days=days_ago)
    return "claude-mem-%s.log" % day.isoformat()


class PruneCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = pathlib.Path(tmp.name)
        self.logs = self.data / "logs"
        self.logs.mkdir()

    def log(self, name, size=1000):
        (self.logs / name).write_bytes(b"x" * size)

    def names(self):
        return sorted(p.name for p in self.logs.iterdir())

    def run_prune(self, *args, **env):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_MEM_")}
        environment["CLAUDE_MEM_DATA_DIR"] = str(self.data)
        environment.update(env)
        done = subprocess.run(["sh", str(SCRIPT), "prune-mem-logs", *args],
                              capture_output=True, text=True, env=environment, timeout=60)
        return done.returncode, done.stdout + done.stderr


class DatedLogs(PruneCase):
    def fixture(self):
        for age in (-1, 0, 1, 13, 14):
            self.log(daily(age))
        self.log(daily(15), 2000)
        self.log(daily(40), 3000)
        # Names that are not the worker's daily file are never touched.
        for name in ("corpus-refresh.log", "runner-errors.log", "claude-mem-2026-13-40.log",
                     "claude-mem-latest.log", daily(40) + ".gz", "notes-" + daily(40)):
            self.log(name)

    def test_a_dry_run_names_the_old_files_and_deletes_nothing(self):
        self.fixture()
        before = self.names()
        code, output = self.run_prune()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.names(), before)
        self.assertIn("would remove " + daily(15), output)
        self.assertIn("would remove " + daily(40), output)
        self.assertNotIn(daily(14), output)
        self.assertIn("would reclaim 5000 bytes", output)

    def test_apply_deletes_only_dated_files_older_than_the_keep_window(self):
        self.fixture()
        code, output = self.run_prune("--apply")
        self.assertEqual(code, 0, output)
        gone = {daily(15), daily(40)}
        self.assertTrue(gone.isdisjoint(self.names()), output)
        for age in (-1, 0, 1, 13, 14):
            self.assertIn(daily(age), self.names())
        for name in ("corpus-refresh.log", "runner-errors.log", "claude-mem-2026-13-40.log",
                     "claude-mem-latest.log", daily(40) + ".gz", "notes-" + daily(40)):
            self.assertIn(name, self.names())
        self.assertIn("reclaimed 5000 bytes", output)

    def test_the_keep_window_is_a_setting(self):
        self.fixture()
        code, output = self.run_prune("--apply", CLAUDE_MEM_LOG_KEEP_DAYS="30")
        self.assertEqual(code, 0, output)
        self.assertIn(daily(15), self.names())
        self.assertNotIn(daily(40), self.names())

    def test_today_and_yesterday_survive_a_zero_keep_window(self):
        for age in (-1, 0, 1, 2):
            self.log(daily(age))
        code, output = self.run_prune("--apply", CLAUDE_MEM_LOG_KEEP_DAYS="0")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.names(), sorted([daily(-1), daily(0), daily(1)]))

    def test_a_symlink_wearing_the_name_is_not_followed_or_removed(self):
        outside = self.data / "keep-me.log"
        outside.write_text("precious")
        (self.logs / daily(40)).symlink_to(outside)
        code, output = self.run_prune("--apply")
        self.assertEqual(code, 0, output)
        self.assertTrue((self.logs / daily(40)).is_symlink())
        self.assertEqual(outside.read_text(), "precious")

    def test_a_keep_window_that_is_not_a_number_fails(self):
        self.log(daily(40))
        code, output = self.run_prune("--apply", CLAUDE_MEM_LOG_KEEP_DAYS="two weeks")
        self.assertEqual(code, 1, output)
        self.assertIn("CLAUDE_MEM_LOG_KEEP_DAYS", output)
        self.assertIn(daily(40), self.names())

    def test_no_log_directory_is_nothing_to_do(self):
        self.logs.rmdir()
        code, output = self.run_prune("--apply")
        self.assertEqual(code, 0, output)
        self.assertIn("nothing to do", output)


DAY_MS = 86400 * 1000
RESPONSE = "r" * 8000


class ToolUses(PruneCase):
    """The database step: tool_uses rows older than the keep window go."""

    def setUp(self):
        super().setUp()
        self.db = self.data / "claude-mem.db"

    def make_db(self, auto_vacuum=2, tool_uses=True):
        db = sqlite3.connect(self.db)
        # auto_vacuum has to be set before the first table exists.
        db.execute("PRAGMA auto_vacuum=%d" % auto_vacuum)
        if tool_uses:
            db.execute("CREATE TABLE tool_uses (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                       " tool_use_id TEXT NOT NULL, tool_response TEXT,"
                       " created_at TEXT NOT NULL, created_at_epoch INTEGER NOT NULL)")
            db.execute("CREATE INDEX idx_tool_uses_created_at_epoch ON tool_uses(created_at_epoch)")
        # Old rows in the tables that must never be touched.
        db.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY, text TEXT,"
                   " created_at_epoch INTEGER NOT NULL)")
        db.execute("CREATE TABLE sync_state (k TEXT, created_at_epoch INTEGER)")
        old = self.epoch(30)
        db.executemany("INSERT INTO observations (text, created_at_epoch) VALUES (?, ?)",
                       [("observation %d" % i, old) for i in range(10)])
        db.executemany("INSERT INTO sync_state VALUES (?, ?)", [("k%d" % i, old) for i in range(5)])
        db.commit()
        # The live database runs in WAL mode.
        self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        self.assertEqual(db.execute("PRAGMA auto_vacuum").fetchone()[0], auto_vacuum)
        db.close()

    @staticmethod
    def epoch(days_ago):
        return int(time.time() * 1000 - days_ago * DAY_MS)

    def add(self, days_ago, count):
        db = sqlite3.connect(self.db)
        db.executemany("INSERT INTO tool_uses (tool_use_id, tool_response, created_at,"
                       " created_at_epoch) VALUES (?, ?, 'x', ?)",
                       [("t%d-%d" % (days_ago * 100, i), RESPONSE, self.epoch(days_ago))
                        for i in range(count)])
        db.commit()
        db.close()

    def fixture(self, auto_vacuum=2):
        self.make_db(auto_vacuum)
        self.add(30, 50)
        self.add(8, 50)
        self.add(6, 30)
        self.add(0.5, 20)

    def query(self, sql):
        db = sqlite3.connect(self.db)
        try:
            return db.execute(sql).fetchall()
        finally:
            db.close()

    def count(self):
        return self.query("SELECT count(*) FROM tool_uses")[0][0]

    def others(self):
        return (self.query("SELECT * FROM observations ORDER BY id"),
                self.query("SELECT * FROM sync_state ORDER BY k"))

    def test_apply_deletes_rows_older_than_seven_days_and_nothing_else(self):
        self.fixture()
        others = self.others()
        code, output = self.run_prune("--apply")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.count(), 50, output)
        oldest = self.query("SELECT min(created_at_epoch) FROM tool_uses")[0][0]
        self.assertGreater(oldest, self.epoch(7))
        self.assertIn("deleted 100 tool_uses row(s) older than 7 day(s)", output)
        self.assertRegex(output, r"claude-mem\.db: \d+ bytes \(\S+\) before, \d+ bytes \(\S+\) after")
        self.assertEqual(self.others(), others)

    def test_no_log_directory_still_prunes_the_table(self):
        self.logs.rmdir()
        self.fixture()
        code, output = self.run_prune("--apply")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.count(), 50, output)

    def test_the_keep_window_is_a_setting(self):
        self.fixture()
        code, output = self.run_prune("--apply", CLAUDE_MEM_TOOL_USES_KEEP_DAYS="10")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.count(), 100, output)
        self.assertIn("deleted 50 tool_uses row(s) older than 10 day(s)", output)

    def test_a_zero_keep_window_still_keeps_the_last_day(self):
        # The worker updates recent rows' observation_id; a row it has just
        # written is never pruned from under it.
        self.fixture()
        code, output = self.run_prune("--apply", CLAUDE_MEM_TOOL_USES_KEEP_DAYS="0")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.count(), 20, output)

    def test_a_keep_window_that_is_not_a_number_fails_and_deletes_nothing(self):
        self.fixture()
        self.log(daily(40))
        for bad in ("a week", "-3"):
            code, output = self.run_prune("--apply", CLAUDE_MEM_TOOL_USES_KEEP_DAYS=bad)
            self.assertEqual(code, 1, output)
            self.assertIn("CLAUDE_MEM_TOOL_USES_KEEP_DAYS", output)
            self.assertEqual(self.count(), 150, output)
            self.assertIn(daily(40), self.names())

    def test_a_busy_timeout_that_is_not_a_number_fails(self):
        self.fixture()
        for bad in ("soon", "0", "-1", "nan"):
            code, output = self.run_prune("--apply", CLAUDE_MEM_DB_BUSY_TIMEOUT=bad)
            self.assertEqual(code, 1, output)
            self.assertIn("CLAUDE_MEM_DB_BUSY_TIMEOUT", output)
            self.assertEqual(self.count(), 150, output)

    def test_a_dry_run_counts_and_writes_nothing(self):
        self.fixture()
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        code, output = self.run_prune()
        self.assertEqual(code, 0, output)
        # A read-only open may leave empty -wal/-shm files; the database
        # file itself is byte for byte the same.
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)
        self.assertEqual(self.count(), 150)
        self.assertIn("would delete 100 tool_uses row(s) older than 7 day(s)", output)
        approx = int(output.split("row(s) older than 7 day(s), about ")[1].split(" bytes")[0])
        self.assertGreaterEqual(approx, 100 * len(RESPONSE))
        self.assertLess(approx, 200 * len(RESPONSE))

    def test_incremental_auto_vacuum_returns_the_space_to_disk(self):
        self.fixture(auto_vacuum=2)
        before = self.db.stat().st_size
        code, output = self.run_prune("--apply")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.query("PRAGMA freelist_count")[0][0], 0, output)
        self.assertLess(self.db.stat().st_size, before - 50 * len(RESPONSE), output)
        self.assertIn("claude-mem.db: %d bytes" % before, output)
        self.assertNotIn("not returned to disk", output)

    def test_without_incremental_auto_vacuum_rows_go_and_the_space_stays(self):
        self.fixture(auto_vacuum=0)
        code, output = self.run_prune("--apply")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.count(), 50, output)
        self.assertGreater(self.query("PRAGMA freelist_count")[0][0], 0, output)
        self.assertIn("auto_vacuum is 0, not 2 (INCREMENTAL): the freed pages are reused"
                      " inside the file, not returned to disk", output)

    def test_no_database_is_skipped_and_not_created(self):
        code, output = self.run_prune("--apply")
        self.assertEqual(code, 0, output)
        self.assertIn("no claude-mem database at %s -- tool_uses skipped" % self.db, output)
        self.assertFalse(self.db.exists())

    def test_a_database_that_is_not_one_fails_and_the_logs_are_still_pruned(self):
        # Only a lock is a skip; anything else is a fault the run reports.
        self.db.write_bytes(b"not a database" * 100)
        self.log(daily(40))
        for args in ((), ("--apply",)):
            code, output = self.run_prune(*args)
            self.assertEqual(code, 1, output)
            self.assertIn("pruning tool_uses in %s failed" % self.db, output)
        self.assertNotIn(daily(40), self.names())

    def test_a_symlinked_database_is_not_followed(self):
        # Like the logs: a link wearing the name is not the worker's file,
        # and deleting through it would reach outside the data directory.
        self.fixture()
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        target = pathlib.Path(elsewhere.name) / "archive.db"
        self.db.rename(target)
        self.db.symlink_to(target)
        for args in ((), ("--apply",)):
            code, output = self.run_prune(*args)
            self.assertEqual(code, 0, output)
            self.assertIn("%s is not a regular file -- tool_uses skipped" % self.db, output)
            self.assertEqual(self.count(), 150, output)

    def test_no_tool_uses_table_is_skipped(self):
        self.make_db(tool_uses=False)
        others = self.others()
        code, output = self.run_prune("--apply")
        self.assertEqual(code, 0, output)
        self.assertIn("no tool_uses table in %s -- skipped" % self.db, output)
        self.assertEqual(self.others(), others)

    def hold_write_lock(self):
        writer = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO observations (text, created_at_epoch) VALUES ('live', 0)")
        return writer

    def test_the_delete_waits_for_a_writer_holding_the_lock_briefly(self):
        self.fixture()
        writer = self.hold_write_lock()
        prune = subprocess.Popen(["sh", str(SCRIPT), "prune-mem-logs", "--apply"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env=self.environment())
        self.addCleanup(prune.kill)
        time.sleep(1.5)
        self.assertIsNone(prune.poll(), "the delete did not wait for the lock")
        writer.execute("COMMIT")
        output = prune.communicate(timeout=60)[0].decode()
        self.assertEqual(prune.returncode, 0, output)
        self.assertNotIn("locked", output)
        self.assertEqual(self.count(), 50, output)
        self.assertEqual(self.query("SELECT count(*) FROM observations")[0][0], 11)

    def test_a_lock_held_past_the_timeout_is_a_logged_skip(self):
        self.fixture()
        writer = self.hold_write_lock()
        started = time.monotonic()
        code, output = self.run_prune("--apply", CLAUDE_MEM_DB_BUSY_TIMEOUT="1")
        # It waited the 1 s it was given, not the 30 s default.
        self.assertGreaterEqual(time.monotonic() - started, 1)
        self.assertLess(time.monotonic() - started, 15)
        writer.execute("ROLLBACK")
        self.assertEqual(code, 0, output)
        self.assertIn("database locked past 1s -- skipped after deleting 0 row(s)", output)
        self.assertEqual(self.count(), 150, output)

    def environment(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_MEM_")}
        env["CLAUDE_MEM_DATA_DIR"] = str(self.data)
        return env


if __name__ == "__main__":
    unittest.main()
