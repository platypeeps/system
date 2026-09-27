"""The cargo seed rules that need a real clonefile copy (sd:1814).

`cargo_seed` copies with `/bin/cp -c`, which only macOS has, so these cases
cannot pass on the Linux CI runner. tests/run-macos-only.sh runs them on a Mac;
the rules that hold on any platform stay in tests/test_cargo_seed.py.
"""

import os
import unittest

from sd_runner import cargo_seed

from tests.test_cargo_seed import OLD, RunnerSeedsFixture, SeedAndRefreshFixture, git


class SeedAndRefreshClonefile(SeedAndRefreshFixture, unittest.TestCase):
    def test_the_clone_gets_its_own_copy_and_every_tracked_file_is_newer_than_it(self):
        self.assertTrue(cargo_seed.seed(self.clone, self.seed))
        copied = self.clone / "target/debug/.fingerprint/app"
        self.assertEqual(copied.read_text(), "built elsewhere")
        self.assertEqual(int(copied.stat().st_mtime), int(OLD + 60))
        self.assertNotEqual(copied.stat().st_ino, self.fingerprint.stat().st_ino)
        for path in (self.clone / "Cargo.toml", self.clone / "src/main.rs"):
            self.assertGreater(path.stat().st_mtime, copied.stat().st_mtime, path)
        self.assertEqual(git(self.clone, "status", "--porcelain", "--untracked-files=no"), "")

    def test_refresh_renames_a_new_copy_into_place(self):
        (self.clone / "target/debug/deps").mkdir(parents=True)
        (self.clone / "target/debug/.fingerprint").mkdir()
        (self.clone / "target/debug/deps/libdep-1.rlib").write_text("passing build")
        self.assertTrue(cargo_seed.refresh(self.clone, self.seed))
        self.assertEqual((self.seed / "debug/deps/libdep-1.rlib").read_text(), "passing build")
        self.assertFalse((self.seed / "debug/.fingerprint/app").exists())
        self.assertEqual(sorted(p.name for p in self.seed.parent.iterdir()), [self.seed.name])

    def test_the_seed_keeps_dependency_artifacts_and_no_final_output(self):
        # A seed holding a binary its branch later removes would let a clone
        # run that binary after a build that no longer makes it.
        target = self.clone / "target"
        kept = ["debug/.fingerprint/dep-1/lib-dep", "debug/build/dep-1/build-script-build",
                "debug/deps/libdep-1.rlib", "debug/deps/libdep-1.rmeta", "debug/deps/dep-1.d",
                "aarch64-apple-darwin/release/.fingerprint/dep-2/lib-dep",
                "aarch64-apple-darwin/release/deps/libdep-2.rlib"]
        dropped = ["debug/tool", "debug/tool.d", "debug/deps/tool-1", "debug/deps/app-1",
                   "debug/deps/tool-1.dSYM/Contents/Info.plist", "debug/examples/demo",
                   "debug/incremental/app-1/s-1/dep-graph.bin", "aarch64-apple-darwin/release/tool",
                   "doc/app/index.html", ".rustc_info.json", "CACHEDIR.TAG"]
        for name in kept + dropped:
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            (target / name).write_text(name)
        self.assertTrue(cargo_seed.refresh(self.clone, self.seed))
        self.assertEqual([n for n in kept if not (self.seed / n).is_file()], [])
        self.assertEqual([n for n in dropped if os.path.lexists(self.seed / n)], [])
        self.assertTrue((target / "debug/tool").is_file(), "the clone's own target/ is untouched")

    def test_a_seed_written_before_pruning_is_pruned_in_the_clone(self):
        # A seed that an older runner wrote whole still holds final outputs;
        # the clone's copy drops them before any build can run them.
        for name in ("debug/tool", "debug/deps/tool-1", "debug/deps/libdep-1.rlib"):
            (self.seed / name).parent.mkdir(parents=True, exist_ok=True)
            (self.seed / name).write_text(name)
        self.assertTrue(cargo_seed.seed(self.clone, self.seed))
        target = self.clone / "target"
        self.assertFalse(os.path.lexists(target / "debug/tool"))
        self.assertFalse(os.path.lexists(target / "debug/deps/tool-1"))
        self.assertTrue((target / "debug/deps/libdep-1.rlib").is_file())
        self.assertTrue((self.seed / "debug/tool").is_file(), "the seed itself is read, not changed")


class RunnerSeedsClonefile(RunnerSeedsFixture, unittest.TestCase):
    def test_a_passing_check_seeds_the_next_clone_of_the_repository(self):
        first, seen = self.run_with("one.rs")
        self.assertIsNone(seen["before"])
        self.assertNotIn("CARGO_TARGET_DIR", seen["env"])
        seed = cargo_seed.seed_path(self.config.work, first["repo"])
        self.assertEqual((seed / "debug/deps" / f"lib{first['id']}.rlib").read_text(), "built")
        second, again = self.run_with("two.rs", item=self.another())
        self.assertEqual(again["before"], [f"debug/deps/lib{first['id']}.rlib"])
        self.assertEqual(sorted(p.name for p in (seed / "debug/deps").iterdir()), sorted([f"lib{first['id']}.rlib", f"lib{second['id']}.rlib"]))


if __name__ == "__main__":
    unittest.main()
