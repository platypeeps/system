"""`runner.sh restart`: a guarded kick of the runner agent (sd:1951).

The deploy of sd:1941 was done by hand: `launchctl kickstart -k` during a
15-worker test gate, then polling `sd runner status`. The cold start stalled
on `diskutil` and launchd ran it twice more (sd:1950). `restart` refuses with
a reason while an assignment is active, while `recovery-plan` is not clean,
or while the load average is at or above the core count, and touches
launchd only after all three pass. `launchctl` here is a stub ahead on PATH
that logs its argv, so no test reaches the real one.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_db.database import connect
from sd_runner import cli
from tests import test_runtime

STUB = """#!/bin/sh
echo "$*" >> {log}
if [ "$1" = kickstart ] && [ -n "{heartbeat}" ]; then
  exec {python} -c 'import sys; from sd_db.database import connect; from sd_db import runner as store; \\
c = connect(sys.argv[1]); store.heartbeat(c, {{"pid": 424242, "healthy": True, "interval_seconds": 10, "runner_commit": "fixture-commit"}}); c.close()' {database}
fi
exit 0
"""


class Restart(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = Path(tempfile.mkdtemp(dir=self.fixture.root))
        self.config = self.root / "runner.json"
        config = self.fixture.config
        self.config.write_text(json.dumps({"database": str(config.database), "work": str(config.work),
                                           "retention": str(config.retention), "pack": str(config.pack)}))
        self.log = self.root / "launchctl.log"
        store.heartbeat(self.fixture.db, {"pid": 111, "healthy": True, "interval_seconds": 10})

    def stub(self, *, heartbeat):
        fake = self.root / "bin"
        fake.mkdir(exist_ok=True)
        (fake / "launchctl").write_text(STUB.format(log=self.log, heartbeat="yes" if heartbeat else "",
                                                    python=sys.executable, database=self.fixture.database))
        (fake / "launchctl").chmod(0o755)
        return {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
                "PYTHONPATH": os.pathsep.join(sys.path)}

    def restart(self, *extra, heartbeat=True, load=0.5):
        output = io.StringIO()
        with patch.dict(os.environ, self.stub(heartbeat=heartbeat)), \
                patch.object(cli.os, "getloadavg", return_value=(load, load, load)), \
                contextlib.redirect_stdout(output):
            code = cli.main(["restart", "--config", str(self.config), *extra])
        return code, json.loads(output.getvalue())

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_launchd_untouched(self):
        self.assertFalse([call for call in self.calls() if not call.startswith("print ")], self.calls())

    def test_an_active_assignment_refuses_without_a_kick(self):
        request = self.fixture.claim()
        code, body = self.restart()
        self.assertEqual(code, 1, body)
        self.assertIn("the queue is not idle", body["reason"])
        self.assertEqual(body["active"], [request["id"]])
        self.assert_launchd_untouched()

    def test_a_recovery_plan_discrepancy_refuses_without_a_kick(self):
        root = journal.directory(self.fixture.database)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / ("0" * 32 + ".json")).write_bytes(b"not a journal record")
        code, body = self.restart()
        self.assertEqual(code, 1, body)
        self.assertIn("recovery-plan is not clean (0 entries, 1 journal issues", body["reason"])
        self.assert_launchd_untouched()

    def test_a_load_at_the_core_count_refuses_without_a_kick(self):
        code, body = self.restart("--max-load", "4", load=4.0)
        self.assertEqual(code, 1, body)
        self.assertIn("load average 4.0 is at or above 4", body["reason"])
        self.assert_launchd_untouched()
        with patch.object(cli.os, "cpu_count", return_value=8):
            code, body = self.restart(load=8.5)
        self.assertEqual((code, body["max_load"]), (1, 8.0), body)
        self.assert_launchd_untouched()

    def test_an_agent_that_is_not_loaded_refuses(self):
        with patch.object(cli, "agent_loaded", return_value=False):
            code, body = self.restart()
        self.assertEqual(code, 1, body)
        self.assertIn("agent is not loaded", body["reason"])
        self.assertEqual(self.calls(), [])

    def test_an_idle_queue_is_kicked_and_waits_for_a_new_healthy_pid(self):
        code, body = self.restart()
        self.assertEqual(code, 0, body)
        self.assertEqual(body, {"ok": True, "pid": 424242, "previous_pid": 111, "runner_commit": "fixture-commit"})
        self.assertEqual(self.calls()[-1], f"kickstart -k gui/{os.getuid()}/{cli.LABEL}")

    def test_no_new_heartbeat_within_the_wait_fails_naming_it(self):
        code, body = self.restart("--wait", "0.2", heartbeat=False)
        self.assertEqual(code, 1, body)
        self.assertIn("no healthy heartbeat with a new pid within 0.2s", body["reason"])
        self.assertEqual(body["pid"], 111)


if __name__ == "__main__":
    unittest.main()
