"""A component installs fleet-wide from the repository (sd:8607).

Setting up local-fseventsd on one machine took four artifacts that live in
that machine's own config folder, so none of them could be authored for the
other two machines from there, and no session reaches a satellite. Three of
the four now come out of the repository, through the refresh every satellite
already answers:

* the envs stage installs a tool's committed `.env.example` when no
  per-machine template names it, and only when that example holds no
  placeholder value -- an installed placeholder is a value the tool acts on;
* the cron stage installs `local-cron-jobs/examples/<job>.job` when no
  cron-jobs folder holds a copy, and a copy there keeps winning;
* the cron stage reads the repository's own `examples/profiles/common.cron`
  beside this machine's roster, and capture keeps those names out of the
  machine's profile.

Each case runs machine-setup.sh from a copy of the tracked repository under a
temporary root, with a logging stub for cron-jobs.sh, so no case touches
launchd, the operator's config folder or the checkout.
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
ROOT = FOLDER.parent
HOST = "fixturehost"

# The repository folders these cases read: the script itself, lib/, the
# folders the envs stage seeds (ENVS) and the job examples the cron stage
# seeds from.
COPIED = ("local-machine-setup", "lib", "local-cron-jobs",
          "local-fseventsd", "local-notify", "local-statusline")

# The job the repository's examples/profiles/common.cron names, so every
# machine installs it; and one it does not, for the seeding cases alone.
FLEET_JOB = "fseventsd-hourly"
OTHER_JOB = "volume-probe-hourly"

CRON_JOBS_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$CRON_LOG"
exit 0
"""

# Nothing is loaded, so a job with a current plist still reads UNLOADED;
# every call is logged.
LAUNCHCTL_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
exit 1
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def tracked_copy(target):
    """The tracked files of COPIED, copied under `target`.

    No git-ignored file comes along: run from the checkout, live_env read an
    ignored local-notify/.env there (sd:6394), and a .env beside a tool would
    hide the very fallback these cases exercise.
    """
    names = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", *COPIED],
                           capture_output=True, text=True, check=True,
                           env=fixture_config.git_env()).stdout
    for name in filter(None, names.split("\0")):
        if "/tests/" in name:
            continue
        dst = target / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, dst)
    return target


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.root = tracked_copy(self.base / "repo")
        self.script = self.root / "local-machine-setup/machine-setup.sh"
        self.examples = self.root / "local-cron-jobs/examples"
        self.config = fixture_config.copy_config(self.base)
        self.profiles = self.config / "machine-setup/profiles"
        self.jobs = self.config / "cron-jobs/jobs"
        self.home = self.base / "home"
        self.agents = self.home / "Library/LaunchAgents"
        self.agents.mkdir(parents=True)
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.cron_log = self.base / "cron.log"
        self.cron_log.touch()
        self.launchctl_log = self.base / "launchctl.log"
        self.launchctl_log.touch()
        self.stubs = self.base / "stubs"
        write_exec(self.stubs / "launchctl", LAUNCHCTL_STUB)
        write_exec(self.stubs / "git", "#!/bin/sh\nexit 1\n")
        # doctor and capture read iTerm2's prefs and every app's bundle id
        # with `defaults`, and Spotlight's list through `sudo`. The real ones
        # read this Mac whatever HOME says (sd:2331): every key unset, no
        # ticket.
        for name in ("defaults", "sudo"):
            write_exec(self.stubs / name, "#!/bin/sh\nexit 1\n")
        # The cron stage's installer, so no case reaches launchd; the job
        # examples beside it stay the repository's own.
        write_exec(self.root / "local-cron-jobs/cron-jobs.sh", CRON_JOBS_STUB)
        fixture_config.seal(self, self.stubs)

    def run_script(self, *args, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config),
            "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
            "CRON_JOBS_HOST": HOST,
            "CRON_LOG": str(self.cron_log),
            "LAUNCHCTL_LOG": str(self.launchctl_log),
            **extra,
        }
        return subprocess.run([str(self.script), *args], env=env, capture_output=True,
                              text=True, cwd=self.tmp.name, stdin=subprocess.DEVNULL,
                              timeout=180)

    def cron_calls(self):
        return self.cron_log.read_text().splitlines()

    def install_plist(self, job):
        """A plist in the shape local-cron-jobs' write_plist renders."""
        path = self.agents / f"{fixture_config.LABEL_PREFIX}.cron.{job}.plist"
        path.write_bytes(plistlib.dumps({
            "Label": f"{fixture_config.LABEL_PREFIX}.cron.{job}",
            "ProgramArguments": ["/opt/example/libexec/job-launch", "/bin/bash",
                                 "/opt/example/local-cron-jobs/cron-jobs.sh", "exec", job],
            "RunAtLoad": False,
        }))
        return path

    def break_job_listing(self):
        """Make the job file listing fail, as a broken python3 or lib does."""
        (self.root / "lib/system_tools_config.py").write_text(
            "raise ImportError('broken for the test')\n")


class EnvFromRepoExample(Case):
    """The envs stage's last fallback: the repository's own .env.example."""

    def live(self, folder):
        return self.config / folder / ".env"

    def template(self, scope, folder):
        path = self.config / f"machine-setup/envs/{scope}/{folder}.env"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_a_tool_with_no_template_is_installed_from_the_repo_example(self):
        result = self.run_script("update", "envs", "--apply")
        out = result.stdout
        live = self.live("fseventsd")
        self.assertIn(f"  MISSING {live}", out, out + result.stderr)
        self.assertEqual(result.returncode, 0, out + result.stderr)
        self.assertEqual(live.read_text(),
                         (self.root / "local-fseventsd/.env.example").read_text())
        self.assertEqual(stat.S_IMODE(live.stat().st_mode), 0o600)
        self.assertTrue((self.state / "installed/envs_local-fseventsd.sha").exists(), out)

    def test_a_profile_template_still_wins_over_the_repo_example(self):
        self.template("personal", "local-fseventsd").write_text("FSEVENTSD_RSS_MB=512\n")
        result = self.run_script("update", "envs", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.live("fseventsd").read_text(), "FSEVENTSD_RSS_MB=512\n")

    def test_a_common_template_still_wins_over_the_repo_example(self):
        self.template("common", "local-fseventsd").write_text("FSEVENTSD_RSS_MB=256\n")
        result = self.run_script("update", "envs", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.live("fseventsd").read_text(), "FSEVENTSD_RSS_MB=256\n")

    def test_an_example_holding_a_placeholder_installs_nothing(self):
        """local-notify's NTFY_TOPIC=change-me would post this machine's
        notifications to the public ntfy.sh topic `change-me`, and
        local-statusline's labels name example.test accounts. Neither is a
        safe default, so both keep asking for a per-machine template."""
        result = self.run_script("update", "envs", "--apply")
        out = result.stdout
        self.assertIn("  --      local-notify/.env no template for personal", out,
                      out + result.stderr)
        self.assertIn("  --      local-statusline/.env no template for personal", out)
        self.assertFalse(self.live("notify").exists(), out)
        self.assertFalse(self.live("statusline").exists(), out)

    def test_a_placeholder_in_the_example_is_not_installed_by_any_name(self):
        """The rule is the example's content, not a list of folder names: an
        example that gains a placeholder stops being installed."""
        (self.root / "local-fseventsd/.env.example").write_text(
            "FSEVENTSD_RSS_MB=4096\nFSEVENTSD_STATE=change-me\n")
        result = self.run_script("update", "envs", "--apply")
        out = result.stdout
        self.assertIn("  --      local-fseventsd/.env no template for personal", out,
                      out + result.stderr)
        self.assertFalse(self.live("fseventsd").exists(), out)

    def test_a_tool_with_no_example_and_no_template_installs_nothing(self):
        (self.root / "local-fseventsd/.env.example").unlink()
        result = self.run_script("update", "envs", "--apply")
        out = result.stdout
        self.assertIn("  --      local-fseventsd/.env no template for personal", out,
                      out + result.stderr)
        self.assertFalse(self.live("fseventsd").exists(), out)

    def test_doctor_still_warns_about_a_placeholder_a_live_env_holds(self):
        """The placeholder rule has one reader list; doctor's own check keeps
        reporting a value that got into a live .env another way."""
        live = self.live("fseventsd")
        live.parent.mkdir(parents=True)
        live.write_text("FSEVENTSD_RSS_MB=change-me\n")
        out = self.run_script("doctor").stdout
        self.assertIn("    WARN    local-fseventsd/.env — 1 of 1 value(s) still placeholder", out)


class CronJobFromRepoExample(Case):
    """The cron stage seeds a job file from local-cron-jobs/examples."""

    def setUp(self):
        super().setUp()
        # OTHER_JOB is in no repository roster, so these cases exercise the
        # seeding alone; the profile names it.
        (self.profiles / "personal.cron").write_text(f"{OTHER_JOB}\n")

    def seeded(self, job=OTHER_JOB):
        return self.jobs / f"{job}.job"

    def test_a_roster_job_with_no_file_is_seeded_from_the_repo_example(self):
        result = self.run_script("update", "cron", "--apply")
        out = result.stdout
        self.assertIn(f"  MISSING {self.seeded()}", out, out + result.stderr)
        self.assertEqual(result.returncode, 0, out + result.stderr)
        self.assertEqual(self.seeded().read_text(),
                         (self.examples / f"{OTHER_JOB}.job").read_text())
        self.assertIn(f"install {OTHER_JOB}", self.cron_calls())

    def test_a_copy_in_the_config_folder_keeps_winning(self):
        self.jobs.mkdir(parents=True)
        own = 'JOB_SCHEDULE="7 4 * * *"\n# this machine\n'
        self.seeded().write_text(own)
        result = self.run_script("update", "cron", "--apply")
        out = result.stdout
        self.assertEqual(result.returncode, 0, out + result.stderr)
        self.assertEqual(self.seeded().read_text(), own)
        self.assertNotIn(f"  MISSING {self.seeded()}", out)

    def test_a_copy_in_this_hosts_folder_keeps_winning(self):
        (self.jobs / HOST).mkdir(parents=True)
        own = 'JOB_SCHEDULE="9 4 * * *"\n'
        (self.jobs / HOST / f"{OTHER_JOB}.job").write_text(own)
        result = self.run_script("update", "cron", "--apply")
        out = result.stdout
        self.assertEqual(result.returncode, 0, out + result.stderr)
        self.assertEqual((self.jobs / HOST / f"{OTHER_JOB}.job").read_text(), own)
        self.assertFalse(self.seeded().exists(), out)

    def test_a_roster_job_the_examples_do_not_hold_is_reported_as_before(self):
        (self.profiles / "personal.cron").write_text("no-such-job\n")
        result = self.run_script("update", "cron", "--apply")
        out = result.stdout
        self.assertIn("  MISSING no-such-job — no plist installed", out, out + result.stderr)
        self.assertFalse((self.jobs / "no-such-job.job").exists(), out)
        self.assertIn("install no-such-job", self.cron_calls())

    def test_a_job_file_list_that_cannot_be_read_seeds_nothing(self):
        """A failed listing is no answer: it must not read as "no copy here"
        and overwrite a job file this machine holds."""
        self.jobs.mkdir(parents=True)
        own = 'JOB_SCHEDULE="7 4 * * *"\n'
        self.seeded().write_text(own)
        self.break_job_listing()
        result = self.run_script("update", "cron", "--apply")
        out = result.stdout
        self.assertIn("  MISSING job file list", out, out + result.stderr)
        self.assertEqual(self.seeded().read_text(), own)

    def test_a_dry_run_seeds_no_job_file(self):
        result = self.run_script("update", "cron")
        out = result.stdout
        self.assertIn(f"  MISSING {self.seeded()}", out, out + result.stderr)
        self.assertIn("[dry-run]", out)
        self.assertFalse(self.seeded().exists(), out)
        self.assertEqual(self.cron_calls(), [])

    def test_a_seed_that_cannot_be_written_is_reported_and_installs_nothing(self):
        self.jobs.parent.mkdir(parents=True)
        self.jobs.mkdir()
        self.jobs.chmod(0o500)
        self.addCleanup(self.jobs.chmod, 0o700)
        result = self.run_script("update", "cron", "--apply")
        out = result.stdout
        self.assertIn("  FAILED  ", out, out + result.stderr)
        self.assertEqual(result.returncode, 1, out + result.stderr)
        self.assertFalse(self.seeded().exists(), out)
        self.assertNotIn(f"install {OTHER_JOB}", self.cron_calls())


if __name__ == "__main__":
    unittest.main()
