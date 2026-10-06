"""`retained-remove` removes one released assignment's retained copy early, with the operator's name (sd:1780).

Every test runs against a temporary retention root and a fixture database.
Nothing here reaches the live retention volume or the live store. The user
immutable flag is real in every test, so this module is macOS-only: CI runs on
Linux, and tests/run-macos-only.sh runs it on a Mac; none is skipped.
"""

import contextlib
import io
import json
import os
import stat
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_runner import cli, maintenance, storage

from tests import test_runtime as fixtures

ACTOR = {"who", "principal", "program", "pid", "ppid", "session"}


def immutable(path):
    return bool(os.lstat(path).st_flags & stat.UF_IMMUTABLE)


class RetainedRemove(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        # As in test_macos_prune_apply: the fixture run's ending reads no
        # process table, which a loaded gate held `pending` (sd:2752).
        self.fixture.runner.observer = lambda run: []
        self.root, self.db, self.config = self.fixture.root, self.fixture.db, self.fixture.config
        # Runs before the temporary directory's own cleanup: a failed test
        # must not leave frozen files the cleanup cannot remove.
        self.addCleanup(subprocess.run, ["chflags", "-R", "nouchg", str(self.root)], check=False)
        self.config_file = self.root / "runner.json"
        self.config_file.write_text(json.dumps({"database": str(self.config.database), "work": str(self.config.work),
                                                "retention": str(self.config.retention)}))

    def released(self):
        run = self.fixture.run_fixture(self.fixture.claim())
        self.assertEqual(run["end_step"], "released", run)
        clone = Path(run["retained_path"])
        self.assertEqual(clone, self.config.retention.resolve() / str(run["assignment"]) / str(run["run"]) / "clone")
        (clone.parent / "retention.json").write_text(json.dumps({"retained_at": run["released_at"]}))
        (clone.parent / "kept.tar").write_bytes(b"an archive that goes with its assignment")
        return run, clone.parents[1]

    def cli(self, *argv):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(["retained-remove", "--config", str(self.config_file), *argv])
        return code, output.getvalue(), errors.getvalue()

    def remove(self, assignment, who="operator", *, clone_only=False):
        return maintenance.remove_retained(self.config, self.db, assignment, who=who, clone_only=clone_only,
                                           principal="fixture", program="test_retained_remove")

    def records(self):
        return [dict(row) for row in self.db.execute(
            "SELECT id, status, fields, body FROM item WHERE source='cron-report' "
            "AND external_id LIKE 'runner-retained-remove:%'")]

    def refused(self, assignment, message, *, who="operator"):
        # Every refusal holds for both scopes: `--clone-only` skips no check (sd:1793).
        for clone_only in (False, True):
            with self.subTest(clone_only=clone_only), self.assertRaisesRegex(store.RunnerRefused, message):
                self.remove(assignment, who=who, clone_only=clone_only)
        self.assertEqual(self.records(), [])

    def outputs(self, clone):
        """Give the attempt the outputs `--clone-only` keeps; return them with their bytes."""
        attempt = clone.parent
        for name in ("archives", "ignored"):
            (attempt / name / "nested").mkdir(parents=True, exist_ok=True)
        (attempt / "archives" / "nested" / "one.tar").write_bytes(b"a refreshed archive")
        (attempt / "ignored" / "nested" / "cache.bin").write_bytes(b"a preserved ignored output")
        kept = [attempt / "kept.tar", attempt / "retention.json", attempt / "archives" / "nested" / "one.tar",
                attempt / "ignored" / "nested" / "cache.bin"]
        return {path: path.read_bytes() for path in kept}

    def test_removes_only_its_assignment_and_records_the_actor(self):
        run, directory = self.released()
        sibling = directory.parent / str(run["assignment"] + 1) / "1" / "clone"
        sibling.mkdir(parents=True)
        (sibling / "unowned").write_text("another assignment's retained bytes")
        code, output, errors = self.cli("--assignment", str(run["assignment"]), "--who", "operator")
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertEqual((result["assignment"], result["path"], result["runs"]),
                         (run["assignment"], str(directory), [run["id"]]))
        self.assertEqual((result["removed"], result["left"]), ([str(directory / str(run["run"]))], []))
        self.assertGreater(result["bytes"], 0)
        self.assertEqual(set(result["actor"]), ACTOR)
        self.assertEqual((result["actor"]["who"], result["actor"]["program"]), ("operator", "runner.sh retained-remove"))
        self.assertFalse(os.path.lexists(directory))
        self.assertEqual((sibling / "unowned").read_text(), "another assignment's retained bytes")
        record, = self.records()
        self.assertEqual((record["id"], record["status"]), (result["record"], "done"))
        fields = json.loads(record["fields"])
        self.assertEqual((fields["record"], fields["report"]["job"]), ("runner-retained-remove", "runner-retained-remove"))
        # The same shape `prune-apply` records its actor in.
        self.assertEqual(set(fields["report"]["actor"]), ACTOR)
        self.assertEqual(fields["report"]["actor"]["who"], "operator")
        text = json.loads(json.loads(record["body"])["text"])
        self.assertEqual((text["assignment"], text["path"], text["runs"][0]["id"]),
                         (run["assignment"], str(directory), run["id"]))

    def test_removes_a_frozen_copy_and_its_frozen_directory(self):
        run, directory = self.released()
        clone = Path(run["retained_path"])
        storage.freeze(clone)
        kept = clone.parent / "kept.tar"
        os.chflags(kept, os.lstat(kept).st_flags | stat.UF_IMMUTABLE)
        os.chflags(directory, os.lstat(directory).st_flags | stat.UF_IMMUTABLE)
        self.assertTrue(all(immutable(path) for path in (clone, clone / "README", kept, directory)))
        result = self.remove(run["assignment"])
        self.assertEqual(result["path"], str(directory))
        self.assertFalse(os.path.lexists(directory))

    def test_a_stop_part_way_leaves_no_clone_and_the_same_command_finishes(self):
        run, directory = self.released()
        storage.freeze(Path(run["retained_path"]))
        with patch.object(maintenance, "_delete", side_effect=OSError("stopped part way")), \
                self.assertRaisesRegex(OSError, "stopped part way"):
            self.remove(run["assignment"])
        # A restore refuses a `.pruning-clone`; it never reads a partial clone.
        self.assertFalse(os.path.lexists(run["retained_path"]))
        self.assertTrue((Path(run["retained_path"]).parent / ".pruning-clone").is_dir())
        first, = self.records()
        result = self.remove(run["assignment"])
        self.assertFalse(os.path.lexists(directory))
        self.assertEqual(result["record"], first["id"])

    def test_a_run_claimed_after_the_check_keeps_its_attempt_and_the_assignment_directory(self):
        run, directory = self.released()
        newer = directory / str(run["run"] + 1)
        survivors = maintenance.processes.survivors

        def claimed_meanwhile(observed):
            # A requeue and claim between the check and the delete: `claim`
            # takes none of this command's locks, so nothing holds it off.
            if not newer.exists():
                (newer / "clone").mkdir(parents=True)
                (newer / "clone" / "work").write_text("an unreleased run's retained bytes")
                (newer / "kept.tar").write_bytes(b"an unreleased run's archive")
                storage.freeze(newer / "clone")
            return survivors(observed)

        with patch.object(maintenance.processes, "survivors", side_effect=claimed_meanwhile):
            result = self.remove(run["assignment"])
        self.assertFalse(os.path.lexists(directory / str(run["run"])))
        self.assertEqual((newer / "clone" / "work").read_text(), "an unreleased run's retained bytes")
        self.assertEqual((newer / "kept.tar").read_bytes(), b"an unreleased run's archive")
        self.assertTrue(immutable(newer / "clone"))
        self.assertTrue(directory.is_dir())
        self.assertEqual((result["removed"], result["left"]), ([str(directory / str(run["run"]))], [newer.name]))

    def test_the_whole_scope_is_named_in_its_result_and_record(self):
        run, directory = self.released()
        result = self.remove(run["assignment"])
        self.assertEqual((result["scope"], result["kept"]), ("attempt", []))
        record, = self.records()
        text = json.loads(json.loads(record["body"])["text"])
        self.assertEqual((text["scope"], text["remove"], text["keep"]),
                         ("attempt", [str(directory / str(run["run"]))], []))

    def test_clone_only_removes_the_clone_and_keeps_every_other_output(self):
        run, directory = self.released()
        clone = Path(run["retained_path"])
        kept = self.outputs(clone)
        code, output, errors = self.cli("--assignment", str(run["assignment"]), "--who", "operator", "--clone-only")
        self.assertEqual(code, 0, errors)
        result = json.loads(output)
        self.assertEqual((result["scope"], result["removed"]), ("clone", [str(clone)]))
        self.assertFalse(os.path.lexists(clone))
        self.assertTrue(clone.parent.is_dir())
        self.assertTrue(directory.is_dir())
        self.assertEqual({path: path.read_bytes() for path in kept}, kept)
        for name in ("archives", "ignored", "kept.tar", "retention.json"):
            self.assertIn(str(clone.parent / name), result["kept"])
        self.assertNotIn(str(clone), result["kept"])
        self.assertEqual(result["left"], [str(run["run"])])
        self.assertGreater(result["bytes"], 0)
        record, = self.records()
        self.assertEqual((record["id"], record["status"]), (result["record"], "done"))
        text = json.loads(json.loads(record["body"])["text"])
        self.assertEqual((text["scope"], text["remove"], text["keep"]), ("clone", [str(clone)], result["kept"]))

    def test_clone_only_keeps_the_immutable_flag_on_what_it_keeps(self):
        run, directory = self.released()
        clone = Path(run["retained_path"])
        kept = self.outputs(clone)
        storage.freeze(clone)
        attempt = clone.parent
        # A frozen directory takes no new entry, so its lock file exists first, as a refresh leaves it.
        with journal.lock(attempt / ".archive.lock", blocking=False, noun="archive"):
            pass
        frozen = [*kept, attempt / "archives", attempt / "ignored", attempt / "archives" / "nested",
                  attempt / "ignored" / "nested", attempt, directory]
        for path in frozen:
            os.chflags(path, os.lstat(path).st_flags | stat.UF_IMMUTABLE)
        result = self.remove(run["assignment"], clone_only=True)
        self.assertEqual(result["removed"], [str(clone)])
        self.assertFalse(os.path.lexists(clone))
        self.assertEqual([path for path in frozen if not immutable(path)], [])
        self.assertEqual({path: path.read_bytes() for path in kept}, kept)

    def test_clone_only_finishes_a_pruning_clone_a_stop_left(self):
        run, directory = self.released()
        clone = Path(run["retained_path"])
        leftover = clone.parent / ".pruning-clone"
        kept = self.outputs(clone)
        storage.freeze(clone)
        with patch.object(maintenance, "_delete", side_effect=OSError("stopped part way")), \
                self.assertRaisesRegex(OSError, "stopped part way"):
            self.remove(run["assignment"], clone_only=True)
        self.assertFalse(os.path.lexists(clone))
        self.assertTrue(leftover.is_dir())
        first, = self.records()
        self.assertEqual(json.loads(json.loads(first["body"])["text"])["remove"], [str(clone)])
        result = self.remove(run["assignment"], clone_only=True)
        self.assertEqual(result["removed"], [str(leftover)])
        self.assertFalse(os.path.lexists(leftover))
        self.assertEqual({path: path.read_bytes() for path in kept}, kept)
        # The finish is its own record, naming the path it removed.
        finish, = [record for record in self.records() if record["id"] != first["id"]]
        self.assertEqual(finish["id"], result["record"])
        self.assertEqual(json.loads(json.loads(finish["body"])["text"])["remove"], [str(leftover)])
        # Nothing is left to remove: a replay files nothing and changes nothing.
        again = self.remove(run["assignment"], clone_only=True)
        self.assertEqual((again["removed"], again["record"], again["bytes"]), ([], None, 0))
        self.assertEqual(len(self.records()), 2)
        self.assertEqual({path: path.read_bytes() for path in kept}, kept)

    def test_clone_only_removes_a_clone_and_an_older_pruning_clone_together(self):
        run, directory = self.released()
        clone = Path(run["retained_path"])
        leftover = clone.parent / ".pruning-clone"
        (leftover / "part").mkdir(parents=True)
        (leftover / "part" / "bytes").write_text("an earlier stopped removal")
        storage.freeze(leftover)
        storage.freeze(clone)
        kept = self.outputs(clone)
        result = self.remove(run["assignment"], clone_only=True)
        self.assertEqual(result["removed"], [str(clone), str(leftover)])
        self.assertFalse(os.path.lexists(clone) or os.path.lexists(leftover))
        self.assertEqual({path: path.read_bytes() for path in kept}, kept)

    def test_refuses_a_process_holder_in_either_scope(self):
        run, directory = self.released()
        with patch.object(maintenance.processes, "survivors", return_value=[{"pid": 1}]):
            self.refused(run["assignment"], "retained attempt has process holders")
        self.assertTrue(Path(run["retained_path"]).is_dir())

    def test_refuses_a_held_archive_lock_in_either_scope(self):
        run, directory = self.released()
        with journal.lock(Path(run["retained_path"]).parent / ".archive.lock", blocking=False, noun="archive"):
            self.refused(run["assignment"], "lock held: .*/.archive.lock")
        self.assertTrue(Path(run["retained_path"]).is_dir())

    def test_refuses_an_unreleased_run(self):
        request = self.fixture.claim()
        directory = self.config.retention / str(request["id"])
        directory.mkdir(parents=True)
        (directory / "bytes").write_text("an active run's retained directory")
        self.refused(request["id"], "unreleased runs: " + request["run"]["id"])
        self.assertTrue((directory / "bytes").is_file())

    def test_refuses_when_a_second_run_of_the_assignment_is_unreleased(self):
        run, directory = self.released()
        second = uuid.uuid4().hex
        columns = [row[1] for row in self.db.execute("PRAGMA table_info(runner_run)")]
        values = {**store.run_state(self.db, run["id"]), "id": second, "run": run["run"] + 1, "released_at": None,
                  "retained_path": str(directory / str(run["run"] + 1) / "clone")}
        self.db.execute(f"INSERT INTO runner_run({','.join(columns)}) VALUES({','.join('?' * len(columns))})",
                        [values[name] for name in columns])
        self.db.commit()
        self.refused(run["assignment"], "unreleased runs: " + second)
        self.assertTrue(Path(run["retained_path"]).is_dir())

    def test_refuses_a_journal_issue_for_its_run(self):
        run, directory = self.released()
        (journal.directory(self.config.database) / f"{run['id']}.partial").write_text("an interrupted journal write")
        self.refused(run["assignment"], "journal issues")
        self.assertTrue(Path(run["retained_path"]).is_dir())

    def test_refuses_a_recovery_entry_for_its_run(self):
        run, directory = self.released()
        # The journal now carries a newer version than the row: a database-from-journal entry.
        self.db.execute("UPDATE runner_run SET journal_version=journal_version-1 WHERE id=?", (run["id"],))
        self.db.commit()
        self.refused(run["assignment"], "recovery plan has an entry for run " + run["id"])
        self.assertTrue(Path(run["retained_path"]).is_dir())

    def test_refuses_a_missing_or_blank_who(self):
        run, directory = self.released()
        code, output, errors = self.cli("--assignment", str(run["assignment"]))
        self.assertEqual((code, output), (1, ""))
        self.assertIn("needs --who naming the operator", errors)
        self.refused(run["assignment"], "needs --who", who="  ")
        self.assertTrue(Path(run["retained_path"]).is_dir())

    def test_refuses_an_assignment_that_is_not_a_positive_integer(self):
        run, directory = self.released()
        for value in ("0", "-1", f"{run['assignment']}/..", "", None, True):
            self.refused(value, "positive integer")
        code, _, errors = self.cli("--who", "operator")
        self.assertEqual(code, 1)
        self.assertIn("positive integer", errors)
        self.assertTrue(Path(run["retained_path"]).is_dir())

    def test_refuses_a_symlinked_assignment_pointing_outside(self):
        run, directory = self.released()
        outside = self.root / "outside"
        os.rename(directory, outside)
        directory.symlink_to(outside, target_is_directory=True)
        self.refused(run["assignment"], "symlink")
        self.assertTrue(directory.is_symlink())
        self.assertTrue((outside / str(run["run"]) / "clone" / "README").is_file())

    def test_refuses_a_nonexistent_assignment(self):
        run, directory = self.released()
        stray = directory.parent / "999"
        stray.mkdir()
        self.refused(999, "assignment 999 has no runner run")
        self.assertTrue(stray.is_dir())
        # A released assignment whose directory is gone is refused by name, too.
        subprocess.run(["chflags", "-R", "nouchg", str(directory)], check=True)
        subprocess.run(["rm", "-rf", str(directory)], check=True)
        self.refused(run["assignment"], f"assignment {run['assignment']} has no retained copy")


if __name__ == "__main__":
    unittest.main()
