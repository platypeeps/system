"""`run` deletes cargo `target/` folders idle for 3 days under ~/worktrees (sd:5994).

Builders leave one build tree per worktree, several GB each, and a worktree
stays until its branch lands. The checkout and its commits stay; only the
build output goes, and the next build makes it again. A folder counts as
cargo's when it holds CACHEDIR.TAG, which cargo writes and git never tracks.
"""

import os
import pathlib
import time

from tests.test_report import Harness, _stub

FOUR_DAYS = 4 * 86400


def _age(root, seconds):
    """Set every entry under `root`, and `root`, to `seconds` ago."""
    when = time.time() - seconds
    for path in sorted(root.rglob("*"), reverse=True):
        os.utime(path, (when, when), follow_symlinks=False)
    os.utime(root, (when, when))


class TargetPruneTest(Harness):
    def setUp(self):
        super().setUp()
        self.worktrees = self.tmp / "home" / "worktrees"
        self.trash = self.tmp / "home" / ".local" / "state" / "maintenance" / "prune-trash"
        # lsof's answer for "nobody holds these": exit 1, nothing printed.
        self.lsof_calls = self.tmp / "lsof-calls"
        self.lsof("exit 1\n")

    def lsof(self, body):
        _stub(self.bin / "lsof", f'printf "%s\\n" "$*" >> "{self.lsof_calls}"\n' + body)

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

    def test_a_target_a_process_holds_stays(self):
        # sd:5994 review: an old mtime is no proof cargo is not using the
        # folder; a build can take its lock after the idle probe. Just
        # before the rename, a holder of the folder or a .cargo-lock keeps it.
        self.lsof('echo 4242\nexit 0\n')
        _checkout, target = self.cargo_target("example-held")
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue((target / "CACHEDIR.TAG").is_file(), run.stdout + run.stderr)
        self.assertIn(f"- kept idle build output {target}: a process holds it", run.stdout)
        [call] = self.lsof_calls.read_text().splitlines()
        self.assertIn(f"{target}/debug/.cargo-lock", call.split())
        self.assertIn(str(target), call.split())

    def test_a_holder_probe_that_fails_keeps_the_target_and_says_so(self):
        self.lsof('echo "lsof: WARNING: cannot stat" >&2\nexit 1\n')
        _checkout, target = self.cargo_target("example-unsure")
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue((target / "CACHEDIR.TAG").is_file())
        self.assertIn(f"- build output prune: could not tell whether a process holds {target}; kept",
                      run.stdout)

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
        self.assertRegex(run.stderr, rf"\[step [\d:]+\] find {self.worktrees} .* \(bound \d+s\)")
        self.assertRegex(run.stderr, rf"\[step [\d:]+\] find {target} -mmin -4320 .* \(bound \d+s\)")
        self.assertRegex(run.stderr, rf"\[step [\d:]+\] lsof -t -- {target} .* \(bound \d+s\)")
        self.assertRegex(run.stderr, rf"\[step [\d:]+\] rm -rf {self.trash}/\S+ \(bound \d+s\)")
        self.assertNotIn("[step ", run.stdout)

    def test_no_worktrees_folder_is_a_clean_run(self):
        run = self.maintenance("# nothing to check\n")
        self.assertIn("all checks clean", run.stdout)


if __name__ == "__main__":
    import unittest

    unittest.main()
