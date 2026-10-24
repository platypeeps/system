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
        self.assertFalse((target.parent / "target.idle-prune").exists())
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

    def test_a_removal_stopped_partway_is_finished_next_run(self):
        # The folder is renamed aside before it is deleted, so a run killed
        # during the delete leaves a name only this step makes, and the next
        # run removes it whatever its age or marker.
        checkout, _target = self.cargo_target("example-stopped")
        aside = checkout / "target.idle-prune"
        (checkout / "target").rename(aside)
        (aside / "CACHEDIR.TAG").unlink()
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse(aside.exists(), run.stdout + run.stderr)
        self.assertIn(f"- removed {aside}, left by an earlier run", run.stdout)

    def test_an_idle_target_beside_a_leftover_aside_goes_with_it(self):
        # The rename lands inside a leftover target.idle-prune; the one
        # delete then takes both, and the leftover's turn finds nothing.
        checkout, target = self.cargo_target("example-both")
        aside = checkout / "target.idle-prune"
        (aside / "debug").mkdir(parents=True)
        run = self.maintenance("# nothing to check\n")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse(target.exists())
        self.assertFalse(aside.exists(), run.stdout + run.stderr)
        self.assertNotIn("could not remove", run.stdout)

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
        self.assertRegex(run.stderr, rf"\[step [\d:]+\] rm -rf {target}.idle-prune \(bound \d+s\)")
        self.assertNotIn("[step ", run.stdout)

    def test_no_worktrees_folder_is_a_clean_run(self):
        run = self.maintenance("# nothing to check\n")
        self.assertIn("all checks clean", run.stdout)


if __name__ == "__main__":
    import unittest

    unittest.main()
