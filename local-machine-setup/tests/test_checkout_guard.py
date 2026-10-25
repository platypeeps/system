"""checkout_guard.py refuses agent writes in the system and pack checkouts (sd:6001).

The hook runs from a copy of itself inside a synthetic system checkout, with
SD_PACK_ROOT naming a synthetic pack checkout. Each case feeds it one
PreToolUse payload on stdin and reads its decision.

Every Bash case also reads the command in process with this guard and with
the 13554b17 guard (tests/fixtures), the first head of sd:6083: a write
the old guard refused, this one must refuse too (sd:6083 round 1).
"""

import importlib.util
import json
import os
import pathlib
import random
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
GUARD = HERE.parent / "checkout_guard.py"


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BRANCH = load(GUARD, "checkout_guard_branch")
BASE = load(HERE / "fixtures/checkout_guard_13554b17.py", "checkout_guard_13554b17")


def denies(module, command, cwd, env, protected):
    """True when MODULE refuses COMMAND; a refusal past the budget counts."""
    with mock.patch.dict(os.environ, env, clear=True):
        try:
            return module.bash_root(command, str(cwd), [str(root) for root in protected]) is not None
        except ValueError:
            return False
        except Exception as error:  # the budget refusal
            if type(error).__name__ == "TooMany":
                return True
            raise


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

    def bash(self, command, cwd=None, fixed=False):
        """FIXED marks a command 13554b17 refused by mistake: the shell writes nothing there."""
        decision = self.decide("Bash", cwd=cwd, command=command)
        self.assertEqual(self.regressed(command, cwd or self.worktree), fixed,
                         f"13554b17 refused, this guard allows: {command!r}")
        return decision

    def regressed(self, command, cwd):
        protected = (self.system, self.pack)
        return (denies(BASE, command, cwd, self.env, protected)
                and not denies(BRANCH, command, cwd, self.env, protected))

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
                        f"cd {self.worktree} && git log | head && touch x",
                        f"cd {self.worktree} &&\ngit log 2>&1 | tail -5 && touch x",
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
                        "echo if then fi done"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=self.system), "allow")
        self.assertEqual(self.bash(f"{{ cd {self.worktree}; }} && touch x", cwd=self.system,
                                   fixed=True), "allow")

    def test_a_skipped_word_or_a_branch_keeps_what_the_base_read(self):
        """sd:6083 round 1: case words, patterns, fallthrough, loop words, break."""
        lane = self.system
        for command in (f"case x in x) cd {lane} ;& y) touch x;; esac",
                        f"case x in x) cd {lane} ;;& y) touch x;; esac",
                        f"case $(touch {lane}/x) in x) :;; esac",
                        f"case x in $(touch {lane}/x)) :;; esac",
                        f"case x in\n(y) :;;\n$(touch {lane}/x)) :;;\nesac",
                        f"for d in $(touch {lane}/x); do :; done",
                        f"for d in a $(touch {lane}/x) b; do :; done",
                        f"while :; do cd {lane}; break; cd /tmp; done; touch x",
                        f"for d in a; do cd {lane}; continue; cd /tmp; done; touch x",
                        f"[[ -n $(touch {lane}/x) ]]",
                        f"(( $(touch {lane}/x) ))",
                        f"D=$(touch {lane}/x)",
                        f"echo > $(touch {lane}/x)"):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command), lane)
        self.assertDenied(self.bash("for d in repos system; do cd $d; done; touch x",
                                    cwd=self.system.parent.parent), lane)
        for command in ("case x in x) :;; y) touch x;; esac",
                        f"while :; do cd {self.worktree}; break; done; touch x"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=self.worktree), "allow")

    def test_a_word_keeps_its_quotes_and_a_wrapper_its_child(self):
        """sd:6083 round 2: quoted `$` and `~` stay text; a wrapper's child does not move
        the shell, and `command`, `builtin` and `time` run in it."""
        for command in ("D='$HOME'; mkdir -p \"$D\"",
                        "export D='$HOME'; touch \"$D\"",
                        "readonly D='$HOME'; touch \"$D\"",
                        "declare D='~'; mkdir \"$D\"",
                        "D=\\$HOME; mkdir \"$D\"",
                        "D=\"\\$HOME\"; mkdir \"$D\"",
                        "mkdir '$HOME'",
                        "mkdir $'$HOME'",
                        "mkdir '~' \"~\" \\~",
                        "env exit 0; touch README.md",
                        "nohup exit 0; touch README.md",
                        "env cd /tmp; touch x",
                        "nohup cd /tmp; touch x",
                        "sudo -u nobody cd /tmp; touch x",
                        "timeout 5 rm README.md",
                        "nice -n 5 rm README.md",
                        "env -i A=1 rm README.md",
                        "echo README.md | xargs rm -f README.md",
                        "eval 'rm README.md'",
                        "D=/tmp; unset D; cd \"$D\"; touch x"):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=self.system), self.system)
        self.assertDenied(self.bash(f"eval \"cd {self.system}\"; touch x"), self.system)
        for command in ('mkdir "$HOME/x"',
                        'D="$HOME" && mkdir -p "$D/x"',
                        f"command cd {self.worktree} && touch x",
                        f"time cd {self.worktree} && touch x"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=self.system), "allow")
        for command in (f"builtin cd {self.worktree} && touch x", "command exit 0; touch x"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=self.system, fixed=True), "allow")

    def test_every_state_change_may_fail(self):
        """sd:6083 round 3: an assignment, cd or eval may fail, so the world it started
        in stays possible: `readonly` makes `export` fail, `cd` may meet a mode."""
        lane, work = self.system, self.worktree
        self.env["D"] = str(lane)
        for command in (f"readonly D={lane}; export D=/tmp; touch \"$D/x\"",
                        f"readonly D={lane}; unset D; touch \"$D/x\"",
                        'D=/tmp; touch "$D/x"',
                        'export D=/tmp; touch "$D/x"',
                        'declare D=/tmp; touch "$D/x"',
                        'local D=/tmp; touch "$D/x"',
                        'typeset D=/tmp; touch "$D/x"',
                        'readonly D=/tmp; touch "$D/x"',
                        'unset D; mkdir -p "$D/x"',
                        'eval "D=/tmp"; touch "$D/x"',
                        f"cd {lane}; cd /tmp; touch x",
                        f"cd {lane}; pushd /tmp; touch x",
                        f"pushd {lane}; popd; touch x",
                        f'set -- {lane}; touch "$1/x"',
                        f'set -- /tmp {lane}; shift; touch "$1/x"'):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=work), lane)
        for command in ("D=/tmp && touch \"$D/x\"",
                        f"cd {work} && touch x",
                        f'set -- /tmp {work} && shift && touch "$1/x"'):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=work), "allow")
        self.assertDenied(self.bash(f"cd {work}; touch x", cwd=lane), lane)

    def test_every_write_the_base_refused_is_refused(self):
        """Property over a shell grammar: where this guard allows, a real bash run in a
        scratch tree writes nothing in the lane. That covers each write 13554b17 refused
        and this guard allows (sd:6083 rounds 1 and 2)."""
        lane, work = self.system, self.worktree
        (lane / "README.md").write_text("x\n")
        rng = random.Random(6083)
        simple = [f"cd {lane}", f"cd {work}", "cd d1", "mkdir -p d1", "touch y", "rm -f README.md",
                  f"D={lane}", f"D={work}", 'touch "$D/y"', 'touch "$d/y"', "true", "false", ":",
                  "echo $(touch z)", "break", "continue", "exit 0", "D='$HOME'", "D=\\$HOME",
                  'D="$HOME"', 'mkdir -p "$D"', "mkdir -p '~'", "env exit 0", "nohup exit 0",
                  "command exit 0", "env cd d1", "command cd d1", "nohup cd d1", "eval 'cd d1'",
                  f"eval 'cd {lane}'", "unset D", 'cd "$D"', "timeout 5 touch y",
                  "env -i A=1 touch y", "builtin cd d1", "time cd d1", f"readonly D={lane}",
                  f"export D={work}", "unset D", "cd /nonexistent-6083", f"pushd {work}",
                  "popd", f"set -- {lane}", "shift", 'touch "$1/y"']

        def gen(depth):
            kind = min(rng.randrange(14 if depth < 3 else 1), 11)  # case weighs three
            a = lambda: gen(depth + 1)
            return [lambda: rng.choice(simple),
                    lambda: f"{a()}; {a()}", lambda: f"{a()} && {a()}", lambda: f"{a()} || {a()}",
                    lambda: f"{a()} | {a()}", lambda: f"{a()} & {a()}", lambda: f"! {a()}",
                    lambda: rng.choice(["( {} )", "{{ {}; }}", "echo $({})"]).format(a()),
                    lambda: f"if {a()}; then {a()}; elif {a()}; then {a()}; else {a()}; fi",
                    lambda: f"for d in {lane} {work} d1; do {a()}; done",
                    lambda: f"while {a()}; do {a()}; break; done",
                    lambda: (f"case {rng.choice(['a', '$(' + a() + ')'])} in a) {a()} "
                             f"{rng.choice([';;', ';&', ';;&'])} b|a) {a()};; esac")][kind]()

        def snapshot():
            return {str(path) for path in lane.rglob("*") if ".git" not in path.parts}

        failures, before = [], snapshot()
        for module in (BASE, BRANCH):  # git ignores nothing these paths reach; skip its calls
            self.enterContext(mock.patch.object(module, "ignored", lambda root, full: False))
        for _ in range(1200):
            command, cwd = gen(0), rng.choice([lane, work])
            if denies(BRANCH, command, cwd, self.env, (lane, self.pack)):
                continue
            try:
                subprocess.run(["bash", "-c", f"ulimit -t 1; {command}\nwait"], cwd=cwd, env=self.env,
                               capture_output=True, timeout=5)
            except subprocess.TimeoutExpired:
                pass
            after = snapshot()
            if after != before:
                failures.append((command, str(cwd), sorted(after ^ before)[:2]))
            for path in sorted(after - before, reverse=True):
                (shutil.rmtree if os.path.isdir(path) else os.unlink)(path)
            (lane / "README.md").write_text("x\n")
            for path in work.iterdir():
                (shutil.rmtree if path.is_dir() else os.unlink)(path)
        self.assertEqual(failures[:3], [], f"{len(failures)} allowed writes in the lane")

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
