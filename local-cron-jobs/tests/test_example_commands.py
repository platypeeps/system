"""The catalogue's JOB_COMMANDs read their environment when they run, not when loaded.

`cmd_exec` sources the job file (`load_job`) before it sources
`~/.config/shell/env.sh`, and under launchd the plist hands the run only
`HOME` and `PATH`. A `${VAR}` expanded inside a double-quoted JOB_COMMAND is
therefore read before env.sh has set it, and an operator's SD_PACK_ROOT there
never reached agent-meter or shadow-sync (sd:1238, 332d7ec72031). Escaped, the
expansion happens in the `bash -c` that runs the command, after env.sh.

Each example that names SD_PACK_ROOT is found on disk, not listed here, and
run through `cron-jobs.sh exec` the way launchd runs it, with SD_PACK_ROOT
set only in the fixture's env.sh.
"""

import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_cron_jobs import FOLDER, Fixture

EXAMPLES = FOLDER / "examples"

# A pack interpreter, `sd` and `sd-ship` that only say they were the ones run.
RECORDER = '#!/bin/sh\nprintf "%s\\n" "$0" >> "$PACK_MARKER"\n'


class PackRootFromEnvShTest(unittest.TestCase):
    def test_every_pack_example_reads_sd_pack_root_from_env_sh(self):
        jobs = sorted(path for path in EXAMPLES.glob("*.job") if "SD_PACK_ROOT" in path.read_text())
        self.assertTrue(jobs, "no example names SD_PACK_ROOT; this test checks nothing")
        for job in jobs:
            with self.subTest(job=job.name):
                fx = Fixture()
                self.addCleanup(fx.destroy)
                pack = fx.tmp / "custom-pack"
                marker = fx.tmp / "pack-ran"
                for relative in (".venv/bin/python", "bin/sd", "bin/sd-ship"):
                    program = pack / relative
                    program.parent.mkdir(parents=True, exist_ok=True)
                    program.write_text(RECORDER)
                    program.chmod(0o755)
                # The working folders the examples name beside the checkout.
                for sibling in ("local-agent-meter",):
                    (fx.tmp / sibling).mkdir(exist_ok=True)
                env_sh = fx.home / ".config" / "shell" / "env.sh"
                env_sh.parent.mkdir(parents=True)
                env_sh.write_text(f'export SD_PACK_ROOT="{pack}"\nexport PACK_MARKER="{marker}"\n')
                fx.write_job(job.stem, job.read_text())
                result = fx.exec_job(job.stem)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                ran = marker.read_text().splitlines() if marker.exists() else []
                self.assertEqual(len(ran), 1, ran)
                self.assertTrue(pathlib.Path(ran[0]).is_relative_to(pack), ran)



class LaneRunJobTest(unittest.TestCase):
    """The lane job is one file for every machine: `sd-ship lane run --hosted` (sd:3003).

    It names no checkout, so a repository moved to another lane host needs no
    new file, and it names no retired flag.
    """

    JOB = EXAMPLES / "lane-run.job"

    def command(self):
        return self.JOB.read_text().split("JOB_COMMAND=", 1)[1]

    def test_the_job_runs_hosted_lanes_and_names_no_checkout_or_retired_flag(self):
        text = self.command()
        self.assertTrue(text.rstrip().endswith('lane run --hosted"'), text)
        for retired in ("-C", "--satellite-only", "change-me"):
            self.assertNotIn(retired, text)

    def test_the_job_runs_the_pack_sd_ship_with_exactly_those_words(self):
        fx = Fixture()
        self.addCleanup(fx.destroy)
        pack, marker = fx.tmp / "custom-pack", fx.tmp / "pack-ran"
        ship = pack / "bin" / "sd-ship"
        ship.parent.mkdir(parents=True)
        ship.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$PACK_MARKER"\n')
        ship.chmod(0o755)
        env_sh = fx.home / ".config" / "shell" / "env.sh"
        env_sh.parent.mkdir(parents=True)
        env_sh.write_text(f'export SD_PACK_ROOT="{pack}"\nexport PACK_MARKER="{marker}"\n')
        fx.write_job(self.JOB.stem, self.JOB.read_text())
        result = fx.exec_job(self.JOB.stem)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(marker.read_text().splitlines(), ["lane run --hosted"])


#: The pack the gate pins (`.sd-pack-rev`), which the hub runs once the pin lands.
PINNED_PACK = pathlib.Path(os.environ.get("SD_ACCEPTANCE_PACK") or os.environ.get("SD_PACK_ROOT")
                           or pathlib.Path.home() / "repos/platypeeps/sd-ai-command-pack")

# A pack `sd` that records its words; with SPAWN_PID set, it starts a sleeper
# through the pinned pack's own `build_run.spawn`, the call the tick makes for
# a supervisor, then outlives the job's limit. SPAWN_STALL seconds pass first,
# as a loaded gate delays the tick's start.
FAKE_DISPATCH = """#!{python}
import os, pathlib, sys, time
with open(os.environ["PACK_MARKER"], "a") as out:
    out.write(" ".join(sys.argv[1:]) + "\\n")
if os.environ.get("SPAWN_PID"):
    time.sleep(float(os.environ.get("SPAWN_STALL", "0")))
    sys.path.insert(0, {lib!r})
    from sd_db import build_run
    pid = build_run.spawn([sys.executable, "-c", "import time; time.sleep(60)"],
                          log=pathlib.Path(os.environ["SPAWN_PID"]).with_suffix(".log"))
    pathlib.Path(os.environ["SPAWN_PID"]).write_text(str(pid))
    time.sleep(30)
"""


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class BuildDispatchJobTest(unittest.TestCase):
    """The hub's build-dispatch tick: `sd assignments dispatch` every two minutes (sd:6098).

    The tick spawns each supervisor in a session of its own, so the job's
    90 s limit and its lock must not reach one (design, Dispatcher).
    """

    JOB = EXAMPLES / "build-dispatch.job"

    def test_the_job_ticks_every_two_minutes_with_a_90_second_limit(self):
        text = self.JOB.read_text()
        self.assertEqual(re.search(r'^JOB_SCHEDULE="([^"]+)"', text, re.M).group(1), "*/2 * * * *")
        self.assertEqual(re.search(r'^JOB_TIMEOUT="([^"]+)"', text, re.M).group(1), "90s")

    def run_job(self, job_text=None, **extra):
        fx = self.fx = Fixture()
        self.addCleanup(fx.destroy)
        pack, self.marker = fx.tmp / "custom-pack", fx.tmp / "pack-ran"
        sd = pack / "bin" / "sd"
        sd.parent.mkdir(parents=True)
        sd.write_text(FAKE_DISPATCH.format(python=sys.executable, lib=str(PINNED_PACK / "lib")))
        sd.chmod(0o755)
        env_sh = fx.home / ".config" / "shell" / "env.sh"
        env_sh.parent.mkdir(parents=True)
        env_sh.write_text(f'export SD_PACK_ROOT="{pack}"\nexport PACK_MARKER="{self.marker}"\n')
        fx.write_job(self.JOB.stem, job_text or self.JOB.read_text())
        return fx.exec_job(self.JOB.stem, extra_env=extra)

    def test_the_job_runs_the_pack_sd_with_exactly_those_words(self):
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.marker.read_text().splitlines(), ["assignments dispatch"])

    def test_the_pinned_pack_has_the_dispatch_verb_and_its_supervise_flag(self):
        """The job names a verb of the pinned pack, not only of a later one.

        The tick spawns its supervisors as `dispatch --supervise RUN` (pack #1669).
        """
        result = subprocess.run(
            [sys.executable, str(PINNED_PACK / "bin" / "sd"), "assignments", "dispatch", "--help"],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, f"{PINNED_PACK}: {result.stdout}{result.stderr}")
        self.assertIn("--supervise", result.stdout)

    def test_a_spawned_supervisor_outlives_the_limit_and_holds_no_lock(self):
        """A tick killed at its limit leaves its supervisor running, and the next slot runs.

        The limit here is 10 s, not 90 s. A supervisor left in the job's process
        group would die with the TERM; one that kept fd 8 open would hold the
        job's flock, and every later slot would log `skipped`. The fake stalls
        3 s before it spawns, as a loaded gate delays Python's start and the
        sd_db import: a 2 s limit killed the tick before the spawn (sd:6742).
        """
        pid_file = pathlib.Path(tempfile.mkdtemp(prefix="build-dispatch.")) / "supervisor.pid"
        self.addCleanup(shutil.rmtree, pid_file.parent, ignore_errors=True)
        # ponytail: a wall-clock limit; a load that delays the spawn 10 s still fails it.
        text = self.JOB.read_text().replace('JOB_TIMEOUT="90s"', 'JOB_TIMEOUT="10s"')
        self.assertNotEqual(text, self.JOB.read_text())
        result = self.run_job(text, SPAWN_PID=str(pid_file), SPAWN_STALL="3")
        self.assertTrue(pid_file.exists(), result.stdout + result.stderr)
        pid = int(pid_file.read_text())
        self.addCleanup(lambda: alive(pid) and os.kill(pid, signal.SIGKILL))
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertTrue(alive(pid), "the supervisor died with the tick")
        again = self.fx.exec_job(self.JOB.stem)
        log = (self.fx.folder / "logs" / f"{self.JOB.stem}.log").read_text()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertNotIn("skipped", log)
        self.assertEqual(self.marker.read_text().splitlines(), ["assignments dispatch"] * 2)

# A scan-for-secrets.sh that exits with SCAN_RC for `critical`, and records
# each call; `mask` exits 0, so only the record shows it ran.
FAKE_SCAN = """#!/bin/sh
printf '%s\\n' "$1" >> "$SCAN_MARKER"
case $1 in
  mask) exit 0 ;;
  critical) exit "${SCAN_RC:-0}" ;;
esac
exit 64
"""


class SecretScanWeeklyTest(unittest.TestCase):
    """The weekly job scans and pages; it never masks (sd:1254). An
    unattended in-place rewrite can lose a line a session appends between
    the last check and the truncate, so masking stays a step the operator
    runs by hand. The job's exit code is the scan's: 2 pages."""

    def run_job(self, scan_rc):
        fx = Fixture()
        self.addCleanup(fx.destroy)
        scan = fx.tmp / "local-scan-for-secrets" / "scan-for-secrets.sh"
        scan.parent.mkdir()
        scan.write_text(FAKE_SCAN)
        marker = fx.tmp / "scan-calls"
        job = EXAMPLES / "secret-scan-weekly.job"
        fx.write_job(job.stem, job.read_text())
        result = fx.exec_job(job.stem, extra_env={"SCAN_MARKER": str(marker), "SCAN_RC": str(scan_rc)})
        return result, marker.read_text().splitlines() if marker.exists() else []

    def test_the_job_never_masks(self):
        self.assertNotIn("mask", (EXAMPLES / "secret-scan-weekly.job").read_text().split("JOB_COMMAND=", 1)[1])
        result, calls = self.run_job(0)
        self.assertEqual(calls, ["critical"], result.stdout + result.stderr)

    def test_the_exit_code_is_the_scans(self):
        for scan_rc in (0, 2):
            with self.subTest(scan=scan_rc):
                result, _ = self.run_job(scan_rc)
                self.assertEqual(result.returncode, scan_rc, result.stdout + result.stderr)

# A stand-in for sd-db, the interpreter that runs offsite-verify, and
# mirror-sync.sh: each records its call and exits with its own code.
RECORD = '#!/bin/sh\nprintf "%s %s\\n" "{name}" "$*" >> "$CHAIN_MARKER"\nexit "${{{rc}:-0}}"\n'
# MIRROR_SLEEP holds the copy the way the share did under nas-full-copy (sd:4912).
MIRROR_RECORD = ('#!/bin/sh\nprintf "mirror %s %s %s\\n" "$MIRROR_SYNC_CONF" "$MIRROR_SYNC_ADDITIVE" "$*"'
                 ' >> "$CHAIN_MARKER"\n[ -z "$MIRROR_SLEEP" ] || sleep "$MIRROR_SLEEP"\nexit "${MIRROR_RC:-0}"\n')


class HourlyBackupJobTest(unittest.TestCase):
    """The hourly snapshot, then its copy to the NAS and the NAS prune, in one job (sd:4009, sd:4011).

    The copy runs only after a snapshot that succeeded and a preflight that
    proved the NAS folder is on the share; the prune only after the copy. A
    separate schedule would not order them: launchd runs missed jobs together at wake.
    """

    JOB = EXAMPLES / "sd-db-backup-hourly.job"
    #: A placeholder the operator replaces, in this job and in the pair list alike.
    NAS = "/Volumes/change-me-nas-share/Backup/sd-backups-hourly"

    def run_job(self, job_text=None, **codes):
        fx = self.fx = Fixture()
        self.addCleanup(fx.destroy)
        # PRUNE_SLEEP holds the prune the way a slow share did (sd:4690).
        sd_db = RECORD.format(name="sd-db", rc="SNAPSHOT_RC").replace(
            "exit", 'case "$*" in *--prune-only*) [ -z "$PRUNE_SLEEP" ] || sleep "$PRUNE_SLEEP" ;; esac\nexit', 1)
        programs = {
            fx.home / "repos/platypeeps/sd-ai-command-pack/bin/sd-db": sd_db,
            fx.tmp / "python": RECORD.format(name="python", rc="PREFLIGHT_RC"),
            fx.tmp / "local-mirror-sync/mirror-sync.sh": MIRROR_RECORD,
        }
        for path, text in programs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            path.chmod(0o755)
        marker = fx.tmp / "chain-calls"
        env = {"CHAIN_MARKER": str(marker), "SD_DB_PYTHON": str(fx.tmp / "python"), **codes}
        fx.write_job(self.JOB.stem, job_text or self.JOB.read_text())
        result = fx.exec_job(self.JOB.stem, extra_env=env)
        return result, marker.read_text().splitlines() if marker.exists() else []

    def test_a_slow_share_ends_the_prune_at_its_own_bound(self):
        """REGRESSION (sd:4690). The prune reads the NAS share; at 01:50 a slow
        share held it until the job's 20-minute limit failed the whole run,
        and the log did not say which step hung. The prune now runs under
        st_step with a 300 s bound; the run shrinks it to 2 s."""
        text = self.JOB.read_text()
        self.assertIn('st_step 300 \\"$HOME/repos/platypeeps/sd-ai-command-pack/bin/sd-db\\" backup --prune-only', text)
        started = time.monotonic()
        result, calls = self.run_job(text.replace("st_step 300 ", "st_step 2 "), PRUNE_SLEEP="20")
        self.assertLess(time.monotonic() - started, 15, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertEqual([call.split()[0] for call in calls], ["sd-db", "python", "mirror", "sd-db"])
        log = result.stdout + result.stderr + "".join(p.read_text() for p in (self.fx.folder / "logs").glob("*.log"))
        self.assertIn("timed out after 2s: ", log)

    def test_a_slow_share_ends_the_copy_at_its_own_bound(self):
        """REGRESSION (sd:4912). At 23:50 on 2026-10-15 the NAS copy held the
        run until the job's limit, and the log named no step. The copy now
        runs under st_step with a 2700 s bound; the run shrinks it to 2 s.
        Nothing is pruned after a copy that did not finish."""
        text = self.JOB.read_text()
        self.assertIn("st_step 2700 env MIRROR_SYNC_CONF=mirrors-nas-hourly.conf MIRROR_SYNC_ADDITIVE=1 sh ", text)
        started = time.monotonic()
        result, calls = self.run_job(text.replace("st_step 2700 ", "st_step 2 "), MIRROR_SLEEP="20")
        self.assertLess(time.monotonic() - started, 15, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertEqual([call.split()[0] for call in calls], ["sd-db", "python", "mirror"])
        log = result.stdout + result.stderr + "".join(p.read_text() for p in (self.fx.folder / "logs").glob("*.log"))
        self.assertIn("timed out after 2s: env MIRROR_SYNC_CONF=mirrors-nas-hourly.conf", log)

    def test_the_bounded_steps_end_inside_the_job_limit(self):
        """sd:4912: each bounded step ends before JOB_TIMEOUT, with five
        minutes left for the snapshot and the preflight, so a stuck step is
        named by its own bound and not by the job's limit. The longest run
        before this, at 02:50 on 2026-10-17, spent 2442 s before its prune."""
        text = self.JOB.read_text()
        limit = re.search(r'^JOB_TIMEOUT="(\d+)m"$', text, re.M)
        self.assertTrue(limit, "JOB_TIMEOUT is not in minutes")
        bounds = [int(n) for n in re.findall(r"st_step (\d+) ", text)]
        self.assertEqual(len(bounds), 2, bounds)
        self.assertLessEqual(sum(bounds) + 300, int(limit.group(1)) * 60, (bounds, limit.group(0)))
        self.assertLess(int(limit.group(1)), 60, "a run must free its lock before the next hourly slot")

    def test_the_snapshot_then_the_preflight_then_the_additive_copy_then_the_prune(self):
        result, calls = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(calls), 4, calls)
        self.assertTrue(calls[0].startswith("sd-db backup --destination /Volumes/local/Backup Local/sd-backups"
                                            " --require-mount /Volumes/local --keep 24 --no-row-prune"), calls)
        self.assertTrue(calls[1].startswith("python "), calls)
        self.assertTrue(calls[1].endswith(f"offsite-verify.py --preflight-only --destination {self.NAS}"), calls)
        self.assertEqual(calls[2], "mirror mirrors-nas-hourly.conf 1 sync")
        self.assertEqual(calls[3], f"sd-db backup --prune-only --destination {self.NAS} --keep-days 7 --prune-unmanifested")

    def test_a_failed_copy_prunes_nothing(self):
        result, calls = self.run_job(MIRROR_RC="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([call.split()[0] for call in calls], ["sd-db", "python", "mirror"])

    def test_the_pair_list_example_copies_to_the_same_placeholder(self):
        conf = EXAMPLES.parent.parent / "local-mirror-sync/mirrors-nas-hourly.conf.example"
        pairs = [line for line in conf.read_text().splitlines() if line and not line.startswith("#")]
        self.assertEqual([pair.split("|")[1] for pair in pairs], [self.NAS])

    def test_a_failed_snapshot_copies_nothing(self):
        result, calls = self.run_job(SNAPSHOT_RC="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([call.split()[0] for call in calls], ["sd-db"])

    def test_a_failed_preflight_copies_nothing(self):
        result, calls = self.run_job(PREFLIGHT_RC="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([call.split()[0] for call in calls], ["sd-db", "python"])

    def test_a_named_destination_still_copies_to_the_nas(self):
        # A promoted satellite names its own local root (design, "Bulk storage
        # and paths"); its hourly copy is the recovery point all the same.
        result, calls = self.run_job(SD_DB_BACKUP_HOURLY_DESTINATION="/tmp/somewhere")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls[0], "sd-db backup --destination /tmp/somewhere --keep 24 --no-row-prune")
        self.assertEqual(calls[2:], ["mirror mirrors-nas-hourly.conf 1 sync",
                                     f"sd-db backup --prune-only --destination {self.NAS} --keep-days 7 --prune-unmanifested"])

    def test_an_empty_nas_folder_takes_the_snapshot_only(self):
        # A one-off or a test switches the copy off by name, not by moving the root.
        result, calls = self.run_job(SD_DB_BACKUP_HOURLY_NAS_FOLDER="")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call.split()[0] for call in calls], ["sd-db"])


class NightlyReviewJobTest(unittest.TestCase):
    """The nightly review runs `nightly-review.sh run` at 06:15, bounded at 30 minutes (sd:3638).

    06:15 keeps it after the night build and out of the 02:xx slot, which
    needs a scheduled wake. Its exit code is the script's, so a failed
    review pages like any failed job.
    """

    JOB = EXAMPLES / "nightly-review.job"

    def run_job(self, rc):
        fx = Fixture()
        self.addCleanup(fx.destroy)
        script = fx.tmp / "local-nightly-review" / "nightly-review.sh"
        script.parent.mkdir()
        script.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$REVIEW_MARKER"\nexit "${REVIEW_RC:-0}"\n')
        marker = fx.tmp / "review-calls"
        fx.write_job(self.JOB.stem, self.JOB.read_text())
        result = fx.exec_job(self.JOB.stem, extra_env={"REVIEW_MARKER": str(marker), "REVIEW_RC": str(rc)})
        return result, marker.read_text().splitlines() if marker.exists() else []

    def test_the_job_runs_at_06_15_with_a_30_minute_limit(self):
        text = self.JOB.read_text()
        self.assertIn('JOB_SCHEDULE="15 6 * * *"', text)
        self.assertIn('JOB_TIMEOUT="30m"', text)

    def test_the_job_runs_the_script_once_and_keeps_its_exit_code(self):
        for rc in (0, 1):
            with self.subTest(rc=rc):
                result, calls = self.run_job(rc)
                self.assertEqual(calls, ["run"], result.stdout + result.stderr)
                self.assertEqual(result.returncode, rc, result.stdout + result.stderr)


class MailIntakeDailyTest(unittest.TestCase):
    """The mail fetch runs on its own job, since the morning digest no longer fetches mail (sd:4869).

    `fetch` exits 3 on a quiet day, and cron-jobs counts every non-zero code as
    a failure, so the job turns 3 into 0 and keeps 1 a failure.
    """

    JOB = EXAMPLES / "mail-intake-daily.job"

    def run_job(self, fetch_rc):
        fx = Fixture()
        self.addCleanup(fx.destroy)
        script = fx.tmp / "local-mail-intake" / "mail-intake.sh"
        script.parent.mkdir()
        script.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$MAIL_MARKER"\nexit "$FETCH_RC"\n')
        marker = fx.tmp / "mail-calls"
        fx.write_job(self.JOB.stem, self.JOB.read_text())
        result = fx.exec_job(self.JOB.stem, extra_env={"MAIL_MARKER": str(marker), "FETCH_RC": str(fetch_rc)})
        return result, marker.read_text().splitlines() if marker.exists() else []

    def test_a_quiet_day_passes_and_an_error_fails(self):
        for fetch_rc, job_rc in ((0, 0), (3, 0), (1, 1)):
            with self.subTest(fetch=fetch_rc):
                result, calls = self.run_job(fetch_rc)
                self.assertEqual(calls, ["fetch"], result.stdout + result.stderr)
                self.assertEqual(result.returncode, job_rc, result.stdout + result.stderr)


class GenaiTracesArchiveTest(unittest.TestCase):
    """The hourly archive (sd:5775) passes `archive`'s exit 3, nothing to do,
    as 0: an unmounted share must not page every hour. A failed write, exit
    1, still fails the run."""

    def test_the_job_runs_every_hour_so_rotation_cannot_outrun_it(self):
        # 30 backups of 100 MB: a daily run lost files to a 3 GB burst.
        text = (EXAMPLES / "genai-traces-archive.job").read_text()
        schedule = re.search(r'^JOB_SCHEDULE="([^"]+)"', text, re.M).group(1).split()
        self.assertTrue(schedule[0].isdigit(), schedule)
        self.assertEqual(schedule[1:], ["*", "*", "*", "*"])

    def test_the_job_runs_archive_and_passes_only_exit_3(self):
        for archive_rc, job_rc in ((0, 0), (3, 0), (1, 1)):
            with self.subTest(archive=archive_rc):
                fx = Fixture()
                self.addCleanup(fx.destroy)
                script = fx.tmp / "local-genai-traces" / "genai-traces.sh"
                script.parent.mkdir()
                script.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$ARCHIVE_MARKER"\n'
                                  'exit "$ARCHIVE_RC"\n')
                marker = fx.tmp / "archive-calls"
                job = EXAMPLES / "genai-traces-archive.job"
                fx.write_job(job.stem, job.read_text())
                result = fx.exec_job(job.stem, extra_env={
                    "ARCHIVE_MARKER": str(marker), "ARCHIVE_RC": str(archive_rc)})
                self.assertEqual(result.returncode, job_rc, result.stdout + result.stderr)
                self.assertEqual(marker.read_text().splitlines(), ["archive"])


if __name__ == "__main__":
    unittest.main()


class HourlyBackupGradeTest(unittest.TestCase):
    """The hourly backup grades a failed NAS step data safe (sd:5997).

    The snapshot is verified on the local disk before the NAS step runs, so
    that step failing leaves the data safe, and the run says so with the
    line the wrapper's push reads (ruling on sd:6030). A failed snapshot
    prints no grade: its data is at risk. Both still fail the run.
    """

    JOB = EXAMPLES / "sd-db-backup-hourly.job"
    SAFE = "SD_REPORT_ATTENTION: data safe, one step failed: NAS copy"

    def run_job(self, snapshot_rc, preflight_rc):
        fx = Fixture()
        self.addCleanup(fx.destroy)
        # The pack's sd-db at the default the job names, exiting SNAPSHOT_RC,
        # and the interpreter offsite-verify runs under, exiting PREFLIGHT_RC.
        sd_db = fx.home / "repos/platypeeps/sd-ai-command-pack/bin/sd-db"
        sd_db.parent.mkdir(parents=True)
        sd_db.write_text('#!/bin/sh\nexit "$SNAPSHOT_RC"\n')
        sd_db.chmod(0o755)
        python = fx.tmp / "preflight"
        python.write_text('#!/bin/sh\nexit "$PREFLIGHT_RC"\n')
        python.chmod(0o755)
        fx.write_job(self.JOB.stem, self.JOB.read_text())
        result = fx.exec_job(self.JOB.stem, {
            "SNAPSHOT_RC": str(snapshot_rc), "PREFLIGHT_RC": str(preflight_rc),
            "SD_DB_PYTHON": str(python),
            "SD_DB_BACKUP_HOURLY_DESTINATION": str(fx.tmp / "backups"),
            "SD_DB_BACKUP_HOURLY_NAS_FOLDER": str(fx.tmp / "nas")})
        return result, (fx.folder / "logs" / f"{self.JOB.stem}.log").read_text().splitlines()

    def test_a_failed_nas_step_after_the_snapshot_is_graded_data_safe(self):
        result, log = self.run_job(0, 4)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn(self.SAFE, log)

    def test_a_failed_snapshot_is_not_graded(self):
        result, log = self.run_job(5, 0)
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertNotIn(self.SAFE, log)


class NightlyBackupGradeTest(unittest.TestCase):
    """The nightly backup grades a failed NAS copy data safe (sd:6030).

    Same contract as the hourly job: the snapshot is verified on the local
    disk before the optional NAS copy runs, so that copy failing leaves the
    data safe. A failed snapshot prints no grade. Both still fail the run.
    """

    JOB = EXAMPLES / "sd-db-backup.job"
    SAFE = "SD_REPORT_ATTENTION: data safe, one step failed: NAS copy"

    def run_job(self, snapshot_rc, preflight_rc, nas=True, env_sh=False):
        fx = Fixture()
        self.addCleanup(fx.destroy)
        sd_db = fx.home / "repos/platypeeps/sd-ai-command-pack/bin/sd-db"
        sd_db.parent.mkdir(parents=True)
        sd_db.write_text('#!/bin/sh\nexit "$SNAPSHOT_RC"\n')
        sd_db.chmod(0o755)
        python = fx.tmp / "preflight"
        python.write_text('#!/bin/sh\nexit "$PREFLIGHT_RC"\n')
        python.chmod(0o755)
        fx.write_job(self.JOB.stem, self.JOB.read_text())
        env = {"SNAPSHOT_RC": str(snapshot_rc), "PREFLIGHT_RC": str(preflight_rc),
               "SD_DB_PYTHON": str(python), "SD_DB_BACKUP_DESTINATION": str(fx.tmp / "backups")}
        if nas and env_sh:
            shell = fx.home / ".config/shell"
            shell.mkdir(parents=True)
            (shell / "env.sh").write_text(f'export SD_DB_BACKUP_NAS_FOLDER="{fx.tmp / "nas"}"\n')
        elif nas:
            env["SD_DB_BACKUP_NAS_FOLDER"] = str(fx.tmp / "nas")
        result = fx.exec_job(self.JOB.stem, env)
        return result, (fx.folder / "logs" / f"{self.JOB.stem}.log").read_text().splitlines()

    def test_a_failed_nas_copy_after_the_snapshot_is_graded_data_safe(self):
        result, log = self.run_job(0, 4)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn(self.SAFE, log)

    def test_a_failed_snapshot_is_not_graded(self):
        result, log = self.run_job(5, 0)
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertNotIn(self.SAFE, log)

    def test_a_nas_folder_set_in_env_sh_reaches_the_job(self):
        # A launchd run's settings come from env.sh; the job file reads them
        # when it loads, so env.sh must come first.
        result, log = self.run_job(0, 4, env_sh=True)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn(self.SAFE, log)

    def test_without_a_nas_folder_the_job_is_the_snapshot_alone(self):
        result, log = self.run_job(0, 4, nas=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(self.SAFE, log)
