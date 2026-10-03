"""The daemon's err log: a time on every line, and one line when health flips (sd:1953).

launchd sends `serve`'s stderr to the agent's `.err` file. It held thousands
of `runner: ...` lines with no time on any of them, and nothing at all when
the heartbeat turned unhealthy or healthy again.
"""

import contextlib
import io
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_runner import cli, runtime

from tests import test_runtime

STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4}"


class StampedLines(unittest.TestCase):
    def test_every_line_starts_with_the_local_time(self):
        sink = io.StringIO()
        stamped = cli.StampedLines(sink, clock=lambda: time.struct_time((2026, 10, 3, 4, 5, 6, 5, 276, 0)))
        stamped.write("runner: first\nrunner: sec")
        stamped.write("ond\n")
        self.assertEqual(sink.getvalue().splitlines(), ["2026-10-03T04:05:06" + time.strftime("%z", stamped.clock()) + " runner: first",
                                                       "2026-10-03T04:05:06" + time.strftime("%z", stamped.clock()) + " runner: second"])

    def test_a_line_without_its_newline_waits_and_close_writes_it(self):
        sink = io.StringIO()
        stamped = cli.StampedLines(sink)
        stamped.write("Traceback (most recent call last):")
        self.assertEqual(sink.getvalue(), "", "half a line is not stamped twice")
        stamped.close()
        self.assertRegex(sink.getvalue(), rf"^{STAMP} Traceback \(most recent call last\):\n$")

    def test_serve_refusal_is_stamped_and_other_verbs_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "runner.json"
            config.write_text(json.dumps({"database": str(Path(tmp) / "sd.db"), "work": tmp, "retention": tmp, "pack": tmp}))
            for verb in ("serve", "once"):
                with self.subTest(verb=verb), patch.object(cli.Runner, "serve", side_effect=OSError("work volume refused")), \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(cli.main([verb, "--config", str(config)]), 1)
                    self.assertIs(sys.stderr, err, "the stream is put back after the verb")
                self.assertRegex(err.getvalue(), rf"^{STAMP} runner: work volume refused\n$")
            with patch.object(cli.storage, "preflight", side_effect=OSError("preflight refused")), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(["preflight", "--config", str(config)]), 1)
            self.assertEqual(err.getvalue(), "runner: preflight refused\n")

    def test_the_launcher_stamps_its_missing_runtime_line(self):
        import os
        import subprocess
        launcher = Path(__file__).resolve().parents[1] / "runner.sh"
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "HOME": tmp, "SD_RUNNER_PYTHON": str(Path(tmp) / "no-such-python")}
            done = subprocess.run(["sh", str(launcher), "serve"], capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertRegex(done.stderr, rf"^{STAMP} runner: missing installed runtime ")


class HealthFlips(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.Fixture(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner, self.db = self.fixture.runner, self.fixture.db
        self.preflight = self.enterContext(patch.object(runtime.storage, "preflight"))
        self.enterContext(patch.object(runtime.gitops, "head", return_value="fixture-head"))

    @staticmethod
    def report(*problems):
        return {"ok": not problems, "problems": list(problems), "database_below_floor": False, "dispatch_allowed": False}

    def ticks(self, count):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            for _ in range(count):
                self.runner.tick(self.db)
        return err.getvalue().splitlines()

    def test_one_line_when_health_goes_and_one_when_it_returns(self):
        self.preflight.side_effect = [self.report(), self.report("work volume below free floor"),
                                      self.report("work volume below free floor", "a second problem"), self.report(), self.report()]
        self.assertEqual(self.ticks(5), ["runner: unhealthy: work volume below free floor", "runner: healthy again"])

    def test_a_tick_that_ends_unhealthy_is_one_line_however_often_it_repeats(self):
        # The pulse writes a healthy heartbeat and the restore hold then writes
        # an unhealthy one, in the same tick. The line names the tick's end.
        self.preflight.return_value = self.report()
        with patch.object(self.runner, "restore_holds", return_value=[{"reason": "fixture restore hold"}]):
            self.assertEqual(self.ticks(3), ["runner: unhealthy: fixture restore hold"])
        self.assertEqual(self.ticks(1), ["runner: healthy again"])

    def test_reasons_are_one_line(self):
        self.preflight.return_value = self.report("first\nsecond")
        self.assertEqual(self.ticks(1), ["runner: unhealthy: first second"])


if __name__ == "__main__":
    unittest.main()
