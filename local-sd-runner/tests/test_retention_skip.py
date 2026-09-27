"""What the second copies leave out, the floor they keep, and the heartbeat they write (sd:1774).

A restart ended a run whose clone held a 30 GB Cargo `target/`. The ignored
inventory digested and copied it file by file, for minutes, the whole-clone
archive took it too, and the runner wrote no heartbeat until the ending
finished, so `runner.sh status` read the row as stale.
"""

import hashlib
import json
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_runner import archive_refresh, reconciliation, restoration, storage

from . import test_runtime as fixtures


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)


class Manifest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clone = Path(self.temp.name).resolve() / "clone"
        self.clone.mkdir()
        git(self.clone, "init", "-q", "-b", "main")
        (self.clone / ".gitignore").write_text("target/\nCLAUDE.local.md\nkept.txt\n")

    def write(self, name, body="bytes\n"):
        path = self.clone / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def test_cargo_target_beside_a_cargo_toml_is_left_out(self):
        self.write("Cargo.toml", "[package]\nname = \"fixture\"\n")
        self.write("target/debug/fixture")
        self.write("crates/inner/Cargo.toml", "[package]\nname = \"inner\"\n")
        self.write("crates/inner/target/release/inner")
        self.write("kept.txt")
        self.assertEqual(sorted(storage.ignored_manifest(self.clone)), ["kept.txt"])

    def test_a_target_with_no_cargo_toml_beside_it_is_kept(self):
        self.write("target/report.txt")
        self.write("docs/target/notes.txt")
        # A Cargo.toml elsewhere does not make an unrelated target Cargo's.
        self.write("crates/inner/Cargo.toml", "[package]\nname = \"inner\"\n")
        self.assertEqual(sorted(storage.ignored_manifest(self.clone)), ["docs/target/notes.txt", "target/report.txt"])

    def test_claude_local_md_at_the_root_is_left_out(self):
        self.write("CLAUDE.local.md", "runner-local override\n")
        self.write("kept.txt")
        self.assertEqual(sorted(storage.ignored_manifest(self.clone)), ["kept.txt"])


class Heartbeat(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db, self.runner = self.fixture.db, self.fixture.runner
        self.fixture.provider.write_text(
            "import subprocess\nfrom pathlib import Path\n"
            "Path('work.txt').write_text('authored work\\n')\n"
            "Path('ignored.txt').write_text('precious ignored bytes\\n')\n"
            "with open('.git/info/exclude', 'a') as exclude:\n    exclude.write('out/\\n')\n"
            "Path('out').mkdir()\n"
            "for index in range(4):\n    Path(f'out/{index}').write_text(str(index))\n"
            "subprocess.run(['git','add','work.txt'],check=True)\n"
            "subprocess.run(['git','commit','-qm','Implement fixture\\n\\nAuthored-with: fixture/fixture'],check=True)\n")

    def test_a_heartbeat_is_written_during_a_long_preserve(self):
        # The row's timestamp has one-second resolution, so the writes are counted, not the stamps.
        first = store.heartbeat(self.db, {"healthy": True, "interval_seconds": self.fixture.config.interval, "pid": 0})
        copying, written = [False], []
        real_preserve, real_heartbeat = storage._preserve_one, store.heartbeat

        def slow(retained, target, name, expected):
            copying[0] = True
            time.sleep(2 * self.fixture.config.interval)
            return real_preserve(retained, target, name, expected)

        def heartbeat(connection, body):
            if copying[0]:
                written.append(body)
            return real_heartbeat(connection, body)

        with patch.object(storage, "_preserve_one", side_effect=slow), patch.object(store, "heartbeat", side_effect=heartbeat):
            result = self.fixture.run_fixture(self.fixture.claim())
        self.assertEqual(result["end_step"], "released", result)
        self.assertGreaterEqual(len(written), 4, "the heartbeat did not move during the copy")
        self.assertTrue(all(body["ending"] == result["id"] for body in written), written)
        self.assertTrue(all(body["healthy"] for body in written), "the last probed health is carried, not invented")
        self.assertTrue(all(body["probes_from"] == first["timestamp"] for body in written), "the probes' own time is kept")



def digest(body):
    return {"sha256": hashlib.sha256(body.encode()).hexdigest(), "mode": 0o644}


class Replay(unittest.TestCase):
    """A manifest stored before the rule, replayed after it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.retained = Path(self.temp.name).resolve() / "clone"
        self.manifest = {}
        for name in ("Cargo.toml", "target/debug/fixture", "docs/target/notes.txt", "kept.txt", "CLAUDE.local.md"):
            path = self.retained / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
            path.chmod(0o644)
            if name != "Cargo.toml":
                self.manifest[name] = digest(name)

    def test_stored_cargo_output_is_not_copied(self):
        storage.preserve_ignored(self.retained, self.manifest)
        copied = sorted(str(path.relative_to(self.retained.parent / "ignored"))
                        for path in (self.retained.parent / "ignored").rglob("*") if path.is_file())
        self.assertEqual(copied, ["docs/target/notes.txt", "kept.txt"])

    def test_an_unsafe_stored_name_is_still_refused(self):
        with self.assertRaisesRegex(store.RunnerRefused, "unsafe preserved-output path"):
            storage.preserve_ignored(self.retained, {"../escape": digest("x")})

    def test_a_copy_that_would_cross_the_floor_is_held_before_it_writes(self):
        needed = len("docs/target/notes.txt") + len("kept.txt")
        free = int(1e9) + needed - 1
        with patch.object(storage, "capacity", return_value={"free": free, "total": 10**12, "device": 0}):
            with self.assertRaises(storage.NoSpace) as caught:
                storage.preserve_ignored(self.retained, self.manifest, floor_gb=1)
        self.assertEqual((caught.exception.copy, caught.exception.needed), ("ignored copy", needed))
        self.assertFalse((self.retained.parent / "ignored").exists(), "nothing is written before the check")
        with patch.object(storage, "capacity", return_value={"free": free + 1, "total": 10**12, "device": 0}):
            storage.preserve_ignored(self.retained, self.manifest, floor_gb=1)
        self.assertEqual((self.retained.parent / "ignored/kept.txt").read_text(), "kept.txt")


class Archive(unittest.TestCase):
    """The whole-clone archive of a dirty clone."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.clone = self.root / "clone"
        self.clone.mkdir()
        git(self.clone, "init", "-q", "-b", "main")
        (self.clone / ".gitignore").write_text("target/\nnode_modules/\nvenv/\n")
        for name in ("Cargo.toml", "src/main.rs", "target/debug/fixture", "node_modules/pkg/index.js",
                     "crates/inner/Cargo.toml", "crates/inner/target/release/inner", "docs/target/notes.txt", "out.log"):
            path = self.clone / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)

    def names(self, archive):
        with tarfile.open(archive) as reader:
            return {name.removeprefix("clone/") for name in reader.getnames()}

    def test_cargo_output_and_caches_are_left_out_and_source_is_kept(self):
        names = self.names(storage.archive(self.clone, self.root / "kept.tar"))
        for kept in ("Cargo.toml", "src/main.rs", "crates/inner/Cargo.toml", "docs/target/notes.txt", "out.log", ".git/HEAD"):
            self.assertIn(kept, names)
        for left in ("target", "target/debug/fixture", "node_modules", "crates/inner/target"):
            self.assertNotIn(left, names)

    def test_a_tracked_file_under_a_cache_name_keeps_its_unstaged_edit(self):
        # Codex review of #634: a directory name alone proves nothing. These
        # bytes live only in the working tree; `.git` cannot bring them back.
        for name in ("venv/source.py", "node_modules/vendored/index.js", "target/keep.rs"):
            path = self.clone / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("committed\n")
            git(self.clone, "add", "-f", name)
        git(self.clone, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "vendored")
        for name in ("venv/source.py", "node_modules/vendored/index.js", "target/keep.rs"):
            (self.clone / name).write_text("unstaged edit\n")
        (self.clone / ".pytest_cache").mkdir()
        (self.clone / ".pytest_cache/untracked").write_text("not ignored\n")
        archive = storage.archive(self.clone, self.root / "kept.tar")
        names = self.names(archive)
        with tarfile.open(archive) as reader:
            for name in ("venv/source.py", "node_modules/vendored/index.js", "target/keep.rs"):
                self.assertEqual(reader.extractfile("clone/" + name).read(), b"unstaged edit\n", name)
        self.assertIn(".pytest_cache/untracked", names, "an untracked file Git does not ignore is archived")
        # Ignored Cargo output and caches beside them are still left out.
        for left in ("target/debug", "node_modules/pkg"):
            self.assertNotIn(left, names)

    def test_git_that_cannot_answer_leaves_nothing_out(self):
        (self.clone / ".git/HEAD").write_text("not a ref\n")
        self.assertIn("target/debug/fixture", self.names(storage.archive(self.clone, self.root / "kept.tar")))

    def test_a_refresh_compares_like_with_like(self):
        archive = storage.archive(self.clone, self.root / "kept.tar")
        skip = storage.archive_skip(self.clone)
        self.assertEqual(archive_refresh._archive_inventory(archive), restoration.inventory(self.clone, skip=skip))

    def test_an_archive_that_would_cross_the_floor_is_held_before_it_writes(self):
        needed = storage.tar_needed(storage.walk(self.clone, skip=storage.archive_skip(self.clone)))
        with patch.object(storage, "capacity", return_value={"free": int(1e9) + needed - 1, "total": 10**12, "device": 0}):
            with self.assertRaises(storage.NoSpace) as caught:
                storage.archive(self.clone, self.root / "kept.tar", floor_gb=1)
        self.assertEqual((caught.exception.copy, caught.exception.needed), ("kept archive", needed))
        self.assertFalse((self.root / "kept.partial").exists())
        self.assertFalse((self.root / "kept.tar").exists())

    def test_the_archive_reports_progress(self):
        calls = []
        storage.archive(self.clone, self.root / "kept.tar", progress=lambda: calls.append(1))
        self.assertGreaterEqual(len(calls), len(self.names(self.root / "kept.tar")))


class Ending(unittest.TestCase):
    """The runner's ending of a Rust run, end to end."""

    def setUp(self):
        self.fixture = fixtures.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db, self.runner, self.config = self.fixture.db, self.fixture.runner, self.fixture.config

    def provide(self, *, dirty):
        self.fixture.provider.write_text(
            "import subprocess\nfrom pathlib import Path\n"
            "Path('Cargo.toml').write_text('[package]\\nname = \"fixture\"\\n')\n"
            "with open('.git/info/exclude', 'a') as exclude:\n    exclude.write('target/\\n')\n"
            "Path('target/debug').mkdir(parents=True)\n"
            "Path('target/debug/fixture').write_text('build output')\n"
            "Path('ignored.txt').write_text('precious ignored bytes\\n')\n"
            "subprocess.run(['git','add','Cargo.toml'],check=True)\n"
            "subprocess.run(['git','commit','-qm','Implement fixture\\n\\nAuthored-with: fixture/fixture'],check=True)\n"
            + ("Path('unfinished.rs').write_text('fn main() {}')\n" if dirty else ""))

    def test_a_stored_manifest_listing_cargo_output_is_replayed_without_it(self):
        self.provide(dirty=False)
        real = storage.ignored_manifest

        def before_the_rule(clone, **kwargs):
            with patch.object(storage, "_cargo_output", return_value=False):
                return real(clone, **kwargs)

        with patch.object(storage, "ignored_manifest", side_effect=before_the_rule):
            result = self.fixture.run_fixture(self.fixture.claim())
        self.assertEqual(result["end_step"], "released", result)
        ignored = Path(result["retained_path"]).parent / "ignored"
        self.assertEqual((ignored / "ignored.txt").read_text(), "precious ignored bytes\n")
        self.assertFalse((ignored / "target").exists(), "stored Cargo output was copied")
        self.assertEqual(sorted(json.loads(result["ignored_manifest"])), ["ignored.txt"])
        (record,) = journal.records(self.config.database)
        self.assertEqual(sorted(json.loads(record["ignored_manifest"])), ["ignored.txt"])

    def test_an_oversized_journal_held_at_the_ignored_copy_shrinks_when_the_ending_resumes(self):
        # Assignment 103: the stored manifest listed about 100k Cargo files, the
        # copy stopped, and recovery-plan blocked the journal as oversized. The
        # runner itself reads the journal with no size limit, so the next
        # ending reaches the replay, which stores the smaller manifest.
        self.provide(dirty=False)
        real = storage.ignored_manifest
        bulk = {f"target/debug/deps/fixture-{index:06d}.rlib": digest(str(index)) for index in range(130000)}

        def before_the_rule(clone, **kwargs):
            return {**real(clone, **kwargs), **bulk}

        def full(retained, manifest, **kwargs):
            raise storage.NoSpace("ignored copy", retained.parent / "ignored", 1)

        with patch.object(storage, "ignored_manifest", side_effect=before_the_rule), \
                patch.object(storage, "kept_ignored", side_effect=lambda retained, manifest: manifest), \
                patch.object(storage, "preserve_ignored", side_effect=full):
            held = self.fixture.run_fixture(self.fixture.claim())
        self.assertEqual(held["end_step"], "retained_clone", held)
        entry = journal.directory(self.config.database) / f"{held['id']}.json"
        self.assertGreater(entry.stat().st_size, reconciliation.MAX_JOURNAL_BYTES)
        (issue,) = reconciliation.plan(self.config)["journal_issues"]
        self.assertEqual(issue["reason"], "unsafe, unowned, linked or oversized journal entry")

        result = self.runner.finish(self.db, held["id"])
        self.assertEqual(result["end_step"], "released", result)
        self.assertLess(entry.stat().st_size, 64 * 1024)
        self.assertEqual(reconciliation.plan(self.config)["journal_issues"], [])
        self.assertEqual(sorted(json.loads(journal.read(entry)["ignored_manifest"])), ["ignored.txt"])

    def test_a_dirty_rust_clone_is_archived_without_its_build_output(self):
        self.provide(dirty=True)
        result = self.fixture.run_fixture(self.fixture.claim())
        self.assertEqual(result["end_step"], "kept", result)
        with tarfile.open(Path(result["retained_path"]).parent / "kept.tar") as reader:
            names = set(reader.getnames())
        self.assertIn("clone/unfinished.rs", names)
        self.assertIn("clone/ignored.txt", names)
        self.assertFalse(any(name.startswith("clone/target") for name in names), sorted(names))

    def test_an_archive_below_the_floor_holds_naming_no_space(self):
        self.provide(dirty=True)
        with patch.object(storage, "capacity", return_value={"free": int(self.config.floor_gb * 1e9), "total": 10**12, "device": 0}):
            result = self.fixture.run_fixture(self.fixture.claim())
        held = json.loads(result["quarantine"])
        self.assertEqual((held["reason"], held["copy"]), ("no space", "kept archive"), result)
        self.assertFalse((Path(result["retained_path"]).parent / "kept.partial").exists())
        self.assertTrue((Path(result["work_path"]) / "unfinished.rs").exists())

    def test_a_heartbeat_is_written_during_a_long_archive(self):
        self.provide(dirty=True)
        store.heartbeat(self.db, {"healthy": True, "interval_seconds": self.config.interval, "pid": 0})
        archiving, written = [False], []
        real_archive, real_heartbeat = storage.archive, store.heartbeat

        def slow(clone, destination, *, progress=None, floor_gb=None):
            def tick():
                archiving[0] = True
                time.sleep(self.config.interval / 2)
                progress()
            return real_archive(clone, destination, progress=tick, floor_gb=floor_gb)

        def heartbeat(connection, body):
            if archiving[0]:
                written.append(body)
            return real_heartbeat(connection, body)

        with patch.object(storage, "archive", side_effect=slow), patch.object(store, "heartbeat", side_effect=heartbeat):
            result = self.fixture.run_fixture(self.fixture.claim())
        self.assertEqual(result["end_step"], "kept", result)
        self.assertGreaterEqual(len(written), 2, "the heartbeat did not move during the archive")
        self.assertTrue(all(body["ending"] == result["id"] for body in written), written)

if __name__ == "__main__":
    unittest.main()
