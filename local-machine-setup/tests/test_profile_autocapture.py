"""Regression tests for profile-autocapture.sh.

The job records additive roster changes with nobody watching. The rosters
live in the config folder and keep no git history, so the job writes them and
stops: it stages, commits and pushes nothing, even when the config folder
happens to be a git checkout.

The fixture is a throwaway config folder (standing in for
$SYSTEM_TOOLS_CONFIG) and a tools folder holding the job beside a stub
`machine-setup.sh`. The stub appends one roster line and exits with
STUB_RC, which is all this job depends on: these are tests of what the job
does with capture's result, not of capture.

Each case says which kind it is. REGRESSION means it fails against a script
without the guard it names. PIN means it records a deliberate decision that
could be undone by accident.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = os.environ.get(
    "PROFILE_AUTOCAPTURE_TEST_SCRIPT", str(FOLDER / "profile-autocapture.sh")
)

SCOPE = "machine-setup/profiles"
ROSTER = f"{SCOPE}/personal.cron"

# An additive capture reduced to what this job reads: the arguments it was
# given, one roster line written, and an exit code.
STUB_MACHINE_SETUP = """#!/bin/sh
set -eu
printf '%s\\n' "$*" > "$SYSTEM_TOOLS_CONFIG/stub-args"
echo "captured-entry" >> "$SYSTEM_TOOLS_CONFIG/machine-setup/profiles/personal.cron"
exit "${STUB_RC:-0}"
"""


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="profile-autocapture-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = pathlib.Path(self.tmp) / "config"
        (self.config / SCOPE).mkdir(parents=True)
        (self.config / ROSTER).write_text("seed-entry\n")

        # The job lives one level below the tools root, exactly as
        # local-machine-setup/profile-autocapture.sh does, and finds
        # machine-setup.sh beside itself.
        tool_dir = pathlib.Path(self.tmp) / "tools" / "local-machine-setup"
        tool_dir.mkdir(parents=True)
        self.script = tool_dir / "profile-autocapture.sh"
        shutil.copy(SCRIPT, self.script)
        self.script.chmod(0o755)
        stub = tool_dir / "machine-setup.sh"
        stub.write_text(STUB_MACHINE_SETUP)
        stub.chmod(0o755)

    def run_job(self, env=None):
        return subprocess.run(
            ["sh", str(self.script)],
            cwd=self.tmp,
            capture_output=True,
            text=True,
            env={**os.environ, "SYSTEM_TOOLS_CONFIG": str(self.config),
                 **(env or {})},
        )

    def roster(self):
        return (self.config / ROSTER).read_text()


class Capture(Fixture):
    def test_runs_an_additive_applied_capture(self):
        """PIN: additive is what makes an unattended write safe.

        A mirror capture would write a removal away; the job must never ask
        for one.
        """
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.config / "stub-args").read_text().split(),
                         ["capture", "--apply", "--additive"])
        self.assertEqual(self.roster(), "seed-entry\ncaptured-entry\n")

    def test_a_held_removal_exits_3(self):
        """PIN: exit 3 fires the failure banner, which is the intended trade."""
        result = self.run_job(env={"STUB_RC": "3"})
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertNotIn("capture failed", result.stderr)

    def test_a_failing_capture_propagates(self):
        """REGRESSION: a failure must not read as success on a quiet night."""
        result = self.run_job(env={"STUB_RC": "9"})
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertIn("capture failed (rc=9)", result.stderr)

    def test_a_missing_profile_directory_is_named(self):
        """PIN: no config means nothing to capture, said once with the path."""
        missing = os.path.join(self.tmp, "no-config")
        result = self.run_job(env={"SYSTEM_TOOLS_CONFIG": missing})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"no profile directory at {missing}/machine-setup/profiles",
                      result.stderr)


class NoGitHistory(Fixture):
    def test_a_config_checkout_gets_no_commit_and_no_push(self):
        """PIN: the rosters keep no git history.

        A config folder that is a git checkout with an upstream is left as
        the job found it: the roster is rewritten on disk, HEAD does not
        move, nothing is staged and the remote gets nothing.
        """
        bare = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.config)], check=True)
        git(self.config, "config", "user.name", "fixture")
        git(self.config, "config", "user.email", "fixture@localhost")
        git(self.config, "add", "-A")
        git(self.config, "commit", "-qm", "seed")
        git(self.config, "remote", "add", "origin", bare)
        git(self.config, "push", "-q", "-u", "origin", "main")
        head = git(self.config, "rev-parse", "HEAD").strip()

        result = self.run_job()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("captured-entry", self.roster())
        self.assertEqual(git(self.config, "rev-parse", "HEAD").strip(), head)
        self.assertEqual(git(bare, "rev-parse", "main").strip(), head)
        self.assertEqual(git(self.config, "diff", "--cached", "--name-only"), "")
        self.assertIn(ROSTER, git(self.config, "status", "--porcelain"))


if __name__ == "__main__":
    unittest.main()
