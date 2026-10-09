"""~/.aws/config is a tracked dotfile, and `aws login` writes into it.

`aws login --profile X` records the session it opened as a `login_session =`
line under that profile. That line is this machine's sign-in, not
configuration: it names the account and user the browser picked, and it
changes every time someone signs in again. So the dotfiles stage reads the file
without those lines when it compares, and carries them over when it
overwrites — otherwise every login reads as DIFFERS, and `--force` signs the
machine out of every profile.

Fixtures are a bare home with the state directory inside it; the tracked file
is the one in the fixture config's dotfiles/, read as the stage reads it.
"""

import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
SCRIPT = FOLDER / "machine-setup.sh"
DOTFILES = fixture_config.DOTFILES

SESSION_DEFAULT = "login_session = arn:aws:sts::EXAMPLE-ACCOUNT-1:assumed-role/Fixture_Role/someone@example.com"
SESSION_ADMIN = "login_session = arn:aws:iam::EXAMPLE-ACCOUNT-2:root"


def tracked(profile="personal"):
    for base in (DOTFILES / profile, DOTFILES / "common"):
        candidate = base / ".aws/config"
        if candidate.is_file():
            return candidate.read_text()
    raise AssertionError("no tracked .aws/config for the profile or common")


def signed_in(text):
    """What `aws login` leaves behind: a session line under two profiles."""
    out = []
    for line in text.splitlines():
        out.append(line)
        if line.strip() == "[default]":
            out.append(SESSION_DEFAULT)
        elif line.strip() == "[profile admin-example]":
            out.append(SESSION_ADMIN)
    return "\n".join(out) + "\n"


class AwsConfigDotfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name) / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.config = self.home / ".aws/config"

    def run_stage(self, *flags):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
        }
        result = subprocess.run([str(SCRIPT), "update", "dotfiles", *flags], env=env,
                                capture_output=True, text=True, cwd=self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The stage's verdict on the file, not the [dry-run] commands under it.
        return [line for line in result.stdout.splitlines()
                if ".aws/config" in line and not line.startswith("  [dry-run]")]

    def test_the_tracked_file_holds_no_session_and_names_the_login_profiles(self):
        text = tracked()
        self.assertNotRegex(text, r"(?m)^\s*login_session\s*=")
        for header in ("[default]", "[profile admin-sandbox]", "[profile admin-example]", "[profile agent-sandbox]"):
            self.assertIn(header, text)

    def test_a_fresh_home_gets_the_tracked_file_private(self):
        self.assertEqual(self.run_stage(), ["  MISSING .aws/config"])
        self.run_stage("--apply")
        self.assertEqual(self.config.read_text(), tracked())
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

    def test_a_login_is_not_drift(self):
        self.run_stage("--apply")
        self.config.write_text(signed_in(self.config.read_text()))
        self.assertEqual(self.run_stage(), ["  ok      .aws/config"])

    def test_a_machine_signed_in_before_the_file_was_tracked_agrees_with_it(self):
        # No record yet, and the only difference is the sessions.
        self.config.parent.mkdir(parents=True)
        self.config.write_text(signed_in(tracked()))
        self.assertEqual(self.run_stage(), ["  ok      .aws/config"])

    def test_force_keeps_each_session_under_its_own_profile(self):
        self.run_stage("--apply")
        edited = signed_in(tracked()).replace("[profile agent-sandbox]", "[profile agent-sandbox]\noutput = json")
        self.config.write_text(edited)
        self.assertEqual(self.run_stage(),
                         ["  DIFFERS .aws/config — edited here; keep it with 'capture --apply', discard it with --force"])
        self.assertEqual(self.run_stage("--apply", "--force"),
                         ["  DIFFERS .aws/config — edited here; --force overwrites (backup kept)"])
        self.assertEqual(self.config.read_text(), signed_in(tracked()))
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(self.run_stage(), ["  ok      .aws/config"])

    def test_force_overwrites_a_file_nothing_was_recorded_for(self):
        # A machine whose config predates tracking: no record, a real
        # difference. The message offers --force, so --force must act.
        self.config.parent.mkdir(parents=True)
        self.config.write_text(signed_in(tracked()).replace("[profile agent-example]", "[profile agent-example]\noutput = json"))
        self.assertEqual(self.run_stage(),
                         ["  DIFFERS .aws/config — machine and repo disagree, nothing recorded says which moved; "
                          "review, then 'capture --apply' or --force"])
        self.assertEqual(self.run_stage("--apply", "--force"),
                         ["  DIFFERS .aws/config — nothing recorded; --force overwrites (backup kept)"])
        self.assertEqual(self.config.read_text(), signed_in(tracked()))
        self.assertEqual(len(list(self.config.parent.glob("config.bak-*"))), 1)
        self.assertEqual(self.run_stage(), ["  ok      .aws/config"])

    def test_force_fills_an_empty_config(self):
        # sd:1177. An empty destination has no records, so `NR == FNR` held
        # for the source's lines too and every one was dropped: --force left
        # the file empty.
        self.config.parent.mkdir(parents=True)
        self.config.write_text("")
        self.run_stage("--apply", "--force")
        self.assertEqual(self.config.read_text(), tracked())
        self.assertEqual(self.run_stage(), ["  ok      .aws/config"])

    def test_a_recorded_empty_config_is_filled_not_emptied_again(self):
        # sd:1361. The state the empty-file bug left behind: an empty file
        # whose hash is the record. The stage reads that as STALE and copies
        # through the same merge, which emptied it again on every run.
        self.config.parent.mkdir(parents=True)
        self.config.write_text("")
        installed = self.state / "installed"
        installed.mkdir()
        # sha256 of zero bytes, as record_hash writes it for an empty file.
        (installed / "dotfiles_.aws_config.sha").write_text(
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n")
        self.assertEqual(self.run_stage("--apply"), ["  STALE   .aws/config — untouched here, repo moved ahead"])
        self.assertEqual(self.config.read_text(), tracked())
        self.assertEqual(self.run_stage(), ["  ok      .aws/config"])


def write_stub(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class AwsConfigCaptureTest(unittest.TestCase):
    """`capture --apply` takes the file back into the repo without the
    session lines (sd:1177).

    capture writes into the checkout it runs from, so it runs from a copy of
    this folder under a temporary root that is not a git checkout. brew,
    defaults and git are stubs that answer nothing, so the copy's manifests
    are all it writes and nothing on this machine is read for them.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.folder = base / "repo/local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        # capture lists host cron jobs through lib/, and stops when it cannot.
        shutil.copytree(FOLDER.parent / "lib", self.folder.parent / "lib",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config_root = fixture_config.copy_capture_config(base)
        self.dotfiles = self.config_root / "machine-setup/dotfiles"
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.config = self.home / ".aws/config"
        self.config.parent.mkdir(parents=True)
        self.stubs = base / "stubs"
        self.stubs.mkdir()
        write_stub(self.stubs, "brew", "exit 0\n")
        write_stub(self.stubs, "git", "exit 1\n")
        # Every key unset, as real `defaults` reports it: exit 1 (sd:1432).
        write_stub(self.stubs, "defaults", "exit 1\n")
        # No sudo ticket: the spotlight step reports it cannot read.
        write_stub(self.stubs, "sudo", "exit 1\n")
        fixture_config.seal(self, self.stubs)

    def capture(self):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
        }
        result = subprocess.run([str(self.folder / "machine-setup.sh"), "capture", "--apply"], env=env,
                                capture_output=True, text=True, cwd=self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [line.strip() for line in result.stdout.splitlines() if ".aws/config" in line]

    def test_an_edit_is_captured_without_the_sessions(self):
        edited = tracked().replace("[profile agent-sandbox]", "[profile agent-sandbox]\noutput = json")
        self.config.write_text(signed_in(edited))
        self.assertEqual(self.capture(), ["capture .aws/config"])
        captured = (self.dotfiles / "personal/.aws/config").read_text()
        self.assertEqual(captured, edited)
        self.assertNotRegex(captured, r"(?m)^\s*login_session\s*=")

    def test_a_session_only_change_captures_nothing(self):
        self.config.write_text(signed_in(tracked()))
        self.assertEqual(self.capture(), ["ok      .aws/config unchanged (common)"])
        self.assertFalse((self.dotfiles / "personal/.aws/config").exists())
        self.assertEqual((self.dotfiles / "common/.aws/config").read_text(), tracked())


if __name__ == "__main__":
    unittest.main()
