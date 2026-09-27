"""The digest, and the local order of this week's failures (sd:1478).

`weekly-digest.sh check` runs against a fixture cron-log tree
(`WEEKLY_DIGEST_CRON_LOGS`) and a fixture machine-setup
(`WEEKLY_DIGEST_MSETUP`). `brew`, `mas` and `hostname` are stubbed on `PATH`
so two runs produce the same bytes -- `brew outdated` does not promise that.
Nothing reaches the network: the order is computed here, from the logs.

Python `unittest` and not sh for the reason CLAUDE.md gives for the sibling
suites: the CI wrapper asserts a unittest summary and refuses skips.
"""

import datetime
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "weekly-digest.sh"
HOSTNAME = "fixturehost"
RUN_TIMEOUT = 120


def _stub(d, name, body):
    path = d / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


class FailureOrder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="weekly-digest-order."))
        cls.home = cls.tmp / "home"
        cls.home.mkdir()
        cls.bin = cls.tmp / "bin"
        cls.bin.mkdir()
        _stub(cls.bin, "brew", "exit 0\n")
        _stub(cls.bin, "mas", "exit 0\n")
        _stub(cls.bin, "hostname", 'echo "%s"\n' % HOSTNAME)
        cls.msetup = _stub(cls.bin, "machine-setup.sh", 'echo "drift : 0"\n')

        today = datetime.date.today()
        cls.day = [(today - datetime.timedelta(days=n)).isoformat() for n in range(7)]
        old = (today - datetime.timedelta(days=30)).isoformat()

        # Four jobs, each settling one step of the order:
        #   twice    two failures this week
        #   recent   one failure, the newest of the single failures
        #   earlier  one failure, older than `recent`
        #   stuck    one failure, the oldest; its run log shows three failures
        #            in a row, which the order does not read
        cls.lines = {
            "stuck": cls.failure(5, "04:00:00", "stuck", 1),
            "twice-old": cls.failure(4, "02:00:00", "twice", 2),
            "twice-new": cls.failure(1, "02:00:00", "twice", 3),
            "recent": cls.failure(1, "06:00:00", "recent", 4),
            "earlier": cls.failure(3, "06:00:00", "earlier", 5),
        }
        cls.logs = cls.make_logs("logs", [
            cls.lines["earlier"], cls.lines["twice-old"], cls.lines["stuck"],
            cls.lines["twice-new"], cls.lines["recent"],
            "%sT01:00:00-0600 old-job FAILED rc=1 (log: logs/old-job.log)" % old,
        ], {
            "stuck": ["done", "FAILED", "FAILED", "FAILED"],
            "twice": ["FAILED", "FAILED", "done"],
            "recent": ["done", "FAILED", "done"],
            "earlier": ["FAILED", "done"],
        })
        cls.ordered = cls.run_check(cls.logs)

        # A line in a shape `notify_failure` never writes: the log's own order
        # stands, and a note says why.
        cls.odd = "%sT03:00:00-0600 something else entirely" % cls.day[2]
        cls.unshaped_logs = cls.make_logs("unshaped", [
            cls.lines["earlier"], cls.odd, cls.lines["stuck"],
        ], {"stuck": ["FAILED", "FAILED"], "earlier": ["FAILED"]})
        cls.unshaped = cls.run_check(cls.unshaped_logs)

        # One failure in the week still gets its label; one unshaped line
        # still gets its note.
        cls.single = cls.run_check(cls.make_logs("single", [cls.lines["stuck"]],
                                                 {"stuck": ["FAILED", "FAILED"]}))
        cls.single_odd = cls.run_check(cls.make_logs("single-odd", [cls.odd], {}))

        # Four fields, without the `(log: ...)` tail `notify_failure` always
        # writes: not the trusted shape, so the log's order stands. Ordered,
        # the newest line (`four`) would move to the top.
        cls.four = "%sT03:00:00-0600 four FAILED rc=1" % cls.day[1]
        cls.four_run = cls.run_check(cls.make_logs("four", [
            cls.lines["earlier"], cls.four, cls.lines["stuck"],
        ], {}))

        # Across a daylight-saving change the offset differs: 01:15-0700 is
        # 08:15 UTC, later than 01:30-0600 (07:30 UTC), though the raw string
        # sorts the other way. Inside one job the same holds.
        cls.dst = {
            "fall": "%sT01:15:00-0700 fall FAILED rc=1 (log: logs/fall.log)" % cls.day[2],
            "spring": "%sT01:30:00-0600 spring FAILED rc=1 (log: logs/spring.log)" % cls.day[2],
            "both-mdt": "%sT01:30:00-0600 both FAILED rc=1 (log: logs/both.log)" % cls.day[3],
            "both-mst": "%sT01:15:00-0700 both FAILED rc=1 (log: logs/both.log)" % cls.day[3],
        }
        cls.dst_run = cls.run_check(cls.make_logs("dst", list(cls.dst.values()), {}))

        # Equal count and newest failure: the name decides.
        cls.tie = {
            job: "%sT05:00:00-0600 %s FAILED rc=1 (log: logs/%s.log)" % (cls.day[2], job, job)
            for job in ("zulu", "alpha", "mike")
        }
        cls.tie_run = cls.run_check(cls.make_logs("tie", list(cls.tie.values()), {}))


    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def failure(cls, days_ago, clock, job, rc):
        return "%sT%s-0600 %s FAILED rc=%d (log: logs/%s.log)" % (cls.day[days_ago], clock, job, rc, job)

    @classmethod
    def make_logs(cls, name, failures, runs):
        logs = cls.tmp / name
        logs.mkdir()
        (logs / "failures.log").write_text("\n".join(failures) + "\n")
        for job, outcomes in runs.items():
            rows = []
            for n, outcome in enumerate(outcomes):
                stamp = "%sT0%d:00:00-0600" % (cls.day[6], n)
                word = "done" if outcome == "done" else "FAILED rc=1"
                rows.append("[%s] %s %s" % (job, stamp, word))
            (logs / ("%s.log" % job)).write_text("\n".join(rows) + "\n")
        return logs

    @classmethod
    def run_check(cls, logs):
        env = dict(os.environ)
        env.update({
            "HOME": str(cls.home),
            "PATH": "%s:%s" % (cls.bin, env.get("PATH", "")),
            "WEEKLY_DIGEST_CRON_LOGS": str(logs),
            "WEEKLY_DIGEST_MSETUP": str(cls.msetup),
        })
        proc = subprocess.run(
            ["sh", str(SCRIPT), "check"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, timeout=RUN_TIMEOUT)
        return {"rc": proc.returncode, "out": proc.stdout, "err": proc.stderr}

    @staticmethod
    def failure_block(out):
        lines = out.split("\n")
        start = lines.index("failures this week:") + 1
        block = []
        for line in lines[start:]:
            if not line.startswith("  "):
                break
            block.append(line[2:])
        return block

    def test_the_digest_still_reports_the_week(self):
        run = self.ordered
        self.assertEqual(run["rc"], 0, run["err"])
        self.assertIn("job activity, last 7 days (runs / failures):", run["out"])
        self.assertNotIn("old-job", run["out"])

    def test_failures_order_by_count_then_recency(self):
        block = self.failure_block(self.ordered["out"])
        expected = ["twice-new", "twice-old", "recent", "earlier", "stuck"]
        self.assertEqual([line.split("  [")[0] for line in block], [self.lines[k] for k in expected])

    def test_each_line_names_why_it_sits_where_it_does(self):
        block = self.failure_block(self.ordered["out"])
        self.assertEqual(block[0], self.lines["twice-new"] + "  [2 this week]")
        self.assertEqual(block[2], self.lines["recent"] + "  [1 this week]")
        self.assertEqual(block[4], self.lines["stuck"] + "  [1 this week]")

    def test_no_failure_is_dropped_or_reworded(self):
        block = self.failure_block(self.ordered["out"])
        self.assertEqual(sorted(line.split("  [")[0] for line in block), sorted(self.lines.values()))

    def test_an_unshaped_line_keeps_the_logs_order_and_says_so(self):
        run = self.unshaped
        self.assertEqual(run["rc"], 0, run["err"])
        self.assertEqual(self.failure_block(run["out"]),
                         [self.lines["earlier"], self.odd, self.lines["stuck"]])
        self.assertIn("failure order skipped: 1 failure line(s) not in the cron failure shape", run["out"])


    def test_a_single_failure_carries_its_label(self):
        run = self.single
        self.assertEqual(run["rc"], 0, run["err"])
        self.assertEqual(self.failure_block(run["out"]),
                         [self.lines["stuck"] + "  [1 this week]"])

    def test_a_single_unshaped_line_says_so(self):
        run = self.single_odd
        self.assertEqual(run["rc"], 0, run["err"])
        self.assertEqual(self.failure_block(run["out"]), [self.odd])
        self.assertIn("failure order skipped: 1 failure line(s) not in the cron failure shape", run["out"])

    def test_a_four_field_line_keeps_the_logs_order_and_says_so(self):
        run = self.four_run
        self.assertEqual(run["rc"], 0, run["err"])
        self.assertEqual(self.failure_block(run["out"]),
                         [self.lines["earlier"], self.four, self.lines["stuck"]])
        self.assertIn("failure order skipped: 1 failure line(s) not in the cron failure shape", run["out"])

    def test_recency_compares_actual_time_across_offsets(self):
        block = self.failure_block(self.dst_run["out"])
        expected = ["both-mst", "both-mdt", "fall", "spring"]
        self.assertEqual([line.split("  [")[0] for line in block], [self.dst[k] for k in expected])

    def test_a_full_tie_orders_by_job_name(self):
        block = self.failure_block(self.tie_run["out"])
        self.assertEqual([line.split("  [")[0] for line in block],
                         [self.tie[k] for k in ("alpha", "mike", "zulu")])


if __name__ == "__main__":
    unittest.main()
