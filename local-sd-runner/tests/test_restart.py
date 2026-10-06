"""`runner.sh restart`: a guarded kick of the runner agent (sd:1951).

The deploy of sd:1941 was done by hand: `launchctl kickstart -k` during a
15-worker test gate, then polling `sd runner status`. The cold start stalled
on `diskutil` and launchd ran it twice more (sd:1950). `restart` asks the
daemon to drain first, through a marker file its pulse reads before it
claims: a snapshot of an idle queue can be claimed from before the kick.
The drain acknowledgement must come from the agent's own process, so a
`--config` that names another database cannot kick the agent. Only then
does it check the queue, `recovery-plan` and the load, and kick.
`launchctl` here is a stub ahead on PATH that logs its argv; `print` names
this test's pid as the agent's, and no test reaches the real one.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_runner import cli, reconciliation, runtime
from tests import test_runtime

STUB = """#!/bin/sh
echo "$*" >> {log}
if [ "$1" = print ]; then
  echo "	pid = {agent}"
fi
if [ "$1" = kickstart ] || [ "$1" = bootstrap ]; then
  exec {python} -c 'import sys; from sd_db.database import connect; from sd_db import runner as store; \\
c = connect(sys.argv[1]); store.heartbeat(c, {{"pid": 424242, "healthy": True, "interval_seconds": 10, "runner_commit": "fixture-commit"}}); c.close()' {database}
fi
exit 0
"""
REPORT = {"ok": True, "problems": [], "database_below_floor": False, "dispatch_allowed": True}


class Restart(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.root = Path(tempfile.mkdtemp(dir=self.fixture.root))
        self.log = self.root / "launchctl.log"
        self.agent = os.getpid()
        #: Whether the fake daemon ticks between the verb's waits: a pulse, then a claim when dispatch is allowed.
        self.daemon = True
        self.claimed = []
        self.enterContext(patch.object(runtime.storage, "preflight", return_value=REPORT))
        self.enterContext(patch.object(runtime.gitops, "head", return_value="fixture-pack-head"))
        self.runner.pulse(self.fixture.db)

    def stub(self):
        fake = self.root / "bin"
        fake.mkdir(exist_ok=True)
        (fake / "launchctl").write_text(STUB.format(log=self.log, agent=self.agent, python=sys.executable,
                                                    database=self.fixture.database))
        (fake / "launchctl").chmod(0o755)
        return {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "PYTHONPATH": os.pathsep.join(sys.path)}

    def step(self, seconds):
        """One daemon tick: the pulse, then a claim of the first queued row when dispatch is allowed."""
        if not self.daemon or any(call.startswith("kickstart") for call in self.calls()):
            return
        pulse = self.runner.pulse(self.fixture.db)
        if pulse["storage"]["dispatch_allowed"]:
            for row in store.queued(self.fixture.db):
                self.claimed.append(store.claim(self.fixture.db, row["id"], owner=self.runner.owner,
                                                work_root=self.fixture.config.work, retention_root=self.fixture.config.retention)["id"])
                break

    def restart(self, *, load=0.5, wait=5.0, **extra):
        with patch.dict(os.environ, self.stub()), \
                patch.object(cli.os, "getloadavg", return_value=(load, load, load)):
            return cli.restart(self.fixture.config, wait=wait, sleep=self.step, **extra)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_not_kicked(self):
        self.assertFalse([call for call in self.calls() if call.startswith("kickstart")], self.calls())
        self.assertFalse(runtime.drain_path(self.fixture.database).exists(), "the drain marker outlived the verb")

    def test_a_queued_row_is_not_claimed_between_the_check_and_the_kick(self):
        store.enqueue(self.fixture.db, [self.fixture.item], who="operator")
        result = self.restart()
        self.assertEqual(self.claimed, [], "the daemon claimed work while the verb was about to kick it")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.calls()[-1], f"kickstart -k gui/{os.getuid()}/{cli.LABEL}")
        self.assertEqual(len(store.queued(self.fixture.db)), 1, "the queued row waits for the new daemon")
        self.assertFalse(runtime.drain_path(self.fixture.database).exists())

    def test_no_drain_acknowledgement_refuses_without_a_kick(self):
        self.daemon = False
        result = self.restart(wait=0.2, clock=_Clock())
        self.assertFalse(result["ok"], result)
        self.assertIn("did not acknowledge the drain", result["reason"])
        self.assert_not_kicked()

    def test_a_database_the_agent_does_not_serve_refuses_without_a_kick(self):
        self.agent = 1
        result = self.restart()
        self.assertFalse(result["ok"], result)
        self.assertIn(f"the daemon serving {self.fixture.database} is pid {os.getpid()}; the {cli.LABEL} agent runs pid 1",
                      result["reason"])
        self.assert_not_kicked()

    def test_an_agent_with_no_process_refuses(self):
        with patch.object(cli, "agent_pid", return_value=None):
            result = self.restart()
        self.assertIn("has no running process", result["reason"])
        self.assert_not_kicked()

    def test_an_active_assignment_refuses_without_a_kick(self):
        request = self.fixture.claim()
        result = self.restart()
        self.assertFalse(result["ok"], result)
        self.assertIn("the queue is not idle", result["reason"])
        self.assertEqual(result["active"], [request["id"]])
        self.assert_not_kicked()

    def test_a_recovery_plan_discrepancy_refuses_without_a_kick(self):
        root = journal.directory(self.fixture.database)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / ("0" * 32 + ".json")).write_bytes(b"not a journal record")
        result = self.restart()
        self.assertFalse(result["ok"], result)
        self.assertIn("recovery-plan is not clean (0 entries, 1 journal issues", result["reason"])
        self.assert_not_kicked()

    def test_a_load_at_the_core_count_refuses_without_a_kick(self):
        result = self.restart(max_load=4, load=4.0)
        self.assertIn("load average 4.0 is at or above 4", result["reason"])
        self.assert_not_kicked()
        with patch.object(cli.os, "cpu_count", return_value=8):
            result = self.restart(load=8.5)
        self.assertEqual((result["ok"], result["max_load"]), (False, 8.0), result)
        self.assert_not_kicked()

    def test_stop_drains_then_boots_the_agent_out_without_a_kick(self):
        store.enqueue(self.fixture.db, [self.fixture.item], who="operator")
        result = self.restart(stop=True)
        self.assertEqual(result, {"ok": True, "stopped": True, "previous_pid": os.getpid()})
        self.assertEqual(self.claimed, [], "the daemon claimed work while the verb was about to stop it")
        self.assertEqual(self.calls()[-1], f"bootout gui/{os.getuid()}/{cli.LABEL}")
        self.assert_not_kicked()

    def test_stop_keeps_every_restart_refusal(self):
        self.fixture.claim()
        result = self.restart(stop=True)
        self.assertIn("the queue is not idle", result["reason"])
        self.assertFalse([call for call in self.calls() if call.startswith("bootout")], self.calls())

    def test_start_bootstraps_the_plist_and_waits_for_a_new_pid(self):
        with patch.object(cli, "agent_loaded", return_value=False), patch.dict(os.environ, self.stub()):
            result = cli.start(self.fixture.config, wait=5.0, sleep=lambda seconds: None)
        self.assertEqual(result["pid"], 424242, result)
        plist = self.fixture.config.home / "Library/LaunchAgents" / f"{cli.LABEL}.plist"
        self.assertEqual(self.calls()[-1], f"bootstrap gui/{os.getuid()} {plist}")
        with patch.object(cli, "agent_loaded", return_value=True):
            self.assertIn("already loaded", cli.start(self.fixture.config)["reason"])

    def test_the_load_limit_runs_as_a_script_without_sd_db(self):
        script = Path(cli.__file__).with_name("load.py")
        for limit, code in (("0.0", 1), ("100000", 0)):
            done = subprocess.run([sys.executable, "-I", "-c",
                                   "import os, runpy, sys; os.cpu_count = lambda: float(sys.argv[2]); "
                                   "sys.modules['sd_db'] = None; runpy.run_path(sys.argv[1], run_name='__main__')",
                                   str(script), limit], capture_output=True, text=True)
            self.assertEqual(done.returncode, code, done.stderr)
            self.assertEqual(json.loads(done.stdout)["ok"], code == 0)

    def test_an_agent_that_is_not_loaded_refuses(self):
        with patch.object(cli, "agent_loaded", return_value=False):
            result = self.restart()
        self.assertIn("agent is not loaded", result["reason"])
        self.assertEqual(self.calls(), [])

    def test_an_idle_queue_is_kicked_and_waits_for_a_new_healthy_pid(self):
        result = self.restart()
        self.assertEqual(result, {"ok": True, "pid": 424242, "previous_pid": os.getpid(), "runner_commit": "fixture-commit"})

    def test_a_marker_removed_before_the_kick_refuses_without_a_kick(self):
        """Only the verb's own marker permits the kick: a hand-removed one may have let the daemon claim."""
        marker = runtime.drain_path(self.fixture.database)
        real = reconciliation.plan

        def removed(config):
            clean = real(config)
            marker.unlink()
            return clean

        with patch.object(reconciliation, "plan", removed):
            result = self.restart()
        self.assertFalse(result["ok"], result)
        self.assertIn("removed or replaced before the kick", result["reason"])
        self.assert_not_kicked()

    def test_a_drain_holds_while_its_restart_lives_however_long(self):
        """Review: the daemon could claim once wall time passed the marker's expiry, mid-restart."""
        store.enqueue(self.fixture.db, [self.fixture.item], who="operator")
        real, now = reconciliation.plan, time.time

        def suspended(config):
            clean = real(config)
            # A day passes while the verb sleeps between its checks; the daemon ticks.
            with patch.object(time, "time", lambda: now() + 86400):
                self.step(0)
            return clean

        with patch.object(reconciliation, "plan", suspended):
            result = self.restart()
        self.assertEqual(self.claimed, [], "the daemon claimed while the restart that drained it still lived")
        self.assertTrue(result["ok"], result)

    def test_the_marker_is_published_once_and_never_renewed(self):
        """Review: a renewal could rewrite a lapsed marker, reviving the same token after a claim."""
        real_plan, real_publish = reconciliation.plan, cli._publish
        writes = []

        def slow(config):
            time.sleep(0.3)
            return real_plan(config)

        def counted(marker, token, *rest):
            writes.append(token)
            return real_publish(marker, token, *rest)

        # The renewal interval the reviewed code read; a renewing verb would write again here.
        with patch.object(cli, "DRAIN_RENEW", 0.05, create=True), \
                patch.object(cli, "_publish", counted), patch.object(reconciliation, "plan", slow):
            self.assertTrue(self.restart()["ok"])
        self.assertEqual(len(writes), 1, "the drain token was written more than once")

    def test_a_marker_whose_restart_died_is_ignored_and_replaced(self):
        """The drain ends when its restart dies: the kernel drops the restart lock, not a clock."""
        marker = runtime.drain_path(self.fixture.database)
        script = ("import json, sys; from pathlib import Path; from sd_db import runner_journal as journal\n"
                  "with journal.lock(Path(sys.argv[1]), blocking=False):\n"
                  "    Path(sys.argv[2]).write_text(json.dumps({'token': 'dead'}))\n")
        subprocess.run([sys.executable, "-c", script, str(runtime.restart_lock_path(self.fixture.database)),
                        str(marker)], check=True, env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
        self.assertIsNone(self.runner.pulse(self.fixture.db)["drain"], "a dead restart's marker still drains")
        result = self.restart()
        self.assertTrue(result["ok"], result)
        self.assertFalse(marker.exists())

    def test_a_second_restart_during_the_first_refuses(self):
        """The second starts after the first holds the lock, before the first's marker exists."""
        real = cli._publish
        second = []

        def racing(marker, token):
            if not second:
                second.append(cli.restart(self.fixture.config, wait=5.0, sleep=self.step))
            return real(marker, token)

        with patch.object(cli, "_publish", racing), patch.object(cli, "RESTART_LOCK_WAIT", 0.1):
            first = self.restart()
        self.assertIn("another restart is running", second[0].get("reason", ""), second[0])
        self.assertTrue(first["ok"], first)
        self.assertEqual(len([call for call in self.calls() if call.startswith("kickstart")]), 1, self.calls())

    def test_a_leftover_partial_file_does_not_block_the_marker(self):
        runtime.drain_path(self.fixture.database).with_suffix(".partial").mkdir()
        self.assertTrue(self.restart()["ok"])

    def test_main_passes_the_options(self):
        output = io.StringIO()
        with patch.object(cli, "restart", return_value={"ok": False, "reason": "fixture"}) as restart, \
                contextlib.redirect_stdout(output):
            code = cli.main(["restart", "--max-load", "3", "--wait", "7"])
        self.assertEqual((code, restart.call_args.kwargs), (1, {"max_load": 3.0, "wait": 7.0}))
        with patch.object(cli, "restart", return_value={"ok": True}) as restart, contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["stop", "--max-load", "5"]), 0)
        self.assertEqual(restart.call_args.kwargs, {"max_load": 5.0, "stop": True})
        with patch.object(cli, "start", return_value={"ok": True}) as start, contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["start", "--wait", "9"]), 0)
        self.assertEqual(start.call_args.kwargs, {"wait": 9.0})


class Drain(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.enterContext(patch.object(runtime.storage, "preflight", return_value=REPORT))
        self.enterContext(patch.object(runtime.gitops, "head", return_value="fixture-pack-head"))

    def held(self):
        """Stand in for a live `runner.sh restart`: hold its lock, as the verb does throughout."""
        return self.enterContext(journal.lock(runtime.restart_lock_path(self.fixture.database), blocking=False))

    def test_a_drain_marker_withholds_dispatch_and_is_acknowledged(self):
        self.held()
        marker = runtime.drain_path(self.fixture.database)
        marker.write_text(json.dumps({"token": "fixture-token"}))
        pulse = self.runner.pulse(self.fixture.db)
        self.assertEqual(pulse["drain"], "fixture-token")
        self.assertIs(pulse["storage"]["dispatch_allowed"], False)
        self.assertIs(pulse["healthy"], True, "a drain is not a fault")
        store.enqueue(self.fixture.db, [self.fixture.item], who="operator")
        with patch.object(store, "claim", side_effect=AssertionError("a draining tick claimed")):
            self.runner.tick(self.fixture.db)

    def test_a_marker_without_a_live_restart_is_ignored(self):
        marker = runtime.drain_path(self.fixture.database)
        marker.write_text(json.dumps({"token": "orphan"}))
        pulse = self.runner.pulse(self.fixture.db)
        self.assertIsNone(pulse["drain"])
        self.assertIs(pulse["storage"]["dispatch_allowed"], True)

    def test_an_unreadable_marker_is_ignored_even_under_a_live_restart(self):
        self.held()
        marker = runtime.drain_path(self.fixture.database)
        for content in ("not json", json.dumps({"token": ""}), json.dumps({"by": "runner.sh restart"})):
            marker.write_text(content)
            pulse = self.runner.pulse(self.fixture.db)
            self.assertIsNone(pulse["drain"], content)
            self.assertIs(pulse["storage"]["dispatch_allowed"], True)
    def test_a_lock_no_restart_could_take_does_not_drain(self):
        """A lock that refuses for any reason but contention refuses the verb too, so no kick follows."""
        runtime.restart_lock_path(self.fixture.database).mkdir()
        runtime.drain_path(self.fixture.database).write_text(json.dumps({"token": "unreachable"}))
        self.assertIsNone(self.runner.pulse(self.fixture.db)["drain"])


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.1
        return self.now


if __name__ == "__main__":
    unittest.main()
