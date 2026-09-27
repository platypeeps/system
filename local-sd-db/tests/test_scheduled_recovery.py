"""The real cron wrapper installs and runs database maintenance safely."""
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sd_db import connect, initialise
from sd_db.operations import LABEL_PREFIX, PREFIX
from sd_db.shadow_sync import TRACKER, read_watermark, write_watermark
from sd_db.testing.home import FixtureHome

SYSTEM = Path(__file__).resolve().parents[2]
PACK = Path(os.environ.get("SD_ACCEPTANCE_PACK", SYSTEM.parent / "pack"))


class ScheduledRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = FixtureHome(self.root)
        self.cron = self.root / "system/local-cron-jobs"
        (self.cron / "jobs").mkdir(parents=True)
        shutil.copy2(SYSTEM / "local-cron-jobs/cron-jobs.sh", self.cron / "cron-jobs.sh")
        for job in ("sd-db-backup", "sd-db-backup-hourly", "shadow-sync-nightly"):
            shutil.copy2(SYSTEM / f"local-cron-jobs/jobs/{job}.job", self.cron / f"jobs/{job}.job")
        (self.cron.parent / "local-sd-db").symlink_to(SYSTEM / "local-sd-db", target_is_directory=True)
        self.env = self.fixture.environment({"PATH": os.environ["PATH"], "PYTHON": sys.executable})
        # cron-jobs.sh names its plists with the same prefix the library reads.
        self.env["SYSTEM_TOOLS_LABEL_PREFIX"] = LABEL_PREFIX
        self.env["SD_DB_BACKUP_DESTINATION"] = str(self.fixture.backups)
        self.env["SD_NOTIFY"] = str(self.fixture.stubs.bin / "local-notify")
        # Prevent a failure notification from opening a real Notification Center dialog.
        notify = self.fixture.stubs.bin / "osascript"
        notify.write_text("#!/bin/sh\nexit 0\n")
        notify.chmod(0o755)
        initialise(home=self.fixture.path)

    def run_job(self, *args):
        return subprocess.run(["/bin/bash", str(self.cron / "cron-jobs.sh"), *args],
                              env=self.env, capture_output=True, text=True, timeout=30)

    def test_both_profile_jobs_install_with_their_real_calendar_and_verify(self):
        # The machine profile that lists these jobs is private configuration and
        # is not in this repository; the jobs themselves are.
        for job, minute in (("sd-db-backup", 10), ("shadow-sync-nightly", 20)):
            result = self.run_job("install", job)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            path = self.fixture.path / f"Library/LaunchAgents/{PREFIX}{job}.plist"
            plist = plistlib.loads(path.read_bytes())
            calendar = plist["StartCalendarInterval"]
            if isinstance(calendar, list):
                calendar = calendar[0]
            self.assertEqual((calendar["Hour"], calendar["Minute"]), (2, minute))
            self.assertEqual(self.run_job("verify", job).returncode, 0)

    def test_the_hourly_job_installs_every_hour_at_fifty(self):
        result = self.run_job("install", "sd-db-backup-hourly")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = self.fixture.path / f"Library/LaunchAgents/{PREFIX}sd-db-backup-hourly.plist"
        calendar = plistlib.loads(path.read_bytes())["StartCalendarInterval"]
        self.assertEqual(calendar, [{"Minute": 50}])
        self.assertEqual(self.run_job("verify", "sd-db-backup-hourly").returncode, 0)

    def hourly_command(self, **environment):
        env = {key: value for key, value in os.environ.items()
               if key != "SD_DB_BACKUP_HOURLY_DESTINATION"}
        result = subprocess.run(
            ["/bin/bash", "-c", '. "$1"; printf "%s" "$JOB_COMMAND"', "job",
             str(self.cron / "jobs/sd-db-backup-hourly.job")],
            env=dict(env, ROOT=str(self.cron), **environment), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_the_hourly_command_requires_the_mounted_local_disk(self):
        command = self.hourly_command()
        self.assertIn('backup --destination "/Volumes/local/Backup Local/sd-backups"'
                      " --require-mount /Volumes/local --keep-days 7 --no-row-prune", command)
        # Naming the destination stands the mount check aside, as it does for
        # the nightly job; it is how this suite runs the job at all.
        command = self.hourly_command(SD_DB_BACKUP_HOURLY_DESTINATION="/tmp/somewhere")
        self.assertNotIn("--require-mount", command)
        self.assertIn('--destination "/tmp/somewhere" --keep-days 7 --no-row-prune', command)

    def test_real_hourly_backup_runs_through_cron_without_a_row_prune(self):
        self.env["SD_DB_BACKUP_HOURLY_DESTINATION"] = str(self.fixture.backups)
        for _ in range(2):
            result = self.run_job("run", "sd-db-backup-hourly")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("row prune skipped (--no-row-prune)", result.stdout)
        self.assertEqual(len(list(self.fixture.backups.glob("*/sd.db"))), 2)
        self.assertFalse((self.cron / "logs/.sd-db-backup-hourly.lock").exists())

    def test_real_backup_runs_through_cron_and_can_restart(self):
        for _ in range(2):
            result = self.run_job("run", "sd-db-backup")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("restored and compared", result.stdout)
        self.assertEqual(len(list(self.fixture.backups.glob("*/sd.db"))), 2)
        self.assertFalse((self.cron / "logs/.sd-db-backup.lock").exists())

    def test_command_failure_is_logged_and_does_not_leak_overlap_lock(self):
        pack = self.root / "pack/bin"
        pack.mkdir(parents=True)
        command = pack / "sd"
        command.write_text('#!/bin/sh\n[ "$1 $2" = "shadow sync" ] || exit 9\nexit 7\n')
        command.chmod(0o755)
        self.env["SD_PACK_ROOT"] = str(pack.parent)
        result = self.run_job("run", "shadow-sync-nightly")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn("shadow-sync-nightly", (self.cron / "logs/failures.log").read_text())
        self.assertFalse((self.cron / "logs/.shadow-sync-nightly.lock").exists())
        command.write_text('#!/bin/sh\n[ "$1 $2" = "shadow sync" ] || exit 9\nexit 0\n')
        result = self.run_job("run", "shadow-sync-nightly")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("done", result.stdout)

    def test_real_shadow_collector_failure_reaches_cron_and_can_recover(self):
        self.assertTrue((PACK / "bin/sd").is_file(),
                        "set SD_ACCEPTANCE_PACK to the companion pack checkout")
        self.env["SD_PACK_ROOT"] = str(PACK)
        self.env["SD_REPORT_BIN"] = "/usr/bin/true"
        # The real CLI and collector run; only external commands are doubles.
        python = self.fixture.stubs.bin / "python3"
        python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        python.chmod(0o755)
        gh = self.fixture.stubs.bin / "gh"
        gh.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
root = Path(os.environ["SD_FIXTURE_STUBS"])
args = sys.argv[1:]
with (root / "shadow-gh.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\\n")
if args == ["auth", "status"]:
    raise SystemExit(0)
if args[:2] != ["api", "graphql"]:
    raise SystemExit(9)
if not (root / "shadow-healthy").exists():
    print("fixture collector unavailable", file=sys.stderr)
    raise SystemExit(7)
print(json.dumps({"data": {"search": {"issueCount": 0, "nodes": [],
    "pageInfo": {"hasNextPage": False, "endCursor": None}}}}))
''')
        gh.chmod(0o755)
        notifier = self.fixture.stubs.bin / "osascript"
        notifier.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
with (Path(os.environ["SD_FIXTURE_STUBS"]) / "shadow-notifications.jsonl").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
''')
        notifier.chmod(0o755)
        before = "2026-01-01T00:00:00Z"
        with closing(connect(home=self.fixture.path)) as connection:
            write_watermark(connection, TRACKER, before, "2025-12-31T23:00:00Z")

        result = self.run_job("run", "shadow-sync-nightly")

        self.assertIn("cursor held: assigned: fixture collector unavailable", result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        calls = [json.loads(line) for line in
                 (self.fixture.stubs.data / "shadow-gh.jsonl").read_text().splitlines()]
        self.assertEqual(calls[0], ["auth", "status"])
        self.assertTrue(any(call[:2] == ["api", "graphql"] for call in calls))
        with closing(connect(home=self.fixture.path)) as connection:
            self.assertEqual(read_watermark(connection), before)
        failures = (self.cron / "logs/failures.log").read_text()
        self.assertIn("shadow-sync-nightly FAILED rc=1", failures)
        notifications = self.fixture.stubs.data / "shadow-notifications.jsonl"
        alerts = [json.loads(line) for line in notifications.read_text().splitlines()]
        self.assertEqual(len(alerts), 1)
        self.assertIn("shadow-sync-nightly", alerts[0][1])
        self.assertFalse((self.cron / "logs/.shadow-sync-nightly.lock").exists())

        (self.fixture.stubs.data / "shadow-healthy").touch()
        result = self.run_job("run", "shadow-sync-nightly")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cursor moved", result.stdout)
        self.assertIn("done", result.stdout)
        with closing(connect(home=self.fixture.path)) as connection:
            self.assertNotEqual(read_watermark(connection), before)
        self.assertEqual((self.cron / "logs/failures.log").read_text(), failures)
        self.assertEqual(len(notifications.read_text().splitlines()), 1)
        self.assertFalse((self.cron / "logs/.shadow-sync-nightly.lock").exists())
