"""A cancel or resume of an owned attempt records the operator who asked (sd:749).

A queued cancel is a row write in `sd_db` and names its caller directly. A
running cancel and a resume go through the installed service instead:
`runner_controls.control` hands them to `invoke_service`, which runs
`runner.sh cancel|resume`, and the runner CLI records whoever it is told. Before
`--who` existed nothing told it, so the dashboard's cancel of a running attempt
was recorded under the runner process's login account, the same name a person
at the terminal gets.
"""

import contextlib
import getpass
import io
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from sd_db import runner as store
from sd_db import runner_controls
from sd_runner import cli

from . import test_runtime as fixtures


class ARunnerControlNamesItsOperator(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.db, self.config = self.fixture.root, self.fixture.db, self.fixture.config
        self.settings = self.root / "runner.json"
        self.settings.write_text(json.dumps({"database": str(self.config.database), "work": str(self.config.work),
                                             "retention": str(self.config.retention)}))

    def cli(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main([*map(str, argv), "--config", str(self.settings)])
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue())

    def running(self):
        """A claimed attempt with a live owned supervisor, as `cancel` requires."""
        request = self.fixture.claim()
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
        def reap():
            if child.poll() is None:
                child.kill()
            child.wait()
        self.addCleanup(reap)
        self.fixture.runner.persist(self.db, request["run"]["id"], supervisor_pid=child.pid, supervisor_pgid=child.pid,
            supervisor_start=fixtures.processes.start_identity(child.pid), start_step="supervised")
        return request, store.queue_state(self.db, request["id"])

    def kept(self):
        """An attempt that ended with its clone kept, as `resume` requires."""
        self.fixture.provider.write_text("from pathlib import Path\nPath('unfinished').write_text('left')\n")
        request = self.fixture.claim()
        kept = self.fixture.run_fixture(request)
        fixtures.git(kept["work_path"], "add", "unfinished")
        fixtures.git(kept["work_path"], "commit", "-qm", "operator resolved")
        return request, store.queue_state(self.db, request["id"])

    def cancel_requested(self, request):
        return self.db.execute("SELECT cancel_requested FROM runner_run WHERE id=?",
                               (request["run"]["id"],)).fetchone()[0]

    def test_cli_cancel_records_the_operator_it_is_given(self):
        request, current = self.running()
        self.cli("cancel", request["id"], "--database", self.config.database, "--if-revision", current["revision"],
                 "--expected-run", current["run"]["id"], "--who", "dashboard")
        self.assertEqual(self.cancel_requested(request), "cancelled by dashboard")

    def test_cli_resume_without_an_operator_records_the_login_account(self):
        request, current = self.kept()
        with patch("sd_runner.controls.Runner", return_value=self.fixture.runner):
            result = self.cli("resume", request["id"], "--database", self.config.database,
                              "--if-revision", current["revision"], "--expected-run", current["run"]["id"])
        self.assertEqual(result["run"]["end_action"], "resume")
        notes = self.db.execute("SELECT body, session FROM note WHERE item=? AND kind='decision'",
                                (self.fixture.item,)).fetchall()
        self.assertIn((f"kept attempt resolved; fresh run requested by {getpass.getuser()}", getpass.getuser()),
                      [tuple(note) for note in notes])

    def test_a_dashboard_cancel_of_a_running_attempt_reaches_the_service_under_its_own_name(self):
        """The whole path: `control` -> `invoke_service` argv -> runner CLI -> the row.

        Only the process boundary is replaced. The argv `invoke_service` builds
        is parsed by the real CLI in this process, so a `--who` that is built
        but not read, or read but not built, both fail here.
        """
        request, current = self.running()
        launcher = self.root / "service/local-sd-runner/runner.sh"
        installation = {"launcher": str(launcher), "interpreter": sys.executable, "config": str(self.settings),
                        "home": str(self.root), "database": str(self.config.database)}
        spawn = subprocess.run
        seen = []
        def service(args, **options):
            if args[0] != str(launcher):
                return spawn(args, **options)
            seen.append(args)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(args[1:])
            return subprocess.CompletedProcess(args, code, output.getvalue(), "")
        with patch("sd_db.runner_controls.service_installation", return_value=installation), \
                patch.object(runner_controls.subprocess, "run", side_effect=service):
            runner_controls.control(self.db, request["id"], "cancel", expected_revision=current["revision"],
                                    who="dashboard")
        self.assertEqual(len(seen), 1)
        self.assertIn("--who", seen[0])
        self.assertEqual(seen[0][seen[0].index("--who") + 1], "dashboard")
        self.assertEqual(self.cancel_requested(request), "cancelled by dashboard")


if __name__ == "__main__":
    unittest.main()
