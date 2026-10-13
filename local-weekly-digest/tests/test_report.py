"""`run` hands the digest to the run's report, and mails nothing (sd:3454).

The script runs from a copy whose sibling `local-notify/notify.sh` is a stub
that records any call, so no run of this suite can reach a real channel.
`brew`, `mas` and `hostname` are stubbed on `PATH`, as in
test_failure_order.py.
"""

import datetime
import pathlib
import shutil
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "weekly-digest.sh"


def _stub(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="weekly-digest-report."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        folder = self.tmp / "local-weekly-digest"
        folder.mkdir()
        shutil.copy(SCRIPT, folder / SCRIPT.name)
        shutil.copytree(HERE.parent.parent / "lib", self.tmp / "lib",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.script = folder / SCRIPT.name
        self.sent = self.tmp / "notify-calls"
        _stub(self.tmp / "local-notify" / "notify.sh", f'printf "%s\\n" "$@" >> "{self.sent}"\n')
        self.bin = self.tmp / "bin"
        _stub(self.bin / "brew", "exit 0\n")
        _stub(self.bin / "mas", "exit 0\n")
        _stub(self.bin / "hostname", 'echo "fixturehost"\n')
        msetup = _stub(self.bin / "machine-setup.sh", 'echo "drift : 0"\n')
        logs = self.tmp / "logs"
        logs.mkdir()
        (logs / "failures.log").write_text("")
        (self.tmp / "home").mkdir()
        self.env = {"HOME": str(self.tmp / "home"), "PATH": f"{self.bin}:/usr/bin:/bin",
                    "WEEKLY_DIGEST_CRON_LOGS": str(logs), "WEEKLY_DIGEST_MSETUP": str(msetup)}

    def digest(self, verb):
        return subprocess.run(["sh", str(self.script), verb], capture_output=True, text=True,
                              env=self.env, timeout=120)

    def test_update_counts_run_bounded_and_a_failed_one_is_unknown(self):
        """sd:3536: brew and mas run under st_step, which names each in the
        job log; a check that failed counts nothing, so it reads unknown."""
        _stub(self.bin / "brew", 'case "$3" in --quiet) echo "a"; echo "b" ;; *) exit 1 ;; esac\n')
        _stub(self.bin / "mas", "exit 1\n")
        run = self.digest("check")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("brew outdated       : 2 formulae, 2 casks", run.stdout)
        self.assertIn("App Store outdated  : unknown", run.stdout)
        self.assertRegex(run.stderr, r"\[step [\d:]+\] brew outdated --formula --quiet \(bound 300s\)")
        self.assertRegex(run.stderr, r"\[step [\d:]+\] mas outdated exited 1")

    def test_run_prints_the_digest_and_its_attention_line_and_mails_nothing(self):
        run = self.digest("run")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("job activity, last 7 days (runs / failures):", run.stdout)
        self.assertEqual(
            [line for line in run.stdout.splitlines() if line.startswith("SD_REPORT_ATTENTION: ")],
            [f"SD_REPORT_ATTENTION: weekly digest: automation summary for fixturehost, {datetime.date.today()}"])
        self.assertFalse(self.sent.exists(), self.sent.read_text() if self.sent.exists() else "")

    def test_check_prints_no_attention_line(self):
        run = self.digest("check")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("SD_REPORT_ATTENTION", run.stdout)


if __name__ == "__main__":
    unittest.main()
