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
import time
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
            if type(error).__name__ == "Unreadable":
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

    def test_an_assignment_that_cannot_fail_sticks(self):
        """sd:6270: a name the shell does not hold is readonly only when the
        command makes it so, so `LOG=..` sticks and `"$LOG"` is its value,
        not a file in the folder."""
        lane, log = self.system, "/tmp/sd-6270.log"
        for command in (f'LOG={log}; echo hi > "$LOG"',
                        f"LOG={log}; echo hi > $LOG",
                        f'LOG={log}; touch "$LOG"',
                        f'export LOG={log}; echo hi >> "$LOG"',
                        f'declare LOG={log}; echo hi > "$LOG"',
                        f'readonly LOG={log}; echo hi > "$LOG"',
                        f'D=/tmp; LOG=$D/x.txt; echo hi > "$LOG"'):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=lane), "allow")
        self.env["LOG"] = str(lane / "held")  # the shell may hold it readonly
        for command in (f'LOG={log}; echo hi > "$LOG"',
                        f'readonly D={lane}; D={log}; touch "$D"',
                        f'local L={log}; touch "$L"',  # outside a function it fails
                        f'export -Z L={log}; touch "$L"',  # a bad option fails
                        f'if false; then L={log}; fi; touch "$L"',
                        f'L={log} && source ./env.sh; touch "$L"'):  # it may set L
            with self.subTest(command=command):
                self.assertTrue(self.bash(command, cwd=lane).startswith("deny: "), command)
        # a name set before `source` may hold anything after it
        self.assertTrue(self.bash(f'cd {lane} && L={log} && source ./env.sh && touch "$L"')
                        .startswith("deny: "))

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

    def test_a_builtin_changes_the_shell_only_as_spelled(self):
        """sd:6083 round 6: a path names a program, which runs in a child and
        changes no shell state; the next command still runs."""
        lane = self.system
        for command in ("/usr/bin/exit 0; touch x",
                        "./exit; touch x",
                        "/usr/bin/time exit 0; touch x",
                        "/usr/bin/command exit 0; touch x",
                        "C=/usr/bin/exit; $C 0; touch x",
                        f"/usr/bin/cd {self.worktree} && touch x",
                        f"/usr/bin/command cd {self.worktree} && touch x",
                        f"/usr/bin/time cd {self.worktree} && touch x",
                        f"/usr/bin/eval 'cd {self.worktree}' && touch x",
                        f"/usr/bin/pushd {self.worktree} && touch x",
                        f"set -- {self.worktree}; /usr/bin/shift; cd \"$1\" && touch x",
                        f"/usr/bin/export D={self.worktree}; cd \"$D\" && touch x"):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=lane), lane)
        # `/usr/bin/read` leaves D as set; the builtin would make it unknown
        self.assertEqual(self.bash(f"D={self.worktree}; /usr/bin/read D; cd \"$D\" && touch x",
                                   cwd=lane), "allow")

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
        # a failed `popd` stays in the lane; one past the known stack is unknown
        self.assertTrue(self.bash(f"pushd {lane}; popd; touch x", cwd=work).startswith("deny: "))

    def test_state_the_reader_cannot_track_is_refused(self):
        """sd:6083 round 4: the folder stack, `cd -`, `~`, CDPATH, braces, globs,
        input, arrays, `${..}` forms, `$@`, `$( )`, a name from a variable, and
        word splitting either resolve as the shell does or refuse."""
        lane, work, repos = self.system, self.worktree, self.system.parent
        self.env["PWD"] = str(work)  # the hook's own folder, not the shell's
        for command, cwd in (("pushd /usr && popd && touch x", lane),
                             ("pushd /usr && pushd && touch x", lane),
                             ("pushd /usr && pushd +1 && touch x", lane),
                             (f"pushd -n {lane} && popd && touch x", work),
                             ("popd && touch x", lane),
                             ("cd /usr && cd - && touch x", lane),
                             ("cd /usr && cd +1 && touch x", lane),
                             ("cd /usr && touch ~-/x", lane),
                             (f'cd {lane} && cd /usr && touch "$OLDPWD/x"', work),
                             ('touch "$PWD/x"', lane),
                             (f"HOME={lane}; touch ~/x", work),
                             (f"HOME={lane}; cd && touch x", work),
                             (f"export CDPATH={repos}; cd system && touch x", work),
                             (f"touch {{/tmp,{lane}}}/x", work),
                             (f"touch {repos}/{{sys,x}}{{tem,y}}/x", work),
                             (f"touch {repos}/{{a..z}}ystem/x", work),
                             (f"rm -rf {repos}/sys*", work),
                             (f"rm -rf {repos}/sys?em", work),
                             (f'for d in {repos}/sys*; do touch "$d/x"; done', work),
                             (f"D='{repos}/sys*'; rm -rf $D", work),
                             (f'D=/tmp; read D <<< {lane}; touch "$D/x"', work),
                             ('D=/tmp; mapfile D < /dev/null; touch "$D/x"', lane),
                             ('D=/tmp; readarray D < /dev/null; touch "$D/x"', lane),
                             (f'D=/tmp; printf -v D %s {lane}; touch "$D/x"', work),
                             ('getopts a: D; touch "$OPTARG/x"', lane),
                             (f'D=({lane}); touch "$D/x"', work),
                             (f'D=/tmp; D[0]={lane}; touch "$D/x"', work),
                             (f'declare -a D=({lane}); touch "$D/x"', work),
                             (f'D={repos}; D+=/system; touch "$D/x"', work),
                             (f'declare -n R=D; D={lane}; touch "$R/x"', work),
                             (f'declare -u D; D={lane}; touch "$D/x"', work),
                             (f"touch {str(lane).upper()}/x", work),
                             (f'D={lane}; touch "${{D:-/tmp}}/x"', work),
                             (f'D={lane}/; touch "${{D%/}}/x"', work),
                             (f'set -- {lane}; touch "$@"', work),
                             (f'set -- {lane}; touch "$*"', work),
                             (f"set -- {lane}/; touch $1x", work),
                             (f'set -- /tmp /tmp {lane}; n=2; shift $n; touch "$1/x"', work),
                             (f'set -- {lane}; for d; do touch "$d/x"; done', work),
                             (f'F="/tmp {lane}"; set -- $F; touch "$2/x"', work),
                             (f'F="/tmp {lane}/x"; touch $F', work),
                             (f"C=touch; $C {lane}/x", work),
                             (f'C="rm -f"; $C {lane}/README.md', work),
                             (f"C=cd; $C {lane}; touch x", work),
                             (f'touch "$(echo {lane})/x"', work),
                             (f"touch $(echo {lane})/x", work),
                             (f"touch `echo {lane}`/x", work),
                             (f'D=$(echo {lane}); touch "$D/x"', work),
                             (f"$(echo touch) {lane}/x", work),
                             (f"D='$E'; E={lane}; eval \"cd $D\"; touch x", work),
                             (f"f() {{ cd {lane}; }}; f; touch x", work),
                             (f"function f {{ cd {lane}; }}; f; touch x", work),
                             ('D=/tmp; source ./env.sh; touch "$D/x"', lane),
                             (". ./env.sh && touch x", lane),
                             ('eval "$(cat commands)"; touch x', lane),
                             (f"cat > n.txt <<EOF\nx\nEOF\nrm -rf {lane}", work)):
            with self.subTest(command=command):
                self.assertTrue(self.bash(command, cwd=cwd).startswith("deny: "), command)
        for command in ("pushd /usr && popd && touch x",
                        f"pushd {repos} && pushd {work} && popd && popd && touch x",
                        "cd /usr && cd - && touch x",
                        "cd - && ls",
                        f"mkdir -p {work}/{{a,b}}/{{1..3}}",
                        "mkdir -p src/{a,b}",
                        f"touch '{repos}/sys*' \"{{/tmp,{lane}}}\"",
                        "rm -rf build/* && touch y.$$",
                        "for f in *.txt; do touch \"$f.bak\"; done",
                        "read D && echo \"$D\"",
                        "D=(a b) && echo \"$D\"",
                        f'D={work}; D+=/x; touch "$D"',
                        "C=touch; $C x",
                        f"cat > s.sh <<-EOF\n\tf() {{ rm -rf {lane}; }}\n\tEOF\ntouch y"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=work), "allow")
        for command in ("D=(rm README.md)",  # an array runs nothing
                        f"cd {work} && cat > n.txt <<'EOF'\nrm -rf {lane}\nEOF"):  # text
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=lane, fixed=True), "allow")

    def test_an_unknown_counts_only_near_a_root(self):
        """Operator ruling on sd:6083 round 4: what the reader cannot know refuses
        only when a world's folder is or may be in a root, or the text names one."""
        lane, work = self.system, self.worktree
        for command in ('touch "$(pwd)/x"', 'read D; cd "$D" && touch x',
                        "source ./env.sh && mkdir y"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=work), "allow")
                self.assertTrue(self.bash(command, cwd=lane).startswith("deny: "))
        for command in (f'cd {lane} && read D; touch "$D"',
                        f"pushd {lane} && popd && popd && touch x",
                        f'touch "$(echo x)" {lane}/README.md',
                        f'D=$(echo {lane}); touch "$D/x"'):
            with self.subTest(command=command):
                self.assertTrue(self.bash(command, cwd=work).startswith("deny: "), command)
        for command in ('mkdir -p "$(git rev-parse --show-toplevel)/build"',
                        "popd && touch x",
                        'read D; touch "$D/x"',
                        "cd \"$(mktemp -d)\" && touch x"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=work), "allow")

    def test_text_read_as_data_hides_no_command(self):
        """sd:6083 round 5: a here-document body, a here-string, quotes, a
        comment, arithmetic and `${..}` are data, but every `$( )` and
        backquote the shell runs in them is read; a skipped body ends where
        the shell ends it."""
        lane, work = self.system, self.worktree
        touch = f"touch {lane}/x"
        for command in (f"cat <<EOF\n$({touch})\nEOF",
                        f"cat <<EOF\n`{touch}`\nEOF",
                        f"cat <<-EOF\n\t${{D:-$({touch})}}\n\tEOF",
                        f'echo "$(cat <<EOF\n$({touch})\nEOF\n)"',
                        f"cat <<<hello\n{touch}",
                        f'cat <<< "$({touch})"',
                        f'cat < "$({touch})"',
                        f'echo "$({touch})"',
                        f'echo "`{touch}`"',
                        f"echo `{touch}`",
                        f"echo `echo \\`{touch}\\``",
                        f'echo $"$({touch})"',
                        f'D="$({touch})"',
                        f'echo "${{D:-$({touch})}}"',
                        f'echo "$((1 + $({touch})))"',
                        f'echo "$( (case a in a) ;; esac); {touch} )"',
                        f"D=($({touch}))",
                        f"echo $'\\''; {touch}",
                        f"echo \\ #$({touch})",
                        f"echo $((1<<2))\n{touch}",
                        f"((D = 1 << 2))\n{touch}",
                        f"echo $[1<<2]\n{touch}",
                        f"echo ${{D:-<<x}}\n{touch}"):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=work), lane)
        for command in (f"cat <<'EOF'\n$({touch})\nEOF",  # 13554b17 read the body
                        f'cat <<"EOF"\n$({touch})\nEOF',
                        f"cat <<\\EOF\n$({touch})\nEOF"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=work, fixed=True), "allow")
        for command in (f'cat <<E"O"F\n`{touch}`\nEOF',
                        f"echo '$({touch})' $'`{touch}`' \"\\$({touch})\"",
                        f"echo # $({touch})",
                        "cat <<EOF\ndon't \"quote\" $(echo x)\nEOF"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=work), "allow")

    def test_a_shell_c_script_is_read_in_a_child(self):
        """sd:6153: `sh -c` and `bash -c` run their script in a child: its
        writes count, its `cd` and assignments stay there."""
        lane, work = self.system, self.worktree
        for command in (f"sh -c 'touch {lane}/x'",
                        f'bash -lc "cd {lane} && rm README.md"',
                        f"bash -e -o pipefail -c 'mkdir {lane}/d'",
                        f"/bin/sh -c 'touch \"$1/x\"' sh {lane}",
                        f'D={lane}; sh -c "touch $D/x"',
                        f"timeout 5 zsh -c 'git -C {lane} commit -m x'",
                        f"echo x | xargs sh -c 'touch {lane}/x'",
                        f"sh -c 'sh -c \"touch {lane}/x\"'"):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=work), lane)
        for command in (f"sh -c 'cd {lane}'; touch x",
                        f"bash -c 'touch \"$1/x\"' _ {work}",
                        "sh -c 'echo hi > /tmp/sd-6153.log'",
                        f"sh -c 'cd {work} && touch x'"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=work), "allow")
        self.assertEqual(self.bash(f"sh -c 'cd {work} && touch x'", cwd=lane), "allow")

    def test_a_function_body_is_read_where_it_is_called(self):
        """sd:6153: a function runs its body at each call, in the caller's
        folder and with the call's arguments; a definition alone runs nothing."""
        lane, work = self.system, self.worktree
        for command in (f"f() {{ touch {lane}/x; }}; f",
                        f"function f {{ cd {lane}; }}; f; touch x",
                        f"function f() {{ cd {lane}; }}\nf && touch x",
                        f'f() {{ touch "$1/x"; }}; f {lane}',
                        f"f() ( cd {lane} && touch x ); f",
                        f"f() {{ touch x; }}; cd {lane} && f",
                        f"f() {{ cat; }} > {lane}/x; f",
                        f"my-f() {{ rm -rf {lane}; }}; my-f"):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=work), lane)
        for command, fixed in (("f() { echo hi; }; f", False),
                               ("f() { rm -f README.md; }", False),
                               (f"f() {{ cd {work}; }}; f && touch x", True),
                               ('f() { touch "$1"; }; f /tmp/sd-6153', False)):
            with self.subTest(command=command):  # FIXED: 13554b17 refused it
                self.assertEqual(self.bash(command, cwd=lane, fixed=fixed), "allow")
        self.assertTrue(self.bash("f() { f; }; f", cwd=lane).startswith("deny: "))
        self.assertEqual(self.bash("f() { f; }; f", cwd=work), "allow")

    def test_a_parameter_form_is_read(self):
        """sd:6153: `${D:-w}`, `${D-w}`, `${D:+w}`, `${D+w}`, `${D:?w}`,
        `${D#p}`, `${D##p}`, `${D%p}`, `${D%%p}` and `${#D}` read as the
        shell reads them; any other form is unknown."""
        lane, work = self.system, self.worktree
        for command, fixed in (('D=/tmp; touch "${D:-README.md}/x"', True),
                               ('D=; touch "${D:-/tmp}/x"', True),
                               ('D=/tmp/; touch "${D%/}/x"', True),
                               ('D=/tmp/a.txt; touch "${D%.txt}.log"', True),
                               ('D=/tmp/x; touch "${D:+$D.bak}"', True),
                               ('D=/tmp/x; touch "${D:+${D}.bak}"', True),
                               ('D=README.md; touch "/tmp/${D##*/}"', False),
                               ('D=/tmp; touch "${D?}/x" "${D:?unset}/y"', True),
                               ('D=/tmp/a/b; touch "${D%%/a*}/x" "${D#/tmp}"', True),
                               ('touch "/tmp/${#D}"', False)):
            with self.subTest(command=command):  # FIXED: 13554b17 refused it
                self.assertEqual(self.bash(command, cwd=lane, fixed=fixed), "allow")
        for command in (f'D={lane}; touch "${{D:-/tmp}}/x"',
                        f'touch "${{D:-{lane}}}/x"',
                        f'D=; touch "${{D:-{lane}}}/x"',
                        f'D=/tmp; touch "${{D:+{lane}}}/x"',
                        f'D={lane}/a; touch "${{D%/a}}/x"',
                        f'D=x{lane}; touch "${{D#x}}/x"',
                        f'D={lane}/a/b; touch "${{D%%/a*}}/x"',
                        f'D={lane}/a/b; touch "${{D%/*}}" "${{D##*/}}"',
                        f'touch "${{D:={lane}}}/x"',
                        f'D={lane}; touch "${{D/x/y}}/x"'):
            with self.subTest(command=command):
                self.assertTrue(self.bash(command, cwd=work).startswith("deny: "), command)
        for command in ('touch "${D:=/tmp}/x"', 'D=/tmp; touch "${D:0:4}/x"'):
            with self.subTest(command=command):
                self.assertTrue(self.bash(command, cwd=lane).startswith("deny: "), command)
                self.assertEqual(self.bash(command, cwd=work), "allow")

    def test_a_quoted_reserved_word_is_a_command(self):
        """sd:6153 round 7: shlex drops the quotes, so `'case'` read as syntax
        and hid the commands after it; a quoted `time` is a program."""
        lane, work = self.system, self.worktree
        for command in (f"'case'; touch {lane}/x",
                        f"'if' true; touch {lane}/x",
                        f"\\for x; touch {lane}/x",
                        f'"done"; touch {lane}/x',
                        f"''while; touch {lane}/x"):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=work), lane)
        self.assertDenied(self.bash("'time' exit 0; touch x", cwd=lane), lane)
        self.assertEqual(self.bash("echo 'if' \"then\" fi; ls", cwd=lane), "allow")

    def test_a_loop_past_two_passes_is_unknown(self):
        """sd:6153 round 8: the reader runs a body twice; a value still
        changing on the second pass may change on a third, so it is unknown."""
        lane, repos = self.system, self.system.parent
        for command in (f'D={repos}/; for c in s y s t e m; do D=$D$c; done; touch "$D/x"',
                        f'D={repos}/; while :; do D=$D/s; done; touch "$D/x"'):
            with self.subTest(command=command):
                self.assertTrue(self.bash(command, cwd=lane).startswith("deny: "), command)
        self.assertEqual(self.bash('D=/tmp; for c in a b c; do D=/tmp/$c; done; touch "$D/x"',
                                   cwd=lane), "allow")

    def test_declaration_arguments_expand_before_it_runs(self):
        """sd:6153 round 8: bash expands every `export` argument first, so
        `E=$D` takes D's value from before the command."""
        lane, work = self.system, self.worktree
        for command in (f'D={lane}; export D=/tmp E=$D; touch "$E/x"',
                        f'D={lane}; declare D=/tmp E=$D; touch "$E/x"'):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=work), lane)
        self.assertDenied(self.bash(f'D=/tmp E={lane}; touch "$E/x"', cwd=work), lane)

    def test_a_reader_error_is_an_unknown(self):
        """sd:6153 round 7: an exception in the reader denied nothing; it is
        an unknown, so it refuses near a root and passes elsewhere."""
        lane, work = self.system, self.worktree
        huge = "9" * 4500
        for command in ("set -- a b; shift ²; touch \"$1\"",
                        f"touch {{1..{huge}}}",
                        f"touch {{a..z..{huge}}}",
                        f"echo $((1 << {huge})); touch x"):
            with self.subTest(command=command[:40]):
                self.assertTrue(self.bash(command, cwd=lane).startswith("deny: "), command[:40])
                self.assertEqual(self.bash(command, cwd=work), "allow")
        self.assertDenied(self.decide("Write", file_path=f"{lane}/{{1..{huge}}}"), lane)
        protected = [str(lane), str(self.pack)]
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(BRANCH, "scan", side_effect=IndexError):
            self.assertRaises(BRANCH.Unreadable, BRANCH.bash_root, "ls", str(lane), protected)
            self.assertIsNone(BRANCH.bash_root("ls", str(work), protected))

    def test_extreme_input_never_passes_a_write_near_a_root(self):
        """Property over extreme words (huge numbers, unicode digits, deep
        nesting, long values, NUL): a `touch` in the lane after them is
        refused, and no input raises past the reader, within BUDGET's time."""
        lane, work = self.system, self.worktree
        rng = random.Random(6153)
        huge = lambda: "1" + "0" * rng.choice([1, 18, 19, 300, 4299, 4400])
        pieces = [lambda: f"{{1..{huge()}}}", lambda: f"{{0..9..{huge()}}}",
                  lambda: f"{{-{huge()}..{huge()}}}", lambda: f"shift {huge()}",
                  lambda: "shift ²", lambda: "set -- ٣ ¹",
                  lambda: f"echo ${huge()}", lambda: "echo ${#D} " + "${D:-" * 50 + "x" + "}" * 50,
                  lambda: "D=" + "x" * rng.choice([1, 4096, 100000]),
                  lambda: f'D="${{D:-{huge()}}}"', lambda: "echo \0 $'\\0'",
                  lambda: "f() { f; }; f", lambda: "sh -c " * 30 + "true",
                  lambda: "for i in 1; do " * 40 + "D=$D$D; " + "done; " * 40,
                  lambda: f"echo $(({huge()} ** {huge()}))", lambda: "'" + "if" * 500 + "'",
                  lambda: "declare -i D=" + huge(), lambda: "${" * 300 + "D" + "}" * 300]
        protected, started = [str(lane), str(self.pack)], time.monotonic()
        for _ in range(150):
            text = "; ".join(rng.choice(pieces)() for _ in range(rng.randrange(1, 4)))
            with self.subTest(command=text[:60]), \
                    mock.patch.dict(os.environ, self.env, clear=True):
                try:
                    root = BRANCH.bash_root(text + "\ntouch x", str(lane), protected)
                except BRANCH.Unreadable:
                    root = str(lane)
                self.assertEqual(root, str(lane))
                try:
                    BRANCH.bash_root(text, str(work), protected)
                except BRANCH.Unreadable:
                    pass
        self.assertLess(time.monotonic() - started, 120)

    def test_every_word_form_resolves_or_is_unknown(self):
        """sd:6153 class pass: an ANSI-C quote spells its escapes, xargs adds
        words from its input, which the reader cannot know, a `$".."` is its
        text, and `${D:=w}` sets D."""
        lane, work, rest = self.system, self.worktree, str(self.system)[1:]
        for command in (f"touch $'\\x2f'{rest}/x",
                        f"mkdir $'\\057{rest}/d'",
                        f"touch $'\\u002f'{rest}/x",
                        f"D=$'\\x2f'{rest}; touch \"$D/x\""):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=work), lane)
        for command in ("git ls-files | xargs rm", "find . | xargs -I{} cp {} {}.bak",
                        "ls | xargs -n 1 touch"):
            with self.subTest(command=command):
                self.assertTrue(self.bash(command, cwd=lane).startswith("deny: "), command)
                self.assertEqual(self.bash(command, cwd=work), "allow")
        for command in ("git ls-files | xargs wc -l", "echo $'a\\tb' > /tmp/sd-6153.txt",
                        "printf $'%s\\n' x"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=lane), "allow")
        # `$".."` is its text; `${D:=w}` and `${D=w}` also set D
        for command in (f'touch $"{lane}/x"', f': ${{D:={lane}}}; touch "$D/x"',
                        f'D=; echo ${{D={lane}}} >/dev/null; touch "$D/x"'):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=work), lane)
        header = f'for x in ${{D:={lane}}}; do :; done; touch "$D/x"'  # an unknown, near a root
        self.assertTrue(self.bash(header, cwd=work).startswith("deny: "), header)
        for command, fixed in (('touch $"/tmp/x"', True), ('D=/tmp; : ${D:=x}; touch "$D/y"', False)):
            with self.subTest(command=command):  # FIXED: 13554b17 refused it
                self.assertEqual(self.bash(command, cwd=lane, fixed=fixed), "allow")
        # every redirect form writes its target; a process substitution runs
        for command in (f"echo hi >> {lane}/x", f"echo hi 2>> {lane}/x", f"echo hi &> {lane}/x",
                        f"echo hi >| {lane}/x", f"echo hi >& {lane}/x", f"exec 3<> {lane}/x",
                        f"cat > >(tee {lane}/x)"):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=work), lane)
        # `$$`, `$?`, `$#` and `$!` are numbers; a name that is not ASCII is a command
        for command in ("touch /tmp/$$ /tmp/$? /tmp/$# /tmp/$!", f"é={lane}; ls"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=lane if "$" in command else work), "allow")

    def test_a_command_reads_what_its_line_hands_it(self):
        """sd:6153 class pass: `D=x cmd` and `env D=x cmd` hand D to a child
        shell, a function and `eval`; a trap body runs later, anywhere; `time`
        and `coproc` run the compound command after them."""
        lane, work = self.system, self.worktree
        for command in (f"D={lane} sh -c 'touch \"$D/x\"'",
                        f"env D={lane} sh -c 'touch \"$D/x\"'",
                        f'f() {{ touch "$D/x"; }}; D={lane} f',
                        f"D={lane} eval 'touch \"$D/x\"'",
                        f'D=/tmp; f() {{ :; }}; D={lane} f; touch "$D/x"',
                        f"trap 'touch {lane}/x' EXIT",
                        f"trap 'cd {lane}' DEBUG; touch x",
                        f"coproc {{ touch {lane}/x; }}",
                        f"coproc X {{ touch {lane}/x; }}",
                        f"coproc touch {lane}/x",
                        f"time {{ touch {lane}/x; }}"):
            with self.subTest(command=command):
                self.assertDenied(self.bash(command, cwd=work), lane)
        for command in ("D=/tmp sh -c 'touch \"$D/x\"'", 'f() { touch "$D/x"; }; D=/tmp f',
                        "D=/tmp eval 'touch \"$D/x\"'", "trap 'rm -f /tmp/x' EXIT; ls",
                        "time { ls; }"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, cwd=lane), "allow")

    def test_a_root_is_named_only_as_a_whole_path(self):
        """sd:6083 round 5: `/lane-tools` names no root; `/lane`, `/lane/x` and
        the `~`, `$HOME` and `${HOME}` forms do."""
        self.env["HOME"] = str(self.system.parent.parent)
        lane, unknown = self.system, 'touch "$(pwd)/x"; echo '
        for form in (str(lane), "~/repos/system", "$HOME/repos/system", "${HOME}/repos/system"):
            with self.subTest(form=form):
                self.assertEqual(self.bash(unknown + form + "-tools", cwd=self.worktree), "allow")
                self.assertTrue(self.bash(unknown + form, cwd=self.worktree).startswith("deny: "))
                self.assertTrue(self.bash(unknown + form + "/x", cwd=self.worktree)
                                .startswith("deny: "))
                self.assertTrue(self.bash(unknown + "D=" + form, cwd=self.worktree)
                                .startswith("deny: "))

    def test_a_root_is_named_only_from_a_path_boundary(self):
        """sd:6236: `/backup/lane` names no root; a root form needs a boundary
        before it as well as after it."""
        self.env["HOME"] = str(self.system.parent.parent)
        lane, unknown = self.system, 'touch "$(pwd)/x"; echo '
        for form in (str(lane), "~/repos/system", "$HOME/repos/system", "${HOME}/repos/system"):
            for prefix in ("/backup", "x", "a-", "/backup/"):
                with self.subTest(form=form, prefix=prefix):
                    self.assertEqual(self.bash(unknown + prefix + form, cwd=self.worktree),
                                     "allow")

    def test_growth_and_nesting_past_the_budget_are_refused_in_time(self):
        """sd:6083 round 4: a variable product, a brace product, a doubling value
        and deep nesting each stop at the budget, not at memory or the stack;
        near a root that refuses, elsewhere it passes."""
        names = [f"A{i}" for i in range(20)]
        for command in ("".join(f"{name}=a; " for name in names)
                        + "touch " + "".join(f"${name}" for name in names),
                        "touch " + "{a,b}" * 30,
                        "D=x; " + "D=$D$D; " * 24 + 'touch "$D"',
                        "(" * 400 + "touch x" + ")" * 400,
                        'echo "' + "$(" * 2000 + "touch x" + ")" * 2000 + '"'):
            with self.subTest(command=command[:40]):
                started = time.monotonic()
                decision = self.decide("Bash", cwd=self.system, command=command)
                self.assertTrue(decision.startswith("deny: "), decision)
                self.assertEqual(self.decide("Bash", cwd=self.worktree, command=command), "allow")
                self.assertLess(time.monotonic() - started, 20)

    def test_every_write_the_base_refused_is_refused(self):
        """Property over a shell grammar: where this guard allows, a real bash run in a
        scratch tree writes nothing in the lane. That covers each write 13554b17 refused
        and this guard allows (sd:6083 rounds 1, 2 and 4)."""
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
                  "popd", f"set -- {lane}", "shift", 'touch "$1/y"', "cd -", "pushd",
                  "popd -n", f"pushd -n {lane}", "pushd +1", f"rm -f {lane.parent}/s*/READ*",
                  f"touch {{{work},{lane}}}/y", f"read D <<< {lane}", "D+=/y", "touch ~-/y",
                  "C=touch", "$C y", f"D=({lane})", f"set -- {lane}/", "touch $1y",
                  f'F="{work} {lane}"', "touch $F", 'echo "$(touch y)"', "echo \"`touch y`\"",
                  'cat <<<"$(touch y)"', "cat <<<y\ntouch y", "cat <<EOF\n$(touch y)\nEOF\n",
                  "cat <<'EOF'\n$(touch y)\nEOF\n", "echo $((1<<2))\ntouch y",
                  "echo $'\\''; touch y",
                  # sd:6153: `sh -c`, functions, quoted reserved words, `${..}`,
                  # loops past two passes, declaration arguments
                  "sh -c 'touch y'", "bash -c 'cd d1 && touch y'", f"sh -c 'cd {lane}'",
                  "sh -c 'touch \"$1\"' _ y", 'sh -c "touch $D/y"', "f() { touch y; }", "f",
                  f"g() {{ cd {lane}; }}", "g", "function h { cd d1; }", "h", "'if' true",
                  "'case'", "'time' exit 0", "\\for x", 'D=${D:-d1}', 'touch "${D:-y}"',
                  'touch "${D%/}y"', 'touch "${D#*/}"', 'D=${D:+$D/a}', 'D=${D%/*}',
                  "for i in 1 2 3; do D=$D/d1; done", f"export D={work} E=$D", 'touch "$E/y"',
                  'echo "$(cd d1; touch y)"',
                  # sd:6153 class pass: what a line hands its command, `${D:=w}`,
                  # `$".."`, trap bodies, compounds after `time` and `coproc`
                  "D=d1 sh -c 'touch \"$D/y\"'", f"D={lane} sh -c 'cd \"$D\" && touch y'",
                  "D=d1 f", "D=d1 eval 'touch \"$D/y\"'", ": ${D:=d1}", f": ${{D:={lane}}}",
                  'touch $"y"', "trap 'touch y' EXIT", f"trap 'cd {lane}' DEBUG",
                  "coproc { touch y; }", "time { touch y; }"]

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
        decision = self.bash(command, cwd=self.system)
        self.assertTrue(decision.startswith("deny: checkout_guard cannot read this command"),
                        decision)
        self.assertEqual(self.bash(command.replace("; ", " && "), cwd=self.system), "allow")
        self.assertEqual(self.bash(command, cwd=self.worktree), "allow")  # no root near


if __name__ == "__main__":
    unittest.main()
