"""`status` answers convention 6: 0 healthy, 3 agent not loaded, 1 stale or unhealthy."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_db.database import connect
from sd_db.migrate import initialise
from sd_runner import cli


class Status(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.database = root / "sd.db"
        initialise(self.database)
        self.config = root / "runner.json"
        self.config.write_text(json.dumps({"database": str(self.database)}))

    def heartbeat(self, healthy=True):
        connection = connect(self.database)
        try:
            store.heartbeat(connection, {"healthy": healthy, "interval_seconds": 10})
        finally:
            connection.close()

    def status(self, loaded):
        output = io.StringIO()
        with patch.object(cli, "agent_loaded", return_value=loaded), contextlib.redirect_stdout(output):
            code = cli.main(["status", "--config", str(self.config)])
        return code, json.loads(output.getvalue())

    def test_loaded_and_fresh_heartbeat_exits_0(self):
        self.heartbeat()
        code, body = self.status(loaded=True)
        self.assertEqual(code, 0)
        self.assertTrue(body["ok"])

    def test_loaded_without_heartbeat_exits_1(self):
        code, body = self.status(loaded=True)
        self.assertEqual(code, 1)
        self.assertFalse(body["ok"])

    def test_loaded_with_a_state_that_omits_ok_exits_1(self):
        # sd:1387 item 6. The verdict defaulted to healthy when the state
        # carried no "ok"; a health verb must default the other way.
        output = io.StringIO()
        with patch.object(cli, "agent_loaded", return_value=True), \
                patch.object(cli, "heartbeat_state", return_value={"healthy": True}), \
                contextlib.redirect_stdout(output):
            code = cli.main(["status", "--config", str(self.config)])
        self.assertEqual(code, 1, output.getvalue())

    def test_agent_not_loaded_exits_3_with_the_same_body(self):
        # The third state moved in from local-health-check's launchctl gate:
        # nothing to check, not broken — and the JSON body is still the
        # heartbeat state, so readers of the body see what they saw.
        self.heartbeat()
        code, body = self.status(loaded=False)
        self.assertEqual(code, 3)
        self.assertTrue(body["ok"])
        self.assertEqual(body["interval_seconds"], 10)

    def test_agent_not_loaded_and_no_store_exits_3_naming_the_absent_store(self):
        # A work machine that never installed the agent holds no store either.
        # The agent-not-loaded verdict is decided before the store is opened,
        # so the answer is 3 (nothing to check), not connect's exit 1, and the
        # body names the absent store in the shape a missing heartbeat uses.
        absent = self.database.parent / "absent.db"
        code, body = self.status_not_loaded_against(absent)
        self.assertEqual(code, 3)
        self.assertIn(f"no database at {absent}", body["reason"])

    def test_agent_not_loaded_and_unreadable_store_exits_3_naming_why(self):
        # The store's answer only fills the body once the agent question is
        # decided, so a store SQLite cannot open is 3 with its error in the
        # body, not an uncaught sqlite3.DatabaseError and a traceback.
        corrupt = self.database.parent / "corrupt.db"
        corrupt.write_bytes(b"not a database\n" * 64)
        code, body = self.status_not_loaded_against(corrupt)
        self.assertEqual(code, 3)
        self.assertIn("file is not a database", body["reason"])

    def status_not_loaded_against(self, database):
        self.config.write_text(json.dumps({"database": str(database)}))
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cli, "agent_loaded", return_value=False), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(["status", "--config", str(self.config)])
        self.assertEqual(code, 3, errors.getvalue())
        self.assertEqual(errors.getvalue(), "")
        body = json.loads(output.getvalue())
        self.assertFalse(body["ok"])
        return code, body

    def hand_written_heartbeat(self, literal):
        connection = connect(self.database)
        try:
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('heartbeat', 'runner', '2026-09-15T00:00:00+00:00', ?) "
                               "ON CONFLICT(key) WHERE kind = 'heartbeat' AND key = 'runner' DO UPDATE SET body = excluded.body", (literal,))
            connection.commit()
        finally:
            connection.close()

    def test_a_heartbeat_row_that_is_not_an_object_is_a_refusal_not_a_traceback(self):
        # sd:934. Only the runner writes the row, so a list, a string or a
        # number in it was put there by hand. The store refuses the shape as
        # an SdDbError, which this command already catches: 3 with the
        # refusal in the body when the agent is not loaded, 1 with it on
        # stderr when it is. Before, both paths raised TypeError past main.
        for literal, found in (('[1, 2]', 'a JSON array'), ('"pulse"', 'a JSON string'), ('7', 'a JSON number')):
            with self.subTest(literal=literal):
                self.hand_written_heartbeat(literal)
                code, body = self.status_not_loaded_against(self.database)
                self.assertEqual(code, 3)
                self.assertIn(f"the runner heartbeat body is {found}", body["reason"])
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(cli, "agent_loaded", return_value=True), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = cli.main(["status", "--config", str(self.config)])
                self.assertEqual(code, 1)
                self.assertEqual(output.getvalue(), "")
                self.assertIn(f"runner: the runner heartbeat body is {found}", errors.getvalue())
                self.assertNotIn("Traceback", errors.getvalue())

    def test_a_heartbeat_interval_of_the_wrong_type_is_a_refusal_not_a_traceback(self):
        # sd:1221. The library refuses a body that is not an object, but one
        # whose `interval_seconds` is a string reaches the freshness
        # comparison, and TypeError was named in neither path.
        self.hand_written_heartbeat(json.dumps({"healthy": True, "interval_seconds": "10"}))
        code, body = self.status_not_loaded_against(self.database)
        self.assertEqual(code, 3)
        self.assertIn("the runner heartbeat body is unusable", body["reason"])
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cli, "agent_loaded", return_value=True), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(["status", "--config", str(self.config)])
        self.assertEqual(code, 1)
        self.assertIn("runner: the runner heartbeat body is unusable", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_the_status_body_is_one_line(self):
        # sd:1237. local-health-check quotes the first line of stdout as the
        # finding text when a tool exits 1; indented, that line was `{`.
        self.heartbeat(healthy=False)
        for loaded in (True, False):
            with self.subTest(loaded=loaded):
                output = io.StringIO()
                with patch.object(cli, "agent_loaded", return_value=loaded), contextlib.redirect_stdout(output):
                    cli.main(["status", "--config", str(self.config)])
                printed = output.getvalue().splitlines()
                self.assertEqual(len(printed), 1, printed)
                self.assertFalse(json.loads(printed[0])["ok"])

    def refreshed(self, completed_at, *, retention=None):
        """A runner.json with its own retention folder, holding one archive-refresh receipt when `completed_at` is set."""
        retention = retention or Path(self.tmp.name).resolve() / "retention"
        (retention / ".archive-refresh").mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({"database": str(self.database), "retention": str(retention)}))
        if completed_at:
            receipt = {"version": 1, "operation": "archive-refresh", "database": str(self.database.resolve()),
                       "retention_root": str(retention.resolve()), "completed_at": completed_at}
            (retention / ".archive-refresh" / "one.json").write_text(json.dumps(receipt))
        return retention

    def test_status_names_the_last_and_next_archive_refresh(self):
        # sd:2209. The dashboard does not know the runner's config or its
        # retention folder; it reads the refresh cadence from this body. The
        # exit code is still the heartbeat's alone.
        self.heartbeat()
        self.refreshed("2026-10-02T03:00:00+00:00")
        code, body = self.status(loaded=True)
        self.assertEqual(code, 0)
        schedule = body["archive_refresh_schedule"]
        self.assertEqual(schedule["last_completed_at"], "2026-10-02T03:00:00+00:00")
        self.assertEqual(schedule["next_due_at"], "2026-10-03T03:00:00+00:00")
        self.assertEqual(schedule["cadence_seconds"], 86400)

    def test_a_runner_that_never_refreshed_says_so_and_is_due_now(self):
        self.heartbeat()
        self.refreshed(None)
        code, body = self.status(loaded=True)
        self.assertEqual(code, 0)
        self.assertIsNone(body["archive_refresh_schedule"]["last_completed_at"])
        self.assertTrue(body["archive_refresh_schedule"]["due"])

    def test_an_unreadable_refresh_receipt_is_a_reason_and_not_a_verdict(self):
        # A receipt from another configuration is refused by the schedule; the
        # body names why, and the heartbeat still decides the code.
        self.heartbeat()
        retention = self.refreshed("2026-10-02T03:00:00+00:00")
        receipt = retention / ".archive-refresh" / "one.json"
        receipt.write_text(json.dumps({**json.loads(receipt.read_text()), "database": "/elsewhere/sd.db"}))
        code, body = self.status(loaded=True)
        self.assertEqual(code, 0)
        self.assertEqual(body["archive_refresh_schedule"], {"reason": "archive refresh cadence belongs to another configuration"})

    def test_an_agent_that_is_not_loaded_reads_no_retention_folder(self):
        # Nothing refreshes without the agent, and the default retention
        # folder is under ~/Documents, which TCC guards: under launchd an
        # ungranted read waits instead of failing. local-health-check runs
        # this verb from a cron job on machines that never installed the
        # agent, so that path does not touch the folder.
        self.heartbeat()
        self.refreshed("2026-10-02T03:00:00+00:00")
        with patch("sd_runner.archive_refresh.schedule", side_effect=AssertionError("read the retention folder")):
            code, body = self.status(loaded=False)
        self.assertEqual(code, 3)
        self.assertEqual(body["archive_refresh_schedule"],
                         {"reason": "the runner agent is not loaded, so no archive refresh is scheduled"})

    def launcher_env(self, *, loaded, absent):
        # `runner.sh status` asks launchctl itself, so the answer is fixed by
        # a `launchctl` ahead of the real one on PATH: exit 0 is loaded, 113
        # is what the real one prints for an unknown service.
        fake = Path(self.tmp.name) / "bin"
        fake.mkdir(exist_ok=True)
        (fake / "launchctl").write_text(f"#!/bin/sh\nexit {0 if loaded else 113}\n")
        (fake / "launchctl").chmod(0o755)
        return {**os.environ, "SD_RUNNER_PYTHON": str(absent), "PATH": f"{fake}:{os.environ.get('PATH', '')}"}

    def test_status_answers_3_when_the_runtime_is_not_provisioned_and_no_agent_is_loaded(self):
        # sd:1237. `status` fell through to the runtime guard, which exits 1,
        # so a machine that never provisioned the pack raised a nightly
        # finding nobody could act on. Convention 6 calls that 3.
        launcher = Path(__file__).resolve().parents[1] / "runner.sh"
        absent = Path(self.tmp.name) / "no-such-python"
        done = subprocess.run(["sh", str(launcher), "status"], capture_output=True, text=True,
                              env=self.launcher_env(loaded=False, absent=absent))
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertEqual(done.stdout.count("\n"), 1, done.stdout)
        self.assertFalse(json.loads(done.stdout)["ok"])
        self.assertIn(str(absent), json.loads(done.stdout)["reason"])
        # Every other verb still refuses: the guard is unchanged for them.
        other = subprocess.run(["sh", str(launcher), "preflight"], capture_output=True, text=True,
                               env=self.launcher_env(loaded=False, absent=absent))
        self.assertEqual(other.returncode, 1, other.stderr)
        self.assertIn("missing installed runtime", other.stderr)

    def test_status_answers_1_when_a_loaded_agent_has_no_runtime(self):
        # A loaded agent whose interpreter is gone (a rebuilt virtualenv, an
        # upgraded Python) cannot start; that is the finding, not "nothing to
        # check". The same one-line body carries the reason.
        launcher = Path(__file__).resolve().parents[1] / "runner.sh"
        absent = Path(self.tmp.name) / "no-such-python"
        done = subprocess.run(["sh", str(launcher), "status"], capture_output=True, text=True,
                              env=self.launcher_env(loaded=True, absent=absent))
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(done.stdout.count("\n"), 1, done.stdout)
        self.assertIn(str(absent), json.loads(done.stdout)["reason"])

    def test_serve_reads_the_interpreter_env_sh_names(self):
        # `serve` and `once` source ~/.config/shell/env.sh first, and that file
        # may set SD_RUNNER_PYTHON. The interpreter resolves after it, so an
        # override there is honoured; resolved before, it was ignored.
        launcher = Path(__file__).resolve().parents[1] / "runner.sh"
        home = Path(self.tmp.name) / "home"
        (home / ".config/shell").mkdir(parents=True)
        chosen = home / "chosen-python"
        chosen.write_text("#!/bin/sh\necho chosen \"$@\"\n")
        chosen.chmod(0o755)
        (home / ".config/shell/env.sh").write_text(f"SD_RUNNER_PYTHON={chosen}\nexport SD_RUNNER_PYTHON\n")
        env = {key: value for key, value in os.environ.items() if key != "SD_RUNNER_PYTHON"}
        done = subprocess.run(["sh", str(launcher), "serve"], capture_output=True, text=True, env={**env, "HOME": str(home)})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith("chosen -I "), done.stdout)

    def test_status_reads_the_interpreter_env_sh_names(self):
        # PR #498 review: `status` resolved SD_RUNNER_PYTHON before env.sh was
        # read, so an interpreter named only there was reported missing while
        # `serve` started with it. Both verbs now read the same file first.
        launcher = Path(__file__).resolve().parents[1] / "runner.sh"
        home = Path(self.tmp.name) / "home-status"
        (home / ".config/shell").mkdir(parents=True)
        chosen = home / "chosen-python"
        chosen.write_text("#!/bin/sh\necho chosen \"$@\"\n")
        chosen.chmod(0o755)
        (home / ".config/shell/env.sh").write_text(f"SD_RUNNER_PYTHON={chosen}\nexport SD_RUNNER_PYTHON\n")
        env = self.launcher_env(loaded=False, absent=Path(self.tmp.name) / "unused")
        env.pop("SD_RUNNER_PYTHON")
        done = subprocess.run(["sh", str(launcher), "status"], capture_output=True, text=True, env={**env, "HOME": str(home)})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(done.stdout.startswith("chosen -I "), done.stdout)

    def test_agent_loaded_asks_launchctl_for_the_label(self):
        completed = subprocess.CompletedProcess(["launchctl"], 0)
        with patch.object(cli.subprocess, "run", return_value=completed) as run:
            self.assertTrue(cli.agent_loaded())
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["launchctl", "print"])
        self.assertTrue(argv[2].startswith("gui/") and argv[2].endswith("/" + cli.LABEL), argv)
        completed.returncode = 113
        with patch.object(cli.subprocess, "run", return_value=completed):
            self.assertFalse(cli.agent_loaded())
        with patch.object(cli.subprocess, "run", side_effect=FileNotFoundError("launchctl")):
            self.assertFalse(cli.agent_loaded())


if __name__ == "__main__":
    unittest.main()
