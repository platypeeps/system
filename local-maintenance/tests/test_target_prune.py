"""`run` deletes cargo `target/` folders idle for 3 days under ~/worktrees (sd:5994).

Builders leave one build tree per worktree, several GB each, and a worktree
stays until its branch lands. The checkout and its commits stay; only the
build output goes, and the next build makes it again. A folder counts as
cargo's when it holds CACHEDIR.TAG, which cargo writes and git never tracks.
"""

import contextlib
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_report import FOLDER, Harness, _stub

FOUR_DAYS = 4 * 86400
_SPEC = importlib.util.spec_from_file_location("prune_move", FOLDER / "prune_move.py")
prune_move = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(prune_move)


def _age(root, seconds):
    """Set every entry under `root`, and `root`, to `seconds` ago."""
    when = time.time() - seconds
    for path in sorted(root.rglob("*"), reverse=True):
        os.utime(path, (when, when), follow_symlinks=False)
    os.utime(root, (when, when))


def _hold(test, lock):
    """A child that holds flock(2) on `lock`, as a running cargo build does."""
    holder = subprocess.Popen(
        [sys.executable, "-c", "import fcntl, sys\n"
         "f = open(sys.argv[1], 'r+')\n"
         "fcntl.flock(f, fcntl.LOCK_EX)\n"
         "print('locked', flush=True)\n"
         "sys.stdin.read()\n", str(lock)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    test.addCleanup(holder.stdout.close)
    test.addCleanup(holder.wait)
    test.addCleanup(holder.stdin.close)
    test.assertEqual(holder.stdout.readline(), "locked\n")


class TargetPruneTest(Harness):
    def setUp(self):
        super().setUp()
        self.worktrees = self.tmp / "home" / "worktrees"
        self.trash = self.tmp / "home" / ".local" / "state" / "maintenance" / "prune-trash"
        # No cargo runs, whatever runs on this machine.
        _stub(self.bin / "pgrep", "exit 1\n")

    def cargo_target(self, worktree, *, idle=FOUR_DAYS, tag=True, under="target"):
        """A worktree with tracked files and a build tree last written `idle` ago."""
        checkout = self.worktrees / worktree
        (checkout / "src").mkdir(parents=True)
        (checkout / "Cargo.toml").write_text("[package]\nname = \"example\"\n")
        (checkout / "src" / "main.rs").write_text("fn main() {}\n")
        target = checkout / under
        (target / "debug" / "deps").mkdir(parents=True)
        (target / "debug" / "deps" / "example-0123").write_bytes(b"\0" * 64)
        (target / "debug" / ".cargo-lock").write_bytes(b"")
        if tag:
            (target / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
        _age(target, idle)
        return checkout, target

    def actions(self, run):
        return [line for line in run.stdout.splitlines() if "build output" in line]

    def test_an_idle_target_goes_and_the_checkout_stays(self):
        checkout, target = self.cargo_target("example-idle")
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse(target.exists(), run.stdout + run.stderr)
        self.assertTrue((checkout / "Cargo.toml").is_file())
        self.assertTrue((checkout / "src" / "main.rs").is_file())
        self.assertIn(f"- removed idle build output {target} (untouched 3 days)", run.stdout)
        self.assertEqual(self.attention(run), [])

    def test_a_target_written_inside_3_days_stays(self):
        _checkout, target = self.cargo_target("example-busy")
        fresh = target / "debug" / "deps" / "example-4567"
        fresh.write_bytes(b"\0")
        _age(fresh, 2 * 86400)
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue(fresh.is_file())
        self.assertEqual(self.actions(run), [])

    def test_a_target_cargo_did_not_write_stays(self):
        _checkout, target = self.cargo_target("example-untagged", tag=False)
        run = self.maintenance("# nothing to check\n")
        self.assertTrue((target / "debug" / "deps" / "example-0123").is_file(), run.stdout)

    def test_a_linked_target_and_what_it_names_stay(self):
        _elsewhere, real = self.cargo_target("example-real", under="build")
        checkout = self.worktrees / "example-linked"
        checkout.mkdir(parents=True)
        (checkout / "target").symlink_to(real)
        run = self.maintenance("# nothing to check\n")
        self.assertTrue((checkout / "target").is_symlink())
        self.assertTrue((real / "CACHEDIR.TAG").is_file(), run.stdout)

    def test_a_worktree_named_target_is_not_build_output(self):
        # Depth 1 is the checkout itself; a branch slug can be "target".
        checkout = self.worktrees / "target"
        checkout.mkdir(parents=True)
        (checkout / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
        _age(checkout, FOUR_DAYS)
        self.maintenance("# nothing to check\n")
        self.assertTrue((checkout / "CACHEDIR.TAG").is_file())

    def test_a_target_whose_cargo_lock_is_held_stays(self):
        # sd:5994 review round 5: cargo locks a build with flock(2) on
        # target/<profile>/.cargo-lock. A child holds a real flock there,
        # as a running build does; the target stays, with no finding.
        _checkout, target = self.cargo_target("example-building")
        lock = target / "debug" / ".cargo-lock"
        _hold(self, lock)
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue((target / "CACHEDIR.TAG").is_file(), run.stdout + run.stderr)
        self.assertTrue(lock.is_file())
        self.assertNotIn("build output", run.stdout)
        self.assertFalse(self.trash.exists() and any(self.trash.iterdir()))

    def test_a_lock_that_cannot_be_opened_keeps_the_target_and_says_so(self):
        # A lock the move cannot open is no answer: the folder stays.
        _checkout, target = self.cargo_target("example-unsure")
        (target / "debug" / ".cargo-lock").chmod(0)
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue((target / "CACHEDIR.TAG").is_file())
        self.assertIn(f"- build output prune: could not move {target} into {self.trash}", run.stdout)
        self.assertIn("Permission denied", run.stdout)

    def test_check_names_the_target_and_removes_nothing(self):
        _checkout, target = self.cargo_target("example-dry")
        run = self.maintenance("# nothing to check\n", "check")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue((target / "CACHEDIR.TAG").is_file())
        self.assertIn(f"- [dry-run] would remove idle build output {target} (untouched 3 days)", run.stdout)

    def test_a_folder_named_like_an_old_aside_stays(self):
        # sd:5994 review round 2: nothing proves a run made a folder named
        # target.idle-prune; at depth 2 it can be a checkout with work.
        checkout = self.worktrees / "x" / "target.idle-prune"
        checkout.mkdir(parents=True)
        (checkout / "notes.txt").write_text("uncommitted\n")
        _age(checkout, FOUR_DAYS)
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue((checkout / "notes.txt").is_file(), run.stdout + run.stderr)

    def test_a_cargo_that_starts_during_the_delete_keeps_its_new_target(self):
        # sd:5994 review round 3: rm -rf walks target/ for minutes, and a
        # cargo can take its lock meanwhile. The stub rm stands in for that
        # cargo: before the delete runs, it makes target/debug/.cargo-lock
        # again. The delete must not reach the new target/.
        _checkout, target = self.cargo_target("example-restart")
        mark = self.tmp / "cargo-started"
        _stub(self.bin / "rm", f'''if [ ! -e "{mark}" ]; then
  : > "{mark}"; mkdir -p "{target}/debug" && : > "{target}/debug/.cargo-lock"
fi
exec /bin/rm "$@"
''')
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue(mark.exists(), run.stdout + run.stderr)
        self.assertTrue((target / "debug" / ".cargo-lock").is_file(), run.stdout + run.stderr)
        self.assertFalse((target / "CACHEDIR.TAG").exists())
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_a_leftover_in_the_trash_goes_and_worktrees_stay(self):
        # A run stopped during a delete leaves its entry in the trash. The
        # next run deletes entries there and nothing under ~/worktrees.
        leftover = self.trash / "Ab12Cd34" / "target"
        (leftover / "debug").mkdir(parents=True)
        (leftover / "debug" / "example-0123").write_bytes(b"\0")
        checkout, target = self.cargo_target("example-fresh", idle=60)
        aside = self.worktrees / "x" / "target.idle-prune"
        aside.mkdir(parents=True)
        (aside / "notes.txt").write_text("uncommitted\n")
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse((self.trash / "Ab12Cd34").exists(), run.stdout + run.stderr)
        self.assertIn(f"- removed {self.trash / 'Ab12Cd34'}, left in the prune trash", run.stdout)
        self.assertTrue((target / "CACHEDIR.TAG").is_file())
        self.assertTrue((checkout / "Cargo.toml").is_file())
        self.assertTrue((aside / "notes.txt").is_file())

    def test_a_target_that_cannot_be_moved_stays_and_is_never_copied(self):
        # rename(2) or nothing: mv would copy across volumes, and a copy is
        # the long window the trash exists to close.
        checkout, target = self.cargo_target("example-stuck")
        checkout.chmod(0o555)
        self.addCleanup(checkout.chmod, 0o755)
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue((target / "CACHEDIR.TAG").is_file())
        self.assertIn(f"- build output prune: could not move {target} into {self.trash}", run.stdout)
        self.assertEqual(list(self.trash.rglob("CACHEDIR.TAG")), [])

    def test_an_idle_probe_that_fails_keeps_the_target_and_says_so(self):
        # A failed probe is no answer: it is not proof the folder is idle.
        _stub(self.bin / "find", 'case " $* " in *" -mmin "*) echo "find: probe failed" >&2; exit 1 ;; esac\n'
              'exec /usr/bin/find "$@"\n')
        _checkout, target = self.cargo_target("example-unknown")
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue((target / "CACHEDIR.TAG").is_file())
        self.assertIn(f"- build output prune: could not read {target}; kept", run.stdout)

    def test_each_bulk_call_runs_bounded_and_names_itself(self):
        """sd:3536: the walks and the delete run under st_step."""
        _checkout, target = self.cargo_target("example-bounded")
        run = self.maintenance("# nothing to check\n")
        self.assertRegex(run.stderr, r"\[step [\d:]+\] pgrep -x cargo\|rustc \(bound \d+s\)")
        self.assertRegex(run.stderr, rf"\[step [\d:]+\] find {self.worktrees} .* \(bound \d+s\)")
        self.assertRegex(run.stderr, rf"\[step [\d:]+\] find {target} -mmin -4320 .* \(bound \d+s\)")
        self.assertRegex(run.stderr, rf"\[step [\d:]+\] rm -rf {self.trash}/\S+ \(bound \d+s\)")
        self.assertNotIn("[step ", run.stdout)

    def test_a_trash_that_cannot_be_made_keeps_the_target_and_says_so(self):
        _checkout, target = self.cargo_target("example-no-trash")
        self.trash.parent.parent.mkdir(parents=True)
        self.trash.parent.write_text("a file where a folder goes\n")
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue((target / "CACHEDIR.TAG").is_file())
        self.assertIn(f"- build output prune: could not make a folder in {self.trash}; kept {target}",
                      run.stdout)

    def test_a_running_cargo_skips_the_prune(self):
        # sd:5994 round 7: a cargo that opened .cargo-lock before the move
        # and waits on it would build against the moved copy. While cargo
        # or rustc runs, the prune does nothing, with no finding.
        _checkout, target = self.cargo_target("example-waiting")
        _stub(self.bin / "pgrep", 'case "$*" in *cargo*) echo 4242; exit 0 ;; esac\nexit 1\n')
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue((target / "CACHEDIR.TAG").is_file(), run.stdout + run.stderr)
        self.assertEqual(self.actions(run), [])
        self.assertEqual(self.attention(run), [])
        self.assertFalse(self.trash.exists())

    def test_a_process_probe_that_fails_skips_the_prune_and_says_so(self):
        _checkout, target = self.cargo_target("example-unknown")
        _stub(self.bin / "pgrep", "exit 3\n")
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue((target / "CACHEDIR.TAG").is_file())
        self.assertIn("- build output prune: could not tell whether cargo runs; skipped", run.stdout)

    def stub_move(self, body):
        """Replace the copied prune_move.py; one argument is the sweep's check."""
        (self.tmp / "local-maintenance" / "prune_move.py").write_text(
            "import os, sys\nif len(sys.argv) == 2:\n    sys.exit(0)\n" + body)

    def test_a_target_a_build_took_back_stays_and_is_named(self):
        # prune_move.py 76: a build started during the move, and target/
        # went back. No finding: nothing needs the operator.
        _checkout, target = self.cargo_target("example-returned")
        self.stub_move("sys.exit(76)\n")
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue((target / "CACHEDIR.TAG").is_file())
        self.assertIn(f"- kept idle build output {target}: a build started during the move", run.stdout)
        self.assertEqual(self.attention(run), [])
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_a_target_that_cannot_come_back_stays_in_the_trash_and_says_so(self):
        # prune_move.py 77: target/ moved, a build started, and target/
        # exists again. The moved copy stays undeleted, with a finding.
        _checkout, target = self.cargo_target("example-crowded")
        self.stub_move("os.rename(sys.argv[1], sys.argv[2])\nos.mkdir(sys.argv[1])\n"
                       "print('busy', file=sys.stderr)\nsys.exit(77)\n")
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        [bin_] = list(self.trash.iterdir())
        self.assertTrue((bin_ / "target" / "CACHEDIR.TAG").is_file(), run.stdout)
        self.assertIn(f"- build output prune: {target} did not come back from {bin_} (busy); "
                      "kept until no build holds a lock there", run.stdout)
        self.assertNotIn("removed idle build output", run.stdout)

    def test_a_trash_entry_a_build_still_locks_stays(self):
        # A build that locked inside the moved copy may still run there; the
        # sweep leaves the entry until its lock is free.
        lock = self.trash / "Ab12Cd34" / "target" / "release" / ".cargo-lock"
        lock.parent.mkdir(parents=True)
        lock.write_bytes(b"")
        _hold(self, lock)
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue(lock.is_file(), run.stdout + run.stderr)
        self.assertNotIn("prune trash", run.stdout)
        self.assertEqual(self.attention(run), [])

    def test_no_worktrees_folder_is_a_clean_run(self):
        run = self.maintenance("# nothing to check\n")
        self.assertIn("all checks clean", run.stdout)


class LockWindowTest(unittest.TestCase):
    """sd:5994 round 6: a build locks a new profile after the lock pass.

    `cargo build --release` can make and lock target/release/.cargo-lock
    after prune_move.py took the locks it found and before the rename. The
    tests run the steps of prune_move.py in turn and start that build
    between them.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        self.target = root / "worktrees" / "example" / "target"
        (self.target / "debug" / "deps").mkdir(parents=True)
        (self.target / "debug" / "deps" / "example-0123").write_bytes(b"\0" * 64)
        (self.target / "debug" / ".cargo-lock").write_bytes(b"")
        (self.target / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
        self.bin = root / "prune-trash" / "Ab12Cd34"
        self.bin.mkdir(parents=True)
        self.dst = self.bin / "target"

    def start_release_build(self):
        lock = self.target / "release" / ".cargo-lock"
        lock.parent.mkdir()
        lock.write_bytes(b"")
        _hold(self, lock)

    def test_a_build_that_starts_in_the_window_gets_its_target_back(self):
        held = prune_move.prepass(str(self.target))
        self.addCleanup(prune_move.release, held)
        self.start_release_build()
        code = prune_move.move(str(self.target), str(self.dst), held)
        self.assertTrue((self.target / "CACHEDIR.TAG").is_file(), "target/ not restored")
        self.assertTrue((self.target / "release" / ".cargo-lock").is_file())
        self.assertTrue((self.target / "debug" / "deps" / "example-0123").is_file())
        self.assertFalse(self.dst.exists())
        self.assertEqual(code, prune_move.RESTORED)

    def test_a_build_in_the_window_and_a_new_target_keep_the_trash(self):
        # target/ exists again by the time the check finds the build: the
        # moved copy stays in the trash, undeleted, for the operator.
        held = prune_move.prepass(str(self.target))
        self.addCleanup(prune_move.release, held)
        self.start_release_build()
        os.rename(self.target, self.dst)
        (self.target / "debug").mkdir(parents=True)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = prune_move.settle(str(self.target), str(self.dst), held)
        self.assertEqual(code, prune_move.KEPT)
        self.assertIn("release/.cargo-lock; [Errno", err.getvalue())
        self.assertTrue((self.dst / "release" / ".cargo-lock").is_file())
        self.assertTrue((self.dst / "debug" / "deps" / "example-0123").is_file())
        self.assertEqual(os.listdir(self.target), ["debug"])

    def test_a_quiet_window_moves_the_target(self):
        held = prune_move.prepass(str(self.target))
        self.addCleanup(prune_move.release, held)
        code = prune_move.move(str(self.target), str(self.dst), held)
        self.assertEqual(code, 0)
        self.assertFalse(self.target.exists())
        self.assertTrue((self.dst / "CACHEDIR.TAG").is_file())

    def test_a_lock_the_check_cannot_open_restores_the_target(self):
        # A failed read is no answer: the build may be there, so restore.
        held = prune_move.prepass(str(self.target))
        self.addCleanup(prune_move.release, held)
        lock = self.target / "release" / ".cargo-lock"
        lock.parent.mkdir()
        lock.write_bytes(b"")
        lock.chmod(0)
        code = prune_move.move(str(self.target), str(self.dst), held)
        self.assertEqual(code, prune_move.RESTORED)
        self.assertTrue((self.target / "CACHEDIR.TAG").is_file())


if __name__ == "__main__":
    unittest.main()
