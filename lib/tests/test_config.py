"""The shared config location: lib/config.sh and lib/system_tools_config.py agree."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LIB))

import system_tools_config as stc  # noqa: E402


def shell(script, env):
    return subprocess.run(["/bin/sh", "-c", f'. "{LIB}/config.sh"; {script}'],
                          env=env, capture_output=True, text=True, check=False)


class Location(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def env(self, **extra):
        return {"PATH": os.environ["PATH"], "HOME": str(self.home), **extra}

    def test_default_is_under_home_config(self):
        env = self.env()
        want = self.home / ".config/system/notify"
        self.assertEqual(stc.config_dir("notify", env), want)
        self.assertEqual(shell("st_config_dir notify", env).stdout.strip(), str(want))

    def test_xdg_config_home_moves_it(self):
        env = self.env(XDG_CONFIG_HOME=str(self.home / "xdg"))
        want = self.home / "xdg/system/repo-sync"
        self.assertEqual(stc.config_dir("repo-sync", env), want)
        self.assertEqual(shell("st_config_dir repo-sync", env).stdout.strip(), str(want))

    def test_system_tools_config_wins(self):
        env = self.env(XDG_CONFIG_HOME=str(self.home / "xdg"), SYSTEM_TOOLS_CONFIG=str(self.home / "c"))
        self.assertEqual(stc.config_dir("weekly-digest", env), self.home / "c/weekly-digest")
        self.assertEqual(shell("st_config_dir weekly-digest", env).stdout.strip(), str(self.home / "c/weekly-digest"))


class EnvFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name) / "c"
        (self.conf / "notify").mkdir(parents=True)
        (self.conf / "notify/.env").write_text(
            "# comment\n\nexport A=1\nB='two words'\nC=\"3\"\nnot a line\n")
        self.envv = {"PATH": os.environ["PATH"], "HOME": self.tmp.name, "SYSTEM_TOOLS_CONFIG": str(self.conf)}

    def test_python_reads_it(self):
        self.assertEqual(stc.read_env("notify", self.envv), {"A": "1", "B": "two words", "C": "3"})

    def test_python_absent_is_empty(self):
        self.assertEqual(stc.read_env("nothing", self.envv), {})

    def test_shell_sources_and_exports_it(self):
        done = shell('st_source_env notify; sh -c \'printf "%s|%s|%s" "$A" "$B" "$C"\'', self.envv)
        self.assertEqual(done.stdout, "1|two words|3")

    def test_shell_absent_is_fine(self):
        self.assertEqual(shell("st_source_env nothing; echo ok", self.envv).stdout.strip(), "ok")

    def test_missing_names_the_path_and_the_example(self):
        done = shell("st_missing NTFY_TOPIC notify .env", self.envv)
        self.assertIn("NTFY_TOPIC is not set", done.stderr)
        self.assertIn(f"copy local-notify/.env.example to {self.conf}/notify/.env", done.stderr)
        msg = stc.missing("NTFY_TOPIC", "notify", ".env", environ=self.envv)
        self.assertIn(f"copy local-notify/.env.example to {self.conf}/notify/.env", msg)

    def test_mezmo_folder_keeps_its_name(self):
        done = shell("st_missing MEZMO_KEY mezmo-pipeline .env mezmo-pipeline", self.envv)
        self.assertIn(f"copy mezmo-pipeline/.env.example to {self.conf}/mezmo-pipeline/.env", done.stderr)


SD_DB = LIB.parent / "local-sd-db"


class CronJobDirs(unittest.TestCase):
    """cron_job_dirs here, its sd_db twin and cron-jobs.sh resolve the same folders."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name) / "c"
        self.jobs = self.conf / "cron-jobs/jobs"
        self.jobs.mkdir(parents=True)
        sys.path.insert(0, str(SD_DB))
        self.addCleanup(sys.path.remove, str(SD_DB))
        from sd_db import config as twin
        self.twin = twin

    def cases(self):
        base = {"PATH": os.environ["PATH"], "HOME": self.tmp.name, "SYSTEM_TOOLS_CONFIG": str(self.conf)}
        return [{**base, "CRON_JOBS_HOST": "Mini"},
                {**base, "CRON_JOBS_HOST": "mini", "CRON_JOBS_EXTRA_DIRS": "/a::/b"},
                base]

    def test_host_folder_sits_between_the_extra_dirs_and_the_shared_one(self):
        env = self.cases()[1]
        self.assertEqual(stc.cron_job_dirs(env), [Path("/a"), Path("/b"), self.jobs / "mini", self.jobs])

    def test_the_twin_agrees(self):
        for env in self.cases():
            self.assertEqual(stc.cron_job_dirs(env), self.twin.cron_job_dirs(env), env)

    def test_the_default_host_matches_hostname_s(self):
        short = subprocess.run(["hostname", "-s"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(stc.cron_job_dirs(self.cases()[2])[0], self.jobs / short.lower())

    def test_env_file_supplies_the_host_and_an_exported_value_wins(self):
        (self.conf / "cron-jobs/.env").write_text('CRON_JOBS_HOST="mini"\n')
        env = self.cases()[2]
        self.assertEqual(stc.cron_job_dirs(env)[0], self.jobs / "mini")
        self.assertEqual(self.twin.cron_job_dirs(env)[0], self.jobs / "mini")
        self.assertEqual(stc.cron_job_dirs({**env, "CRON_JOBS_HOST": "studio"})[0], self.jobs / "studio")


if __name__ == "__main__":
    unittest.main()
