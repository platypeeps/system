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

The ingest folds a run into the open report with the same job and every
declared line, and gives that report the newest body. So a reason names its
findings, not the day: one open report per job and host while the findings
stay the same, and a new or changed finding opens its own (sd:4182, ruling
on sd:4042) and closes the older one of that host (pack sd:4641). Each job
prints one line per finding, so a smaller set is a subset of the open
report's lines and folds into it (pack sd:4658, sd:4664). An
acknowledged report takes the same findings while they repeat without a
break on the host, and stays done (pack sd:4629).
"""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from sd_db import connect
from sd_db.migrate import initialise
from sd_db import reporting, workflow
from sd_db.ship import this_host

ROOT = Path(__file__).resolve().parent.parent

# A notify.sh call that sends a scheduled report: `-k status` or `-k brief`.
SENDER = re.compile(r"notify.*\s-k\s+(status|brief)\b|\$NOTIFY\".*\s-k\s+(status|brief)\b", re.I)
# The obsidian digest mails an HTML card with signed action buttons, which
# Reports cannot show; whether it keeps mailing is an open sd:3537 question.
PENDING = {"local-obsidian-review/obsidian-review.sh"}


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
        # notify.conf now serves only report_unrecorded, the lost-report push.
        self.assertIn("report is not recorded", (ROOT / "local-cron-jobs/notify.conf.example").read_text())

    def test_no_findings_reason_ends_in_the_date(self):
        """sd:4182: a date in the reason filed a report a night for unchanged
        findings. weekly-digest is a weekly summary, not a finding: each week
        is its own report, or a fold would keep last week's body."""
        dated = [f"{name}:{number}" for name in tracked_scripts()
                 for number, line in enumerate((ROOT / name).read_text(errors="replace").splitlines(), 1)
                 if "SD_REPORT_ATTENTION: " in line and "date '+%Y-%m-%d'" in line
                 and name != "local-weekly-digest/weekly-digest.sh"]
        self.assertEqual(dated, [])


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
        # brew-doctor sources ../lib/bounded.sh for its bounded brew calls (sd:3536).
        (self.tmp / "lib").mkdir()
        shutil.copy(ROOT / "lib/bounded.sh", self.tmp / "lib")
        self.sent = self.tmp / "notify-calls"
        _stub(self.tmp / "local-notify/notify.sh", f'printf "%s\\n" "$@" >> "{self.sent}"\n')
        self.bin = self.tmp / "bin"
        # brew doctor prints the doctor file and exits with the rc file:
        # two warnings that no auto-fix touches, unless a test changes them.
        self.doctor = self.tmp / "doctor"
        self.doctor.write_text("Warning: one\nWarning: two\n")
        self.doctor_rc = self.tmp / "doctor-rc"
        self.doctor_rc.write_text("1\n")
        _stub(self.bin / "brew", f'case "$1" in doctor) cat "{self.doctor}"; exit "$(cat "{self.doctor_rc}")";; esac\nexit 0\n')
        # The host file stands for `hostname -s`; the day file for the date.
        self.host = self.tmp / "host"
        self.host.write_text("fixturehost\n")
        _stub(self.bin / "hostname", f'cat "{self.host}"\n')
        self.day = self.tmp / "day"
        self.day.write_text("2026-10-10\n")
        _stub(self.bin / "date", f'[ "$1" = "+%Y-%m-%d" ] && {{ cat "{self.day}"; exit 0; }}\nexec /bin/date "$@"\n')
        self.database = self.tmp / "sd.db"
        initialise(self.database)

    def doctor_run(self, run_id, day="2026-10-10", ingest_host=None):
        """One brew-doctor run on DAY, its output ingested as cron-jobs ingests it, on INGEST_HOST if named."""
        self.day.write_text(day + "\n")
        run = subprocess.run(["sh", str(self.tmp / "local-brew-doctor/brew-doctor.sh"), "run"],
                             capture_output=True, text=True, timeout=120,
                             env={"HOME": str(self.tmp), "PATH": f"{self.bin}:/usr/bin:/bin"})
        log = self.tmp / f"{run_id}.log"
        log.write_text(run.stdout + run.stderr)
        with closing(connect(self.database)) as connection, \
                mock.patch("sd_db.ship.socket.gethostname", return_value=ingest_host or this_host()):
            state = reporting.ingest_log(connection, job="brew-doctor-nightly", run_id=run_id,
                                         started=f"{day}T08:15:00Z", ended=f"{day}T08:16:00Z",
                                         exit_code=run.returncode, log_path=str(log), offset=0)
        return run, state

    def open_reports(self):
        with closing(connect(self.database)) as connection:
            return [row["id"] for row in reporting.reports(connection) if row["status"] != "done"]

    def test_brew_doctor_warnings_open_a_report_that_needs_attention(self):
        run, state = self.doctor_run("r1")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse(self.sent.exists(), self.sent.read_text() if self.sent.exists() else "")
        self.assertEqual(state["recorded"], "report")
        # sd:4664: one line per warning, each with its host and a checksum of that warning's text.
        self.assertRegex(state["why"], r"^brew doctor on fixturehost: Warning: one \[\d+\]\n"
                                       r"brew doctor on fixturehost: Warning: two \[\d+\]$")
        self.assertIn("Warning: two", state["item"]["body"])

    def test_a_shrinking_set_of_warnings_folds_into_the_open_report(self):
        """PIN of the pack (sd:4658): one warning fixed is the same report with
        the smaller body, not a new report that supersedes it (sd:4664)."""
        _, first = self.doctor_run("r1", "2026-10-10")
        self.doctor.write_text("Warning: one\n")
        _, second = self.doctor_run("r2", "2026-10-11")
        self.assertEqual((second["recorded"], second["item"]["id"]), ("folded", first["item"]["id"]), second)
        self.assertNotIn("Warning: two", second["item"]["body"])
        self.assertEqual(self.open_reports(), [first["item"]["id"]])

    def test_a_package_cleared_from_a_warning_folds_into_the_open_report(self):
        """sd:4664: a warning's listed packages are each their own line, so
        one package fixed leaves a subset that folds (pack sd:4658)."""
        listing = "Warning: Some installed formulae are deprecated.\nReplace them:\n  example-a\n  example-b\n\n"
        self.doctor.write_text(listing)
        _, first = self.doctor_run("r1", "2026-10-10")
        self.doctor.write_text(listing.replace("  example-b\n", ""))
        _, second = self.doctor_run("r2", "2026-10-11")
        self.assertEqual((second["recorded"], second["item"]["id"]), ("folded", first["item"]["id"]), second)

    def test_the_same_warnings_on_the_next_night_fold_into_the_open_report(self):
        """sd:4182: unchanged findings keep one open report, which counts the nights."""
        _, first = self.doctor_run("r1", "2026-10-10")
        _, second = self.doctor_run("r2", "2026-10-11")
        self.assertEqual((second["recorded"], second["count"]), ("folded", 2), second)
        self.assertEqual(second["why"], first["why"])
        self.assertEqual(self.open_reports(), [first["item"]["id"]])

    def test_a_changed_warning_files_its_own_report_and_supersedes_the_older_one(self):
        """sd:3537: a checksum with digits erased folded worker2 into
        worker1's report, which then showed only worker1. PIN of the pack
        (rule e, sd:4641): the new report closes the older one of the job and
        host, so one findings report per host stays open."""
        self.doctor.write_text("Warning: worker1 is unlinked\n")
        _, first = self.doctor_run("r1", "2026-10-10")
        self.doctor.write_text("Warning: worker2 is unlinked\n")
        _, second = self.doctor_run("r2", "2026-10-11")
        self.assertEqual(second["recorded"], "report", second)
        self.assertIn("worker2", second["item"]["body"])
        self.assertEqual(self.open_reports(), [second["item"]["id"]])
        with closing(connect(self.database)) as connection:
            older = workflow.item_state(connection, first["item"]["id"])
        self.assertEqual(older["item"]["status"], "done")
        self.assertIn(f"superseded by sd:{second['item']['id']}",
                      [n["body"] for n in older["notes"] if n["kind"] == "status_change"][-1])

    def test_the_same_warnings_fold_into_the_open_report(self):
        self.doctor_run("r1")
        _, state = self.doctor_run("r2")
        self.assertEqual(state["recorded"], "folded", state)

    def test_warnings_that_return_fold_into_their_open_report(self):
        """A clean night between files nothing and closes nothing."""
        _, first = self.doctor_run("r1", "2026-10-10")
        self.doctor.write_text("")
        self.doctor_rc.write_text("0\n")
        _, clean = self.doctor_run("r2", "2026-10-11")
        self.assertEqual(clean["recorded"], "heartbeat", clean)
        self.doctor.write_text("Warning: one\nWarning: two\n")
        self.doctor_rc.write_text("1\n")
        _, back = self.doctor_run("r3", "2026-10-12")
        self.assertEqual((back["recorded"], back["item"]["id"]), ("folded", first["item"]["id"]), back)

    def test_an_acknowledged_report_takes_an_unbroken_repeat_and_stays_done(self):
        """PIN of the pack (sd:4629): the same findings the next night, with no
        break on the host, fold into the done report and open nothing."""
        _, first = self.doctor_run("r1", "2026-10-10")
        with closing(connect(self.database)) as connection:
            reporting.acknowledge(connection, first["item"]["id"], expected_revision=first["revision"],
                                  who="test", resolve_ingest_followups=True)
        _, second = self.doctor_run("r2", "2026-10-11")
        self.assertEqual((second["recorded"], second["item"]["id"]), ("folded", first["item"]["id"]), second)
        self.assertEqual(second["item"]["status"], "done", second)
        self.assertEqual(self.open_reports(), [])

    def test_an_acknowledged_report_is_followed_by_a_new_one_after_a_clean_night(self):
        """PIN of the pack (sd:4629): a clean run breaks the chain, so the same
        findings coming back are a new incident with a fresh report."""
        _, first = self.doctor_run("r1", "2026-10-10")
        with closing(connect(self.database)) as connection:
            reporting.acknowledge(connection, first["item"]["id"], expected_revision=first["revision"],
                                  who="test", resolve_ingest_followups=True)
        warnings = self.doctor.read_text()
        self.doctor.write_text("")
        self.doctor_rc.write_text("0\n")
        _, clean = self.doctor_run("r2", "2026-10-11")
        self.assertEqual(clean["recorded"], "heartbeat", clean)
        self.doctor.write_text(warnings)
        self.doctor_rc.write_text("1\n")
        _, back = self.doctor_run("r3", "2026-10-12")
        self.assertEqual((back["recorded"], back["why"]), ("report", first["why"]), back)
        self.assertEqual(self.open_reports(), [back["item"]["id"]])

    def test_each_host_keeps_its_own_report(self):
        """PIN: hosts share a job name in the one database, so the host stays
        in the reason, or one host's report would hide another's; another
        host's report supersedes nothing here (pack sd:4641)."""
        _, first = self.doctor_run("r1")
        self.host.write_text("otherhost\n")
        _, second = self.doctor_run("r2", ingest_host="otherhost")
        self.assertEqual(second["recorded"], "report", second)
        self.assertNotEqual(second["item"]["id"], first["item"]["id"])
        self.assertEqual(sorted(self.open_reports()), sorted([first["item"]["id"], second["item"]["id"]]))

    def test_a_doctor_that_fails_without_a_warning_fails_the_run(self):
        """A crash used to read as "brew doctor: clean"."""
        self.doctor.write_text("Error: something broke\n")
        run, state = self.doctor_run("r1")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertEqual((state["recorded"], state["why"]), ("report", f"job exited 1 on {this_host()}"))

    def test_a_failed_run_does_not_fold_into_the_findings_report(self):
        """PIN: a non-zero exit is the pack's basis, `job exited N on <host>`
        (sd:4629), so a crash after a night of warnings opens its own report.
        A failure report is never superseded (pack sd:4641)."""
        _, first = self.doctor_run("r1", "2026-10-10")
        self.doctor.write_text("Error: something broke\n")
        _, crash = self.doctor_run("r2", "2026-10-11")
        self.assertEqual((crash["recorded"], crash["why"]), ("report", f"job exited 1 on {this_host()}"), crash)
        self.assertNotEqual(crash["item"]["id"], first["item"]["id"])
        self.doctor.write_text("Warning: three\n")
        _, changed = self.doctor_run("r3", "2026-10-12")
        self.assertEqual(sorted(self.open_reports()), sorted([crash["item"]["id"], changed["item"]["id"]]))

if __name__ == "__main__":
    unittest.main(argv=sys.argv)
