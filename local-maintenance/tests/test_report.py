"""`run` hands its findings to the run's report, and mails nothing (sd:3454).

The script runs from a copy whose sibling `local-notify/notify.sh` is a stub
that records any call, so no run of this suite can reach a real channel.
`softwareupdate`, `mas`, `docker` and `hostname` are stubs on PATH; the one
finding comes from a conf line naming a certificate that does not exist.
"""

import pathlib
import shutil
import subprocess
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
LIB = FOLDER.parent / "lib"


def _stub(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="maintenance-report."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "local-maintenance").mkdir()
        shutil.copy(FOLDER / "maintenance.sh", self.tmp / "local-maintenance")
        shutil.copytree(LIB, self.tmp / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.sent = self.tmp / "notify-calls"
        _stub(self.tmp / "local-notify" / "notify.sh", f'printf "%s\\n" "$@" >> "{self.sent}"\n')
        self.bin = self.tmp / "bin"
        _stub(self.bin / "softwareupdate", 'echo "No new software available."\n')
        _stub(self.bin / "mas", "exit 0\n")
        _stub(self.bin / "docker", "exit 1\n")
        _stub(self.bin / "hostname", 'echo "fixturehost"\n')
        # FAKE_DAY stands for the run's date in the report's reason.
        _stub(self.bin / "date", '''[ -n "$FAKE_DAY" ] && [ "$1" = "+%Y-%m-%d" ] && { echo "$FAKE_DAY"; exit 0; }
exec /bin/date "$@"
''')
        # df reports DF_USED percent used, 50 unless a test sets it.
        _stub(self.bin / "df", 'echo "Filesystem 512-blocks Used Available Capacity Mounted on"\n'
              'echo "/dev/disk1 100 ${DF_USED:-50} 0 ${DF_USED:-50}% /"\n')
        self.config = self.tmp / "config"
        (self.config / "maintenance").mkdir(parents=True)
        (self.tmp / "home").mkdir()
        self.env = {"HOME": str(self.tmp / "home"), "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                    "SYSTEM_TOOLS_CONFIG": str(self.config), "MAINTENANCE_PROFILE": "none"}

    def maintenance(self, conf, verb="run", **extra):
        (self.config / "maintenance" / "maintenance.conf").write_text(conf)
        return subprocess.run(["sh", str(self.tmp / "local-maintenance" / "maintenance.sh"), verb],
                              capture_output=True, text=True, env={**self.env, **extra}, timeout=120)

    def attention(self, run):
        return [line for line in run.stdout.splitlines() if line.startswith("SD_REPORT_ATTENTION: ")]

    def test_a_finding_needs_attention_and_mails_nothing(self):
        run = self.maintenance(f"cert|{self.tmp}/missing.pem|30\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn(f"- cert: {self.tmp}/missing.pem missing", run.stdout)
        [line] = self.attention(run)
        self.assertRegex(line, r"^SD_REPORT_ATTENTION: maintenance: 1 finding\(s\) on fixturehost, \d{4}-\d\d-\d\d$")
        self.assertFalse(self.sent.exists(), self.sent.read_text() if self.sent.exists() else "")

    def test_each_day_files_its_own_report(self):
        """sd:3537: the ingest folds an open reason and keeps its first body.
        A checksum with digits erased made worker1 and worker2 one report."""
        first = self.attention(self.maintenance(f"cert|{self.tmp}/worker1.pem|30\n", FAKE_DAY="2026-10-12"))
        second = self.attention(self.maintenance(f"cert|{self.tmp}/worker2.pem|30\n", FAKE_DAY="2026-10-13"))
        self.assertEqual(first, ["SD_REPORT_ATTENTION: maintenance: 1 finding(s) on fixturehost, 2026-10-12"])
        self.assertEqual(second, ["SD_REPORT_ATTENTION: maintenance: 1 finding(s) on fixturehost, 2026-10-13"])

    def test_a_clean_run_needs_nothing(self):
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("all checks clean", run.stdout)
        self.assertEqual(self.attention(run), [])

    def test_check_prints_findings_without_the_line(self):
        run = self.maintenance(f"cert|{self.tmp}/missing.pem|30\n", "check")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("missing.pem missing", run.stdout)
        self.assertEqual(self.attention(run), [])


if __name__ == "__main__":
    unittest.main()
