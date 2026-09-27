"""Regression tests for autocommit.sh.

The script commits and pushes without anyone watching, so every case here is
about a way it could report success having done nothing, or commit something
nobody asked it to. Both are failures that only show up later: the first as a
generated file that silently stopped landing, the second as somebody's hand
edit inside an unattended commit.

The fixture is a throwaway work tree with a local bare repository as `origin`.
Nothing touches the network -- `push` and `pull --ff-only` both run against a
path on disk, which exercises the real git code paths that a stub would not.

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
SCRIPT = os.environ.get("AUTOCOMMIT_TEST_SCRIPT", str(FOLDER / "autocommit.sh"))

SCOPE = "generated"
OTHER = "hand-written"


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


class Fixture(unittest.TestCase):
    """A work tree with a real `origin`, and the script copied in beside it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="autocommit-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bare = os.path.join(self.tmp, "origin.git")
        self.work = os.path.join(self.tmp, "work")

        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.bare], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", self.work], check=True)
        git(self.work, "config", "user.name", "fixture")
        git(self.work, "config", "user.email", "fixture@localhost")

        # The tool lives one level below the repository root, exactly as
        # local-autocommit/autocommit.sh does, because it resolves ROOT as
        # "$DIR/..". A fixture that put it at the root would pass while the
        # shipped layout failed.
        tool_dir = pathlib.Path(self.work) / "local-autocommit"
        tool_dir.mkdir()
        self.script = tool_dir / "autocommit.sh"
        shutil.copy(SCRIPT, self.script)
        self.script.chmod(0o755)

        (pathlib.Path(self.work) / SCOPE).mkdir()
        self.write(f"{SCOPE}/inventory.txt", "first\n")
        self.write(f"{OTHER}/notes.txt", "mine\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "seed")
        git(self.work, "remote", "add", "origin", self.bare)
        git(self.work, "push", "-q", "-u", "origin", "main")
        # `git init --bare` leaves no origin/HEAD; the script asks the remote
        # for the default branch and falls back only when it cannot.
        git(self.work, "remote", "set-head", "origin", "-a")

        # The deploy key and the LaunchAgents folder are this machine's, so a
        # run that read them would answer for the machine and not the case.
        # Both point into the fixture: no key, and no job loaded.
        self.key = os.path.join(self.tmp, "ssh", "system_autocommit")
        self.agents = os.path.join(self.tmp, "LaunchAgents")
        os.makedirs(self.agents)

    def write(self, rel, text):
        path = pathlib.Path(self.work) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_tool(self, *args, env=None):
        # AUTOCOMMIT_GH defaults to `false`, an unreadable answer, so no case
        # asks the real GitHub; DeployKeyTest swaps in a stub that answers.
        environment = {**os.environ, "AUTOCOMMIT_SSH_KEY": self.key,
                       "AUTOCOMMIT_LAUNCH_AGENTS": self.agents,
                       "AUTOCOMMIT_GH": "false", **(env or {})}
        return subprocess.run(
            ["sh", str(self.script), *args],
            cwd=self.work, capture_output=True, text=True, env=environment,
        )

    def remote_log(self):
        return git(self.bare, "log", "--oneline", "main")

    def advance_remote(self, subject="landed while nobody was pulling"):
        """Somebody else's pull request lands, and this checkout does not know.

        The real cause of a stranded commit: pull requests land here all day
        and nothing pulls `main` on a timer, so a nightly job fires on a
        checkout that is a commit or two behind.
        """
        other = os.path.join(self.tmp, "other-clone")
        subprocess.run(["git", "clone", "-q", self.bare, other], check=True)
        git(other, "config", "user.name", "someone")
        git(other, "config", "user.email", "someone@example.com")
        pathlib.Path(other, "landed.txt").write_text(subject + "\n")
        git(other, "add", "-A")
        git(other, "commit", "-qm", subject)
        git(other, "push", "-q", "origin", "main")


class CheckTest(Fixture):
    def test_clean_scope_on_default_branch_passes(self):
        """PIN: the ordinary answer is 0, or no job would ever write."""
        result = self.run_tool("check", "--scope", SCOPE)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dirty_scope_defers(self):
        """REGRESSION: a hand edit in the scope must stop the generator.

        Without this the generator overwrites the edit and the commit that
        follows carries whatever survived, unreviewed and unattributed.
        """
        self.write(f"{SCOPE}/inventory.txt", "someone was editing this\n")
        result = self.run_tool("check", "--scope", SCOPE)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("already dirty", result.stderr)

    def test_feature_branch_defers(self):
        """REGRESSION: a generated commit must not land on someone's branch.

        Three sessions share the real checkout. A nightly job that fires while
        the tree sits on a feature branch used to commit and push there, and
        the churn rode into that branch's pull request.
        """
        git(self.work, "switch", "-qc", "feature/whatever")
        result = self.run_tool("check", "--scope", SCOPE)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("feature/whatever", result.stderr)

    def test_unpushed_commit_by_same_author_fails(self):
        """REGRESSION: a stuck commit must not read as a healthy quiet run.

        Without it the run that failed to push says so once, and every later
        run reports "no changes" and exits 0 while the commit sits local.
        """
        self.write(f"{SCOPE}/inventory.txt", "second\n")
        git(self.work, "-c", "user.name=ai-apps", "-c", "user.email=ai-apps@localhost",
            "commit", "-qam", "stuck")
        result = self.run_tool("check", "--scope", SCOPE, "--author", "ai-apps")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("did not push", result.stderr)

    def test_unpushed_commit_by_another_author_is_ignored(self):
        """PIN: a person's own local commit is a normal state, not a block."""
        self.write(f"{OTHER}/notes.txt", "my work in progress\n")
        git(self.work, "commit", "-qam", "mine, unpushed")
        result = self.run_tool("check", "--scope", SCOPE, "--author", "ai-apps")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class CommitTest(Fixture):
    def test_commits_and_pushes(self):
        """PIN: the whole point. The remote must actually have it."""
        self.write(f"{SCOPE}/inventory.txt", "second\n")
        result = self.run_tool("commit", "--scope", SCOPE,
                               "--author", "ai-apps", "--message", "refresh inventory")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("refresh inventory", self.remote_log())
        self.assertEqual(git(self.work, "status", "--porcelain"), "")

    def test_scope_may_be_a_single_file(self):
        """REGRESSION: the outside-path guard matched "$SCOPE/" only.

        A file scope has no trailing slash, so every staged path looked
        outside it and the tool refused its own correct commit.
        """
        self.write(f"{OTHER}/notes.txt", "regenerated\n")
        result = self.run_tool("commit", "--scope", f"{OTHER}/notes.txt",
                               "--author", "repo-sync", "--message", "reconcile list")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reconcile list", self.remote_log())

    def test_no_change_defers(self):
        result = self.run_tool("commit", "--scope", SCOPE,
                               "--author", "ai-apps", "--message", "nothing")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)

    def test_no_change_with_empty_ok_passes(self):
        """PIN: generators usually change nothing; that is not a failure."""
        result = self.run_tool("commit", "--scope", SCOPE, "--empty-ok",
                               "--author", "ai-apps", "--message", "nothing")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_staged_path_outside_scope_refuses_and_unstages(self):
        """REGRESSION: the commit must carry the scope and nothing else.

        Anything already staged when the job runs would otherwise ride along.
        The reset matters as much as the refusal: leaving it staged hands the
        next run a tree it will refuse again for a reason it did not cause.
        """
        self.write(f"{SCOPE}/inventory.txt", "second\n")
        self.write(f"{OTHER}/notes.txt", "not mine to commit\n")
        git(self.work, "add", "--", f"{OTHER}/notes.txt")
        result = self.run_tool("commit", "--scope", SCOPE,
                               "--author", "ai-apps", "--message", "refresh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("outside", result.stderr)
        self.assertEqual(git(self.work, "diff", "--cached", "--name-only"), "")
        self.assertNotIn("refresh", self.remote_log())

    def test_push_failure_reports_and_keeps_the_commit(self):
        """REGRESSION: `pull && push` under `set -e` reached the success line.

        `set -e` exempts a failing command inside an AND-OR list, so the list
        could fail whole and the script still printed that it had pushed.
        """
        shutil.rmtree(self.bare)
        self.write(f"{SCOPE}/inventory.txt", "second\n")
        result = self.run_tool("commit", "--scope", SCOPE,
                               "--author", "ai-apps", "--message", "refresh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("committed and pushed", result.stdout)
        self.assertIn("local only", result.stderr)
        self.assertIn("refresh", git(self.work, "log", "--oneline", "-1"))


class BehindTheRemoteTest(Fixture):
    """The checkout is behind `origin` when the job fires.

    This is the ordinary state, not an edge case, and it used to strand the
    commit for good: the tool committed on whatever the checkout held and only
    then tried to fast-forward, by which point the new commit and the remote's
    were siblings. 2026-09-21 is the case in the wild -- one commit behind was
    enough, and `check` refused every night after.
    """

    def test_commit_still_reaches_the_remote(self):
        """REGRESSION: the fast-forward ran after the commit, so it could not."""
        self.advance_remote()
        self.write(f"{SCOPE}/inventory.txt", "second\n")

        result = self.run_tool("commit", "--scope", SCOPE,
                               "--author", "ai-apps", "--message", "refresh inventory")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("refresh inventory", self.remote_log())
        self.assertIn("landed while nobody was pulling", self.remote_log())

    def test_check_fast_forwards_before_the_generator_writes(self):
        """REGRESSION: the safe moment to catch up is while the scope is clean."""
        self.advance_remote()

        result = self.run_tool("check", "--scope", SCOPE, "--author", "ai-apps")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(pathlib.Path(self.work, "landed.txt").exists(),
                        "check left the checkout behind the remote")

    def test_a_genuinely_diverged_checkout_is_still_reported(self):
        """PIN: fast-forward only. Resolving a divergence unattended is banned.

        A commit this author could not push stays the loud `1`, because a
        person has to reconcile it.
        """
        self.write(f"{SCOPE}/inventory.txt", "second\n")
        git(self.work, "-c", "user.name=ai-apps", "-c", "user.email=ai-apps@localhost",
            "commit", "-qam", "stranded")
        self.advance_remote()

        result = self.run_tool("check", "--scope", SCOPE, "--author", "ai-apps")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("did not push", result.stderr)


class ArgumentTest(Fixture):
    def test_absolute_scope_refused(self):
        """PIN: every check below parse_options assumes a repo-relative path."""
        result = self.run_tool("check", "--scope", self.work)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("repo-relative", result.stderr)

    def test_climbing_scope_refused(self):
        result = self.run_tool("check", "--scope", f"../{SCOPE}")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_missing_scope_refused(self):
        result = self.run_tool("check", "--scope", "no-such-folder")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_no_argument_prints_usage_to_stderr_and_exits_1(self):
        """PIN: convention 1 in CLAUDE.md."""
        result = self.run_tool()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Usage:", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_help_exits_0(self):
        """PIN: convention 1 in CLAUDE.md."""
        for flag in ("-h", "--help", "help"):
            with self.subTest(flag=flag):
                result = self.run_tool(flag)
                self.assertEqual(result.returncode, 0)
                self.assertIn("Usage:", result.stdout)

    def test_help_declares_itself_to_local_health_check(self):
        """PIN: convention 6 -- the sweep finds tools by this string alone.

        There is no list of tools in health-check.sh. Losing this sentence
        removes the tool from the nightly sweep and nothing else reports it.
        """
        result = self.run_tool("help")
        self.assertIn("local-health-check", result.stdout)


class MultipleScopeTest(Fixture):
    """A generator that rewrites more than one tracked file.

    repo-sync is the real case: `remove_entry` loops over every conf the
    profile reads, so a removal rewrites the common conf as well as the
    profile one. The call site named the profile conf alone, which left the
    common one dirty after every removal -- for good, because `check` then
    defers on it every night after.
    """

    SECOND = "generated-too"

    def setUp(self):
        super().setUp()
        self.write(f"{self.SECOND}/other.txt", "first\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "seed second scope")
        git(self.work, "push", "-q", "origin", "main")

    def test_commit_lands_every_scope_it_was_given(self):
        """REGRESSION: a single --scope left the other file dirty for good."""
        self.write(f"{SCOPE}/inventory.txt", "second\n")
        self.write(f"{self.SECOND}/other.txt", "second\n")
        result = self.run_tool("commit", "--scope", SCOPE,
                               "--scope", self.SECOND,
                               "--author", "repo-sync", "--message", "reconcile")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reconcile", self.remote_log())
        self.assertEqual(git(self.work, "status", "--porcelain"), "")
        landed = git(self.work, "show", "--name-only", "--format=", "HEAD").split()
        self.assertIn(f"{SCOPE}/inventory.txt", landed)
        self.assertIn(f"{self.SECOND}/other.txt", landed)

    def test_check_defers_when_any_one_scope_is_dirty(self):
        """REGRESSION: a scope nobody asked about is a scope nobody guards."""
        self.write(f"{self.SECOND}/other.txt", "hand edit\n")
        result = self.run_tool("check", "--scope", SCOPE,
                               "--scope", self.SECOND, "--author", "repo-sync")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn(self.SECOND, result.stderr)

    def test_a_path_outside_every_scope_still_refuses(self):
        """PIN: widening the pathspec must not widen the blast radius."""
        self.write(f"{SCOPE}/inventory.txt", "second\n")
        self.write(f"{OTHER}/notes.txt", "mine, edited\n")
        git(self.work, "add", "--", f"{OTHER}/notes.txt")
        result = self.run_tool("commit", "--scope", SCOPE,
                               "--scope", self.SECOND,
                               "--author", "repo-sync", "--message", "reconcile")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("outside", result.stderr)
        self.assertEqual(git(self.work, "diff", "--cached", "--name-only"), "")
        self.assertNotIn("reconcile", self.remote_log())


class StatusTest(Fixture):
    def test_status_reports_clean(self):
        result = self.run_tool("status")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("no auto-committed scopes", result.stdout)

    def test_status_enumerates_every_profile_conf(self):
        """REGRESSION: the list named repos.personal.conf and nothing else.

        Three tracked conf files sat beside it and `status` reported clean on
        all three, having never looked at them. The write set is enumerated
        from the filesystem now, so a new conf is covered the day it lands.
        """
        for profile in ("common", "personal", "work", "laptop"):
            self.write(f"local-repo-sync/repos.{profile}.conf", "seed\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "seed confs")
        git(self.work, "push", "-q", "origin", "main")
        self.write("local-repo-sync/repos.common.conf", "edited\n")

        result = self.run_tool("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for profile in ("common", "personal", "work", "laptop"):
            self.assertIn(f"local-repo-sync/repos.{profile}.conf", result.stdout)
        self.assertIn("DIRTY   local-repo-sync/repos.common.conf", result.stdout)
        self.assertIn("4 scope(s), 1 dirty", result.stdout)

    def test_status_sees_a_stranded_commit_by_any_job(self):
        """REGRESSION: this asked `unpushed_for autocommit` and nothing else.

        `autocommit` is the default identity no caller passes -- ai-apps
        commits as `ai-apps`, repo-sync as `repo-sync` -- so the single state
        this report exists to raise was the one state it could not see. On
        2026-09-21 an `ai-apps` commit sat unpushed for nine hours while this
        printed "nothing unpushed" and exited 0, and local-health-check read
        that 0.
        """
        self.write("local-ai-apps/profiles/personal.inv", "seed\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "seed inventory")
        git(self.work, "push", "-q", "origin", "main")

        self.write("local-ai-apps/profiles/personal.inv", "refreshed\n")
        git(self.work, "-c", "user.name=ai-apps", "-c", "user.email=ai-apps@localhost",
            "commit", "-qam", "chore(ai-apps): refresh personal inventory")

        result = self.run_tool("status")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("refresh personal inventory", result.stdout)
        self.assertIn("ai-apps", result.stdout)

    def test_a_pushed_scope_is_not_reported_as_stranded(self):
        """PIN: the ordinary healthy machine must stay silent, or the sweep
        raises a finding every night and the signal stops meaning anything."""
        self.write("local-ai-apps/profiles/personal.inv", "seed\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "seed inventory")
        git(self.work, "push", "-q", "origin", "main")

        result = self.run_tool("status")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing unpushed", result.stdout)


# Logs each call as one line of NUL-free, tab-joined arguments, so a path with
# a space stays one argument in the log. With EXPECT_KEY set it refuses, as
# GitHub's ruleset would, any call that does not offer exactly that key with
# the agent off; the remote command itself runs locally.
SSH_STUB = r"""#!/bin/sh
line=""; key=""; agent=""; prev=""
for arg; do
  line="$line$arg	"
  [ "$prev" = "-i" ] && key=$arg
  [ "$arg" = "IdentityAgent=none" ] && agent=off
  prev=$arg
done
printf '%s\n' "$line" >> "$SSH_LOG"
if [ -n "${EXPECT_KEY:-}" ] && { [ "$key" != "$EXPECT_KEY" ] || [ "$agent" != off ]; }; then
  echo "stub ssh: refused, key='$key' agent='$agent'" >&2
  exit 255
fi
exec sh -c "$prev"
"""

# Stands in for `gh api orgs/<owner> --jq ...`: logs its arguments and prints
# GH_ANSWER, or fails as an unauthenticated or offline `gh` does.
GH_STUB = r"""#!/bin/sh
line=""
for arg; do line="$line$arg	"; done
printf '%s\n' "$line" >> "$GH_LOG"
[ -n "${GH_ANSWER:-}" ] || { echo "stub gh: not logged in" >&2; exit 1; }
echo "$GH_ANSWER"
"""


class DeployKeyTest(Fixture):
    """The jobs push with a per-machine deploy key (sd:1417).

    The ruleset on main requires checks, and exempts only that key. A push
    that goes out as any other identity is refused, so the key has to reach
    every fetch, pull and push this tool makes, and a missing key has to be
    named rather than surfacing as a bare push failure.

    `origin` is an ssh-style URL here, and `ssh` on PATH is a stub that logs
    its arguments and runs the remote command locally. The real git transport
    runs; only the network is replaced.
    """

    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        self.ssh_log = os.path.join(self.tmp, "ssh.log")
        stub = pathlib.Path(bindir, "ssh")
        stub.write_text(SSH_STUB)
        stub.chmod(0o755)
        gh = pathlib.Path(bindir, "gh")
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        self.gh_log = os.path.join(self.tmp, "gh.log")
        git(self.work, "remote", "set-url", "origin", f"fakehost:{self.bare}")
        self.env = {"PATH": f"{bindir}:{os.environ['PATH']}", "SSH_LOG": self.ssh_log,
                    "GIT_SSH_VARIANT": "ssh", "AUTOCOMMIT_GH": str(gh),
                    "GH_LOG": self.gh_log}

    def make_key(self, mode=0o600):
        os.makedirs(os.path.dirname(self.key), exist_ok=True)
        pathlib.Path(self.key).write_text("not a real key\n")
        os.chmod(self.key, mode)

    def load_job(self, job="repo-sync-nightly"):
        pathlib.Path(self.agents, f"local.system-tools.cron.{job}.plist").write_text("<plist/>\n")

    def gh_calls(self):
        path = pathlib.Path(self.gh_log)
        return path.read_text().splitlines() if path.exists() else []

    def healthy_machine_on_github(self):
        """A pushing job with its key, and origin naming the real org."""
        self.seed_scope()
        self.load_job()
        self.make_key()
        git(self.work, "remote", "set-url", "origin",
            "ssh://git@ssh.github.com:443/platypeeps/system.git")

    def ssh_calls(self):
        path = pathlib.Path(self.ssh_log)
        return path.read_text().splitlines() if path.exists() else []

    def test_the_push_goes_out_with_the_deploy_key(self):
        """REGRESSION: without it the push used the operator's own key, which
        the required checks on main refuse. The stub refuses any call that
        does not offer exactly this key with the agent switched off."""
        self.make_key()
        self.write(f"{SCOPE}/inventory.txt", "second\n")

        result = self.run_tool("commit", "--scope", SCOPE, "--message", "regen",
                               env=dict(self.env, EXPECT_KEY=self.key))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("regen", self.remote_log())
        self.assertTrue([call for call in self.ssh_calls() if "git-receive-pack" in call],
                        self.ssh_calls())

    def test_a_key_path_the_shell_would_rewrite_reaches_ssh_intact(self):
        """REGRESSION: the path was interpolated inside double quotes, so a
        `"` or a `$(...)` in it changed the command git ran."""
        self.key = os.path.join(self.tmp, "ssh dir", "it's \"a\" $(key)")
        self.make_key()
        self.write(f"{SCOPE}/inventory.txt", "second\n")

        result = self.run_tool("commit", "--scope", SCOPE, "--message", "regen",
                               env=dict(self.env, EXPECT_KEY=self.key))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("regen", self.remote_log())

    def test_a_missing_key_is_named_and_the_push_is_still_tried(self):
        """PIN: before the ruleset lands the default identity still pushes,
        and after it the refusal needs a cause a person can act on."""
        self.write(f"{SCOPE}/inventory.txt", "second\n")

        result = self.run_tool("commit", "--scope", SCOPE, "--message", "regen", env=self.env)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MISSING deploy key", result.stderr)
        self.assertTrue(self.ssh_calls())
        self.assertFalse([call for call in self.ssh_calls() if "-i\t" in call])

    def test_ssh_command_defers_without_a_key(self):
        """PIN: a profile-capture job reads this, and exit 3 is how it knows."""
        result = self.run_tool("key", "ssh-command")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.make_key()
        result = self.run_tool("key", "ssh-command")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(),
                         f"ssh -i '{self.key}' -o IdentitiesOnly=yes -o IdentityAgent=none"
                         " -o BatchMode=yes")

    def seed_scope(self):
        self.write("local-ai-apps/profiles/personal.inv", "seed\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "seed inventory")
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=self.work, check=True,
                       capture_output=True, env=dict(os.environ, **self.env))

    def test_status_fails_a_loaded_job_without_its_key(self):
        """REGRESSION: the push would be refused every night from then on."""
        self.seed_scope()
        self.load_job()
        result = self.run_tool("status", env=self.env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("MISSING deploy key", result.stdout)
        self.assertIn("repo-sync-nightly", result.stdout)

    def test_status_fails_a_key_others_can_read(self):
        """REGRESSION: ssh refuses such a key, so the push fails all the same."""
        self.seed_scope()
        self.load_job()
        self.make_key(0o644)
        result = self.run_tool("status", env=self.env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("mode -rw-r--r--", result.stdout)

    def test_status_passes_a_loaded_job_with_its_key(self):
        """PIN: the healthy machine stays silent."""
        self.seed_scope()
        self.load_job()
        self.make_key()
        result = self.run_tool("status", env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_status_fails_an_installed_job_without_its_key_even_with_no_scopes(self):
        """REGRESSION: "no auto-committed scopes" answered 3 before the key was
        looked at, so a machine that would fail every push read as quiet."""
        self.load_job()
        result = self.run_tool("status", env=self.env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("MISSING deploy key", result.stdout)

    def test_pushing_jobs_and_label_prefix_are_configurable(self):
        """PIN: AUTOCOMMIT_PUSHING_JOBS replaces the job list, and
        SYSTEM_TOOLS_LABEL_PREFIX names the plist that marks a job installed."""
        pathlib.Path(self.agents, "example.test.cron.private-push.plist").write_text("<plist/>\n")
        env = dict(self.env, AUTOCOMMIT_PUSHING_JOBS="private-push")
        self.assertEqual(self.run_tool("status", env=env).returncode, 3)
        env["SYSTEM_TOOLS_LABEL_PREFIX"] = "example.test"
        result = self.run_tool("status", env=env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("private-push", result.stdout)

    def test_key_create_makes_a_private_key_and_a_usable_command(self):
        """PIN: the key is 0600, and the printed command names this
        repository and reads the public key from its file."""
        git(self.work, "remote", "set-url", "origin",
            "ssh://git@ssh.github.com:443/platypeeps/system.git")
        result = self.run_tool("key", "create")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(oct(os.stat(self.key).st_mode & 0o777), "0o600")
        self.assertIn("gh api repos/platypeeps/system/keys", result.stdout)
        self.assertIn(f"-F key=@'{self.key}.pub'", result.stdout)
        again = self.run_tool("key", "create")
        self.assertEqual(again.returncode, 1, again.stdout + again.stderr)

    def test_status_ignores_the_key_where_no_job_pushes(self):
        """PIN: a machine that loads no pushing job has no use for the key,
        and a finding there is one nobody can act on."""
        self.seed_scope()
        result = self.run_tool("status", env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_status_fails_when_the_org_disallows_deploy_keys(self):
        """REGRESSION: the enterprise owns this switch, and a flip refuses
        every deploy key at once. Status read the key on disk only, so it
        stayed green until a push was refused."""
        self.healthy_machine_on_github()
        result = self.run_tool("status", env=dict(self.env, GH_ANSWER="false"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("DISABLED deploy keys in org platypeeps", result.stdout)
        self.assertIn("repo-sync-nightly", result.stdout)

    def test_status_asks_the_org_that_origin_names(self):
        """REGRESSION: the question goes to the org the push goes to."""
        self.healthy_machine_on_github()
        result = self.run_tool("status", env=dict(self.env, GH_ANSWER="true"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.gh_calls(),
                         ["api\torgs/platypeeps\t--jq\t.deploy_keys_enabled_for_repositories\t"])

    def test_status_notes_an_unreadable_setting_and_passes(self):
        """REGRESSION: no `gh` login under launchd says nothing about the
        setting. It is named on stderr, and it is not a finding."""
        self.healthy_machine_on_github()
        result = self.run_tool("status", env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("could not read org platypeeps's deploy-key setting", result.stderr)

    def test_status_asks_github_nothing_where_no_job_pushes(self):
        """PIN: a machine with no pushing job makes no network call."""
        self.seed_scope()
        self.run_tool("status", env=dict(self.env, GH_ANSWER="false"))
        self.assertEqual(self.gh_calls(), [])


if __name__ == "__main__":
    unittest.main()
