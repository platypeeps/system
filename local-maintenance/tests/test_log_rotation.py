"""Rotation waits for a cron job's run and its lost report (sd:8808).

Rotation truncates the log a run report reads from an offset. Done while a
run writes, or while `.<job>.unreported` still points into the log, the
report's slice was gone and the failure died unreported.
"""

import fcntl

from tests.test_report import Harness


class LogRotationTest(Harness):
    def setUp(self):
        super().setUp()
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.log = self.logs / "demo.log"
        self.body = b"x" * (2 * 1024 * 1024)
        self.log.write_bytes(self.body)

    def rotate(self):
        return self.maintenance(f"logs|{self.logs}|1\n")

    def test_an_oversized_log_rotates(self):
        (self.logs / ".demo.flock").write_bytes(b"")
        run = self.rotate()
        self.assertIn(f"- rotated {self.log}", run.stdout)
        self.assertEqual(b"", self.log.read_bytes())

    def test_a_pending_report_defers_the_rotation(self):
        # `.reporting.<run>` is the record of a run killed while it filed its
        # report (build 1232 round 5); the next run moves it to
        # `.unreported`. Both point into the log.
        for name in (".demo.unreported", ".demo.reporting.20260101T000000Z-1"):
            with self.subTest(name):
                record = self.logs / name
                record.write_text("0 run-1 a b 7\n")
                run = self.rotate()
                record.unlink()
                self.assertEqual(self.body, self.log.read_bytes())
                self.assertIn(f"- rotation of {self.log} deferred: its job", run.stdout)
                self.assertEqual(self.attention(run), [])

    def test_a_running_job_defers_the_rotation(self):
        lock = self.logs / ".demo.flock"
        with lock.open("ab") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run = self.rotate()
        self.assertEqual(self.body, self.log.read_bytes())
        self.assertIn(f"- rotation of {self.log} deferred: its job", run.stdout)


if __name__ == "__main__":
    import unittest
    unittest.main()
