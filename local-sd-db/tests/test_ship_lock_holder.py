"""The ship lock names its holder, waits to a deadline, and reports its files.

sd:1958 (pack items sd:1936, sd:1937, sd:1940). The flock is the lock. The
holder record a holder writes into the lock file is advisory: a refusal and
`lock_files` read it back, and a record whose pid is gone never blocks. Every
hold here is a real flock taken by a child process, not a mock.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from sd_db import ship
from sd_db.workflow import WorkflowError

REPOSITORY = "fixture/repo"

#: Hold the lock, say so on stdout, and release after argv[3] seconds.
HOLDER = """
import sys, time
from pathlib import Path
from sd_db import ship
with ship.repository_lock(Path(sys.argv[1]), sys.argv[2],
                          holder={"command": "sd-ship prepare --item 1872", "item": 1872}):
    print("held", flush=True)
    time.sleep(float(sys.argv[3]))
"""


class ShipLockHolder(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.database = self.root / "sd.db"
        self.children = []

    def tearDown(self):
        for child in self.children:
            child.kill()
            child.wait()
            child.stdout.close()
        shutil.rmtree(self.root)

    def hold(self, seconds=60.0):
        """A child process that holds the flock for `seconds`; returns once it holds."""
        child = subprocess.Popen([sys.executable, "-c", HOLDER, str(self.database), REPOSITORY, str(seconds)],
                                 stdout=subprocess.PIPE, text=True)
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child

    def lock_path(self):
        (path,) = (self.root / "ship-locks").iterdir()
        return path

    def dead_pid(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        return child.pid

    def test_the_refusal_names_the_holder_pid_command_item_and_age(self):
        child = self.hold()
        with self.assertRaises(WorkflowError) as refused:
            with ship.repository_lock(self.database, REPOSITORY):
                self.fail("second owner")
        message = str(refused.exception)
        self.assertIn("another ship operation owns this repository", message)
        self.assertIn(f"pid {child.pid}", message)
        self.assertIn("sd-ship prepare --item 1872", message)
        self.assertRegex(message, r"held \d+s")

    def test_the_record_is_in_the_lock_file_and_cleared_on_release(self):
        child = self.hold(seconds=0.5)
        record = json.loads(self.lock_path().read_text())
        self.assertEqual(record["pid"], child.pid)
        self.assertEqual(record["repository"], REPOSITORY)
        self.assertEqual(record["item"], 1872)
        self.assertIn("started_at", record)
        child.wait(timeout=10)
        self.assertEqual(self.lock_path().read_text(), "")

    def test_a_record_whose_pid_is_gone_never_blocks(self):
        with ship.repository_lock(self.database, REPOSITORY):
            pass
        stale = {"pid": self.dead_pid(), "command": "sd-ship prepare --item 7", "item": 7,
                 "repository": REPOSITORY, "started_at": "2026-09-01T00:00:00+00:00"}
        self.lock_path().write_text(json.dumps(stale))
        (entry,) = ship.lock_files(self.database)
        self.assertEqual(entry["state"], "stale")
        self.assertEqual(ship.held_locks(self.database), [])
        started = time.monotonic()
        with ship.repository_lock(self.database, REPOSITORY, holder={"item": 8}):
            self.assertEqual(json.loads(self.lock_path().read_text())["item"], 8)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_status_lists_a_held_lock_and_not_an_idle_file(self):
        with ship.repository_lock(self.database, "fixture/idle"):
            pass
        child = self.hold()
        held = ship.held_locks(self.database)
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["pid"], child.pid)
        self.assertEqual(held[0]["item"], 1872)
        self.assertEqual(held[0]["repository"], REPOSITORY)
        self.assertEqual(held[0]["command"], "sd-ship prepare --item 1872")
        self.assertIsInstance(held[0]["age_seconds"], int)
        states = sorted(entry["state"] for entry in ship.lock_files(self.database))
        self.assertEqual(states, ["held", "idle"])

    def test_without_wait_the_refusal_is_immediate(self):
        self.hold()
        started = time.monotonic()
        with self.assertRaises(WorkflowError):
            with ship.repository_lock(self.database, REPOSITORY):
                self.fail("second owner")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_wait_refuses_at_the_deadline_and_names_the_holder(self):
        child = self.hold()
        started = time.monotonic()
        with self.assertRaises(WorkflowError) as refused:
            with ship.repository_lock(self.database, REPOSITORY, wait=1.5):
                self.fail("second owner")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertLess(elapsed, 1.5 + ship.WAIT_POLL_SECONDS + 1.0)
        self.assertIn(f"pid {child.pid}", str(refused.exception))
        self.assertIn("waited 1.5s", str(refused.exception))

    def test_wait_runs_once_the_holder_releases(self):
        self.hold(seconds=1.0)
        started = time.monotonic()
        with ship.repository_lock(self.database, REPOSITORY, wait=10, holder={"item": 9}):
            elapsed = time.monotonic() - started
            self.assertEqual(json.loads(self.lock_path().read_text())["pid"], os.getpid())
        self.assertLess(elapsed, 1.0 + ship.WAIT_POLL_SECONDS + 1.0)

    def test_a_negative_wait_is_refused(self):
        with self.assertRaises(WorkflowError):
            with ship.repository_lock(self.database, REPOSITORY, wait=-1):
                self.fail("locked with a negative wait")


if __name__ == "__main__":
    unittest.main()
