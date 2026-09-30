"""The appstore and cron stages must not end a nightly run early (sd:2220, sd:2221).

appstore: a beta or TestFlight build lists in `mas list` under id 0, so an id
compare reads it as missing and runs `mas install`. That install needs sudo,
fails under launchd, and used to take the whole run down under `set -e`.

cron: a job in this host's own folder, <config>/cron-jobs/jobs/<host>/, is in
no profile, so the orphan sweep used to uninstall it every night.

The script runs from a copy of this folder under a temporary root, beside a
copy of lib/ and a stub cron-jobs.sh that only logs, so no case touches
launchd or the checkout. `mas` and `launchctl` are stubs on PATH.
"""

import pathlib
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

    def install_plist(self, job):
        (self.agents / f"{fixture_config.LABEL_PREFIX}.cron.{job}.plist").write_text("<plist/>\n")


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


class CaptureTest(StageTest):
    """capture must not write a host job or a beta's id 0 into a shared profile."""

    def test_capture_leaves_host_jobs_and_id_0_out_of_the_profile(self):
        (self.jobs / HOST / "host-job.job").write_text('JOB_SCHEDULE="0 23 * * *"\n')
        for job in ("host-job", "own-job"):
            self.install_plist(job)
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
        }
        result = subprocess.run([str(self.folder / "machine-setup.sh"), "capture", "--apply"],
                                env=env, capture_output=True, text=True, cwd=self.tmp.name,
                                stdin=subprocess.DEVNULL, timeout=120)
        self.assertIn("cron: 1 entries", result.stdout, result.stdout + result.stderr)
        cron = (self.profiles / "personal.cron").read_text()
        self.assertIn("own-job", cron)
        self.assertNotIn("host-job", cron)
        mas = (self.profiles / "personal.mas").read_text()
        self.assertIn("111 Alpha", mas)
        self.assertNotIn("Beta App", mas)


if __name__ == "__main__":
    unittest.main()
