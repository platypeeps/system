"""checkout_guard.py refuses agent writes in the system and pack checkouts (sd:6001).

The hook runs from a copy of itself inside a synthetic system checkout, with
SD_PACK_ROOT naming a synthetic pack checkout. Each case feeds it one
PreToolUse payload on stdin and reads its decision.
"""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
GUARD = HERE.parent / "checkout_guard.py"


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)


class CheckoutGuard(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = pathlib.Path(tmp.name).resolve()
        self.system = base / "repos/system"
        self.pack = base / "repos/pack"
        self.worktree = base / "worktrees/system-x"
        for repo in (self.system, self.pack):
            repo.mkdir(parents=True)
            git("init", "-q", str(repo))
        (self.system / ".gitignore").write_text("docs/dashboard/\n")
        (self.system / "local-machine-setup").mkdir()
        self.guard = self.system / "local-machine-setup/checkout_guard.py"
        shutil.copy(GUARD, self.guard)
        self.worktree.mkdir(parents=True)
        self.env = {"HOME": str(base / "home"), "PATH": "/usr/bin:/bin", "SD_PACK_ROOT": str(self.pack)}

    def decide(self, tool, cwd=None, guard=None, **tool_input):
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd or self.worktree)}
        result = subprocess.run(["python3", str(guard or self.guard)], input=json.dumps(payload),
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return "allow"
        output = json.loads(result.stdout)["hookSpecificOutput"]
        return output["permissionDecision"] + ": " + output["permissionDecisionReason"]

    def bash(self, command, cwd=None):
        return self.decide("Bash", cwd=cwd, command=command)

    def assertDenied(self, decision, root):
        self.assertTrue(decision.startswith(f"deny: {root} is a lane checkout"), decision)
        self.assertIn("worktree add ~/worktrees/", decision)

    def test_file_tools_in_either_checkout_are_denied(self):
        self.assertDenied(self.decide("Write", file_path=str(self.system / "docs/work/x/design.md"),
                                      content="x"), self.system)
        self.assertDenied(self.decide("Edit", file_path=str(self.pack / "README.md")), self.pack)
        self.assertDenied(self.decide("MultiEdit", file_path=str(self.pack / "a.py")), self.pack)
        self.assertDenied(self.decide("NotebookEdit", notebook_path=str(self.system / "n.ipynb")),
                          self.system)

    def test_a_relative_path_resolves_against_the_session_folder(self):
        self.assertDenied(self.decide("Write", cwd=self.system, file_path="docs/work/x/design.md"),
                          self.system)

    def test_a_symlink_into_a_checkout_is_denied(self):
        link = self.worktree / "link"
        link.symlink_to(self.system)
        self.assertDenied(self.decide("Write", file_path=str(link / "x.md")), self.system)

    def test_writes_outside_and_ignored_paths_pass(self):
        self.assertEqual(self.decide("Write", file_path=str(self.worktree / "x.md")), "allow")
        self.assertEqual(self.decide("Write", file_path=str(self.system / "docs/dashboard/r.html")),
                         "allow")
        self.assertEqual(self.decide("Read", file_path=str(self.system / "README.md")), "allow")

    def test_git_verbs_that_move_the_work_tree_are_denied(self):
        for command in (f"git -C {self.system} checkout -b topic",
                        f"rtk git -C {self.pack} commit -m x",
                        f"cd {self.system} && git stash",
                        f"cd {self.worktree}; git -C ../../repos/system merge origin/main"):
            with self.subTest(command=command):
                decision = self.bash(command)
                self.assertTrue(decision.startswith("deny: "), decision)
        self.assertDenied(self.bash("git pull", cwd=self.system), self.system)

    def test_git_reads_fetch_and_worktrees_pass(self):
        for command in (f"git -C {self.system} fetch -q origin",
                        f"git -C {self.system} worktree add {self.worktree}-2 -b b origin/main",
                        f"git -C {self.system} status --short",
                        f"git -C {self.system} branch -D old",
                        f"git -C {self.worktree} commit -m x",
                        f"sd-ship -C {self.system} prepare"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), "allow")
        self.assertEqual(self.bash("git log -1 2>&1 | head", cwd=self.system), "allow")

    def test_shell_writes_into_a_checkout_are_denied(self):
        for command in ("mkdir -p docs/work/2026-10-24-x",
                        "touch notes.md",
                        "echo hi > notes.md",
                        "printf x | tee -a log.txt",
                        f"cp {self.worktree}/a.md .",
                        "rm -f README.md"):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=self.system), self.system)
        self.assertDenied(self.bash(f"cd {self.pack} && mkdir x"), self.pack)

    def test_shell_reads_and_writes_elsewhere_pass(self):
        for command in (f"cp {self.system}/README.md {self.worktree}/",
                        f"cat {self.system}/README.md > {self.worktree}/copy.md",
                        "ls docs 2>/dev/null",
                        "echo x > docs/dashboard/r.html",
                        f"cd {self.worktree} && mkdir y"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=self.system), "allow")

    def test_a_quoted_operator_is_text(self):
        """sd:6083: shlex drops the quotes, so `">"` read as a redirect."""
        for command in ('printf "%s\\n" ">" README.md',
                        "echo '|' rm README.md",
                        "echo ';' touch x",
                        "echo \\> README.md"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=self.system), "allow")
        for command in ('echo "<" > README.md',
                        "echo fix#2 && rm README.md",
                        "echo $(echo a)#b; touch README.md",
                        "ls # a comment\nrm README.md"):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=self.system), self.system)
        self.assertEqual(self.bash("# rm README.md", cwd=self.system), "allow")

    def test_the_folder_returns_when_a_subshell_ends(self):
        """sd:6083: a `cd` inside `( )` or `$( )` does not leak out."""
        for command in (f"(cd {self.system} && cat README.md); mkdir output",
                        f"echo $(cd {self.system}; pwd) > out.txt",
                        f"(cd {self.system}; (cd /tmp); ls) && touch y"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), "allow")
        self.assertEqual(self.bash("printf x | tee >(cat) <(ls)", cwd=self.system), "allow")
        self.assertDenied(self.bash("(cd /tmp) && mkdir x", cwd=self.system), self.system)
        self.assertDenied(self.bash(f"(cd {self.system} && touch x)"), self.system)

    def test_a_shell_variable_names_a_checkout(self):
        """sd:6083: `$DEST` set in the command resolves like the shell does."""
        for command in (f'DEST={self.system}; mkdir -p "$DEST/docs/work"',
                        f'export DEST={self.pack} && echo x > "$DEST/n.md"',
                        f"declare -x DEST={self.pack}; touch $DEST/x",
                        f'D={self.system}; git -C "$D" commit -m x',
                        f"B={self.system}; C=${{B}}/docs; touch $C/x"):
            with self.subTest(command=command):
                self.assertTrue(self.bash(command).startswith("deny: "), command)
        for command in (f'DEST={self.system} true; mkdir "$DEST/x"',
                        f'(D={self.system}); mkdir "$D/x"',
                        f'DEST={self.worktree}; touch "$DEST/x"'):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=self.worktree), "allow")

    def test_a_change_that_may_not_happen_keeps_the_old_state(self):
        """sd:6083: `&&`, `||`, `|`, `&` and `cd` may leave the folder and variables."""
        self.env["DEST"] = str(self.system)
        for command in ('false && DEST=/tmp; touch "$DEST/x"',
                        "true || cd /tmp; touch x",
                        "false && cd /tmp; mkdir x",
                        f"cd {self.worktree}/new; touch x",
                        "cd /tmp | true; touch x",
                        'DEST=/tmp | true; touch "$DEST/x"',
                        f"true | cd {self.worktree}; touch x",
                        "cd /tmp & touch x",
                        "true && cd /tmp & touch x",
                        "(cd /tmp) & touch x",
                        "(exit 1) || touch x"):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=self.system), self.system)
        self.assertDenied(self.bash(f"! cd {self.system} || touch x"), self.system)
        for command in (f"cd {self.worktree} && touch x",
                        f"cd {self.worktree} || exit 1; touch x",
                        f"cd {self.worktree}/new && touch x",
                        f"cd {self.worktree}/new || exit 1; touch x",
                        f"cd {self.worktree} && git log | head; touch x",
                        f"cd {self.worktree} &&\ngit log 2>&1 | tail -5 && touch x",
                        f"(cd {self.worktree}) || touch x",
                        f"DEST={self.worktree} && touch $DEST/x"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=self.system), "allow")

    def test_a_compound_command_may_not_run_its_body(self):
        """sd:6083: `if`, `while`, `for`, `case` and `{ }` hid the command after them."""
        self.env["DEST"] = str(self.system)
        for command in ("if true; then rm README.md; fi",
                        "if false; then DEST=/tmp; fi; touch \"$DEST/x\"",
                        "if false; then :; elif true; then :; else cd /tmp; fi; touch x",
                        "while false; do cd /tmp; done; touch x",
                        "until true; do cd /tmp; done > log.txt",
                        "{ rm README.md; }",
                        "{ cd /tmp; } | cat; touch x",
                        "case a in a) rm README.md;; esac",
                        "case a in\n  b|c) cd /tmp\n  ;;\nesac\ntouch x"):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=self.system), self.system)
        self.assertDenied(self.bash(f"for d in /tmp {self.system}; do touch $d/x; done"),
                          self.system)
        for command in (f"if cd {self.worktree}; then touch x; fi",
                        f"for d in /tmp {self.worktree}; do touch $d/x; done",
                        f"{{ cd {self.worktree}; }}; touch x",
                        "echo if then fi done"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=self.system), "allow")

    def test_a_copy_in_a_linked_worktree_protects_nothing_there(self):
        """A builder's worktree carries this file too; its `.git` is a file."""
        linked = self.worktree / "local-machine-setup"
        linked.mkdir()
        (self.worktree / ".git").write_text("gitdir: elsewhere\n")
        shutil.copy(GUARD, linked / GUARD.name)

        decision = self.decide("Write", guard=linked / GUARD.name, file_path=str(self.worktree / "x.md"))

        self.assertEqual(decision, "allow")
        self.assertDenied(self.decide("Write", guard=linked / GUARD.name,
                                      file_path=str(self.pack / "x.md")), self.pack)

    def test_unreadable_input_and_unsplittable_commands_fail_open(self):
        result = subprocess.run(["python3", str(self.guard)], input="{not json", env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertIn("checkout_guard: unreadable input, allowing", result.stderr)
        self.assertEqual(self.bash("echo 'unclosed > x", cwd=self.system), "allow")

    def test_a_command_past_the_budget_is_denied_in_time(self):
        """Each `cd` to a missing folder doubles the worlds; BUDGET bounds the work.

        Past it the writes are unknown, which is not a pass.
        """
        command = "".join(f"cd d{i}; " for i in range(16)) + "ls"
        decision = self.bash(command, cwd=self.worktree)
        self.assertTrue(decision.startswith("deny: checkout_guard cannot read this command"),
                        decision)
        self.assertEqual(self.bash(command.replace("; ", " && "), cwd=self.worktree), "allow")


if __name__ == "__main__":
    unittest.main()
