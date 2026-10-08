"""Jev's route is both halves or neither, and its score is a real number (sd:1238).

notify.sh asks Jev two things: which channels (`choice`) and how urgent
(`score`). It applied each answer on its own, so a `desk` pick with a failed
score sent a banner-only alert at the default priority, and a failed pick
with a good score sent a Jev priority down the default channels: a route
nobody chose (77951db62483). The same rule already holds for the caller's own
flags: an explicit -c or -p skips Jev entirely.

The score check passed `.` and `1..2` to awk, which read them as 0 and 1,
and refused `-1` instead of clamping it to `min` (a7e3487c30be).

Runs on the fixture in test_notify.py: a copy of notify.sh beside a stub jev.
"""

import hashlib
import unittest

from test_notify import NotifyTestCase


class BothHalvesOrNeitherTest(NotifyTestCase):
    def test_a_pick_without_a_score_keeps_the_default_route(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_CHOICE": "desk", "JEV_STUB_SCORE_FAIL": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ntfy_sent(), "a desk pick applied without its score")
        self.assertEqual(self.priority_header(), "default")
        self.assertIn("notify.sh: jev gave no complete route; using the defaults", r.stderr)
        self.assertNotIn("jev routing on", r.stderr)

    def test_a_score_without_a_pick_keeps_the_default_route(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_CHOICE_FAIL": "1", "JEV_STUB_SCORE": "3.0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ntfy_sent())
        self.assertEqual(self.priority_header(), "default", "a score applied without its pick")
        self.assertIn("notify.sh: jev gave no complete route; using the defaults", r.stderr)

    def test_both_halves_apply_together(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_CHOICE": "phone", "JEV_STUB_SCORE": "3.0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ntfy_sent())
        self.assertEqual(self.priority_header(), "high")
        self.assertIn("notify.sh: jev routing on: channels=local,ntfy priority=high", r.stderr)


class ScoreIsANumberTest(NotifyTestCase):
    def test_a_negative_score_is_clamped_to_min(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_SCORE": "-1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.priority_header(), "min")

    def test_a_score_that_is_not_a_number_is_refused(self):
        for raw in (".", "1..2", "-", "1.2.3", "2\n4"):
            with self.subTest(raw=raw):
                self.fx.curl_log.unlink(missing_ok=True)
                r = self.fx.run(["msg"], env={"JEV_STUB_SCORE": raw})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.priority_header(), "default", f"{raw!r} reached the priority")


class TheLedgerCanJoinAnOutcome(NotifyTestCase):
    """Each half names the notification by hash, and one run is one `JEV_RUN` (sd:2953)."""

    def test_each_half_names_the_notification_by_hash_and_the_run_is_one_id(self):
        r = self.fx.run(["-t", "Disk nearly full", "pat@example.org wrote"], env={})
        self.assertEqual(r.returncode, 0, r.stderr)
        asked = [ln.split("\t")[1:] for ln in self.jev_calls()
                 if ln.split("\t")[1] in ("choice", "score")]
        self.assertEqual([argv[0] for argv in asked], ["choice", "score"])
        digest = hashlib.sha256(b"Disk nearly full\npat@example.org wrote\n").hexdigest()[:16]
        self.assertEqual([argv[argv.index("--subject") + 1] for argv in asked],
                         [f"notify:{digest}:route", f"notify:{digest}:priority"])
        runs = set(self.fx.read(self.fx.root / "jev.log.run").split())
        self.assertEqual(len(runs), 1, runs)
        self.assertRegex(runs.pop(), r"^notify-\d{8}T\d{6}-[0-9a-f]{4}$")

    def test_a_run_that_called_notify_keeps_its_own_id(self):
        self.fx.run(["msg"], env={"JEV_RUN": "health-check-20261007T120000-ab12"})
        self.assertEqual(set(self.fx.read(self.fx.root / "jev.log.run").split()),
                         {"health-check-20261007T120000-ab12"})


if __name__ == "__main__":
    unittest.main()
