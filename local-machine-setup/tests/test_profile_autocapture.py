"""Regression tests for profile-autocapture.sh.

The job commits and pushes the profile rosters with nobody watching, so every
case here is about a way it could refuse forever, or write from a checkout
that is not current.

`capture --apply` refuses outright on a checkout that is behind the remote --
the manifests it rewrites wholesale are the ones the missing commits may have
changed. Nothing pulls `main` on a timer, so the checkout this job fires on is
routinely a commit or two behind, and the refusal repeated every night.

The rosters live in the operator's config, not in this repository, so the
fixture is a throwaway config checkout (standing in for $SYSTEM_TOOLS_CONFIG)
with a local bare repository as `origin`, a tools folder holding the job
beside a stub `machine-setup.sh` that refuses exactly as the real one does,
and local-autocommit's helper next to it.
Nothing touches the network: `pull --ff-only` and `push` both run against a
path on disk, which exercises the real git code paths a stub would not.

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

# Refuses when the checkout is behind, writes one roster line when it is not.
# That is `check_repo_current` plus an additive capture, reduced to the two
# behaviours this job depends on.
STUB_MACHINE_SETUP = """#!/bin/sh
set -eu
cd "$SYSTEM_TOOLS_CONFIG/machine-setup/profiles"
if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "captured-entry" >> personal.cron
  exit 0
fi
cd "$(git rev-parse --show-toplevel)"
if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
  behind=$(git rev-list --count 'HEAD..@{u}' 2>/dev/null || echo 0)
  if [ "$behind" -gt 0 ]; then
    echo "  STALE   checkout is $behind commit(s) behind"
    echo "  refusing to write: capture rewrites the profile manifests"
    exit 1
  fi
fi
echo "captured-entry" >> machine-setup/profiles/personal.cron
"""


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


class Fixture(unittest.TestCase):
    """A config checkout with a real `origin`, and the job beside a stub."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="profile-autocapture-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bare = os.path.join(self.tmp, "origin.git")
        self.work = os.path.join(self.tmp, "work")

        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", self.bare], check=True
        )
        subprocess.run(["git", "init", "-q", "-b", "main", self.work], check=True)
        git(self.work, "config", "user.name", "fixture")
        git(self.work, "config", "user.email", "fixture@localhost")

        # The job lives one level below the tools root, exactly as
        # local-machine-setup/profile-autocapture.sh does, because it finds
        # local-autocommit as "$DIR/..". A fixture that put it elsewhere would
        # pass while the shipped layout failed.
        self.tools = pathlib.Path(self.tmp) / "tools"
        tool_dir = self.tools / "local-machine-setup"
        tool_dir.mkdir(parents=True)
        self.script = tool_dir / "profile-autocapture.sh"
        shutil.copy(SCRIPT, self.script)
        self.script.chmod(0o755)
        # The job asks autocommit.sh for the deploy key's ssh command, so the
        # fixture ships it: without it every run printed "No such file" and
        # the tests passed anyway. The key points into the fixture, absent.
        helper_dir = self.tools / "local-autocommit"
        helper_dir.mkdir()
        shutil.copy(FOLDER.parent / "local-autocommit" / "autocommit.sh",
                    helper_dir / "autocommit.sh")
        self.key = os.path.join(self.tmp, "no-key")
        stub = tool_dir / "machine-setup.sh"
        stub.write_text(STUB_MACHINE_SETUP)
        stub.chmod(0o755)

        (pathlib.Path(self.work) / SCOPE).mkdir(parents=True)
        self.write(ROSTER, "seed-entry\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "seed")
        git(self.work, "remote", "add", "origin", self.bare)
        git(self.work, "push", "-q", "-u", "origin", "main")

    def write(self, rel, text):
        path = pathlib.Path(self.work) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_job(self, env=None):
        return subprocess.run(
            ["sh", str(self.script)],
            cwd=self.work,
            capture_output=True,
            text=True,
            env={**os.environ, "AUTOCOMMIT_SSH_KEY": self.key,
                 "SYSTEM_TOOLS_CONFIG": self.work, **(env or {})},
        )

    def advance_remote(self, roster=None, subject="a pull request landed"):
        """Land a commit on `origin/main` that this work tree does not have.

        With `roster`, the commit rewrites the roster this machine captures --
        the case the pre-capture fast-forward must not swallow. Without it, the
        commit touches an unrelated file, which is what the checkout is
        normally behind by.
        """
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "clone", "-q", self.bare, other], check=True)
        git(other, "config", "user.name", "elsewhere")
        git(other, "config", "user.email", "elsewhere@localhost")
        if roster is None:
            pathlib.Path(other, "unrelated.txt").write_text("landed on the remote\n")
        else:
            pathlib.Path(other, ROSTER).write_text(roster)
        git(other, "add", "-A")
        git(other, "commit", "-qm", subject)
        git(other, "push", "-q", "origin", "main")
        head = git(other, "rev-parse", "HEAD").strip()
        shutil.rmtree(other)
        return head

    def head(self):
        return git(self.work, "rev-parse", "HEAD").strip()

    def log_subjects(self):
        return git(self.work, "log", "--format=%s").splitlines()


class BehindTheRemote(Fixture):
    def test_fast_forwards_before_capture(self):
        """REGRESSION: a checkout one commit behind used to fail every night.

        Capture refuses on a stale checkout and names a pull as the remedy.
        The job never ran one, so the same refusal repeated until a person
        pulled by hand.
        """
        landed = self.advance_remote()
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            landed,
            git(self.work, "log", "--format=%H"),
            "the remote's commit is missing: the job did not fast-forward",
        )
        self.assertIn(
            "captured-entry",
            pathlib.Path(self.work, ROSTER).read_text(),
            "capture never ran",
        )
        self.assertEqual(
            git(self.work, "rev-parse", "HEAD").strip(),
            git(self.work, "rev-parse", "origin/main").strip(),
            "the roster commit was not pushed",
        )

    def test_incoming_roster_change_is_not_fast_forwarded(self):
        """REGRESSION: an unconditional pull reverts a removal made elsewhere.

        The seeded entry is still installed on this machine. Pull a commit
        that removes it from the roster and capture reads the removal as an
        addition, writes it back and pushes -- undoing overnight what a person
        did on another machine. That is the case capture's own refusal names:
        "those commits may be what changed them".

        Against a script that pulls unconditionally this run ends
        "committed and pushed", exit 0, with the removal reverted.
        """
        landed = self.advance_remote(
            roster="", subject="drop the entry this machine still has installed"
        )
        before = self.head()

        result = self.run_job()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(before, self.head(), "the job fast-forwarded anyway")
        self.assertNotIn(
            landed,
            git(self.work, "log", "--format=%H"),
            "the roster-changing commit was pulled in",
        )
        self.assertIn(f"incoming commits change {SCOPE}", result.stderr)
        self.assertEqual(
            "seed-entry\n",
            pathlib.Path(self.work, ROSTER).read_text(),
            "capture ran and rewrote the roster",
        )

    def test_diverged_checkout_is_reported_not_merged(self):
        """PIN: fast-forward only. A diverged checkout still wants a person.

        Merging unattended would put work nobody reviewed on `main`.
        """
        self.advance_remote()
        pathlib.Path(self.work, "local-only.txt").write_text("mine\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "a local commit")
        before = self.head()

        result = self.run_job()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(before, self.head(), "HEAD moved on a diverged checkout")
        self.assertIn("reconcile", result.stderr)
        self.assertNotIn(
            "captured-entry",
            pathlib.Path(self.work, ROSTER).read_text(),
            "capture wrote from a checkout that is not current",
        )


class Current(Fixture):
    def test_current_checkout_commits_and_pushes(self):
        """PIN: the pull is an addition, not a replacement of the normal path."""
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("committed and pushed", result.stdout)
        self.assertEqual(
            git(self.work, "rev-parse", "HEAD").strip(),
            git(self.work, "rev-parse", "origin/main").strip(),
        )

    def test_no_upstream_skips_the_pre_capture_pull(self):
        """PIN: a checkout with no upstream has nothing to fast-forward onto.

        The pre-capture pull is skipped rather than failing the run, the same
        shape the unpushed-commit guard below it already uses. The run still
        ends unhappily further down, where the post-commit push has no remote
        to push to -- that is the older behaviour and not this guard's.
        """
        git(self.work, "branch", "--unset-upstream")
        result = self.run_job()
        self.assertNotIn(
            "cannot fast-forward onto the remote",
            result.stderr,
            "the pre-capture fast-forward fired with no upstream to read",
        )
        self.assertIn("captured-entry", pathlib.Path(self.work, ROSTER).read_text())

    def test_unversioned_config_captures_and_commits_nothing(self):
        """PIN: a profile directory under no version control is not an error.

        Capture still records what appeared; there is simply no commit.
        """
        shutil.rmtree(os.path.join(self.work, ".git"))
        result = self.run_job()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not under version control", result.stdout)
        self.assertIn("captured-entry", pathlib.Path(self.work, ROSTER).read_text())

    def test_a_missing_profile_directory_is_named(self):
        """PIN: no config means nothing to capture, said once with the path."""
        missing = os.path.join(self.tmp, "no-config")
        result = self.run_job(env={"SYSTEM_TOOLS_CONFIG": missing})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"no profile directory at {missing}/machine-setup/profiles", result.stderr)

    def test_dirty_scope_still_refuses_before_any_pull(self):
        """PIN: the dirty-scope refusal stays the first thing the job does.

        A hand edit sitting in the scope must be found before HEAD moves.
        """
        self.advance_remote()
        self.write(ROSTER, "seed-entry\nsomeone-was-editing\n")
        before = self.head()

        result = self.run_job()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("already dirty before capture", result.stdout)
        self.assertEqual(before, self.head(), "HEAD moved past a hand edit")



# Refuses, as the ruleset on main would, any call that does not offer exactly
# EXPECT_KEY with the agent off, and otherwise runs the remote command locally.
SSH_STUB = r"""#!/bin/sh
key=""; agent=""; prev=""
for arg; do
  [ "$prev" = "-i" ] && key=$arg
  [ "$arg" = "IdentityAgent=none" ] && agent=off
  prev=$arg
done
if [ -n "${EXPECT_KEY:-}" ] && { [ "$key" != "$EXPECT_KEY" ] || [ "$agent" != off ]; }; then
  echo "stub ssh: refused, key='$key' agent='$agent'" >&2
  exit 255
fi
exec sh -c "$prev"
"""


class DeployKey(Fixture):
    """The job pushes with the deploy key autocommit.sh names (sd:1417)."""

    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        stub = pathlib.Path(bindir, "ssh")
        stub.write_text(SSH_STUB)
        stub.chmod(0o755)
        git(self.work, "remote", "set-url", "origin", f"fakehost:{self.bare}")
        self.env = {"PATH": f"{bindir}:{os.environ['PATH']}", "GIT_SSH_VARIANT": "ssh"}

    def test_the_fetch_and_the_push_go_out_with_the_deploy_key(self):
        """REGRESSION: the fetch replaced GIT_SSH_COMMAND with its own, so a
        machine whose only way in is the deploy key compared against a stale
        ref; the push used the operator's key."""
        self.key = os.path.join(self.tmp, "ssh", "system_autocommit")
        os.makedirs(os.path.dirname(self.key))
        pathlib.Path(self.key).write_text("not a real key\n")
        os.chmod(self.key, 0o600)
        landed = self.advance_remote()

        result = self.run_job(env=dict(self.env, EXPECT_KEY=self.key))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("fetch failed", result.stderr)
        self.assertEqual(git(self.bare, "rev-parse", "main").strip(), self.head())
        self.assertEqual(git(self.work, "rev-parse", "HEAD~1").strip(), landed)

    def test_a_missing_key_is_named_and_the_push_is_still_tried(self):
        """PIN: before the ruleset lands the default identity still pushes."""
        result = self.run_job(env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MISSING deploy key", result.stderr)

    def test_a_broken_helper_stops_the_job(self):
        """REGRESSION: any helper failure read as "no key" and the job went on
        to push as the default identity."""
        pathlib.Path(self.tools, "local-autocommit", "autocommit.sh").write_text("exit 2\n")
        before = self.head()

        result = self.run_job(env=self.env)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("key ssh-command failed (2)", result.stderr)
        self.assertEqual(before, self.head())
        self.assertNotIn("captured-entry", pathlib.Path(self.work, ROSTER).read_text())


if __name__ == "__main__":
    unittest.main()
