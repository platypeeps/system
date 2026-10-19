"""leak-guard.sh against throwaway git repositories.

Every test runs the real entrypoint as a subprocess with HOME, the git global
config and SYSTEM_TOOLS_CONFIG in a temporary directory. The patterns are
synthetic; the operator's pattern file is never read.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "leak-guard.sh"
LIB = HERE.parent.parent / "lib"

SECRET = "zebra-canary-4711"
PATTERNS = "# synthetic test patterns\n\nzebra-canary-[0-9]+\n"


class LeakGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.config = self.tmp / "config"
        self.config.mkdir()
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = Test\n\temail = test@example.test\n"
            "[init]\n\tdefaultBranch = main\n"
        )
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp),
            "SYSTEM_TOOLS_CONFIG": str(self.config),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
        # A copy outside git: run from this checkout, install would name the
        # main checkout's script when this one is a linked worktree (sd:3390).
        self.script = self.copy_guard(self.tmp / "copy")
        self.remote = self.tmp / "remote.git"
        self.git(self.tmp, "init", "-q", "--bare", str(self.remote))
        self.repo = self.tmp / "repo"
        self.git(self.tmp, "clone", "-q", str(self.remote), str(self.repo))
        self.commit("README", "hello\n", "initial")
        self.git(self.repo, "push", "-q", "origin", "HEAD:main")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # helpers

    def git(self, cwd: pathlib.Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, env=self.env, check=True,
            capture_output=True, text=True,
        ).stdout

    @staticmethod
    def copy_guard(root: pathlib.Path) -> pathlib.Path:
        shutil.copytree(HERE.parent, root / "local-leak-guard",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, root / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        return root / "local-leak-guard" / "leak-guard.sh"

    def commit(self, name: str, text: str, message: str) -> str:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", message)
        return self.git(self.repo, "rev-parse", "HEAD").strip()

    def run_guard(self, *args: str, cwd: pathlib.Path | None = None,
                  stdin: str = "", script: pathlib.Path | None = None,
                  ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(script or self.script), *args], cwd=cwd or self.repo, env=self.env,
            input=stdin, capture_output=True, text=True, timeout=60,
        )

    def push(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "push", "origin", "HEAD:main"], cwd=self.repo,
            env=self.env, capture_output=True, text=True, timeout=60,
        )

    def write_patterns(self, text: str = PATTERNS) -> None:
        (self.config / "privacy-patterns").write_text(text)

    def assertRefused(self, result, sha: str) -> None:
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(sha, result.stderr)
        # The report names where, never what.
        self.assertNotIn(SECRET, result.stderr)
        self.assertNotIn(SECRET, result.stdout)

    # interface

    def test_help_exits_zero(self) -> None:
        for arg in ("help", "-h", "--help"):
            result = self.run_guard(arg)
            self.assertEqual(result.returncode, 0)
            self.assertIn("leak-guard.sh check", result.stdout)

    def test_no_argument_prints_usage_to_stderr_and_exits_one(self) -> None:
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Usage:", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_unknown_verb_exits_one(self) -> None:
        self.assertEqual(self.run_guard("nope").returncode, 1)

    # pattern file

    def test_missing_pattern_file_warns_and_passes(self) -> None:
        self.commit("a.txt", f"{SECRET}\n", "add")
        result = self.run_guard("check")
        self.assertEqual(result.returncode, 0)
        self.assertIn("no pattern file", result.stderr)

    def test_comment_and_blank_lines_match_nothing(self) -> None:
        self.write_patterns("# only a comment\n\n   \n")
        self.commit("a.txt", "anything at all\n", "add")
        result = self.run_guard("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("holds no pattern", result.stderr)

    def test_leak_guard_patterns_names_another_file(self) -> None:
        other = self.tmp / "other-patterns"
        other.write_text("quokka\n")
        self.env["LEAK_GUARD_PATTERNS"] = str(other)
        sha = self.commit("a.txt", "a quokka\n", "add")
        self.assertRefused(self.run_guard("check"), sha)

    # check

    def test_clean_commit_passes(self) -> None:
        self.write_patterns()
        self.commit("a.txt", "nothing private\n", "add")
        result = self.run_guard("check")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_added_line_is_refused(self) -> None:
        self.write_patterns()
        sha = self.commit("docs/a.txt", f"host is {SECRET}\n", "add")
        result = self.run_guard("check")
        self.assertRefused(result, sha)
        self.assertIn("added line docs/a.txt", result.stderr)

    def test_commit_message_is_refused(self) -> None:
        self.write_patterns()
        sha = self.commit("a.txt", "fine\n", f"deploy to {SECRET}")
        result = self.run_guard("check")
        self.assertRefused(result, sha)
        self.assertIn("message", result.stderr)

    def test_file_path_is_refused_and_withheld(self) -> None:
        self.write_patterns()
        sha = self.commit(f"{SECRET}.conf", "fine\n", "add")
        result = self.run_guard("check")
        self.assertRefused(result, sha)
        self.assertIn("path withheld", result.stderr)

    def test_removed_line_passes(self) -> None:
        self.commit("a.txt", f"{SECRET}\n", "add")
        self.git(self.repo, "push", "-q", "origin", "HEAD:main")
        self.write_patterns()
        self.commit("a.txt", "clean\n", "scrub")
        result = self.run_guard("check")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_line_added_then_removed_is_still_refused(self) -> None:
        self.write_patterns()
        sha = self.commit("a.txt", f"{SECRET}\n", "add")
        self.commit("a.txt", "clean\n", "scrub")
        self.assertRefused(self.run_guard("check"), sha)

    def test_pushed_commits_are_not_checked_again(self) -> None:
        self.commit("a.txt", f"{SECRET}\n", "add")
        self.git(self.repo, "push", "-q", "origin", "HEAD:main")
        self.write_patterns()
        self.commit("b.txt", "clean\n", "more")
        result = self.run_guard("check")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_range_limits_the_check(self) -> None:
        self.write_patterns()
        self.commit("a.txt", f"{SECRET}\n", "add")
        before = self.git(self.repo, "rev-parse", "HEAD").strip()
        self.commit("b.txt", "clean\n", "more")
        result = self.run_guard("check", "--range", f"{before}..HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_guard("check", "--range", f"{before}~1..HEAD")
        self.assertRefused(result, before)

    def test_range_needs_a_value(self) -> None:
        self.assertEqual(self.run_guard("check", "--range").returncode, 2)

    def test_check_outside_a_repository_exits_two(self) -> None:
        result = self.run_guard("check", cwd=self.home)
        self.assertEqual(result.returncode, 2)

    # hook

    def test_hook_skips_a_deleted_ref(self) -> None:
        self.write_patterns()
        zero = "0" * 40
        line = f"(delete) {zero} refs/heads/gone {zero}\n"
        result = self.run_guard("hook", "origin", "url", stdin=line)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hook_checks_only_new_commits_of_an_existing_ref(self) -> None:
        self.write_patterns()
        base = self.git(self.repo, "rev-parse", "HEAD").strip()
        sha = self.commit("a.txt", f"{SECRET}\n", "add")
        line = f"refs/heads/main {sha} refs/heads/main {base}\n"
        self.assertRefused(self.run_guard("hook", "origin", "url", stdin=line), sha)
        clean = self.commit("b.txt", "clean\n", "more")
        line = f"refs/heads/main {clean} refs/heads/main {sha}\n"
        result = self.run_guard("hook", "origin", "url", stdin=line)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hook_skips_commits_another_ref_of_the_remote_holds(self) -> None:
        # A branch that merges main pushes main's commits to the branch ref
        # for the first time. They are already on the remote, so already
        # published; refusing them blocks every catch-up merge.
        base = self.git(self.repo, "rev-parse", "HEAD").strip()
        self.git(self.repo, "checkout", "-q", "-b", "topic")
        topic = self.commit("t.txt", "topic\n", "topic")
        self.git(self.repo, "push", "-q", "origin", "topic")
        self.git(self.repo, "checkout", "-q", base)
        self.commit("a.txt", "fine\n", f"deploy to {SECRET}")
        self.git(self.repo, "push", "-q", "origin", "HEAD:main")
        self.git(self.repo, "fetch", "-q", "origin")
        self.git(self.repo, "checkout", "-q", "topic")
        self.git(self.repo, "merge", "-q", "--no-edit", "origin/main")
        merge = self.git(self.repo, "rev-parse", "HEAD").strip()
        self.write_patterns()
        line = f"refs/heads/topic {merge} refs/heads/topic {topic}\n"
        result = self.run_guard("hook", "origin", str(self.remote), stdin=line)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Unreadable destination: nothing is skipped beyond the ref's tip.
        result = self.run_guard("hook", "origin", str(self.tmp / "gone.git"),
                                stdin=line)
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_hook_does_not_trust_a_remote_tracking_ref(self) -> None:
        # A tracking ref can be stale or come from another fetch URL; only
        # what the push destination itself holds counts as published.
        base = self.git(self.repo, "rev-parse", "HEAD").strip()
        sha = self.commit("a.txt", "fine\n", f"deploy to {SECRET}")
        self.git(self.repo, "update-ref", "refs/remotes/origin/stale", sha)
        self.write_patterns()
        line = f"refs/heads/main {sha} refs/heads/main {base}\n"
        result = self.run_guard("hook", "origin", str(self.remote), stdin=line)
        self.assertRefused(result, sha)
        line = f"refs/heads/new {sha} refs/heads/new {'0' * 40}\n"
        result = self.run_guard("hook", "origin", str(self.remote), stdin=line)
        self.assertRefused(result, sha)

    # install and a real push

    def test_install_refuses_a_push_that_leaks(self) -> None:
        self.write_patterns()
        result = self.run_guard("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        hook = self.repo / ".git" / "hooks" / "pre-push"
        self.assertTrue(os.access(hook, os.X_OK))
        sha = self.commit("a.txt", f"{SECRET}\n", "add")
        pushed = self.push()
        self.assertNotEqual(pushed.returncode, 0)
        self.assertIn(sha, pushed.stderr)
        self.assertNotIn(SECRET, pushed.stderr)
        remote_head = self.git(self.remote, "rev-parse", "main").strip()
        self.assertNotEqual(remote_head, sha)

    def test_install_lets_a_clean_push_through(self) -> None:
        self.write_patterns()
        self.run_guard("install")
        sha = self.commit("a.txt", "clean\n", "add")
        pushed = self.push()
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), sha)

    def test_install_checks_a_new_branch_against_the_remote(self) -> None:
        self.write_patterns()
        self.run_guard("install")
        self.git(self.repo, "switch", "-q", "-c", "topic")
        sha = self.commit("a.txt", f"{SECRET}\n", "add")
        pushed = subprocess.run(
            ["git", "push", "origin", "topic"], cwd=self.repo, env=self.env,
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(pushed.returncode, 0)
        self.assertIn(sha, pushed.stderr)

    def test_install_honours_core_hooks_path(self) -> None:
        self.git(self.repo, "config", "core.hooksPath", "githooks")
        result = self.run_guard("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / "githooks" / "pre-push").exists())
        self.assertFalse((self.repo / ".git" / "hooks" / "pre-push").exists())

    def test_install_takes_a_repository_argument(self) -> None:
        result = self.run_guard("install", str(self.repo), cwd=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / ".git" / "hooks" / "pre-push").exists())

    def test_install_refuses_to_replace_a_foreign_hook(self) -> None:
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\nexit 0\n")
        result = self.run_guard("install")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(hook.read_text(), "#!/bin/sh\nexit 0\n")

    def test_install_is_idempotent_and_remove_deletes_it(self) -> None:
        self.assertEqual(self.run_guard("install").returncode, 0)
        self.assertEqual(self.run_guard("install").returncode, 0)
        hook = self.repo / ".git" / "hooks" / "pre-push"
        self.assertTrue(hook.exists())
        self.assertEqual(self.run_guard("remove").returncode, 0)
        self.assertFalse(hook.exists())
        self.assertEqual(self.run_guard("remove").returncode, 0)

    def linked_worktree(self) -> tuple[pathlib.Path, pathlib.Path]:
        """A main checkout holding the guard, and a linked worktree of it."""
        main = self.tmp / "main"
        self.copy_guard(main)
        self.git(self.tmp, "init", "-q", str(main))
        self.git(main, "add", "-A")
        self.git(main, "commit", "-q", "-m", "guard")
        linked = self.tmp / "linked"
        self.git(main, "worktree", "add", "-q", "--detach", str(linked))
        return main, linked

    def test_install_from_a_linked_worktree_names_the_main_checkout(self) -> None:
        # sd:3390: a hook that names a linked worktree fails every push once
        # the worktree is removed.
        main, linked = self.linked_worktree()
        result = self.run_guard("install", str(self.repo),
                                script=linked / "local-leak-guard" / "leak-guard.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (self.repo / ".git" / "hooks" / "pre-push").read_text()
        self.assertIn(f"exec sh '{main.resolve()}/local-leak-guard/leak-guard.sh' hook", text)
        self.assertNotIn(str(linked.name), text)

    def test_install_from_the_main_checkout_names_it(self) -> None:
        main, _ = self.linked_worktree()
        script = main / "local-leak-guard" / "leak-guard.sh"
        self.assertEqual(self.run_guard("install", script=script).returncode, 0)
        text = (self.repo / ".git" / "hooks" / "pre-push").read_text()
        self.assertIn(f"exec sh '{main.resolve()}/local-leak-guard/leak-guard.sh' hook", text)

    def test_install_refuses_when_the_main_checkout_lacks_the_guard(self) -> None:
        main, linked = self.linked_worktree()
        (main / "local-leak-guard" / "leak-guard.sh").unlink()
        result = self.run_guard("install", script=linked / "local-leak-guard" / "leak-guard.sh")
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"{main.resolve()}/local-leak-guard/leak-guard.sh", result.stderr)
        self.assertFalse((self.repo / ".git" / "hooks" / "pre-push").exists())

    def test_remove_leaves_a_foreign_hook(self) -> None:
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\nexit 0\n")
        self.assertEqual(self.run_guard("remove").returncode, 2)
        self.assertTrue(hook.exists())

    def test_hook_runs_through_a_symlink(self) -> None:
        link_dir = self.tmp / "bin"
        link_dir.mkdir()
        (link_dir / "leak-guard").symlink_to(SCRIPT)
        result = subprocess.run(
            ["sh", str(link_dir / "leak-guard"), "help"], env=self.env,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()


class ScanTest(unittest.TestCase):
    """`scan` (sd:5103): text on stdin, the number of each line a pattern matches on stdout, never the line."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.config = self.tmp / "config"
        self.config.mkdir()
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.tmp), "TMPDIR": str(self.tmp),
                    "SYSTEM_TOOLS_CONFIG": str(self.config)}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def scan(self, text: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", str(SCRIPT), "scan", *args], input=text, env=self.env,
                              capture_output=True, text=True, timeout=30, check=False)

    def test_a_hit_prints_line_numbers_only_and_exits_1(self) -> None:
        (self.config / "privacy-patterns").write_text(PATTERNS)
        done = self.scan(f"{SECRET} at the start\nclean\nat the end {SECRET}\n\x00binary {SECRET}")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(done.stdout.split(), ["1", "3", "4"])
        self.assertNotIn(SECRET, done.stdout + done.stderr)

    def test_clean_text_exits_0_and_prints_nothing(self) -> None:
        (self.config / "privacy-patterns").write_text(PATTERNS)
        done = self.scan("nothing private here\n")
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_no_pattern_file_or_an_empty_one_exits_3_not_0(self) -> None:
        """Failures: no patterns is unknown, not clean, so a caller can refuse."""
        self.assertEqual(self.scan(f"{SECRET}\n").returncode, 3)
        (self.config / "privacy-patterns").write_text("# only a comment\n\n")
        self.assertEqual(self.scan(f"{SECRET}\n").returncode, 3)

    def test_a_pattern_grep_cannot_compile_exits_2_without_echoing_it(self) -> None:
        (self.config / "privacy-patterns").write_text("zebra-[unclosed\n")
        done = self.scan("zebra-x\n")
        self.assertEqual(done.returncode, 2)
        self.assertNotIn("unclosed", done.stdout + done.stderr)

    def test_an_argument_is_a_usage_error(self) -> None:
        self.assertEqual(self.scan("x\n", "extra").returncode, 2)
