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


class CaptureTest(StageTest):
    """capture must not write a host job or a beta's id 0 into a shared profile."""

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
