"""Jobs report findings through the run's report, not by mail (sd:3454, sd:3537).

cron-jobs ingests every run with `sd reports ingest`, and the pack opens a
report that needs attention for a run that exits non-zero or prints a line
`SD_REPORT_ATTENTION: <reason>`. Today lists such a report until it is
acknowledged. That line is the one findings contract: a job with findings
prints it and exits 0, and sends nothing.

Two checks. The inventory reads every tracked shell script, so a sender
added later is caught too. The end-to-end case runs brew-doctor against a
stub `brew` and feeds its output to the pinned pack's ingest, so the line a
job prints is proved to be the line the pack reads.
"""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sd_db import connect
from sd_db.migrate import initialise
from sd_db import reporting

ROOT = Path(__file__).resolve().parent.parent

# A notify.sh call that sends a scheduled report: `-k status` or `-k brief`.
SENDER = re.compile(r"notify.*\s-k\s+(status|brief)\b|\$NOTIFY\".*\s-k\s+(status|brief)\b", re.I)
# The two obsidian digests mail an HTML card with signed action buttons, which
# Reports cannot show; whether they keep mailing is an open sd:3537 question.
PENDING = {"local-obsidian-tasks/obsidian-tasks.sh", "local-obsidian-review/obsidian-review.sh"}


def tracked_scripts():
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.sh"], capture_output=True,
                            text=True, check=True).stdout.split()
    return [name for name in listed if not name.startswith("local-notify/")]


class InventoryTest(unittest.TestCase):
    def test_no_job_mails_or_pushes_a_report(self):
        senders = []
        for name in tracked_scripts():
            for number, line in enumerate((ROOT / name).read_text(errors="replace").splitlines(), 1):
                if SENDER.search(line) and not line.lstrip().startswith("#") and name not in PENDING:
                    senders.append(f"{name}:{number}: {line.strip()}")
        self.assertEqual(senders, [])

    def test_no_job_exits_1_to_fire_a_failure_push(self):
        """The push is gone, so a path that fails a run to fire it is dead."""
        stale = [f"{name}:{number}" for name in tracked_scripts()
                 for number, line in enumerate((ROOT / name).read_text(errors="replace").splitlines(), 1)
                 if "failure push fires" in line]
        self.assertEqual(stale, [])

    def test_the_failure_wrapper_pushes_nothing(self):
        text = (ROOT / "local-cron-jobs/cron-jobs.sh").read_text()
        body = text[text.index("notify_failure() {"):]
        body = body[:body.index("\n}\n")]
        self.assertNotIn("curl", body)
        self.assertNotIn("osascript", body)
        self.assertFalse((ROOT / "local-cron-jobs/notify.conf.example").exists())


def _stub(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


class EndToEndTest(unittest.TestCase):
    """brew-doctor's findings reach the pack's ingest as a report that needs attention."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="findings-contract."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "local-brew-doctor").mkdir()
        shutil.copy(ROOT / "local-brew-doctor/brew-doctor.sh", self.tmp / "local-brew-doctor")
        self.sent = self.tmp / "notify-calls"
        _stub(self.tmp / "local-notify/notify.sh", f'printf "%s\\n" "$@" >> "{self.sent}"\n')
        self.bin = self.tmp / "bin"
        # Two warnings that no auto-fix touches, before and after the fixes.
        _stub(self.bin / "brew", 'case "$1" in doctor) printf "Warning: one\\nWarning: two\\n";; esac\nexit 0\n')
        _stub(self.bin / "hostname", 'echo "fixturehost"\n')

    def test_brew_doctor_warnings_open_a_report_that_needs_attention(self):
        run = subprocess.run(["sh", str(self.tmp / "local-brew-doctor/brew-doctor.sh"), "run"],
                             capture_output=True, text=True, timeout=120,
                             env={"HOME": str(self.tmp), "PATH": f"{self.bin}:/usr/bin:/bin"})
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse(self.sent.exists(), self.sent.read_text() if self.sent.exists() else "")
        log = self.tmp / "brew-doctor-nightly.log"
        log.write_text(run.stdout + run.stderr)
        database = self.tmp / "sd.db"
        initialise(database)
        with closing(connect(database)) as connection:
            state = reporting.ingest_log(connection, job="brew-doctor-nightly", run_id="r1",
                                         started="2026-10-12T08:15:00Z", ended="2026-10-12T08:16:00Z",
                                         exit_code=0, log_path=str(log), offset=0)
        self.assertEqual((state["recorded"], state["why"]),
                         ("report", "brew doctor: 2 warning(s) on fixturehost"))
        self.assertIn("Warning: two", state["item"]["body"])


if __name__ == "__main__":
    unittest.main(argv=sys.argv)
