"""A new Rust clone starts from a copy of its repository's last passing build (sd:1814).

Assignment 114's check spent its whole 900 seconds compiling dependencies
into an empty `target/`. The runner now copies the repository's seed into a
Rust clone's own `target/`, touches every tracked file so the clone's own
crates rebuild, and replaces the seed after a check passes. These tests hold
the file-level rules; a real two-clone Cargo build is in the PR's evidence.
The cases that need a real `cp -c` clonefile copy are macOS-only and live in
tests/macos/test_macos_cargo_seed.py.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_db.writes import create_item
from sd_runner import cargo_seed

from . import test_runtime

OLD = time.time() - 7200


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


class SeedAndRefreshFixture:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.clone = self.root / "clone"
        (self.clone / "src").mkdir(parents=True)
        (self.clone / "Cargo.toml").write_text('[package]\nname = "app"\n')
        (self.clone / "src/main.rs").write_text("fn main() {}\n")
        git(self.root, "init", "-q", str(self.clone))
        git(self.clone, "add", ".")
        git(self.clone, "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture", "commit", "-qm", "base")
        self.seed = cargo_seed.seed_path(self.root / "work", "~/repos/org/app")
        (self.seed / "debug/.fingerprint").mkdir(parents=True)
        self.fingerprint = self.seed / "debug/.fingerprint/app"
        self.fingerprint.write_text("built elsewhere")
        # Built after this clone's checkout: newer than its sources.
        for path in (self.clone / "Cargo.toml", self.clone / "src/main.rs"):
            os.utime(path, (OLD, OLD))
        os.utime(self.fingerprint, (OLD + 60, OLD + 60))


class SeedAndRefresh(SeedAndRefreshFixture, unittest.TestCase):
    def test_a_clone_without_cargo_toml_or_with_a_target_is_not_seeded(self):
        (self.clone / "target").mkdir()
        self.assertFalse(cargo_seed.seed(self.clone, self.seed))
        self.assertEqual(list((self.clone / "target").iterdir()), [])
        (self.clone / "target").rmdir()
        (self.clone / "Cargo.toml").unlink()
        self.assertFalse(cargo_seed.seed(self.clone, self.seed))
        self.assertFalse((self.clone / "target").exists())

    def test_no_seed_is_a_cold_clone(self):
        self.assertFalse(cargo_seed.seed(self.clone, self.root / "missing"))
        self.assertFalse((self.clone / "target").exists())

    def test_a_failed_copy_leaves_no_target(self):
        def partial(argv, **kwargs):
            Path(argv[-1]).mkdir()
            return subprocess.CompletedProcess(argv, 1)
        with patch("sd_runner.cargo_seed.subprocess.run", side_effect=partial):
            self.assertFalse(cargo_seed.seed(self.clone, self.seed))
        self.assertFalse((self.clone / "target").exists())

    def test_a_copy_whose_sources_cannot_be_touched_is_removed(self):
        with patch("sd_runner.cargo_seed.os.utime", side_effect=PermissionError("denied")):
            self.assertFalse(cargo_seed.seed(self.clone, self.seed))
        self.assertFalse((self.clone / "target").exists())

    def test_a_failed_refresh_keeps_the_old_seed(self):
        (self.clone / "target").mkdir()
        with patch("sd_runner.cargo_seed._copy", return_value=False):
            self.assertFalse(cargo_seed.refresh(self.clone, self.seed))
        self.assertEqual(self.fingerprint.read_text(), "built elsewhere")
        self.assertEqual(sorted(p.name for p in self.seed.parent.iterdir()), [self.seed.name])

    def test_distinct_repositories_get_distinct_seeds(self):
        work = self.root / "work"
        one = cargo_seed.seed_path(work, "~/repos/org/tool")
        self.assertNotEqual(one, cargo_seed.seed_path(work, "~/repos/elsewhere/tool"))
        self.assertEqual(one, cargo_seed.seed_path(work, "~/repos/org/tool"))
        self.assertEqual(one.parent, work / cargo_seed.SEEDS)


class RunnerSeedsFixture:
    """Through the runner: a passing check seeds, the next clone starts from it, a failing one never seeds."""

    def setUp(self):
        self.fixture = test_runtime.Fixture(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.config = self.fixture.root, self.fixture.config
        self.seen = self.root / "check-seen.json"
        check = self.root / "check.py"
        # The check reports the files the clone's target held, then builds a dependency into it, as cargo would.
        check.write_text("import json,os,sys\nfrom pathlib import Path\n"
                         "target = Path('target')\n"
                         "before = sorted(str(p.relative_to(target)) for p in target.rglob('*') if p.is_file()) if target.is_dir() else None\n"
                         f"Path({str(self.seen)!r}).write_text(json.dumps({{'before': before, 'env': dict(os.environ)}}))\n"
                         "Path(target, 'debug/.fingerprint').mkdir(parents=True, exist_ok=True)\n"
                         "Path(target, 'debug/deps').mkdir(exist_ok=True)\n"
                         "Path(target, 'debug/deps', f\"lib{os.environ['SD_ASSIGNMENT']}.rlib\").write_text('built')\n"
                         "sys.exit(int(Path('fail').exists()))\n")
        self.check = [sys.executable, str(check)]

    def run_with(self, name, item=None, fail=False, prelude=""):
        files = ["Cargo.toml", ".gitignore", name] + (["fail"] if fail else [])
        self.fixture.provider.write_text(
            "import subprocess\nfrom pathlib import Path\n" + prelude +
            "Path('Cargo.toml').write_text('[package]\\nname = \"fixture\"\\n')\n"
            "Path('.gitignore').write_text('ignored.txt\\n/target/\\n')\n"
            + "".join(f"Path({f!r}).write_text('work\\n')\n" for f in files[2:])
            + f"subprocess.run(['git','add',*{files!r}],check=True)\n"
            "subprocess.run(['git','commit','-qm','Rust work\\n\\nAuthored-with: fixture/fixture'],check=True)\n")
        fixture = self.fixture
        assignment = store.enqueue(fixture.db, [item or fixture.item], who="operator")[0]
        request = store.claim(fixture.db, assignment["id"], owner=fixture.runner.owner,
                              work_root=self.config.work, retention_root=self.config.retention)
        result = fixture.runner.execute(fixture.db, request, command=[sys.executable, str(fixture.provider)],
                                        environment={"PATH": os.environ["PATH"], "HOME": str(self.root)}, check=self.check)
        self.assertEqual(result["end_step"], "released", result)
        return result, json.loads(self.seen.read_text())

    def another(self):
        return create_item(self.fixture.db, kind="task", title="more fixture work", repo=str(self.fixture.checkout),
                           branch="work/item", status="ready")


class RunnerSeeds(RunnerSeedsFixture, unittest.TestCase):
    def test_a_failing_check_never_seeds(self):
        result, _ = self.run_with("one.rs", fail=True)
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertFalse((self.config.work / cargo_seed.SEEDS).exists())

    def test_the_author_session_starts_from_the_seed(self):
        # sd:1816. Only the check was seeded, so the session that wrote the
        # change compiled every dependency cold first. The first run pushes a
        # Cargo.toml, so the second clone is a Rust one before its session.
        saw = self.root / "session-saw.json"
        prelude = ("import json\n"
                   f"Path({str(saw)!r}).write_text(json.dumps(sorted(str(p.relative_to('target')) for p in Path('target').rglob('*') "
                   "if p.is_file()) if Path('target').is_dir() else None))\n")
        # `cp -c` is macOS's; a copy that keeps file times stands in for it here.
        copy = lambda source, destination: bool(shutil.copytree(source, destination, symlinks=True))
        with patch("sd_runner.cargo_seed._copy", side_effect=copy):
            first, _ = self.run_with("one.rs", prelude=prelude)
            self.assertIsNone(json.loads(saw.read_text()), "the first clone had no Cargo.toml before its session")
            second, seen = self.run_with("two.rs", item=self.another(), prelude=prelude)
        self.assertEqual(second["outcome"], "done", second)
        self.assertEqual(json.loads(saw.read_text()), [f"debug/deps/lib{first['id']}.rlib"])
        # The check found the session's `target/`, the seeded one.
        self.assertEqual(seen["before"], [f"debug/deps/lib{first['id']}.rlib"])


if __name__ == "__main__":
    unittest.main()
