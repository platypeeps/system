"""Run (sd:5886): a button that gave a line to copy posts /api/run, and the server runs a fixed command from its registry.

What this slice promises: the page names an action and ids, never a command. Each id is read against what the server
holds -- an installed job, a configured storage folder, a mount, a port number, a tile -- and a refusal runs nothing.
A step that ran answers 200 with its exit and its output's tail, whatever the exit; the journal keeps that exit. A step
past its bound is 504 with `outcome: unknown`. Stubs on PATH stand in for launchctl, lsof, df, du and sd: no test
reaches launchd, the network or the live database.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

from sd_db.workflow import WorkflowError

from sd_dashboard import health_collectors, home_screen, run_actions
from test_workflow_actions import BrowserSession

STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "{log}"
{body}
"""


class Jobs:
    """launchd as the library reads it: one installed job, `nightly-sync`."""

    def inspect(self, name):
        if name != "nightly-sync":
            raise WorkflowError("job is not supported: installed configuration is unavailable or invalid")
        return {"name": name, "label": "fixture." + name, "service": "gui/501/fixture." + name, "schedule": [],
                "state": "idle", "pid": None, "last_exit": 0, "last_signal": None, "reason": "", "_observation": name}

    def names(self):
        return ["nightly-sync"]


class Run(BrowserSession):
    def setUp(self):
        self.backend = Jobs()
        super().setUp()
        root = Path(self.tmp.name).resolve()
        self.log, self.bin = root / "ran", root / "bin"
        self.bin.mkdir()
        self.stub("launchctl", 'echo "gui/501/fixture.nightly-sync = {"; echo "}"')
        self.stub("lsof", "exit 1")
        self.stub("df", 'echo "Filesystem Size Used Avail Capacity Mounted on"; echo "/dev/disk3s5 460Gi 300Gi 160Gi 66% $2"')
        self.stub("du", 'printf "10\\t$4/small\\n4194304\\t$4/big\\n4194314\\t$4\\n"')
        self.stub("sd", 'echo "scanned 3 sessions"')
        path = patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}"})
        path.start()
        self.addCleanup(path.stop)
        # The checkout's own scripts, stubbed: cron-jobs.sh and machine-setup.sh print what they were asked.
        self.checkout = root / "checkout"
        for script, body in (("local-cron-jobs/cron-jobs.sh", 'echo "log of $2, last $3 lines"'),
                             ("local-machine-setup/machine-setup.sh", 'echo "✓ disk"; exit 2')):
            (self.checkout / script).parent.mkdir(parents=True)
            (self.checkout / script).write_text(STUB.replace("{log}", str(self.log)).replace("{body}", body))
        checkout = patch.object(run_actions, "CHECKOUT", self.checkout)
        checkout.start()
        self.addCleanup(checkout.stop)
        self.store = root / "store"
        (self.store / "captures").mkdir(parents=True)
        storage = patch.object(health_collectors, "storage_list", lambda connection: (f"storage|{self.store}", "file", ""))
        storage.start()
        self.addCleanup(storage.stop)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(STUB.replace("{log}", str(self.log)).replace("{body}", body))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def ran(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run_(self, action, **ids):
        status, _, body = self.post("/api/run", {"action": action, **ids})
        return status, body


class TheReads(Run):
    def test_each_read_runs_its_fixed_command_and_answers_its_exit_and_output(self):
        status, body = self.run_("job.log", job="nightly-sync")
        self.assertEqual((status, body["exit"], body["output"]), (200, 0, "log of nightly-sync, last 200 lines"))
        self.assertEqual(body["line"], "local-cron-jobs/cron-jobs.sh logs nightly-sync")
        status, body = self.run_("job.print", job="nightly-sync")
        self.assertEqual((status, body["line"]), (200, "launchctl print gui/501/fixture.nightly-sync"))
        status, body = self.run_("port.lsof", port=8080)
        # lsof exits 1 when nothing listens: an answer, worded, with its exit kept.
        self.assertEqual((status, body["exit"], body["output"]), (200, 1, "Nothing listens on TCP port 8080."))
        status, body = self.run_("volume.df", path=str(self.store))
        self.assertEqual((status, body["exit"]), (200, 0))
        status, body = self.run_("storage.du", path=str(self.store))
        self.assertEqual(status, 200)
        self.assertEqual([line.split()[-1] for line in body["output"].splitlines()],
                         [str(self.store), f"{self.store}/big", f"{self.store}/small"])
        self.assertIn("4.0 GiB", body["output"])
        status, body = self.run_("skill.scan")
        self.assertEqual((status, body["output"]), (200, "scanned 3 sessions"))
        status, body = self.run_("check.run", row="doc:0123456789ab")
        self.assertEqual((status, body["exit"], body["output"]), (200, 2, "✓ disk"))
        self.assertEqual(self.ran(), ["cron-jobs.sh logs nightly-sync 200", "launchctl print gui/501/fixture.nightly-sync",
                                      "lsof -nP -iTCP:8080 -sTCP:LISTEN", f"df -h {self.store}", f"du -k -d 1 {self.store}",
                                      "sd skill scan", "machine-setup.sh doctor"])

    def test_the_journal_keeps_each_runs_own_exit(self):
        self.run_("job.log", job="nightly-sync")
        self.run_("port.lsof", port=8080)
        self.assertEqual(self.journalled(), [("Run · job.log nightly-sync", 0), ("Run · port.lsof 8080", 1)])

    def test_a_refusal_runs_nothing_and_records_nothing(self):
        before = self.snapshot()
        for ids, said in (({"action": "rm"}, "Choose one of"),
                          ({"action": "job.log", "job": "nightly-sync", "argv": ["sh"]}, "job.log takes job"),
                          ({"action": "job.log", "job": "other"}, "job is not supported"),
                          ({"action": "job.print", "job": "../x"}, "is not a job name"),
                          ({"action": "port.lsof", "port": "80; rm -rf /"}, "A port is a whole number"),
                          ({"action": "port.lsof", "port": 70000}, "A port is a whole number"),
                          ({"action": "port.lsof", "port": True}, "A port is a whole number"),
                          ({"action": "storage.du", "path": "/"}, "is not a storage folder Health reads"),
                          ({"action": "storage.du", "path": f"{self.store}/captures/../../.."}, "is not a storage folder"),
                          ({"action": "volume.df", "path": f"{self.store}/captures"}, "is not a mounted volume"),
                          ({"action": "check.run", "row": "wt:ok"}, "Re-check reads Health again")):
            with self.subTest(ids=ids):
                status, _, body = self.post("/api/run", ids)
                self.assertEqual(status, 400)
                self.assertIn(said, body["error"])
        self.assertEqual(self.ran(), [])
        self.assertEqual(self.snapshot(), before)

    def test_a_folder_inside_a_storage_folder_is_read_and_a_symlink_out_is_not(self):
        status, _ = self.run_("storage.du", path=str(self.store / "captures"))
        self.assertEqual(status, 200)
        (self.store / "out").symlink_to(Path(self.tmp.name) / "bin")
        status, body = self.run_("storage.du", path=str(self.store / "out"))
        self.assertEqual((status, "is not a storage folder" in body["error"]), (400, True))

    def test_an_unread_storage_list_refuses_rather_than_guessing(self):
        with patch.object(health_collectors, "storage_list", lambda connection: (None, "missing", "the row was not read")):
            status, body = self.run_("storage.du", path=str(self.store))
        self.assertEqual(status, 400)
        self.assertIn("the row was not read", body["error"])

    def test_the_credentials_probe_runs_in_the_server_and_describes_each_probe(self):
        probes = {"probes": [{"name": "GitHub PAT", "ok": True}, {"name": "gh CLI", "ok": False}]}
        with patch("sd_db.credentials.check", lambda connection, env: probes), \
             patch("sd_db.credentials.describe", lambda probe: f"{probe['name']}: {'ok' if probe['ok'] else 'failed'}"):
            for ids in ({"action": "credentials.probe"}, {"action": "check.run", "row": "cred:stale"}):
                status, _, body = self.post("/api/run", ids)
                self.assertEqual((status, body["output"], body["line"]), (200, "GitHub PAT: ok\ngh CLI: failed", "sd-db credentials"))

    def test_read_state_reads_only_a_tile_in_the_tile_list(self):
        tiles = "tile|Water|binary_sensor.water_leak|Leak\n"
        with patch.object(home_screen, "_current", lambda connection, config: ("file", tiles)), \
             patch.object(home_screen, "_settings", lambda environ: ("http://192.0.2.10:8123", "token")), \
             patch.object(home_screen, "_reread", lambda base, token, entity, timeout: ({"value": "off", "changed": "x"}, "")):
            status, body = self.run_("sensor.read", entity_id="binary_sensor.water_leak")
            self.assertEqual((status, body["exit"]), (200, 0))
            self.assertIn('"value": "off"', body["output"])
            self.assertNotIn("token", body["output"])
            status, body = self.run_("sensor.read", entity_id="lock.front_door")
            self.assertEqual(status, 400)
            self.assertIn("is not a tile in the tile list", body["error"])


class TheBounds(Run):
    def test_a_step_past_its_bound_is_504_and_its_outcome_unknown(self):
        self.stub("slow", "sleep 5")
        status, body = run_actions.run("job.log", run_actions.Step(line="slow", argv=("slow",), seconds=1))
        self.assertEqual((status, body["outcome"]), (504, "unknown"))

    def test_a_long_output_keeps_its_tail(self):
        self.stub("loud", f"head -c {run_actions.OUTPUT_CHARS + 100} /dev/zero | tr '\\0' a; echo; echo the end")
        status, body = run_actions.run("job.log", run_actions.Step(line="loud", argv=("loud",)))
        self.assertEqual((status, body["cut"], len(body["output"])), (200, True, run_actions.OUTPUT_CHARS))
        self.assertTrue(body["output"].endswith("the end"))

    def test_a_program_that_does_not_start_is_503_and_ran_nothing(self):
        status, body = run_actions.run("job.log", run_actions.Step(line="gone", argv=(str(self.bin / "gone"),)))
        self.assertEqual(status, 503)
        self.assertIn("did not start", body["error"])
