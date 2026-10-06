"""`prune-apply` removes exactly the retained clones a prune plan lists (sd:770)."""

import contextlib
import io
import json
import os
import stat
import subprocess
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sd_db import removal
from sd_db import workflow
from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_db.database import connect
from sd_runner import cli, maintenance, storage

from tests import test_runtime as fixtures


def immutable(path):
    return bool(os.lstat(path).st_flags & stat.UF_IMMUTABLE)


@contextlib.contextmanager
def mounted(*paths):
    """Report `paths` as another device, the way a volume mounted there reads."""
    inodes = {os.lstat(path).st_ino for path in paths}
    real = os.stat

    def fake(*args, **kwargs):
        details = real(*args, **kwargs)
        if details.st_ino not in inodes:
            return details
        return os.stat_result((details.st_mode, details.st_ino, details.st_dev + 1, *tuple(details)[3:]),
                              {"st_flags": details.st_flags})

    with patch.object(os, "stat", fake):
        yield


class PruneApply(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.db, self.config = self.fixture.root, self.fixture.db, self.fixture.config
        # Runs before the temporary directory's own cleanup: a failed test
        # must not leave frozen files the cleanup cannot remove.
        self.addCleanup(subprocess.run, ["chflags", "-R", "nouchg", str(self.root)], check=False)
        # The fixture run's ending reads the process table through the
        # runner's `observer` seam; a `ps eww` past even the fixture's 120 s
        # under a loaded gate held it `pending` (sd:2752). The prune's own
        # holder checks below still read the real table.
        self.fixture.runner.observer = lambda run: []
        request = self.fixture.claim()
        self.run_row = self.fixture.run_fixture(request)
        self.assertEqual(self.run_row["end_step"], "released", self.run_row)
        self.clone = Path(self.run_row["retained_path"])
        self.directory = self.clone.parent
        old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
        self.db.execute("UPDATE runner_run SET released_at=? WHERE id=?", (old, self.run_row["id"]))
        self.run_row = store.run_state(self.db, self.run_row["id"])
        (self.directory / "retention.json").write_text(json.dumps({"retained_at": old}))
        (self.directory / "kept.tar").write_bytes(b"an archive the prune never takes")

    def freeze(self):
        storage.freeze(self.clone)
        self.assertTrue(immutable(self.clone))

    def apply(self, fingerprint, **kwargs):
        return maintenance.apply_prune(self.config, self.db, fingerprint=fingerprint, who="operator",
                                       principal="fixture", program="test_prune_apply", **kwargs)

    def i6(self):
        plan = removal.plan_item(self.db, self.fixture.item, home=self.root)
        return [refusal for refusal in plan["refusals"] if refusal["code"] == "I6"]

    def records(self):
        return [dict(row) for row in self.db.execute(
            "SELECT id, status, fields, body FROM item WHERE source='cron-report' AND external_id LIKE 'runner-prune:%'")]

    def test_apply_removes_only_the_planned_clone_and_files_its_record_first(self):
        sibling = self.directory.parent / "2" / "clone"
        sibling.mkdir(parents=True)
        (sibling / "unowned").write_text("no run row names this")
        self.freeze()
        self.assertIn("retained clone still on disk", self.i6()[0]["message"])
        plan = maintenance.plan_prune(self.config, self.db)
        self.assertEqual([(entry["run"], entry["operation"]) for entry in plan["entries"]], [(self.run_row["id"], "remove")])
        config = self.root / "runner.json"
        config.write_text(json.dumps({"database": str(self.config.database), "work": str(self.config.work),
                                      "retention": str(self.config.retention)}))
        filed = []
        remove = maintenance._remove

        def observed(*args):
            filed.append(len(self.records()))
            return remove(*args)

        output = io.StringIO()
        with patch.object(maintenance, "_remove", side_effect=observed), contextlib.redirect_stdout(output):
            code = cli.main(["prune-apply", "--config", str(config), "--fingerprint", plan["fingerprint"], "--who", "operator"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0, result)
        self.assertEqual(filed, [1])
        self.assertEqual(result["removed"], [{"run": self.run_row["id"], "operation": "remove",
                                              "path": str(self.clone), "bytes": plan["entries"][0]["bytes"]}])
        self.assertFalse(os.path.lexists(self.clone))
        self.assertFalse(os.path.lexists(self.directory / ".pruning-clone"))
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()),
                         [".archive.lock", "ignored", "kept.tar", "retention.json"])
        self.assertEqual((self.directory / "ignored" / "ignored.txt").read_text(), "precious ignored bytes\n")
        self.assertEqual((self.directory / "kept.tar").read_bytes(), b"an archive the prune never takes")
        self.assertEqual((sibling / "unowned").read_text(), "no run row names this")
        record, = self.records()
        self.assertEqual(record["id"], result["record"])
        self.assertEqual(record["status"], "done")
        fields = json.loads(record["fields"])
        self.assertEqual((fields["record"], fields["report"]["job"], fields["report"]["run_id"]),
                         ("runner-prune", "runner-prune", plan["fingerprint"]))
        self.assertEqual(fields["report"]["actor"]["who"], "operator")
        text = json.loads(json.loads(record["body"])["text"])
        self.assertEqual(text["entries"][0]["run"], self.run_row["id"])
        self.assertEqual(text["entries"][0]["path"], str(self.clone))
        self.assertEqual(text["entries"][0]["proof"]["run"]["id"], self.run_row["id"])
        # The acceptance, for a clone the plan lists: the sd:754 refusal clears.
        self.assertEqual(self.i6(), [])
        self.assertEqual(maintenance.plan_prune(self.config, self.db)["entries"], [])

    def test_a_younger_clone_is_not_planned_and_not_removed(self):
        young = datetime.now(UTC).isoformat()
        (self.directory / "retention.json").write_text(json.dumps({"retained_at": young}))
        plan = maintenance.plan_prune(self.config, self.db)
        self.assertEqual(plan["entries"], [])
        self.assertEqual(self.apply(plan["fingerprint"])["removed"], [])
        self.assertTrue(self.clone.is_dir())
        self.assertEqual(self.records(), [])

    def test_a_changed_selection_refuses_before_any_record_or_removal(self):
        self.freeze()
        stale = maintenance.plan_prune(self.config, self.db, days=40)["fingerprint"]
        with self.assertRaisesRegex(store.RunnerRefused, "selection changed"):
            self.apply(stale)
        with self.assertRaisesRegex(store.RunnerRefused, "fingerprint of a prune plan"):
            self.apply("not-a-fingerprint")
        self.assertTrue(immutable(self.clone))
        self.assertEqual(self.records(), [])

    def test_off_macos_apply_refuses(self):
        plan = maintenance.plan_prune(self.config, self.db)
        with patch.object(maintenance.sys, "platform", "linux"), self.assertRaisesRegex(store.RunnerRefused, "macOS"):
            self.apply(plan["fingerprint"])
        self.assertTrue(self.clone.is_dir())

    def test_a_linked_assignment_inside_the_root_refuses(self):
        inside = self.config.retention / "moved"
        assignment = self.directory.parent
        assignment.rename(inside)
        assignment.symlink_to(inside)
        self.assertTrue(self.clone.resolve().is_relative_to(self.config.retention.resolve()))
        with self.assertRaisesRegex(store.RunnerRefused, "does not match its owned assignment"):
            maintenance.plan_prune(self.config, self.db)
        self.assertTrue((inside / str(self.run_row["run"]) / "clone").is_dir())

    def test_a_path_outside_the_root_refuses(self):
        outside = self.root / "outside"
        assignment = self.directory.parent
        assignment.rename(outside)
        assignment.symlink_to(outside)
        with self.assertRaisesRegex(store.RunnerRefused, "does not match|escapes"):
            maintenance.plan_prune(self.config, self.db)
        assignment.unlink()
        outside.rename(assignment)
        self.db.execute("UPDATE runner_run SET retained_path=? WHERE id=?", (str(outside / "1" / "clone"), self.run_row["id"]))
        with self.assertRaisesRegex(store.RunnerRefused, "does not match"):
            maintenance.plan_prune(self.config, self.db)
        self.assertTrue(self.clone.is_dir())

    def test_a_linked_clone_refuses(self):
        real = self.directory / "real"
        self.clone.rename(real)
        self.clone.symlink_to(real)
        with self.assertRaisesRegex(store.RunnerRefused, "does not match"):
            maintenance.plan_prune(self.config, self.db)
        self.assertTrue(real.is_dir())

    def test_links_inside_the_clone_are_removed_as_links_and_never_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "kept").write_text("outside the retention root")
        (outside / "file").write_text("a linked file")
        (self.clone / "linked-directory").symlink_to(outside)
        (self.clone / "linked-file").symlink_to(outside / "file")
        subprocess.run(["chflags", "-R", "uchg", str(outside)], check=True)
        self.freeze()
        plan = maintenance.plan_prune(self.config, self.db)
        self.assertEqual(len(self.apply(plan["fingerprint"])["removed"]), 1)
        self.assertFalse(os.path.lexists(self.clone))
        self.assertEqual((outside / "kept").read_text(), "outside the retention root")
        for path in (outside, outside / "kept", outside / "file"):
            self.assertTrue(immutable(path), path)

    def test_a_live_holder_keeps_the_clone(self):
        self.freeze()
        plan = maintenance.plan_prune(self.config, self.db)
        holder = subprocess.Popen(["sleep", "60"], cwd=self.clone)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        deadline = time.monotonic() + 10
        while not maintenance.processes.holders(self.clone) and time.monotonic() < deadline:
            time.sleep(0.1)
        with self.assertRaisesRegex(store.RunnerRefused, "process holders"):
            maintenance._remove(self.config, self.db, plan["entries"][0])
        held = maintenance.plan_prune(self.config, self.db)
        self.assertEqual((held["entries"], held["skipped"][0]["reason"]), ([], "retained clone has process holders"))
        self.assertEqual(self.apply(held["fingerprint"])["removed"], [])
        self.assertTrue(immutable(self.clone))

    def test_a_held_ending_lock_skips_the_entry_and_the_same_plan_finishes_later(self):
        self.freeze()
        plan = maintenance.plan_prune(self.config, self.db)
        ending = self.config.database.parent / "runner-ending" / f"{self.run_row['id']}.lock"
        with journal.lock(ending):
            result = self.apply(plan["fingerprint"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["removed"], [])
        self.assertIn(str(ending), result["skipped"][0]["reason"])
        self.assertTrue(immutable(self.clone))
        again = self.apply(plan["fingerprint"])
        self.assertEqual((again["ok"], again["record"]), (True, result["record"]))
        self.assertEqual(len(self.records()), 1)
        self.assertFalse(os.path.lexists(self.clone))

    def test_a_held_archive_lock_skips_the_clone_instead_of_waiting(self):
        """sd:1217: the plan took this lock blocking, so a read-only plan waited on the refresher.

        The plan runs in a thread with its own connection, so a plan that
        waits fails the assertion instead of hanging the suite.
        """
        self.freeze()
        plans = []

        def planned():
            with contextlib.closing(connect(self.config.database, write=False)) as connection:
                plans.append(maintenance.plan_prune(self.config, connection))

        thread = threading.Thread(target=planned, daemon=True)
        with journal.lock(self.directory / ".archive.lock"):
            thread.start()
            thread.join(timeout=30)
            waiting = thread.is_alive()
        thread.join(timeout=30)
        self.assertFalse(waiting, "the plan waited for the archive lock instead of skipping the clone")
        self.assertEqual(plans[0]["entries"], [])
        self.assertEqual(plans[0]["skipped"], [{"run": self.run_row["id"], "reason": "archive lock is held by another holder"}])
        # The lock is free again: the same plan selects the clone.
        self.assertEqual([entry["run"] for entry in maintenance.plan_prune(self.config, self.db)["entries"]], [self.run_row["id"]])

    def test_a_held_archive_lock_refuses_the_removal(self):
        self.freeze()
        plan = maintenance.plan_prune(self.config, self.db)
        with journal.lock(self.directory / ".archive.lock"), self.assertRaisesRegex(store.RunnerRefused, "lock held"):
            maintenance._remove(self.config, self.db, plan["entries"][0])
        self.assertTrue(immutable(self.clone))

    def test_a_linked_lock_file_refuses(self):
        self.freeze()
        plan = maintenance.plan_prune(self.config, self.db)
        target = self.root / "lock-target"
        ending = self.config.database.parent / "runner-ending" / f"{self.run_row['id']}.lock"
        ending.unlink()  # the run's ending left it; a link replaces it
        ending.symlink_to(target)
        result = self.apply(plan["fingerprint"])
        self.assertIn("unsafe ownership or type", result["skipped"][0]["reason"])
        self.assertFalse(os.path.lexists(target))
        self.assertTrue(immutable(self.clone))
        (self.directory / ".archive.lock").unlink()
        (self.directory / ".archive.lock").symlink_to(target)
        with self.assertRaisesRegex(store.RunnerRefused, "unsafe ownership or type"):
            maintenance.plan_prune(self.config, self.db)
        self.assertFalse(os.path.lexists(target))

    def test_a_removal_that_stops_is_finished_and_nothing_reads_the_leftover_meanwhile(self):
        self.freeze()
        plan = maintenance.plan_prune(self.config, self.db)
        with patch.object(maintenance, "_delete", side_effect=OSError("stopped part way")):
            stopped = self.apply(plan["fingerprint"])
        leftover = self.directory / ".pruning-clone"
        self.assertEqual((stopped["removed"], stopped["skipped"][0]["reason"]), ([], "stopped part way"))
        self.assertEqual(stopped["skipped"][0]["path"], str(leftover))
        self.assertFalse(os.path.lexists(self.clone))
        self.assertTrue(leftover.is_dir())
        with self.assertRaisesRegex(store.RunnerRefused, "stopped part way"):
            storage.restore(self.run_row, self.root / "restored")
        self.assertFalse(os.path.lexists(self.root / "restored"))
        self.assertIn("an interrupted runner prune left", self.i6()[0]["message"])
        finish = maintenance.plan_prune(self.config, self.db)
        entry, = finish["entries"]
        self.assertEqual((entry["operation"], entry["path"]), ("finish", str(leftover)))
        self.assertNotIn("files", entry)
        with patch.object(maintenance.restoration, "inventory", side_effect=AssertionError("read what is left")):
            done = self.apply(finish["fingerprint"])
        self.assertEqual(done["removed"][0]["operation"], "finish")
        self.assertFalse(os.path.lexists(leftover))
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.i6(), [])

    def test_apply_needs_who_on_the_command_line(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["prune-apply", "--fingerprint", "0" * 64])
        plan = maintenance.plan_prune(self.config, self.db)
        config = self.root / "runner.json"
        config.write_text(json.dumps({"database": str(self.config.database), "work": str(self.config.work),
                                      "retention": str(self.config.retention)}))
        error = io.StringIO()
        with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["prune-apply", "--config", str(config), "--fingerprint", plan["fingerprint"], "--who", ""])
        self.assertEqual(code, 1)
        self.assertIn("who must not be blank", error.getvalue())
        with self.assertRaisesRegex(workflow.WorkflowError, "who must not be blank"):
            maintenance.apply_prune(self.config, self.db, fingerprint=plan["fingerprint"], who="  ",
                                    principal="fixture", program="test_prune_apply")
        self.assertTrue(self.clone.is_dir())
        self.assertEqual(self.records(), [])

    def test_a_rename_that_fails_leaves_the_clone_frozen(self):
        self.freeze()
        plan = maintenance.plan_prune(self.config, self.db)
        with patch.object(maintenance.os, "rename", side_effect=OSError("rename refused")):
            result = self.apply(plan["fingerprint"])
        self.assertEqual((result["removed"], result["skipped"][0]["reason"]), ([], "rename refused"))
        self.assertEqual(result["skipped"][0]["path"], str(self.clone))
        self.assertTrue(immutable(self.clone))
        self.assertEqual(maintenance.plan_prune(self.config, self.db)["fingerprint"], plan["fingerprint"])

    def test_a_holder_check_that_times_out_skips_its_entry_and_apply_goes_on(self):
        self.freeze()
        plan = maintenance.plan_prune(self.config, self.db)
        config = self.root / "runner.json"
        config.write_text(json.dumps({"database": str(self.config.database), "work": str(self.config.work),
                                      "retention": str(self.config.retention)}))
        plan_prune, survivors, calls = maintenance.plan_prune, maintenance.processes.survivors, []

        def twice(*args, **kwargs):
            document = plan_prune(*args, **kwargs)
            return {**document, "entries": [*document["entries"], *document["entries"]]}

        def timing_out(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise subprocess.TimeoutExpired(["lsof", "+D"], 20)
            return survivors(*args, **kwargs)

        output = io.StringIO()
        with patch.object(maintenance, "plan_prune", side_effect=twice), \
                patch.object(maintenance.processes, "survivors", side_effect=timing_out), \
                contextlib.redirect_stdout(output):
            code = cli.main(["prune-apply", "--config", str(config), "--fingerprint", plan["fingerprint"], "--who", "operator"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1, result)
        self.assertIn("lsof", result["skipped"][0]["reason"])
        self.assertEqual(len(result["removed"]), 1, result)
        self.assertFalse(os.path.lexists(self.clone))

    def test_a_mount_inside_the_clone_refuses_the_plan_and_names_it(self):
        mount = self.clone / "mnt"
        mount.mkdir()
        (mount / "precious.txt").write_text("on another volume")
        self.freeze()
        with mounted(mount, mount / "precious.txt"), self.assertRaisesRegex(store.RunnerRefused, f"mount point.*{mount}"):
            maintenance.plan_prune(self.config, self.db)
        with mounted(self.clone), self.assertRaisesRegex(store.RunnerRefused, f"mount point.*{self.clone}$"):
            maintenance.plan_prune(self.config, self.db)
        self.assertEqual((mount / "precious.txt").read_text(), "on another volume")
        self.assertTrue(immutable(self.clone))

    def test_a_mount_after_the_plan_refuses_the_entry_before_anything_changes(self):
        mount = self.clone / "mnt"
        mount.mkdir()
        (mount / "precious.txt").write_text("on another volume")
        self.freeze()
        entry, = maintenance.plan_prune(self.config, self.db)["entries"]
        with mounted(mount, mount / "precious.txt"), self.assertRaisesRegex(store.RunnerRefused, f"mount point.*{mount}"):
            maintenance._remove(self.config, self.db, entry)
        self.assertTrue(immutable(self.clone))
        self.assertTrue(immutable(mount / "precious.txt"))
        self.assertFalse(os.path.lexists(self.directory / ".pruning-clone"))
        self.assertEqual((mount / "precious.txt").read_text(), "on another volume")

    def test_every_flag_clear_and_unlink_checks_the_device_again(self):
        tree = self.root / "tree"
        (tree / "a").mkdir(parents=True)
        (tree / "a" / "file").write_text("same volume")
        (tree / "mnt").mkdir()
        (tree / "mnt" / "precious.txt").write_text("on another volume")
        subprocess.run(["chflags", "-R", "uchg", str(tree / "mnt")], check=True)
        device = os.lstat(tree).st_dev
        with mounted(tree / "mnt", tree / "mnt" / "precious.txt"):
            with self.assertRaisesRegex(store.RunnerRefused, "mount point"):
                maintenance._thaw(tree, device)
            self.assertTrue(immutable(tree / "mnt" / "precious.txt"))
            subprocess.run(["chflags", "-R", "nouchg", str(tree / "mnt")], check=True)
            with self.assertRaisesRegex(store.RunnerRefused, "mount point"):
                maintenance._delete(tree, device)
        self.assertEqual((tree / "mnt" / "precious.txt").read_text(), "on another volume")

    def test_a_mount_inside_an_interrupted_leftover_refuses_the_plan(self):
        self.freeze()
        plan = maintenance.plan_prune(self.config, self.db)
        with patch.object(maintenance, "_delete", side_effect=OSError("stopped part way")):
            self.apply(plan["fingerprint"])
        leftover = self.directory / ".pruning-clone"
        mount = leftover / "mnt"
        mount.mkdir()
        (mount / "precious.txt").write_text("on another volume")
        with mounted(mount, mount / "precious.txt"), self.assertRaisesRegex(store.RunnerRefused, f"mount point.*{mount}"):
            maintenance.plan_prune(self.config, self.db)
        self.assertEqual((mount / "precious.txt").read_text(), "on another volume")

    def test_a_tree_swapped_in_after_the_rename_is_not_touched(self):
        self.freeze()
        entry, = maintenance.plan_prune(self.config, self.db)["entries"]
        leftover = self.directory / ".pruning-clone"
        impostor = self.root / "impostor"
        impostor.mkdir()
        (impostor / "kept").write_text("not the planned clone")
        subprocess.run(["chflags", "uchg", str(impostor / "kept")], check=True)
        real = journal.fsync_directory

        def swap(path):
            real(path)
            if impostor.exists():
                leftover.rename(self.root / "stashed")
                impostor.rename(leftover)

        with patch.object(maintenance.journal, "fsync_directory", side_effect=swap), \
                self.assertRaisesRegex(store.RunnerRefused, "changed during the removal"):
            maintenance._remove(self.config, self.db, entry)
        self.assertEqual((leftover / "kept").read_text(), "not the planned clone")
        self.assertTrue(immutable(leftover / "kept"))

    def test_a_leftover_beside_the_planned_clone_refuses_the_removal(self):
        entry, = maintenance.plan_prune(self.config, self.db)["entries"]
        (self.directory / ".pruning-clone").mkdir()
        with self.assertRaisesRegex(store.RunnerRefused, "changed since the prune plan"):
            maintenance._remove(self.config, self.db, entry)
        self.assertTrue(self.clone.is_dir())
        self.assertEqual(os.listdir(self.directory / ".pruning-clone"), [])

    def test_a_clone_replaced_since_the_plan_refuses_the_removal(self):
        entry, = maintenance.plan_prune(self.config, self.db)["entries"]
        self.clone.rename(self.directory / "original")
        self.clone.mkdir()
        (self.clone / "replacement").write_text("not what the plan read")
        with self.assertRaisesRegex(store.RunnerRefused, "changed since the prune plan"):
            maintenance._remove(self.config, self.db, entry)
        self.assertEqual((self.clone / "replacement").read_text(), "not what the plan read")

    def test_a_run_row_changed_since_the_plan_refuses_the_removal(self):
        entry, = maintenance.plan_prune(self.config, self.db)["entries"]
        older = (datetime.now(UTC) - timedelta(days=32)).isoformat()
        self.db.execute("UPDATE runner_run SET released_at=? WHERE id=?", (older, self.run_row["id"]))
        with self.assertRaisesRegex(store.RunnerRefused, "run changed since the prune plan"):
            maintenance._remove(self.config, self.db, entry)
        self.assertTrue(self.clone.is_dir())

    def test_a_leftover_that_is_not_a_directory_refuses_the_plan(self):
        leftover = self.directory / ".pruning-clone"
        leftover.write_text("a file where a leftover tree would be")
        with self.assertRaisesRegex(store.RunnerRefused, "linked or not a directory"):
            maintenance.plan_prune(self.config, self.db)
        leftover.unlink()
        leftover.symlink_to(self.clone)
        with self.assertRaisesRegex(store.RunnerRefused, "linked or not a directory"):
            maintenance.plan_prune(self.config, self.db)
        self.assertTrue(self.clone.is_dir())

    def test_a_clone_and_a_leftover_both_present_are_skipped(self):
        (self.directory / ".pruning-clone").mkdir()
        plan = maintenance.plan_prune(self.config, self.db)
        self.assertEqual((plan["entries"], plan["skipped"][0]["reason"]),
                         ([], "retained clone and an interrupted prune leftover both exist"))

    def test_a_hardlinked_lock_file_refuses(self):
        self.freeze()
        plan = maintenance.plan_prune(self.config, self.db)
        ending = self.config.database.parent / "runner-ending" / f"{self.run_row['id']}.lock"
        os.link(ending, self.root / "second-name")
        result = self.apply(plan["fingerprint"])
        self.assertIn("unsafe ownership or type", result["skipped"][0]["reason"])
        self.assertTrue(immutable(self.clone))

    def test_a_record_past_the_report_bound_refuses_before_filing_or_removing(self):
        self.freeze()
        plan = maintenance.plan_prune(self.config, self.db)
        with patch.object(maintenance.reporting, "MAX_REPORT", 100), \
                self.assertRaisesRegex(store.RunnerRefused, "report bound"):
            self.apply(plan["fingerprint"])
        self.assertEqual(self.records(), [])
        self.assertTrue(immutable(self.clone))


if __name__ == "__main__":
    unittest.main()
