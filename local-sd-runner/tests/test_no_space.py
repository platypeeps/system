"""A second copy that meets ENOSPC waits in `ending` naming `no space`; nothing is removed to make it fit.

Criterion 6 of `docs/work/archive/2026-09/2026-09-05-the-runner-works-the-queue/prd.md`
(`prd.md:1339-1352`): the row stays `ending` naming `no space` with its
clone present and its partial copy in place through ten ticks, Today naming
the floor, the free space and the space needed, every retained clone on the
backup path present throughout, and the copy completing on the tick after
the double reports space. The double is the write itself -- `copyfileobj`
for the ignored copy, `TarFile.add` for the kept archive -- raising
`OSError(ENOSPC)`, the documented point, and never a real full volume.
"""

import errno
import hashlib
import json
import os
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_db import runner_retention as retention
from sd_db.writes import create_item
from sd_runner import maintenance, storage

from . import test_runtime as fixtures

FULL = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


class Volume:
    """A switch the doubles read: full raises ENOSPC at the write, not before it."""

    def __init__(self):
        self.full = True
        self.refusals = 0
        self.real_copy, self.real_add = storage.shutil.copyfileobj, tarfile.TarFile.add

    def copyfileobj(self, reader, writer, *args, **kwargs):
        if self.full:
            self.refusals += 1
            raise FULL
        return self.real_copy(reader, writer, *args, **kwargs)

    def add(self, archive, *args, **kwargs):
        if self.full:
            self.refusals += 1
            raise FULL
        return self.real_add(archive, *args, **kwargs)


def backdate(retained: Path, *, days=31, connection=None, run=None, database=None) -> None:
    """Age a retained clone past the prune cutoff, receipt and row together.

    `plan_prune` gates on `max(retained_at, released_at)`, so a released run
    whose receipt alone is moved stays inside the cutoff and is skipped for
    its release date (sd:1232). A caller that names the connection and the
    run ages both; a run still ending has no release to age.

    The row edit bumps `journal_version` and rewrites the durable journal.
    Without that, `restore_holds` reads a same-version conflict and every
    later tick stops before it retries a waiting copy.
    """
    receipt = retained.parent / "retention.json"
    body = json.loads(receipt.read_text())
    stamp = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    body["retained_at"] = stamp
    receipt.write_text(json.dumps(body))
    if connection is not None:
        connection.execute("UPDATE runner_run SET released_at = ?, journal_version = journal_version + 1 WHERE id = ?", (stamp, run))
        connection.commit()
        journal.persist(database, store.run_state(connection, run))


class Copies(unittest.TestCase):
    """The two second copies, exercised directly: what a retry rewrites and what a full volume reports."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(suffix=".noindex")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.retained = self.root / "clone"
        self.retained.mkdir()

    def manifest(self, name, body):
        path = self.retained / name
        path.write_text(body)
        path.chmod(0o640)
        return {name: {"sha256": hashlib.sha256(body.encode()).hexdigest(), "mode": 0o640}}

    def test_a_retry_leaves_a_file_the_previous_pass_finished(self):
        # sd:1221. `_remaining_ignored` counts a renamed destination as
        # written, and this path rewrote it anyway, needing space the hold
        # said it did not.
        manifest = self.manifest("out.txt", "precious ignored bytes\n")
        storage.preserve_ignored(self.retained, manifest)
        destination = self.root / "ignored/out.txt"
        self.assertEqual(destination.read_text(), "precious ignored bytes\n")
        written = destination.stat().st_mtime_ns
        with patch.object(storage.shutil, "copyfileobj", side_effect=AssertionError("a finished file was rewritten")):
            storage.preserve_ignored(self.retained, manifest)
        self.assertEqual(destination.stat().st_mtime_ns, written)
        self.assertFalse(destination.with_name(destination.name + ".sd-copy-partial").exists())

    def test_a_destination_that_differs_is_still_rewritten(self):
        manifest = self.manifest("out.txt", "precious ignored bytes\n")
        (self.root / "ignored").mkdir()
        (self.root / "ignored/out.txt").write_text("someone else's bytes\n")
        storage.preserve_ignored(self.retained, manifest)
        self.assertEqual((self.root / "ignored/out.txt").read_text(), "precious ignored bytes\n")

    def test_a_full_volume_at_the_flush_is_a_no_space_hold(self):
        # sd:1221. Delayed allocation reports ENOSPC at the fsync, not at the
        # write; outside the translation it fell through as a plain OSError.
        (self.retained / "unfinished").write_text("do not lose this")
        with patch.object(storage.os, "fsync", side_effect=FULL):
            with self.assertRaises(storage.NoSpace) as caught:
                storage.archive(self.retained, self.root / "kept.tar")
        self.assertEqual(caught.exception.copy, "kept archive")
        self.assertTrue((self.root / "kept.partial").exists(), "the partial archive stays for the next tick")

    def test_needed_bytes_cover_the_headers_a_tar_writes(self):
        # sd:1221. The payload alone underestimated the archive, so a hold
        # asked for less space than the next attempt needs.
        for name in ("a.txt", "b.txt"):
            (self.retained / name).write_text("x" * 100)
        payload = 200
        with patch.object(tarfile.TarFile, "add", side_effect=FULL):
            with self.assertRaises(storage.NoSpace) as caught:
                storage.archive(self.retained, self.root / "kept.tar")
        self.assertGreater(caught.exception.needed, payload + 2 * 4096)
        self.assertEqual(caught.exception.needed, storage.tar_needed(storage.walk(self.retained)))


class NoSpace(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.db, self.config, self.runner = self.fixture.root, self.fixture.db, self.fixture.config, self.fixture.runner
        self.volume = Volume()
        # The end run is the subject; the tick's other probes are stubbed so
        # ten ticks read the row and not the host's disks or the pack's HEAD.
        self.report = {"ok": True, "problems": [], "database_below_floor": False, "dispatch_allowed": False}
        self.enterContext(patch.object(storage, "preflight", return_value=self.report))
        self.enterContext(patch.object(fixtures.Runner, "watch_deliveries"))
        self.enterContext(patch.object(fixtures.Runner, "refresh_archives"))

    def full_volume(self):
        self.enterContext(patch.object(storage.shutil, "copyfileobj", side_effect=self.volume.copyfileobj))
        # A plain function on the class binds the TarFile; a bound method would not.
        self.enterContext(patch.object(tarfile.TarFile, "add", lambda archive, *args, **kwargs: self.volume.add(archive, *args, **kwargs)))

    def ticks(self, count):
        for _ in range(count):
            self.runner._tick(self.db)
            yield store.heartbeat_state(self.db)

    def parallel_claim(self, item=None, *, branch="work/second"):
        """A second row on the same repository: the parallel lane, since a `no space` row holds its lease."""
        if item is None:
            fixtures.git(self.fixture.checkout, "branch", branch, "main")
            item = create_item(self.db, kind="task", title="second fixture work", repo=str(self.fixture.checkout), branch=branch, status="ready")
        assignment = store.enqueue(self.db, [item], parallel=True, who="operator")[0]
        return store.claim(self.db, assignment["id"], owner=self.runner.owner, work_root=self.config.work, retention_root=self.config.retention)

    def assert_held(self, run_id, *, copy, partial):
        row = store.run_state(self.db, run_id)
        self.assertEqual(store.queue_state(self.db, row["assignment"])["status"], "ending")
        self.assertIsNone(row["released_at"])
        held = json.loads(row["quarantine"])
        self.assertEqual(held["reason"], "no space")
        self.assertEqual(held["copy"], copy)
        self.assertEqual(held["free_floor_gb"], self.config.floor_gb)
        self.assertIsInstance(held["free_bytes"], int)
        self.assertGreater(held["needed_bytes"], 0)
        self.assertIn("no space", row["detail"])
        self.assertIn(f"free_floor_gb={self.config.floor_gb}", row["detail"])
        self.assertTrue(partial.exists(), partial)
        return row, held

    def test_ignored_copy_waits_naming_no_space_through_ten_ticks_then_completes(self):
        # The oldest released run: its retained clone and ignored copy must stand untouched throughout.
        earlier = self.fixture.run_fixture(self.fixture.claim())
        self.assertEqual(earlier["end_step"], "released", earlier)
        earlier_clone = Path(earlier["retained_path"])
        earlier_copy = earlier_clone.parent / "ignored/ignored.txt"
        backdate(earlier_clone, connection=self.db, run=earlier["id"], database=self.config.database)
        earlier_state = (earlier_copy.read_bytes(), earlier_copy.stat().st_mtime_ns, sorted(str(p) for p in earlier_clone.rglob("*")))

        self.full_volume()
        request = self.parallel_claim()
        result = self.fixture.run_fixture(request)
        self.assertEqual(result["end_step"], "retained_clone", result)
        self.assertEqual(result["outcome"], "done")
        retained = Path(result["retained_path"])
        partial = retained.parent / "ignored/ignored.txt.sd-copy-partial"
        row, held = self.assert_held(result["id"], copy="ignored copy", partial=partial)
        self.assertEqual(held["needed_bytes"], len("precious ignored bytes\n"))
        self.assertEqual(held["path"], str(retained.parent / "ignored"))
        self.assertEqual((retained / "ignored.txt").read_text(), "precious ignored bytes\n")
        self.assertFalse(Path(result["work_path"]).exists())

        refusals = self.volume.refusals
        for heartbeat in self.ticks(10):
            self.assert_held(result["id"], copy="ignored copy", partial=partial)
            self.assertFalse((retained.parent / "ignored/ignored.txt").exists())
            self.assertTrue(earlier_clone.is_dir())
            self.assertEqual((earlier_copy.read_bytes(), earlier_copy.stat().st_mtime_ns, sorted(str(p) for p in earlier_clone.rglob("*"))), earlier_state)
            (hold,) = heartbeat["space_holds"]
            self.assertEqual(hold["run"], result["id"])
            self.assertEqual({hold["free_floor_gb"], hold["copy"]}, {self.config.floor_gb, "ignored copy"})
            self.assertEqual(hold["needed_bytes"], len("precious ignored bytes\n"))
            self.assertIsInstance(hold["free_bytes"], int)
        self.assertEqual(self.volume.refusals, refusals + 10, "the copy is rechecked on every tick")
        # The nightly job's candidates are released rows only: a copy still waiting is never one.
        # The earlier run is released and aged past the cutoff in both dates the plan reads, so
        # the plan selects it and still never names the row whose copy is waiting.
        self.assertEqual([candidate["run"]["id"] for candidate in retention.candidates(self.db)], [earlier["id"]])
        planned = maintenance.plan_prune(self.config, self.db, days=30)["entries"]
        self.assertEqual([entry["run"] for entry in planned], [earlier["id"]])
        self.assertTrue(earlier_clone.is_dir())

        self.volume.full = False
        list(self.ticks(1))
        row = store.run_state(self.db, result["id"])
        self.assertEqual(row["end_step"], "released", row)
        self.assertIsNone(row["quarantine"])
        self.assertIsNotNone(row["released_at"])
        self.assertEqual(store.queue_state(self.db, request["id"])["status"], "done")
        self.assertEqual((retained.parent / "ignored/ignored.txt").read_text(), "precious ignored bytes\n")
        self.assertFalse(partial.exists())
        # The heartbeat is written at the start of a tick, before the end run; the next one reads the release.
        (heartbeat,) = self.ticks(1)
        self.assertEqual(heartbeat["space_holds"], [])

    def test_kept_archive_waits_naming_no_space_then_keeps(self):
        self.fixture.provider.write_text("from pathlib import Path\nPath('unfinished').write_text('do not lose this')\nPath('ignored.txt').write_text('ignored secret')\n")
        self.full_volume()
        result = self.fixture.run_fixture(self.fixture.claim())
        self.assertEqual(result["end_step"], "survivors_clear", result)
        clone, retained = Path(result["work_path"]), Path(result["retained_path"])
        partial = retained.parent / "kept.partial"
        row, held = self.assert_held(result["id"], copy="kept archive", partial=partial)
        self.assertEqual(held["path"], str(partial))
        self.assertGreaterEqual(held["needed_bytes"], len("do not lose this") + len("ignored secret"))
        self.assertEqual((clone / "unfinished").read_text(), "do not lose this")
        self.assertFalse(retained.exists())
        for _ in self.ticks(3):
            self.assert_held(result["id"], copy="kept archive", partial=partial)
            self.assertEqual((clone / "unfinished").read_text(), "do not lose this")
            self.assertFalse((retained.parent / "kept.tar").exists())
        self.volume.full = False
        list(self.ticks(1))
        row = store.run_state(self.db, result["id"])
        self.assertEqual(row["end_step"], "kept", row)
        self.assertIsNone(row["quarantine"])
        self.assertFalse(partial.exists())
        with tarfile.open(retained.parent / "kept.tar") as archive:
            self.assertIn("clone/unfinished", archive.getnames())
        self.assertEqual((clone / "unfinished").read_text(), "do not lose this")

    def test_two_rows_ending_with_no_space_both_wait_and_the_job_removes_neither(self):
        self.full_volume()
        # Both rows are claimed before either ends: a row waiting on space holds its lease and its quarantine
        # refuses a new claim on the repository, so two rows can only end together if they started together.
        requests = self.parallel_claim(self.fixture.item), self.parallel_claim()
        first, second = (self.fixture.run_fixture(request) for request in requests)
        clones = []
        for result in (first, second):
            retained = Path(result["retained_path"])
            self.assert_held(result["id"], copy="ignored copy", partial=retained.parent / "ignored/ignored.txt.sd-copy-partial")
            self.assertEqual((retained / "ignored.txt").read_text(), "precious ignored bytes\n")
            backdate(retained)
            clones.append(retained)
        self.assertEqual(maintenance.plan_prune(self.config, self.db, days=30)["entries"], [])
        list(self.ticks(2))
        self.assertTrue(all(clone.is_dir() for clone in clones))
        self.assertEqual(len(store.heartbeat_state(self.db)["space_holds"]), 2)
        self.volume.full = False
        list(self.ticks(1))
        for result in (first, second):
            row = store.run_state(self.db, result["id"])
            self.assertEqual(row["end_step"], "released", row)
            self.assertEqual((Path(result["retained_path"]).parent / "ignored/ignored.txt").read_text(), "precious ignored bytes\n")
        (heartbeat,) = self.ticks(1)
        self.assertEqual(heartbeat["space_holds"], [])
