"""Slow service commands must not monopolize SQL or cross a live restore."""

import errno
import os
import shutil
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from sd_db import connect, create_item, operations, runner, runner_journal
from sd_db.backup import restore, run
from sd_db.errors import BackupError
from sd_db.operations import job_state, retry_job
from sd_db.services import restart_service, stop_service
from sd_db.workflow import WorkflowError

from tests import test_backup, test_operations, test_services


class ControlContention(unittest.TestCase):
    def service(self):
        fixture = test_services.Services()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def other(self, database):
        connection = connect(database)
        connection.execute("PRAGMA busy_timeout=20")
        self.addCleanup(connection.close)
        return connection

    def test_service_inspection_and_dispatch_allow_independent_heartbeat(self):
        fixture = self.service()
        before = fixture.state()
        other = self.other(fixture.root / "sd.db")
        phases = []

        def during(argv):
            self.assertFalse(fixture.db.in_transaction, argv)
            runner.heartbeat(other, {"healthy": True, "phase": argv[1]})
            phases.append(argv[1])

        fixture.hook = during
        result = fixture.act(restart_service, before)
        self.assertEqual(result["request"]["status"], "accepted")
        self.assertTrue({"print", "bootout", "bootstrap", "kickstart"}.issubset(phases))
        self.assertEqual([call[1] for call in fixture.actions()], ["bootout", "bootstrap", "kickstart"])

    def test_job_inspection_and_dispatch_allow_independent_heartbeat(self):
        fixture = test_operations.Operations()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        before = fixture.snapshot()
        other = self.other(fixture.root / "sd.db")
        original = fixture.backend.runner
        phases = []

        def during(argv):
            self.assertFalse(fixture.db.in_transaction, argv)
            runner.heartbeat(other, {"healthy": True, "phase": argv[1]})
            phases.append(argv[1])
            return original(argv)

        fixture.backend.runner = during
        result = retry_job(fixture.db, fixture.job, expected_revision=before["revision"], backend=fixture.backend, who="operator")
        self.assertEqual(result["request"]["status"], "accepted")
        self.assertIn("print", phases)
        self.assertEqual(phases.count("kickstart"), 1)

    def test_job_cannot_cross_another_service_dispatch(self):
        fixture = self.service()
        job = test_operations.Operations()
        job.setUp()
        self.addCleanup(job.doCleanups)
        other = self.other(fixture.root / "sd.db")
        before = fixture.state()
        revision = job_state(other, job.job, backend=job.backend)["revision"]

        def during(argv):
            if argv[1] == "bootout":
                with self.assertRaisesRegex(WorkflowError, "control or restore.*progress"):
                    retry_job(other, job.job, expected_revision=revision, backend=job.backend, who="operator")

        fixture.hook = during
        self.assertEqual(fixture.act(restart_service, before)["request"]["status"], "accepted")
        self.assertEqual(job.actions(), [])

    def test_restore_cannot_cross_dispatch_after_final_restore_check(self):
        fixture = self.service()
        fixture.db.close()
        home = fixture.root / "home"
        database = home / ".local/share/sd/sd.db"
        database.parent.mkdir(parents=True)
        shutil.move(fixture.root / "sd.db", database)
        fixture.db = connect(database)
        self.addCleanup(fixture.db.close)
        snapshot = run(home=home)
        item = create_item(fixture.db, kind="work", title="newer preserved work")
        before = fixture.state()
        attempts = []

        def during(argv):
            if argv[1] == "bootout":
                with self.assertRaisesRegex(BackupError, "control or restore.*progress"):
                    restore(snapshot.directory, home=home)
                attempts.append(argv[1])

        fixture.hook = during
        with patch("sd_db.backup._install_restore", side_effect=AssertionError("restore crossed dispatch")):
            self.assertEqual(fixture.act(restart_service, before)["request"]["status"], "accepted")
        self.assertEqual(attempts, ["bootout"])
        self.assertIsNotNone(fixture.db.execute("SELECT id FROM item WHERE id=?", (item,)).fetchone())
        self.assertFalse(runner.restoration_pending(fixture.db))

    def test_restore_gate_blocks_control_before_any_live_install(self):
        fixture = self.service()
        before = fixture.state()
        gate = fixture.root / "operation-locks" / "control.lock"
        with runner_journal.lock(gate):
            with self.assertRaisesRegex(WorkflowError, "control or restore.*progress"):
                fixture.act(stop_service, before)
        self.assertEqual(fixture.actions(), [])
        self.assertIsNone(fixture.state()["last_request"])

    def test_a_gate_that_cannot_be_opened_is_not_reported_as_work_in_progress(self):
        """A read-only or unwritable state directory has no competing process."""
        fixture = self.service()
        before = fixture.state()
        locks = fixture.root / "operation-locks"
        locks.mkdir(exist_ok=True)
        locks.chmod(0o500)
        try:
            with self.assertRaises(WorkflowError) as refused:
                fixture.act(stop_service, before)
        finally:
            locks.chmod(0o700)
        self.assertIn("control lock cannot be opened", str(refused.exception))
        self.assertIn(os.strerror(errno.EACCES), str(refused.exception))
        self.assertNotIn("in progress", str(refused.exception))
        self.assertEqual(fixture.actions(), [])

    def test_restore_refuses_configuration_changed_while_waiting_for_gate(self):
        fixture = test_backup.BackupCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.configuration()
        snapshot = run(home=fixture.home)
        config = fixture.state / "providers.yaml"
        config.write_text("staged original\n")
        original_gate = operations.control_gate

        @contextmanager
        def changed(database):
            config.write_text("new concurrent configuration\n")
            with original_gate(database):
                yield

        with patch.object(operations, "control_gate", side_effect=changed):
            with self.assertRaisesRegex(BackupError, "configuration changed"):
                restore(snapshot.directory, home=fixture.home)
        self.assertEqual(config.read_text(), "new concurrent configuration\n")
