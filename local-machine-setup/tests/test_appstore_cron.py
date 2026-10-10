"""The appstore and cron stages must not end a nightly run early (sd:2220, sd:2221).

appstore: a beta or TestFlight build lists in `mas list` under id 0, so an id
compare reads it as missing and runs `mas install`. That install needs sudo,
fails under launchd, and used to take the whole run down under `set -e`.

cron: a job in this host's own folder, <config>/cron-jobs/jobs/<host>/, is in
no profile, so the orphan sweep used to uninstall it every night.

cron: another repository's installer can label its agent <prefix>.cron.<name>
too, and the sweep used to uninstall that agent as an orphan (sd:2321).

The script runs from a copy of this folder under a temporary root, beside a
copy of lib/ and a stub cron-jobs.sh that only logs, so no case touches
launchd or the checkout. `mas` and `launchctl` are stubs on PATH.
"""

import os
import pathlib
import plistlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"
HOST = "fixturehost"

# `mas list` prints MAS_LIST; `mas install` logs and exits MAS_INSTALL_RC.
MAS_STUB = r"""#!/bin/sh
case "$1" in
  list) printf '%b' "$MAS_LIST" ;;
  install) printf 'install %s\n' "$2" >> "$MAS_LOG"
           echo "sudo: a password is required" >&2
           exit "${MAS_INSTALL_RC:-0}" ;;
esac
"""

CRON_JOBS_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$CRON_LOG"
exit 0
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class StageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        repo = base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        write_exec(repo / "local-cron-jobs/cron-jobs.sh", CRON_JOBS_STUB)
        self.config_root = fixture_config.copy_config(base)
        self.profiles = self.config_root / "machine-setup/profiles"
        self.jobs = self.config_root / "cron-jobs/jobs"
        (self.jobs / HOST).mkdir(parents=True)
        self.home = base / "home"
        self.agents = self.home / "Library/LaunchAgents"
        self.agents.mkdir(parents=True)
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.mas_log = base / "mas.log"
        self.cron_log = base / "cron.log"
        self.mas_log.touch()
        self.cron_log.touch()
        self.stubs = base / "stubs"
        write_exec(self.stubs / "mas", MAS_STUB)
        write_exec(self.stubs / "launchctl", "#!/bin/sh\nexit 0\n")
        write_exec(self.stubs / "git", "#!/bin/sh\nexit 1\n")
        fixture_config.seal(self, self.stubs)

    def run_stage(self, stage, *flags, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
            "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
            "CRON_JOBS_HOST": HOST,
            "MAS_LOG": str(self.mas_log),
            "CRON_LOG": str(self.cron_log),
            **extra,
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), "update", stage, *flags],
                              env=env, capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    def plist_path(self, job):
        return self.agents / f"{fixture_config.LABEL_PREFIX}.cron.{job}.plist"

    def install_plist(self, job):
        """A plist in the shape local-cron-jobs' write_plist renders."""
        self.plist_path(job).write_bytes(plistlib.dumps({
            "Label": f"{fixture_config.LABEL_PREFIX}.cron.{job}",
            "ProgramArguments": ["/bin/bash", "/opt/example/local-cron-jobs/cron-jobs.sh",
                                 "exec", job],
            "RunAtLoad": False,
        }))

    def install_foreign_plist(self, job):
        """Another repository's installer, using the same label shape."""
        self.plist_path(job).write_bytes(plistlib.dumps({
            "Label": f"{fixture_config.LABEL_PREFIX}.cron.{job}",
            "ProgramArguments": ["/opt/example/foreign/run.sh"],
            "RunAtLoad": False,
        }))

    def break_discovery(self):
        """Make the host job listing fail the way a broken python3 or lib does."""
        (self.folder.parent / "lib/system_tools_config.py").write_text(
            "raise ImportError('broken for the test')\n")

    def lock_host_folder(self):
        """An unreadable host folder: a glob would read it as empty."""
        host = self.jobs / HOST
        host.chmod(0)
        self.addCleanup(host.chmod, 0o755)


class AppStoreTest(StageTest):
    def setUp(self):
        super().setUp()
        (self.profiles / "personal.mas").write_text("111 Alpha\n222 Beta App\n333 Gamma\n")

    def test_an_app_listed_under_id_0_with_the_same_name_is_installed(self):
        result = self.run_stage("appstore", "--apply",
                                MAS_LIST="111  Alpha  (1.0)\n0  Beta App  (2.0b1)\n333  Gamma  (3.0)\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("MISSING", result.stdout)
        self.assertIn("222 Beta App", result.stdout)
        self.assertEqual(self.mas_log.read_text(), "")

    def test_a_failed_install_reports_and_the_stage_goes_on(self):
        result = self.run_stage("appstore", "--apply", MAS_INSTALL_RC="1",
                                MAS_LIST="111  Alpha  (1.0)\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MISSING 222 Beta App", result.stdout)
        self.assertIn("MISSING 333 Gamma", result.stdout)
        # Both installs were tried: the first failure did not end the loop.
        self.assertEqual(self.mas_log.read_text(), "install 222\ninstall 333\n")
        self.assertIn("FAILED  mas install 222", result.stdout)

    def test_a_truly_missing_app_is_still_missing(self):
        result = self.run_stage("appstore", MAS_LIST="111  Alpha  (1.0)\n0  Other  (1.0)\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MISSING 222 Beta App", result.stdout)


class CronTest(StageTest):
    def setUp(self):
        super().setUp()
        (self.profiles / "personal.cron").write_text("shared-job\n")
        (self.jobs / "shared-job.job").write_text('JOB_SCHEDULE="0 1 * * *"\n')
        (self.jobs / HOST / "host-job.job").write_text('JOB_SCHEDULE="0 23 * * *"\n')
        other = self.jobs / "otherhost"
        other.mkdir()
        (other / "other-job.job").write_text('JOB_SCHEDULE="0 2 * * *"\n')

    def test_an_installed_host_job_is_not_swept(self):
        for job in ("shared-job", "host-job"):
            self.install_plist(job)
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("EXTRA", result.stdout)
        self.assertIn("ok      host-job", result.stdout)
        self.assertNotIn("uninstall", self.cron_log.read_text())

    def test_a_missing_host_job_is_installed(self):
        self.install_plist("shared-job")
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MISSING host-job", result.stdout)
        self.assertIn("install host-job", self.cron_log.read_text())

    def test_another_hosts_job_and_a_dropped_job_are_still_swept(self):
        for job in ("shared-job", "host-job", "other-job", "dropped-job"):
            self.install_plist(job)
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.cron_log.read_text()
        self.assertIn("uninstall other-job", log)
        self.assertIn("uninstall dropped-job", log)
        self.assertNotIn("uninstall host-job", log)


class CronDiscoveryTest(CronTest):
    """The sweep uninstalls; it must not run on an incomplete wanted list."""

    def assert_nothing_uninstalled(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MISSING host job list", result.stdout)
        self.assertIn("ok      shared-job", result.stdout)
        self.assertNotIn("uninstall", self.cron_log.read_text())

    def test_a_broken_discovery_uninstalls_nothing(self):
        for job in ("shared-job", "host-job", "dropped-job"):
            self.install_plist(job)
        self.break_discovery()
        self.assert_nothing_uninstalled(self.run_stage("cron", "--apply"))

    def test_an_unreadable_host_folder_uninstalls_nothing(self):
        for job in ("shared-job", "host-job"):
            self.install_plist(job)
        self.lock_host_folder()
        self.assert_nothing_uninstalled(self.run_stage("cron", "--apply"))

    def test_an_unsearchable_jobs_folder_uninstalls_nothing(self):
        # os.path.lexists reads a PermissionError on an ancestor as "absent".
        for job in ("shared-job", "host-job"):
            self.install_plist(job)
        self.jobs.chmod(0)
        self.addCleanup(self.jobs.chmod, 0o755)
        self.assert_nothing_uninstalled(self.run_stage("cron", "--apply"))

    def test_no_host_folder_is_not_a_failure(self):
        (self.jobs / HOST / "host-job.job").unlink()
        (self.jobs / HOST).rmdir()
        self.install_plist("shared-job")
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("MISSING", result.stdout)


class CronExtraDirsTest(CronTest):
    """CRON_JOBS_EXTRA_DIRS defines jobs; the profile still picks which run."""

    def setUp(self):
        super().setUp()
        self.extra = pathlib.Path(self.tmp.name) / "extra"
        self.extra.mkdir()
        for job in ("picked", "unpicked"):
            (self.extra / f"{job}.job").write_text('JOB_SCHEDULE="0 3 * * *"\n')
        (self.profiles / "personal.cron").write_text("shared-job\npicked\n")

    def test_an_extra_dir_job_needs_a_profile_entry(self):
        for job in ("shared-job", "host-job", "picked", "unpicked"):
            self.install_plist(job)
        result = self.run_stage("cron", "--apply", CRON_JOBS_EXTRA_DIRS=str(self.extra))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.cron_log.read_text()
        self.assertIn("uninstall unpicked", log)
        self.assertNotIn("uninstall picked", log)
        self.assertNotIn("uninstall host-job", log)

    def test_an_unpicked_extra_dir_job_is_not_installed(self):
        self.install_plist("shared-job")
        self.install_plist("host-job")
        self.run_stage("cron", "--apply", CRON_JOBS_EXTRA_DIRS=str(self.extra))
        log = self.cron_log.read_text()
        self.assertIn("install picked", log)
        self.assertNotIn("install unpicked", log)


class CronOwnershipTest(StageTest):
    """The sweep uninstalls only what local-cron-jobs installed (sd:2321)."""

    def setUp(self):
        super().setUp()
        (self.profiles / "personal.cron").write_text("shared-job\n")
        (self.jobs / "shared-job.job").write_text('JOB_SCHEDULE="0 1 * * *"\n')
        (self.jobs / HOST / "host-job.job").write_text('JOB_SCHEDULE="0 23 * * *"\n')
        # The wanted jobs are in place, so the sweep is all a case exercises.
        for job in ("shared-job", "host-job"):
            self.install_plist(job)

    def test_a_foreign_agent_under_the_prefix_is_left_in_place(self):
        self.install_foreign_plist("foreign-job")
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("FOREIGN foreign-job — not installed by local-cron-jobs; left in place",
                      result.stdout)
        self.assertNotIn("EXTRA", result.stdout)
        self.assertNotIn("uninstall", self.cron_log.read_text())
        self.assertTrue(self.plist_path("foreign-job").exists())

    def test_a_dropped_job_is_uninstalled_beside_a_foreign_agent(self):
        self.install_foreign_plist("foreign-job")
        self.install_plist("dropped-job")
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("EXTRA   dropped-job — installed but no longer in this profile", result.stdout)
        log = self.cron_log.read_text()
        self.assertIn("uninstall dropped-job", log)
        self.assertNotIn("uninstall foreign-job", log)

    def test_a_plist_that_runs_another_job_is_not_ours(self):
        # The right shape under the wrong name: the mark binds label and job.
        self.install_plist("renamed-job")
        path = self.plist_path("renamed-job")
        plist = plistlib.loads(path.read_bytes())
        plist["ProgramArguments"][3] = "other-name"
        path.write_bytes(plistlib.dumps(plist))
        result = self.run_stage("cron", "--apply")
        self.assertIn("FOREIGN renamed-job", result.stdout)
        self.assertNotIn("uninstall", self.cron_log.read_text())

    def test_a_profile_naming_a_foreign_agent_does_not_abort_the_stage(self):
        # sd:2538: a capture wrote another installer's agent into the profile.
        # The real cron-jobs.sh fails verify and install for a name with no job
        # file, and that install ended the nightly run before every later stage.
        write_exec(pathlib.Path(self.tmp.name) / "repo/local-cron-jobs/cron-jobs.sh", r"""#!/bin/sh
printf '%s\n' "$*" >> "$CRON_LOG"
case "$1 $2" in
  "verify foreign-job"|"install foreign-job")
    echo "ERROR: no such job 'foreign-job' (expected <config>/cron-jobs/jobs/foreign-job.job)" >&2
    exit 1 ;;
esac
exit 0
""")
        (self.profiles / "personal.cron").write_text("foreign-job\nshared-job\n")
        self.install_foreign_plist("foreign-job")
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("FOREIGN foreign-job — in this profile, but another installer's agent holds "
                      "its label; left in place", result.stdout)
        # The jobs after it are still checked, and nothing touches its agent.
        self.assertIn("ok      shared-job", result.stdout)
        self.assertNotIn("STALE", result.stdout)
        log = self.cron_log.read_text()
        self.assertNotIn("foreign-job", log)
        self.assertTrue(self.plist_path("foreign-job").exists())

    def test_a_profile_job_with_an_unreadable_plist_is_still_reinstalled(self):
        # Only a plist proven another installer's is skipped; one nobody can
        # parse, under a name the profile wants, keeps the old repair.
        self.plist_path("shared-job").write_text("not a plist\n")
        write_exec(pathlib.Path(self.tmp.name) / "repo/local-cron-jobs/cron-jobs.sh", r"""#!/bin/sh
printf '%s\n' "$*" >> "$CRON_LOG"
[ "$1" = verify ] && exit 1
exit 0
""")
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("STALE   shared-job", result.stdout)
        self.assertIn("install shared-job", self.cron_log.read_text())

    def test_an_unreadable_plist_is_left_in_place(self):
        # Fail closed: a plist nobody can parse is not proven ours.
        self.plist_path("garbled-job").write_text("not a plist\n")
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UNKNOWN garbled-job — cannot tell who installed it; left in place",
                      result.stdout)
        self.assertNotIn("uninstall", self.cron_log.read_text())
        self.assertTrue(self.plist_path("garbled-job").exists())

    def test_a_plist_the_real_generator_rendered_is_recognised(self):
        # Ties the mark to write_plist: render with the real cron-jobs.sh, then
        # drop the job from the profile. A generator change that loses the mark
        # fails here instead of leaving dropped jobs firing on every machine.
        base = pathlib.Path(self.tmp.name) / "real"
        shutil.copytree(FOLDER.parent / "local-cron-jobs", base / "local-cron-jobs",
                        ignore=shutil.ignore_patterns("tests", "__pycache__", "logs"))
        shutil.copytree(LIB, base / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        (self.jobs / "dropped-job.job").write_text('JOB_SCHEDULE="0 4 * * *"\nJOB_COMMAND="true"\n')
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
            "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
            "CRON_JOBS_HOST": HOST,
        }
        rendered = subprocess.run(
            ["/bin/bash", str(base / "local-cron-jobs/cron-jobs.sh"), "install", "dropped-job"],
            env=env, capture_output=True, text=True, cwd=self.tmp.name,
            stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
        self.assertTrue(self.plist_path("dropped-job").exists())
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("EXTRA   dropped-job", result.stdout)
        self.assertIn("uninstall dropped-job", self.cron_log.read_text())


class CronRetiredTest(StageTest):
    """A job in local-cron-jobs/retired-jobs.txt is uninstalled, never reinstalled (sd:3155).

    A profile or a host folder written before the retirement still names the
    job; the sweep left it running because it was wanted, and verify read it ok.
    """

    def setUp(self):
        super().setUp()
        (self.folder.parent / "local-cron-jobs/retired-jobs.txt").write_text(
            "# a comment line\nretired-job   # sd:1 2026-10-01\n\nother-retired\n")
        (self.profiles / "personal.cron").write_text("shared-job\nretired-job\n")
        for job in ("shared-job", "retired-job"):
            (self.jobs / f"{job}.job").write_text('JOB_SCHEDULE="0 1 * * *"\n')
        self.install_plist("shared-job")

    def test_a_retired_job_the_profile_names_is_uninstalled_and_reported(self):
        self.install_plist("retired-job")
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("EXTRA   retired-job — retired in local-cron-jobs/retired-jobs.txt, still installed",
                      result.stdout)
        self.assertIn("retired-job is retired, yet this profile or this host's jobs folder names it",
                      result.stdout)
        log = self.cron_log.read_text().splitlines()
        self.assertEqual(log.count("uninstall retired-job"), 1, log)
        self.assertNotIn("install retired-job", log)
        self.assertNotIn("verify retired-job", log)
        self.assertNotIn("ok      retired-job", result.stdout)

    def test_a_retired_job_is_not_installed_when_its_plist_is_gone(self):
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("MISSING retired-job", result.stdout)
        self.assertNotIn("retired-job", self.cron_log.read_text())
        self.assertNotIn("EXTRA", result.stdout)

    def test_a_retired_job_in_the_host_folder_is_uninstalled(self):
        (self.profiles / "personal.cron").write_text("shared-job\n")
        (self.jobs / HOST / "other-retired.job").write_text('JOB_SCHEDULE="0 2 * * *"\n')
        self.install_plist("other-retired")
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("EXTRA   other-retired — retired", result.stdout)
        self.assertEqual(self.cron_log.read_text().splitlines().count("uninstall other-retired"), 1)

    def test_a_retired_job_is_uninstalled_when_the_sweep_cannot_run(self):
        # The host job list fails, so the sweep is off; the retired list still applies.
        self.install_plist("retired-job")
        self.break_discovery()
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MISSING host job list", result.stdout)
        self.assertIn("uninstall retired-job", self.cron_log.read_text().splitlines())

    def test_a_foreign_agent_under_a_retired_name_is_left_in_place(self):
        self.install_foreign_plist("retired-job")
        result = self.run_stage("cron", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("FOREIGN retired-job — retired, but not installed by local-cron-jobs; left in place",
                      result.stdout)
        self.assertNotIn("retired-job", self.cron_log.read_text())
        self.assertTrue(self.plist_path("retired-job").exists())

    def test_a_dry_run_names_the_retired_job_as_drift_once(self):
        self.install_plist("retired-job")
        result = self.run_stage("cron")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        extra = [line for line in result.stdout.splitlines() if "EXTRA" in line]
        self.assertEqual(len(extra), 1, result.stdout)
        self.assertIn("retired-job", extra[0])
        self.assertIn("[dry-run]", result.stdout)
        self.assertNotIn("uninstall", self.cron_log.read_text())


class RetiredListTest(unittest.TestCase):
    def test_each_retired_job_names_its_item_and_date(self):
        path = FOLDER.parent / "local-cron-jobs/retired-jobs.txt"
        lines = [line for line in path.read_text().splitlines() if line and not line.startswith("#")]
        self.assertIn("ai-apps-nightly", [line.split()[0] for line in lines])
        for line in lines:
            self.assertRegex(line, r"^[A-Za-z0-9._-]+ +# sd:[0-9]+ [0-9]{4}-[0-9]{2}-[0-9]{2}$")


class CaptureTest(StageTest):
    """capture must not write a host job or a beta's id 0 into a shared profile."""

    def setUp(self):
        super().setUp()
        # capture reads iTerm2's prefs folder and every app's bundle id with
        # `defaults`, and Spotlight's list with `sudo`. Real ones read this
        # Mac whatever HOME says (sd:2331), so: every key unset, no ticket.
        write_exec(self.stubs / "defaults", "#!/bin/sh\nexit 1\n")
        write_exec(self.stubs / "sudo", "#!/bin/sh\nexit 1\n")
        # The shared job most cases install; a job no file defines is not adopted.
        (self.jobs / "own-job.job").write_text('JOB_SCHEDULE="0 4 * * *"\n')
        (self.profiles / "personal.agent").write_text("# no agents in a capture case\n")  # as copy_capture_config

    def capture(self, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
            "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
            "CRON_JOBS_HOST": HOST,
            "MAS_LIST": "111  Alpha  (1.0)\n0  Beta App  (2.0b1)\n",
            "MAS_LOG": str(self.mas_log),
            **extra,
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), "capture", "--apply"],
                              env=env, capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    def test_capture_leaves_host_jobs_and_id_0_out_of_the_profile(self):
        (self.jobs / HOST / "host-job.job").write_text('JOB_SCHEDULE="0 23 * * *"\n')
        for job in ("host-job", "own-job"):
            self.install_plist(job)
        result = self.capture()
        self.assertIn("cron: 1 entries", result.stdout, result.stdout + result.stderr)
        cron = (self.profiles / "personal.cron").read_text()
        self.assertIn("own-job", cron)
        self.assertNotIn("host-job", cron)
        mas = (self.profiles / "personal.mas").read_text()
        self.assertIn("111 Alpha", mas)
        self.assertNotIn("Beta App", mas)

    def test_capture_leaves_a_foreign_agent_out_of_the_profile(self):
        # A profile naming it would send the cron stage to install a job with
        # no job file, every night (sd:2321).
        self.install_plist("own-job")
        self.install_foreign_plist("foreign-job")
        result = self.capture()
        self.assertIn("cron: 1 entries", result.stdout, result.stdout + result.stderr)
        cron = (self.profiles / "personal.cron").read_text()
        self.assertIn("own-job", cron)
        self.assertNotIn("foreign-job", cron)

    def test_capture_keeps_an_installed_agent_the_profile_names(self):
        # sd:3106: the work profile names agents outside the label prefix,
        # owned through MACHINE_SETUP_AGENT_GLOBS. A capture run without that
        # glob read "manifest: 0 label(s)" and wrote the roster empty, though
        # every plist was still installed.
        (self.profiles / "personal.agent").write_text("org.example.helper\n")
        (self.agents / "org.example.helper.plist").write_bytes(plistlib.dumps({
            "Label": "org.example.helper",
            "ProgramArguments": ["/opt/example/helper"],
        }))
        result = self.capture()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("manifest: 1 label(s)", result.stdout)
        self.assertIn("org.example.helper", (self.profiles / "personal.agent").read_text())

    def test_capture_keeps_a_profile_app_now_installed_as_a_beta(self):
        # update counts "222 Beta App" as installed by its id-0 name; capture
        # must not drop it, or a rebuild would never install the store build.
        (self.profiles / "personal.mas").write_text("111 Alpha\n222 Beta App\n")
        result = self.capture(MAS_LIST="111  Alpha  (1.0)\n0  Beta App  (2.0b1)\n0  Other  (1.0)\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        mas = (self.profiles / "personal.mas").read_text()
        self.assertIn("111 Alpha", mas)
        self.assertIn("222 Beta App", mas)
        self.assertNotIn("Other", mas)
        self.assertFalse([l for l in mas.splitlines() if l.startswith("0 ")], mas)

    def test_capture_keeps_an_extra_dir_job_in_the_profile(self):
        extra = pathlib.Path(self.tmp.name) / "extra"
        extra.mkdir()
        (extra / "picked.job").write_text('JOB_SCHEDULE="0 3 * * *"\n')
        self.install_plist("picked")
        result = self.capture(CRON_JOBS_EXTRA_DIRS=str(extra))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("picked", (self.profiles / "personal.cron").read_text())

    def test_capture_does_not_adopt_a_job_no_job_file_defines(self):
        # sd:3106. A retired or deleted job leaves its plist installed. Adopted
        # into the profile, `cron-jobs.sh verify` cannot render it, so the cron
        # stage reads it STALE every night and its install fails.
        (self.jobs / "retired-job.job.retired-2026-10-01").write_text('JOB_SCHEDULE="0 5 * * *"\n')
        for job in ("own-job", "retired-job"):
            self.install_plist(job)
        result = self.capture()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SKIPPED retired-job", result.stdout)
        self.assertIn("cron: 1 entries", result.stdout)
        cron = (self.profiles / "personal.cron").read_text()
        self.assertIn("own-job", cron)
        self.assertNotIn("retired-job", cron)

    def test_capture_stops_when_the_job_files_cannot_be_listed(self):
        self.install_plist("own-job")
        self.jobs.chmod(0o300)
        self.addCleanup(self.jobs.chmod, 0o755)
        result = self.capture()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.profiles / "personal.cron").exists())

    def test_capture_refuses_to_write_a_roster_empty(self):
        # sd:3106. capture wrote the work roster with "manifest: 0 label(s)",
        # and the next update would have removed every agent it named.
        (self.profiles / "personal.agent").write_text("org.example.helper\n")
        (self.profiles / "personal.cron").write_text("own-job\n")
        result = self.capture()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("REFUSED personal.agent", result.stdout)
        self.assertIn("REFUSED personal.cron", result.stdout)
        self.assertEqual((self.profiles / "personal.agent").read_text(), "org.example.helper\n")
        self.assertEqual((self.profiles / "personal.cron").read_text(), "own-job\n")

    def test_capture_skips_an_agent_launchctl_reports_disabled(self):
        # sd:3183. `launchctl disable` turns an agent off and leaves its plist
        # in place; capture read the plist and wrote the label back into the
        # roster, so the next update would turn the agent on again.
        for label in ("org.example.helper", "org.example.paused"):
            (self.agents / f"{label}.plist").write_bytes(plistlib.dumps({
                "Label": label, "ProgramArguments": ["/opt/example/helper"]}))
        write_exec(self.stubs / "launchctl", f"""#!/bin/sh
if [ "$1 $2" = "print-disabled gui/{os.getuid()}" ]; then
  printf 'disabled services = {{\\n\\t"org.example.helper" => enabled\\n'
  printf '\\t"org.example.paused" => disabled\\n}}\\n'
fi
exit 0
""")
        result = self.capture(MACHINE_SETUP_AGENT_GLOBS="org.example.*")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SKIPPED org.example.paused", result.stdout)
        agents = (self.profiles / "personal.agent").read_text()
        self.assertIn("org.example.helper", agents)
        self.assertNotIn("org.example.paused", agents)

    def test_capture_keeps_an_agent_when_launchctl_cannot_say_if_it_is_disabled(self):
        # sd:3183. A failed probe is unknown, not "enabled" and not "disabled":
        # capture says so and records the agent as it did before.
        (self.agents / "org.example.helper.plist").write_bytes(plistlib.dumps({
            "Label": "org.example.helper", "ProgramArguments": ["/opt/example/helper"]}))
        write_exec(self.stubs / "launchctl", "#!/bin/sh\nexit 1\n")
        result = self.capture(MACHINE_SETUP_AGENT_GLOBS="org.example.*")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UNKNOWN", result.stdout)
        self.assertIn("org.example.helper", (self.profiles / "personal.agent").read_text())

    def test_capture_leaves_a_cron_jobs_plist_out_of_the_agent_roster(self):
        # A label glob wider than the prefix can reach a plist local-cron-jobs
        # rendered; it is a cron job, so the agent roster must not adopt it.
        (self.agents / "org.example.cron.nightly.plist").write_bytes(plistlib.dumps({
            "Label": "org.example.cron.nightly",
            "ProgramArguments": ["/bin/bash", "/opt/example/local-cron-jobs/cron-jobs.sh", "exec", "nightly"],
        }))
        (self.agents / "org.example.helper.plist").write_bytes(plistlib.dumps({
            "Label": "org.example.helper", "ProgramArguments": ["/opt/example/helper"]}))
        result = self.capture(MACHINE_SETUP_AGENT_GLOBS="org.example.*")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        agents = (self.profiles / "personal.agent").read_text()
        self.assertIn("org.example.helper", agents)
        self.assertNotIn("org.example.cron.nightly", agents)

    def test_capture_lists_apps_from_the_applications_override(self):
        # REGRESSION (sd:2331). The scan read /Applications, so a test capture
        # wrote this Mac's apps into the fixture profile.
        apps = pathlib.Path(self.tmp.name) / "Applications"
        (apps / "Example Tool.app" / "Contents").mkdir(parents=True)
        result = self.capture(MACHINE_SETUP_APPLICATIONS_DIR=str(apps))
        self.assertIn("app: 1 entries", result.stdout, result.stdout + result.stderr)
        listed = [l for l in (self.profiles / "personal.app").read_text().splitlines()
                  if l and not l.startswith("#")]
        self.assertEqual(listed, ["Example Tool"])

    def test_capture_stops_when_host_jobs_cannot_be_listed(self):
        (self.jobs / HOST / "host-job.job").write_text('JOB_SCHEDULE="0 23 * * *"\n')
        self.install_plist("host-job")
        before = (self.profiles / "personal.cron").read_text() if (self.profiles / "personal.cron").exists() else None
        self.lock_host_folder()
        result = self.capture()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("host cron jobs", result.stderr)
        after = (self.profiles / "personal.cron").read_text() if (self.profiles / "personal.cron").exists() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
