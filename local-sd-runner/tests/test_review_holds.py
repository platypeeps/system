"""Review regressions use isolated databases and never inspect host processes."""

import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from xml.parsers.expat import ExpatError

from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_runner import cli, reconciliation, runtime
from tests import test_probe_holds as probes
from tests.test_probe_holds import LiveChild, LoopFinished


class ReviewHolds(unittest.TestCase):
    setUp = probes.ProbeHolds.setUp
    assert_owned_unchanged = probes.ProbeHolds.assert_owned_unchanged
    assert_transient_probe_hold = probes.ProbeHolds.assert_transient_probe_hold

    def test_malformed_diskutil_xml_retains_owner_and_recovers(self):
        self.assert_transient_probe_hold(ExpatError("truncated diskutil XML"))

    def test_low_floor_stop_failures_are_visible_and_other_groups_are_attempted(self):
        self.preflight.return_value = {**self.report, "database_below_floor": True}
        other = {**self.run, "id": "f" * 32}
        for error in (subprocess.TimeoutExpired(["ps"], 10),
                      store.RunnerRefused("unsupported process identity"),
                      ProcessLookupError("group exited before signal")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(store, "active_runs", return_value=[self.run, other]):
                self.terminate.reset_mock()
                self.terminate.side_effect = [error, True]
                pulse = self.runner.pulse(self.db)
                self.assertEqual(self.terminate.call_count, 2)
                self.assertFalse(pulse["healthy"])
                self.assertFalse(pulse["storage"]["dispatch_allowed"])
                self.assertIn(str(error), json.dumps(pulse["probe_holds"]))
                self.assertEqual(store.run_state(self.db, self.run["id"]), self.run)

    def test_restore_stop_timeout_is_a_visible_hold(self):
        newer = self.runner.persist(self.db, self.run["id"], start_step="supervised")
        self.db.execute("UPDATE runner_run SET journal_version=0 WHERE id=?", (self.run["id"],))
        self.terminate.side_effect = subprocess.TimeoutExpired(["ps"], 10)
        before = self.journal_path.read_bytes()
        self.assertEqual(self.runner.tick(self.db), [])
        heartbeat = store.heartbeat_state(self.db)
        self.assertFalse(heartbeat["healthy"])
        self.assertIn("ps", json.dumps(heartbeat))
        self.assertEqual(self.journal_path.read_bytes(), before)
        self.assertEqual(journal.read(self.journal_path), newer)
        self.claim.assert_not_called()

    def test_deferred_corrupt_journal_holds_owner_without_replaying(self):
        self.runner.pending_endings[self.run["id"]] = {
            "run": self.run, "outcome": "blocked", "detail": "fixture ending", "exit_code": None}
        self.journal_path.write_bytes(b"{")
        child = LiveChild()
        self.runner.supervisors["e" * 32] = child

        def pause(_):
            pulse = store.heartbeat_state(self.db)
            self.assertFalse(pulse["healthy"])
            self.assertIn("runner journal is unreadable", json.dumps(pulse))
            with self.assertRaisesRegex(store.RunnerRefused, "ownership lock held"):
                with journal.lock(self.fixture.database.parent / "runner.lock", blocking=False):
                    self.fail("owner was released")
            self.assertEqual(self.journal_path.read_bytes(), b"{")
            self.assertIn(self.run["id"], self.runner.pending_endings)
            self.assertEqual(child.waits, [])
            self.terminate.assert_not_called()
            self.claim.assert_not_called()
            raise LoopFinished

        with patch.object(self.runner, "recover", return_value=[]), \
             patch.object(runtime.time, "sleep", side_effect=pause):
            with self.assertRaises(LoopFinished):
                self.runner.serve()

    def test_once_process_observer_failure_keeps_owner_until_worker_finishes(self):
        worker = Mock()
        worker.is_alive.side_effect = [True, True, False]
        self.runner.threads[self.run["id"]] = worker
        self.preflight.side_effect = [self.report, {**self.report, "database_below_floor": True}, self.report]
        self.terminate.side_effect = subprocess.TimeoutExpired(["ps"], 10)
        pulses = []
        actual_pulse = self.runner.pulse

        def observed(connection):
            value = actual_pulse(connection)
            pulses.append(value)
            with self.assertRaisesRegex(store.RunnerRefused, "ownership lock held"):
                with journal.lock(self.fixture.database.parent / "runner.lock", blocking=False):
                    self.fail("once owner was released")
            return value

        with patch.object(self.runner, "recover", return_value=[]), \
             patch.object(self.runner, "tick", return_value=[]), \
             patch.object(self.runner, "pulse", side_effect=observed), \
             patch.object(runtime.time, "sleep"):
            self.runner.serve(once=True)
        self.assertEqual(len(pulses), 2)
        self.assertFalse(pulses[0]["healthy"])
        self.assertTrue(pulses[1]["healthy"])
        self.claim.assert_not_called()


class RecoveryJournal(unittest.TestCase):
    setUp = probes.ProbeHolds.setUp

    def issue(self, name, content):
        path = self.journal_path.parent / name
        path.write_bytes(content)
        plan = reconciliation.plan(self.fixture.config)
        self.assertEqual(plan["entries"], [])
        issue = next(row for row in plan["journal_issues"] if row["entry"] == name)
        self.assertEqual(issue["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(len(issue["fingerprint"]), 64)
        return path, issue

    def test_plan_reports_partial_and_keeps_strict_reader_refusal(self):
        path, _ = self.issue(self.run["id"] + ".partial", b"interrupted")
        with self.assertRaises(store.RunnerRefused):
            journal.records(self.fixture.database)
        self.assertEqual(path.read_bytes(), b"interrupted")

    def test_explicit_quarantine_preserves_unknown_bytes_and_database(self):
        path, issue = self.issue(".DS_Store", b"finder metadata")
        mode = stat.S_IMODE(path.stat().st_mode)
        before = store.run_state(self.db, self.run["id"])
        result = reconciliation.quarantine(self.fixture.config, path.name, fingerprint=issue["fingerprint"])
        self.assertTrue(result["ok"])
        self.assertFalse(path.exists())
        self.assertEqual(Path(result["evidence"]).read_bytes(), b"finder metadata")
        self.assertEqual(stat.S_IMODE(Path(result["evidence"]).stat().st_mode), mode)
        self.assertEqual(store.run_state(self.db, self.run["id"]), before)
        self.assertEqual(journal.records(self.fixture.database), [self.run])

    def test_stale_fingerprint_preserves_changed_input(self):
        path, issue = self.issue(".DS_Store", b"first")
        path.write_bytes(b"newer")
        with self.assertRaises(store.RunnerRefused):
            reconciliation.quarantine(self.fixture.config, path.name, fingerprint=issue["fingerprint"])
        self.assertEqual(path.read_bytes(), b"newer")

    def test_partial_writer_lock_refuses_quarantine(self):
        path, issue = self.issue(self.run["id"] + ".partial", b"writer still owns this")
        with journal.lock(self.journal_path.with_suffix(".lock"), blocking=False):
            with self.assertRaisesRegex(store.RunnerRefused, "ownership lock held"):
                reconciliation.quarantine(self.fixture.config, path.name, fingerprint=issue["fingerprint"])
        self.assertEqual(path.read_bytes(), b"writer still owns this")

    def test_active_daemon_lock_refuses_quarantine(self):
        path, issue = self.issue(".DS_Store", b"preserve")
        with journal.lock(self.fixture.database.parent / "runner.lock", blocking=False):
            with self.assertRaisesRegex(store.RunnerRefused, "ownership lock held"):
                reconciliation.quarantine(self.fixture.config, path.name, fingerprint=issue["fingerprint"])
        self.assertEqual(path.read_bytes(), b"preserve")

    @contextmanager
    def swapped_only_while_the_lock_opens(self, lock, replacement):
        """Put `replacement` at `lock` for exactly the duration of the lock's open.

        The swap happens inside the open call and is undone before it returns,
        so a check of the path made at any time before or after the open sees
        the original single-link file. Only a check of what the open itself
        returned can see the replacement. Opens by `os.open`, `io.open` and
        `open` are hooked; an open through a directory descriptor is not.
        """
        aside = lock.with_name(lock.name + ".aside")
        swapped = []

        def around(real):
            def opener(file, *args, **kwargs):
                if swapped or not isinstance(file, (str, os.PathLike)) or os.fspath(file) != os.fspath(lock):
                    return real(file, *args, **kwargs)
                swapped.append(True)
                os.rename(lock, aside)
                os.rename(replacement, lock)
                try:
                    return real(file, *args, **kwargs)
                finally:
                    os.rename(lock, replacement)
                    os.rename(aside, lock)
            return opener

        with patch("os.open", around(os.open)), patch("io.open", around(io.open)), \
             patch("builtins.open", around(open)):
            yield swapped

    def test_a_link_present_only_while_the_lock_opens_creates_no_target(self):
        path, issue = self.issue(".DS_Store", b"preserve")
        lock = self.fixture.database.parent / "runner.lock"
        lock.write_bytes(b"")
        target = self.fixture.database.parent / "lock-target"
        link = self.fixture.database.parent / "staged-link"
        link.symlink_to(target)
        with self.swapped_only_while_the_lock_opens(lock, link) as swapped:
            with self.assertRaisesRegex(store.RunnerRefused, "unsafe ownership or type"):
                reconciliation.quarantine(self.fixture.config, path.name, fingerprint=issue["fingerprint"])
        self.assertEqual(swapped, [True])
        self.assertFalse(os.path.lexists(target))
        self.assertEqual(path.read_bytes(), b"preserve")

    def test_a_second_link_present_only_while_the_lock_opens_refuses(self):
        path, issue = self.issue(".DS_Store", b"preserve")
        lock = self.fixture.database.parent / "runner.lock"
        lock.write_bytes(b"")
        staged = self.fixture.database.parent / "staged-file"
        staged.write_bytes(b"")
        os.link(staged, self.fixture.database.parent / "staged-file-second")
        with self.swapped_only_while_the_lock_opens(lock, staged) as swapped:
            with self.assertRaisesRegex(store.RunnerRefused, "unsafe ownership or type"):
                reconciliation.quarantine(self.fixture.config, path.name, fingerprint=issue["fingerprint"])
        self.assertEqual(swapped, [True])
        self.assertEqual((lock.stat().st_nlink, staged.stat().st_nlink), (1, 2))
        self.assertEqual(path.read_bytes(), b"preserve")

    def test_a_link_present_only_while_a_later_open_creates_no_target(self):
        """Swap during the n-th open of the lock path, not only the first."""
        for nth in (1, 2, 3):
            with self.subTest(nth=nth):
                root = self.fixture.database.parent
                lock = root / "runner.lock"
                target, link, aside = root / "lock-target", root / "staged-link", root / "runner.lock.aside"
                for leftover in (target, link, aside):
                    if os.path.lexists(leftover):
                        os.unlink(leftover)
                lock.write_bytes(b"")
                link.symlink_to(target)
                seen, swapped = [], []

                def around(real):
                    def opener(file, *args, **kwargs):
                        if not isinstance(file, (str, os.PathLike)) or os.fspath(file) != os.fspath(lock):
                            return real(file, *args, **kwargs)
                        seen.append(True)
                        if len(seen) != nth:
                            return real(file, *args, **kwargs)
                        swapped.append(True)
                        os.rename(lock, aside)
                        os.rename(link, lock)
                        try:
                            return real(file, *args, **kwargs)
                        finally:
                            os.rename(lock, link)
                            os.rename(aside, lock)
                    return opener

                path, issue = self.issue(f"probe-{nth}.DS_Store", b"preserve")
                with patch("os.open", around(os.open)), patch("io.open", around(io.open)), \
                     patch("builtins.open", around(open)):
                    try:
                        reconciliation.quarantine(self.fixture.config, path.name, fingerprint=issue["fingerprint"])
                    except store.RunnerRefused:
                        pass
                self.assertFalse(os.path.lexists(target), f"target created through a link swapped in at open {nth}")
                # What each subTest proved is said here. `journal.lock` opens
                # the path once, so only nth=1 swaps; 2 and 3 asserted nothing
                # and read as coverage they did not have (sd:1232). A second
                # open makes this fail rather than pass quietly, and the
                # subTest that gains it then has a swap to assert.
                self.assertEqual(len(seen), 1, f"the hook saw {len(seen)} opens of the lock path")
                self.assertEqual(swapped, [True] if nth == 1 else [])

    def test_reconcile_refuses_a_link_at_the_runner_lock_and_creates_no_target(self):
        """`reconcile` takes `runner.lock` through `journal.lock`, the same hardened opener `quarantine` uses."""
        lock = self.fixture.database.parent / "runner.lock"
        target = self.fixture.database.parent / "lock-target"
        lock.symlink_to(target)
        with self.assertRaisesRegex(store.RunnerRefused, "unsafe ownership or type"):
            reconciliation.reconcile(self.fixture.config, self.run["id"], fingerprint="a" * 64)
        self.assertFalse(os.path.lexists(target))
        self.assertEqual(journal.records(self.fixture.database), [self.run])

    def test_a_fifo_at_the_lock_path_refuses(self):
        path, issue = self.issue(".DS_Store", b"preserve")
        lock = self.fixture.database.parent / "runner.lock"
        os.mkfifo(lock)
        with self.assertRaisesRegex(store.RunnerRefused, "unsafe ownership or type"):
            reconciliation.quarantine(self.fixture.config, path.name, fingerprint=issue["fingerprint"])
        self.assertTrue(stat.S_ISFIFO(lock.lstat().st_mode))
        self.assertEqual(path.read_bytes(), b"preserve")

    def test_a_lock_file_owned_by_another_user_refuses(self):
        """No second user is at hand: the lock descriptor's stat is answered as another user's."""
        path, issue = self.issue(".DS_Store", b"preserve")
        lock = self.fixture.database.parent / "runner.lock"
        lock.write_bytes(b"")
        identity = (lock.stat().st_dev, lock.stat().st_ino)
        real = os.fstat

        def fstat(descriptor):
            details = real(descriptor)
            if (details.st_dev, details.st_ino) != identity:
                return details
            values = list(details[:10])
            values[4] = details.st_uid + 1
            return os.stat_result(tuple(values))

        with patch("os.fstat", fstat):
            with self.assertRaisesRegex(store.RunnerRefused, "unsafe ownership or type"):
                reconciliation.quarantine(self.fixture.config, path.name, fingerprint=issue["fingerprint"])
        self.assertEqual(path.read_bytes(), b"preserve")

    def test_path_escape_refuses_without_touching_file(self):
        outside = self.journal_path.parent.parent / "precious"
        outside.write_bytes(b"keep")
        with self.assertRaises(store.RunnerRefused):
            reconciliation.quarantine(self.fixture.config, "../precious", fingerprint="a" * 64)
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_symlink_is_reported_but_cannot_be_quarantined(self):
        linked = self.journal_path.parent / ".DS_Store"
        linked.symlink_to(self.journal_path)
        plan = reconciliation.plan(self.fixture.config)
        issue = next(row for row in plan["journal_issues"] if row["entry"] == linked.name)
        self.assertTrue(issue["blocked"])
        with self.assertRaises(store.RunnerRefused):
            reconciliation.quarantine(self.fixture.config, linked.name, fingerprint=issue["fingerprint"])
        self.assertTrue(linked.is_symlink())

    def test_healthy_known_record_cannot_be_quarantined(self):
        before = self.journal_path.read_bytes()
        with self.assertRaises(store.RunnerRefused):
            reconciliation.quarantine(self.fixture.config, self.journal_path.name, fingerprint="a" * 64)
        self.assertEqual(self.journal_path.read_bytes(), before)

    def test_healthy_lock_cannot_be_quarantined(self):
        path = self.journal_path.with_suffix(".lock")
        before = path.read_bytes()
        with self.assertRaises(store.RunnerRefused):
            reconciliation.quarantine(self.fixture.config, path.name, fingerprint="a" * 64)
        self.assertEqual(path.read_bytes(), before)

    def test_existing_quarantine_destination_is_never_overwritten(self):
        path, issue = self.issue(".DS_Store", b"preserve source")
        archive = self.fixture.database.parent / "runner-recovery-evidence"
        archive.mkdir(mode=0o700)
        destination = archive / ("quarantine-" + issue["fingerprint"])
        destination.mkdir(mode=0o700)
        (destination / "entry").write_bytes(b"preserve destination")
        result = reconciliation.quarantine(self.fixture.config, path.name, fingerprint=issue["fingerprint"])
        self.assertEqual(Path(result["evidence"]).read_bytes(), b"preserve source")
        self.assertEqual((destination / "entry").read_bytes(), b"preserve destination")

    def test_failed_quarantine_attempt_can_retry_without_deleting_evidence(self):
        path, issue = self.issue(".DS_Store", b"preserve source")
        with patch.object(reconciliation.os, "rename", side_effect=OSError("fixture move failure")):
            with self.assertRaisesRegex(OSError, "fixture move failure"):
                reconciliation.quarantine(self.fixture.config, path.name, fingerprint=issue["fingerprint"])
        archive = self.fixture.database.parent / "runner-recovery-evidence"
        previous = next(archive.iterdir())
        receipt = (previous / "receipt.json").read_bytes()
        self.assertEqual(path.read_bytes(), b"preserve source")
        result = reconciliation.quarantine(self.fixture.config, path.name, fingerprint=issue["fingerprint"])
        self.assertEqual(Path(result["evidence"]).read_bytes(), b"preserve source")
        self.assertNotEqual(Path(result["evidence"]).parent, previous)
        self.assertEqual((previous / "receipt.json").read_bytes(), receipt)
        self.assertEqual(len(list(archive.iterdir())), 2)

    def test_journal_growth_read_is_bounded_and_refuses_authority(self):
        path, _ = self.issue(".DS_Store", b"small")
        actual_open = reconciliation.os.fdopen
        sizes = []
        limit = 16 * 1024 * 1024

        @contextmanager
        def grew(fd, mode):
            with actual_open(fd, mode) as source:
                path.write_bytes(b"x" * (limit + 2))
                proxy = Mock(wraps=source)
                def read(size=-1):
                    sizes.append(size)
                    return source.read(size)
                proxy.read.side_effect = read
                yield proxy

        with patch.object(reconciliation.os, "fdopen", side_effect=grew):
            with self.assertRaises(store.RunnerRefused):
                reconciliation.plan(self.fixture.config)
        self.assertEqual(sizes, [limit + 1])
        self.assertEqual(path.stat().st_size, limit + 2)

    def test_cli_requires_exact_entry_and_fingerprint_for_quarantine(self):
        path, issue = self.issue(".DS_Store", b"fixture CLI evidence")
        output = io.StringIO()
        with patch.object(cli, "configuration", return_value=self.fixture.config), \
             patch("sys.stdout", output):
            self.assertEqual(cli.main(["recovery-quarantine", "--entry", path.name,
                                       "--fingerprint", issue["fingerprint"]]), 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def restore_evidence(self):
        archive = self.fixture.database.parent / "runner-recovery-evidence"
        archive.mkdir(mode=0o700)
        material = archive / ("restore-" + "e" * 32) / "runner-journal"
        material.mkdir(parents=True)
        return material / self.journal_path.name

    def linked_issue(self):
        plan = reconciliation.plan(self.fixture.config)
        return next(row for row in plan["journal_issues"] if row["entry"] == self.journal_path.name)

    def unlink_copy(self, evidence):
        return evidence.parent.parent / ("unlink-" + evidence.name)

    @contextmanager
    def racing_lstat(self, target, copy, **fields):
        """Report changed identity for target once the unlink copy exists, as a racer would."""
        original = Path.lstat
        names = ["st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid",
                 "st_size", "st_mtime_ns", "st_ctime_ns"]

        def lstat(path):
            details = original(path)
            if path != target or not copy.exists():
                return details
            return SimpleNamespace(**{**{name: getattr(details, name) for name in names}, **fields})

        with patch.object(Path, "lstat", lstat):
            yield

    def test_evidence_on_another_device_is_refused_before_the_replace(self):
        # A hard link cannot span devices, so only a swapped evidence name reports one.
        evidence = self.restore_evidence()
        os.link(self.journal_path, evidence)
        issue = self.linked_issue()
        copy, before = self.unlink_copy(evidence), self.journal_path.read_bytes()
        with self.racing_lstat(evidence, copy, st_dev=self.journal_path.stat().st_dev + 1):
            with self.assertRaisesRegex(store.RunnerRefused, "changed before unlink"):
                reconciliation.unlink_restore_link(self.fixture.config, self.journal_path.name,
                                                   fingerprint=issue["fingerprint"])
        self.assertEqual((self.journal_path.stat().st_nlink, evidence.stat().st_ino),
                         (2, self.journal_path.stat().st_ino))
        self.assertEqual((copy.read_bytes(), self.journal_path.read_bytes()), (before, before))

    def test_a_link_taken_before_the_replace_is_refused(self):
        evidence = self.restore_evidence()
        os.link(self.journal_path, evidence)
        issue = self.linked_issue()
        copy = self.unlink_copy(evidence)
        with self.racing_lstat(self.journal_path, copy, st_nlink=3):
            with self.assertRaisesRegex(store.RunnerRefused, "changed before unlink"):
                reconciliation.unlink_restore_link(self.fixture.config, self.journal_path.name,
                                                   fingerprint=issue["fingerprint"])
        self.assertEqual((self.journal_path.stat().st_nlink, evidence.stat().st_ino),
                         (2, self.journal_path.stat().st_ino))

    def test_a_crashed_unlink_copy_is_replaced_by_the_next_attempt(self):
        evidence = self.restore_evidence()
        os.link(self.journal_path, evidence)
        copy = self.unlink_copy(evidence)
        copy.write_bytes(b"partial bytes from a crashed unlink")
        issue = self.linked_issue()
        result = reconciliation.unlink_restore_link(self.fixture.config, self.journal_path.name,
                                                    fingerprint=issue["fingerprint"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.journal_path.stat().st_nlink, 1)
        self.assertEqual(evidence.read_bytes(), self.journal_path.read_bytes())
        self.assertEqual(sorted(path.name for path in evidence.parent.parent.iterdir()), ["runner-journal"])

    def test_a_linked_unlink_copy_from_a_crash_is_refused(self):
        evidence = self.restore_evidence()
        os.link(self.journal_path, evidence)
        copy = self.unlink_copy(evidence)
        copy.write_bytes(b"partial bytes from a crashed unlink")
        os.link(copy, copy.parent / "other-name")
        issue = self.linked_issue()
        with self.assertRaisesRegex(store.RunnerRefused, "restore unlink copy is linked or invalid"):
            reconciliation.unlink_restore_link(self.fixture.config, self.journal_path.name,
                                               fingerprint=issue["fingerprint"])
        self.assertEqual((self.journal_path.stat().st_nlink, copy.stat().st_nlink), (2, 2))

    def test_restore_link_is_removed_and_evidence_keeps_its_bytes(self):
        # sd:779: a restore before the fix left the live file linked to its restore material.
        evidence = self.restore_evidence()
        os.link(self.journal_path, evidence)
        before, inode = self.journal_path.read_bytes(), self.journal_path.stat().st_ino
        issue = self.linked_issue()
        self.assertTrue(issue["blocked"])
        result = reconciliation.unlink_restore_link(self.fixture.config, self.journal_path.name, fingerprint=issue["fingerprint"])
        self.assertTrue(result["ok"])
        self.assertEqual((self.journal_path.stat().st_nlink, self.journal_path.stat().st_ino), (1, inode))
        self.assertEqual((evidence.read_bytes(), evidence.stat().st_nlink), (before, 1))
        self.assertEqual(reconciliation.plan(self.fixture.config)["journal_issues"], [])
        self.assertEqual(journal.records(self.fixture.database), [self.run])
        self.assertEqual(sorted(path.name for path in evidence.parent.parent.iterdir()), ["runner-journal"])

    def test_foreign_second_link_is_refused_beside_a_separate_evidence_copy(self):
        evidence = self.restore_evidence()
        evidence.write_bytes(self.journal_path.read_bytes())
        foreign = self.fixture.database.parent / "foreign-link"
        os.link(self.journal_path, foreign)
        separate = evidence.stat().st_ino
        issue = self.linked_issue()
        with self.assertRaisesRegex(store.RunnerRefused, "not a restore evidence copy"):
            reconciliation.unlink_restore_link(self.fixture.config, self.journal_path.name, fingerprint=issue["fingerprint"])
        self.assertEqual((self.journal_path.stat().st_nlink, evidence.stat().st_ino), (2, separate))
        self.assertEqual(foreign.stat().st_ino, self.journal_path.stat().st_ino)

    def test_evidence_link_beside_a_foreign_link_is_refused(self):
        evidence = self.restore_evidence()
        os.link(self.journal_path, evidence)
        os.link(self.journal_path, self.fixture.database.parent / "foreign-link")
        issue = self.linked_issue()
        with self.assertRaisesRegex(store.RunnerRefused, "exactly one other link"):
            reconciliation.unlink_restore_link(self.fixture.config, self.journal_path.name, fingerprint=issue["fingerprint"])
        self.assertEqual((self.journal_path.stat().st_nlink, evidence.stat().st_ino), (3, self.journal_path.stat().st_ino))

    def test_restore_unlink_waits_for_a_pending_restore(self):
        evidence = self.restore_evidence()
        os.link(self.journal_path, evidence)
        issue = self.linked_issue()
        (self.fixture.database.parent / "runner-restore-intent.json").write_text("{}")
        with self.assertRaisesRegex(store.RunnerRefused, "pending runner journal restore"):
            reconciliation.unlink_restore_link(self.fixture.config, self.journal_path.name, fingerprint=issue["fingerprint"])
        self.assertEqual(evidence.stat().st_ino, self.journal_path.stat().st_ino)

    def test_healthy_single_link_journal_is_refused_by_restore_unlink(self):
        before = self.journal_path.read_bytes()
        with self.assertRaisesRegex(store.RunnerRefused, "is healthy"):
            reconciliation.unlink_restore_link(self.fixture.config, self.journal_path.name, fingerprint="a" * 64)
        self.assertEqual((self.journal_path.read_bytes(), self.journal_path.stat().st_nlink), (before, 1))

    def test_second_link_without_any_restore_evidence_is_refused(self):
        os.link(self.journal_path, self.fixture.database.parent / "foreign-link")
        issue = self.linked_issue()
        with self.assertRaisesRegex(store.RunnerRefused, "not a restore evidence copy"):
            reconciliation.unlink_restore_link(self.fixture.config, self.journal_path.name, fingerprint=issue["fingerprint"])
        self.assertEqual(self.journal_path.stat().st_nlink, 2)

    def test_two_restore_evidence_links_are_refused(self):
        evidence = self.restore_evidence()
        os.link(self.journal_path, evidence)
        second = evidence.parent.parent.with_name("restore-" + "f" * 32) / "runner-journal" / evidence.name
        second.parent.mkdir(parents=True)
        os.link(self.journal_path, second)
        issue = self.linked_issue()
        with self.assertRaisesRegex(store.RunnerRefused, "exactly one other link"):
            reconciliation.unlink_restore_link(self.fixture.config, self.journal_path.name, fingerprint=issue["fingerprint"])
        self.assertEqual({evidence.stat().st_ino, second.stat().st_ino}, {self.journal_path.stat().st_ino})

    def test_cli_runs_restore_unlink_with_exact_entry_and_fingerprint(self):
        evidence = self.restore_evidence()
        os.link(self.journal_path, evidence)
        issue = self.linked_issue()
        output = io.StringIO()
        with patch.object(cli, "configuration", return_value=self.fixture.config), \
             patch("sys.stdout", output):
            self.assertEqual(cli.main(["recovery-unlink", "--entry", self.journal_path.name,
                                       "--fingerprint", issue["fingerprint"]]), 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        self.assertEqual(self.journal_path.stat().st_nlink, 1)

    def test_invalid_journal_directory_authority_refuses(self):
        self.journal_path.parent.chmod(0o777)
        with self.assertRaises(store.RunnerRefused):
            reconciliation.quarantine(self.fixture.config, ".DS_Store", fingerprint="a" * 64)


class RestoreLinkCleanup(unittest.TestCase):
    def test_unlink_after_a_linking_restore_keeps_the_resume_fingerprint(self):
        import importlib
        from sd_db import connect, create_assignment, upsert_repo
        from sd_db.migrate import initialise
        # `sd_db.backup` the name is re-exported as a function; the module is what this reads.
        backup = importlib.import_module("sd_db.backup")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = Path(directory.name) / "home"
        state = home / ".local/share/sd"
        state.mkdir(parents=True)
        initialise(home=home)
        database = state / "sd.db"
        connection = connect(home=home)
        self.addCleanup(connection.close)
        upsert_repo(connection, "/fixture")
        assignment = create_assignment(connection, role="worker", status="queued")
        connection.execute("INSERT INTO runner_run (id,assignment,run,repo,branch,owner,journal_version,work_path,retained_path,created_at,updated_at) "
                           "VALUES (?,?,1,'/fixture','fixture','fixture',1,'/tmp/work','/tmp/retained','fixture','fixture')", ("c" * 32, assignment))
        journal.persist(database, dict(connection.execute("SELECT * FROM runner_run").fetchone()))
        snapshot = backup.run(home=home)
        live = journal.directory(database) / ("c" * 32 + ".json")
        live.unlink()
        backup.restore(snapshot.directory, home=home)
        material = next((state / "runner-recovery-evidence").glob("restore-*/runner-journal"))
        # What a restore before sd:779 left: the live name linked to its material.
        live.unlink()
        os.link(material / live.name, live)
        config = runtime.Config(database, home / "work", home / "retained", home, home)
        issue = next(row for row in reconciliation.plan(config)["journal_issues"] if row["entry"] == live.name)
        reconciliation.unlink_restore_link(config, live.name, fingerprint=issue["fingerprint"])
        self.assertEqual((live.stat().st_nlink, (material / live.name).stat().st_nlink), (1, 1))
        # The resume check reads the replaced evidence copy and still verifies it.
        saved = backup._runner_files(snapshot.directory / "runner-journal")
        intent = state / "runner-restore-intent.json"
        intent.write_bytes((material.parent / "completed-intent.json").read_bytes())
        self.assertEqual(backup._runner_restore_material(saved, state), material)
        intent.unlink()


if __name__ == "__main__":
    unittest.main()
