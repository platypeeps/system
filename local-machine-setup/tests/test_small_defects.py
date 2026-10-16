"""The small machine-setup defects of sd:3548, one class each.

Each case runs machine-setup.sh from a copy of this folder, with stubs on PATH
for every tool the verb asks; fixture_config.seal fails a case that reaches a
real `defaults`, `sudo` or `launchctl`.
"""

import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"
SCRIPT = FOLDER / "machine-setup.sh"
HOST = "fixturehost"

# Logs every call; lists nothing.
LOGGING_STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$CALL_LOG"
exit 1
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        repo = self.base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.retired = repo / "local-cron-jobs/retired-jobs.txt"
        self.retired.parent.mkdir(parents=True)
        self.retired.write_text("# job  why\nold-job  replaced\n")
        self.config = fixture_config.copy_capture_config(self.base)
        self.profiles = self.config / "machine-setup/profiles"
        self.jobs = self.config / "cron-jobs/jobs"
        (self.jobs / HOST).mkdir(parents=True)
        self.home = self.base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.call_log = self.base / "calls.log"
        self.call_log.touch()
        self.stubs = self.base / "stubs"
        for name in ("brew", "defaults", "sudo", "launchctl", "git", "docker", "fnm", "tailscale", "mas"):
            write_exec(self.stubs / name, LOGGING_STUB)
        fixture_config.seal(self, self.stubs)

    def run_script(self, *args, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config),
            "CRON_JOBS_HOST": HOST,
            "CALL_LOG": str(self.call_log),
            **extra,
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args], env=env,
                              capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    def calls(self):
        return self.call_log.read_text().splitlines()


class Verbs(unittest.TestCase):
    """Help, the no-arg usage and the header each held their own verb list,
    and no two agreed with the dispatch."""

    @staticmethod
    def dispatched():
        text = SCRIPT.read_text()
        block = text[text.rindex('case "$VERB" in'):]
        block = block[:block.index("\nesac")]
        return re.findall(r"^  ([a-z][a-z-]*)\)", block, re.M)

    @staticmethod
    def invoke(*args):
        return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", **fixture_config.env()},
                              stdin=subprocess.DEVNULL, timeout=60)

    def test_help_and_the_no_arg_usage_list_every_verb_dispatch_runs(self):
        verbs = self.dispatched()
        self.assertGreater(len(verbs), 10)
        listed = re.search(r"^verbs: (.*)$", self.invoke("help").stdout, re.M)
        self.assertIsNotNone(listed, "help prints no verbs: line")
        self.assertEqual(sorted(listed.group(1).split()), sorted(verbs))
        usage = self.invoke()
        self.assertEqual(usage.returncode, 1)
        for verb in verbs:
            self.assertRegex(usage.stderr, rf"(^|[\s|]){re.escape(verb)}([\s|]|$)")

    def test_help_names_every_variable_the_script_reads(self):
        read = set(re.findall(r"\$\{((?:MACHINE_SETUP|SD|SYSTEM_TOOLS)_[A-Z0-9_]*):[-=]", SCRIPT.read_text()))
        out = self.invoke("help").stdout
        self.assertEqual(sorted(name for name in read if name not in out), [])

    def test_help_calls_status_a_drift_report_and_stays_out_of_the_health_sweep(self):
        out = " ".join(self.invoke("help").stdout.split())
        self.assertIn("a drift report, not a convention-6 health status", out)
        # The sweep runs the status of every tool whose help names it.
        self.assertNotIn("local-health-check", out)


class StatusWithoutAProfile(Case):
    def test_no_recorded_profile_is_nothing_to_check(self):
        (self.state / "profile").unlink()
        result = self.run_script("status")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("no profile recorded", result.stderr)


class KnownProfiles(Case):
    def test_a_dotfile_in_the_profile_directory_names_no_profile(self):
        # A pin, not a fix: the review read .DS_Store as an empty profile
        # name, but the `*.*` glob never matches a leading dot.
        (self.profiles / ".DS_Store").write_bytes(b"\0")
        result = self.run_script("setup")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("setup needs a profile: personal, work\n", result.stderr)

    def test_an_exported_value_beats_machine_setup_env_an_empty_one_too(self):
        # sd:3964: the env file was sourced bare, so it beat the environment.
        (self.config / "machine-setup/machine-setup.env").write_text('MACHINE_SETUP_PROFILES="from-file"\n')
        for exported, listed in ((None, "from-file"), ("from-shell", "from-shell"), ("", "personal, work")):
            with self.subTest(exported=exported):
                extra = {} if exported is None else {"MACHINE_SETUP_PROFILES": exported}
                result = self.run_script("setup", **extra)
                self.assertIn(f"setup needs a profile: {listed}\n", result.stderr)


class Obsidian(Case):
    def setUp(self):
        super().setUp()
        (self.profiles / "personal.obsidian").write_text("dataview\n")
        self.vault = self.base / "vault"

    def test_a_vault_with_no_plugins_reports_each_one_missing(self):
        (self.vault / ".obsidian/plugins").mkdir(parents=True)
        result = self.run_script("update", "obsidian", OBSIDIAN_VAULT=str(self.vault))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  MISSING plugin dataview", result.stdout)
        self.assertNotIn("extra", result.stdout)

    def test_the_vault_comes_from_shared_env_when_none_is_exported(self):
        # sd:3990. Kept once in <config>/shared.env, not in machine-setup.env.
        (self.vault / ".obsidian/plugins").mkdir(parents=True)
        (self.config / "shared.env").write_text(f'OBSIDIAN_VAULT="{self.vault}"\n')
        result = self.run_script("update", "obsidian")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  MISSING plugin dataview", result.stdout)

    def test_no_vault_anywhere_is_skipped(self):
        result = self.run_script("update", "obsidian")
        self.assertIn("  SKIP    OBSIDIAN_VAULT not set (shared.env or the environment)", result.stdout)

    def test_no_vault_is_skipped(self):
        result = self.run_script("update", "obsidian", OBSIDIAN_VAULT=str(self.vault))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"  SKIP    vault not found or unreadable: {self.vault}", result.stdout)

    def test_a_vault_that_does_not_answer_is_no_answer_not_skipped(self):
        # sd:4816. The read ran unbounded, so a vault on a stalled share hung
        # the nightly update; bounded, a timeout is no answer, never no vault.
        stall = self.base / "stall-python"
        write_exec(stall, "#!/bin/sh\nexec sleep 30\n")
        result = self.run_script("update", "obsidian", OBSIDIAN_VAULT=str(self.vault),
                                 MACHINE_SETUP_PYTHON=str(stall), MACHINE_SETUP_STEP_TIMEOUT="1",
                                 ST_BOUNDED_GRACE="1")
        self.assertIn(f"  FAILED  the vault did not answer in time: {self.vault}; its plugins were not checked",
                      result.stdout)
        self.assertNotIn("SKIP", result.stdout)


class CaptureApps(Case):
    def test_capture_lists_standalone_apps_without_a_cask_search(self):
        # brew lists nothing and answers; no mas, so no App Store list.
        write_exec(self.stubs / "brew", LOGGING_STUB.replace("exit 1", "exit 0"))
        (self.stubs / "mas").unlink()
        apps = self.base / "Applications"
        (apps / "Example Tool.app/Contents").mkdir(parents=True)
        result = self.run_script("capture", "--apply", MACHINE_SETUP_APPLICATIONS_DIR=str(apps))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  app: 1 entries", result.stdout)
        self.assertIn("\nExample Tool\n", (self.profiles / "personal.app").read_text())
        self.assertEqual([c for c in self.calls() if c.startswith("brew search")], [])


    def test_a_failed_app_scan_writes_no_app_list(self):
        # The scan runs where set -e is off; a failed step read as a list.
        write_exec(self.stubs / "brew", LOGGING_STUB.replace("exit 1", "exit 0"))
        (self.stubs / "mas").unlink()
        cp = shutil.which("cp")
        write_exec(self.stubs / "cp", f'#!/bin/sh\nfor last; do :; done\ncase "$last" in */scan.app) exit 1 ;; esac\nexec {cp} "$@"\n')
        (self.profiles / "personal.app").write_text("# kept\n\nOld Tool\n")
        apps = self.base / "Applications"
        (apps / "Example Tool.app/Contents").mkdir(parents=True)
        result = self.run_script("capture", "--apply", MACHINE_SETUP_APPLICATIONS_DIR=str(apps))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  app: UNKNOWN — the standalone app scan failed; personal.app not written", result.stdout)
        self.assertEqual((self.profiles / "personal.app").read_text(), "# kept\n\nOld Tool\n")


class DoctorCron(Case):
    def test_doctor_checks_the_cron_stages_list(self):
        (self.profiles / "personal.cron").write_text("shared-job\nold-job\n")
        (self.jobs / HOST / "host-job.job").write_text('JOB_SCHEDULE="0 23 * * *"\n')
        self.run_script("doctor")
        checked = [c.rsplit(".", 1)[-1] for c in self.calls()
                   if c.startswith(f"launchctl print gui/") and ".cron." in c]
        self.assertEqual(sorted(checked), ["host-job", "shared-job"])


class NoMachinePath(unittest.TestCase):
    def test_no_hint_names_a_volume_of_one_machine(self):
        # The restore hint named one machine's backup disk.
        self.assertEqual(re.findall(r"(?<!/System)/Volumes/\S*", SCRIPT.read_text()), [])


if __name__ == "__main__":
    unittest.main()
