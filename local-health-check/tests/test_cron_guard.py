"""The launchd sweep's cron guard, run end to end against a fixture machine.

`health-check.sh check` is pointed at a fixture `HOME` holding four
`org.example.cron.*` plists (`SYSTEM_TOOLS_LABEL_PREFIX=org.example`) and at
a `HEALTH_CHECK_TOOLS_ROOT` holding one
`local-cron-jobs`, with a stubbed `launchctl` that answers per label. One
`check` run is shared by the cases (setUpClass): the machine stages cost tens
of seconds and the hang fixture costs the guard's own 15-second bound, and
neither buys extra evidence when paid per case.

REGRESSION (Copilot on 9df3139). `cron-jobs.sh status` has three returns --
0 healthy, 1 broken, 3 nothing installed -- and the guard tested for success
with `&& continue`, which collapsed 1 and 3 into one fall-through to launchd's
counter. A job run by hand that failed records `exit=3` while launchd's last
SPAWNED run exited 0, so the fall-through read `last exit code = 0`, raised
nothing, and the recorded failure was gone.

The `demo` label is the real `cron-jobs.sh` reading a real record, not a stub
that exits 1: the claim is about what the two programs do together.
"""

import os
import pathlib
import shutil
import signal
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
# REGRESSION means the case fails against the code from before its fix, and
# this is how that is checked -- the sibling cron suite's idiom:
#
#     HEALTH_CHECK_TEST_SCRIPT=$(git show 9df3139:local-health-check/health-check.sh ...)
SCRIPT = pathlib.Path(os.environ.get("HEALTH_CHECK_TEST_SCRIPT")
                      or HERE.parent / "health-check.sh")
CRON_JOBS = HERE.parent.parent / "local-cron-jobs" / "cron-jobs.sh"
# The guard's bound is not overridable, so the hang fixture costs it once.
RUN_TIMEOUT = 300

# What the stub `launchctl` reports per label: launchd's spawn counter and its
# verdict on the last run it spawned.
LAUNCHD = {"demo": ("5", "0"), "hang": ("5", "0"), "legacy": ("2", "2"),
           "green": ("5", "1")}


class CronGuardAgainstFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="health-check-cron."))
        cls.home = cls.tmp / "home"
        agents = cls.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        for job in LAUNCHD:
            (agents / f"org.example.cron.{job}.plist").write_text("<plist/>\n")

        cls.root = cls.tmp / "tools"
        folder = cls.root / "local-cron-jobs"
        (folder / "jobs").mkdir(parents=True)
        (folder / "logs").mkdir()
        # The real script, reached by the wrapper for every job the wrapper
        # does not stage itself.
        shutil.copy(CRON_JOBS, folder / "real-cron-jobs.sh")
        # It sources the shared config resolver from its sibling `lib/`.
        # Without it the script died before `status`, and that death passed
        # for the failure verdict under bash and fell through under dash.
        shutil.copytree(CRON_JOBS.parent.parent / "lib", cls.root / "lib")
        # `demo`'s evidence: a completed run that recorded a failure, with no
        # launchd identity -- which is what a run by hand writes when launchd
        # holds no coalition for the label.
        (folder / "logs" / ".demo.runs").write_text("exit=3\n")
        wrapper = folder / "cron-jobs.sh"
        wrapper.write_text(
            "#!/bin/sh\n"
            'case "${2:-}" in\n'
            "  hang) sleep 600 ;;\n"
            "  legacy) exit 3 ;;\n"
            "  green) exit 0 ;;\n"
            '  *) exec sh "$(dirname "$0")/real-cron-jobs.sh" "$@" ;;\n'
            "esac\n")
        wrapper.chmod(0o755)

        stub = cls.tmp / "bin"
        stub.mkdir()
        # The real `log show --last 24h` reads the whole unified log, which
        # alone can outlast RUN_TIMEOUT on a busy machine; this suite is not
        # about fault volume, so the stub prints no faults.
        log = stub / "log"
        log.write_text("#!/bin/sh\nexit 0\n")
        log.chmod(0o755)

        launchctl = stub / "launchctl"
        arms = "".join(
            f'    {job}) printf \'\\truns = {runs}\\n\\tlast exit code = {code}\\n\' ;;\n'
            for job, (runs, code) in LAUNCHD.items())
        launchctl.write_text(
            "#!/bin/sh\n"
            'case "$1" in print) ;; *) exit 0 ;; esac\n'
            'label=${2##*/}\n'
            'case "$label" in org.example.cron.*) ;; *) exit 1 ;; esac\n'
            "printf '\\tstate = not running\\n'\n"
            'case "${label#org.example.cron.}" in\n'
            f"{arms}"
            "  *) exit 1 ;;\n"
            "esac\n"
            "printf '\\tresource coalition = {\\n\\t\\tID = 85948\\n\\t}\\n'\n"
            "exit 0\n")
        launchctl.chmod(0o755)

        env = dict(os.environ)
        env["HOME"] = str(cls.home)
        env["PATH"] = f"{stub}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        env["HEALTH_CHECK_TOOLS_ROOT"] = str(cls.root)
        env["HEALTH_CHECK_STATE"] = str(cls.tmp / "state")
        env["HEALTH_CHECK_STATUS_BOUND"] = "2"
        env["SYSTEM_TOOLS_LABEL_PREFIX"] = "org.example"
        # This suite is about the guard, not about Jev, and the ordering
        # spends real tokens labelling findings it never reads.
        env["JEV_HEALTH_CHECK"] = "0"
        proc = subprocess.Popen(
            ["sh", str(SCRIPT), "check"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
        try:
            cls.out, cls.err = proc.communicate(timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
        cls.rc = proc.returncode

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def findings(self):
        """Every finding as (headline, fix), from the `findings:` block."""
        lines = self.out.splitlines()
        if "findings:" not in lines:
            return []
        start = lines.index("findings:") + 1
        end = lines.index("notes:") if "notes:" in lines else len(lines)
        block = [l for l in lines[start:end] if l.strip()]
        pairs = []
        for i in range(0, len(block), 2):
            head, fix = block[i], block[i + 1] if i + 1 < len(block) else ""
            pairs.append((head[2:], fix[len("  fix: "):]))
        return pairs

    def about(self, job):
        """The findings naming this fixture label. Findings from the machine
        stages cannot name a label that did not exist a second ago."""
        label = f"org.example.cron.{job}"
        return [(h, f) for h, f in self.findings() if label in h]

    def test_the_run_finished_and_reported(self):
        self.assertEqual(self.rc, 0, self.err)
        self.assertIn("findings:", self.out, self.out)

    def test_a_recorded_failure_is_a_finding_although_launchd_is_clean(self):
        """The finding itself. launchd's counter says the last run it spawned
        exited 0; the job's own record says a completed run exited 3; `status`
        exits 1 and the sweep used to print nothing."""
        raised = self.about("demo")
        self.assertEqual(len(raised), 1,
                         "the sweep raised nothing for a job whose own record "
                         f"says a completed run exited 3:\n{self.out}")
        head, fix = raised[0]
        self.assertIn("reports a failed run", head)
        # The remedy points at what raised it, not at the plist.
        self.assertIn("logs/demo.log", fix)
        self.assertIn("logs/.demo.runs", fix)
        self.assertNotIn("StandardErrorPath", fix)

    def test_a_status_timeout_does_not_manufacture_a_finding(self):
        """`bounded` returns 124 of its own when it kills a hung `status`. A
        timeout is not a verdict, and the fall-through is where it belongs:
        launchd reports a clean exit for this label, so nothing is raised."""
        self.assertEqual(self.about("hang"), [], self.out)

    def test_nothing_installed_still_falls_through_to_launchd(self):
        """rc=3 keeps the behaviour it had. The job answers nothing, launchd
        reports exit 2, and that is the finding."""
        raised = self.about("legacy")
        self.assertEqual(len(raised), 1, self.out)
        self.assertIn("last exited 2", raised[0][0])

    def test_a_clean_status_still_suppresses_launchd_s_counter(self):
        """sd:1201's own case, unchanged: `status` exits 0, launchd's counter
        says 1, and the counter is dropped."""
        self.assertEqual(self.about("green"), [], self.out)
