"""Step 6 of the second-machine plan: what runs only on the hub refuses by name.

`docs/work/2026-09-22-run-the-framework-from-a-second-machine/implement.md`.
Under a remote connection, or a path a hub serves, the two file locks and
every directory beside the database raise `HubOnly` naming the verb and the
hub (seam 2, gap (b)). `ledger.reserve` raises `LedgerRefused` naming the
hub, and `ledger.release_orphans` sweeps nothing (gap C1).

Ruling Q2 = A keeps criterion 1: a test marked `hub_only` runs local under
`sd-db.sh test --remote`. `TheMarks` runs each marked test over the wire and
fails one that meets no hub-only refusal there.
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import (create_item, database, initialise, ledger, publication_journal, removal, remote,
                   retention, runner_controls, runner_exec, seed, ship, writing)
from sd_db.backup import restore, run as take_backup
from sd_db.database import default_path
from sd_db.operations import control_gate
from sd_db.registry import parse
from sd_db.testing import wire

from tests.test_hub import Satellite
from tests.test_ledger import REGISTRY
from tests.test_wire import HERE, Served

PACKAGE = HERE / "sd_db"

#: Functions that read `PRAGMA database_list` and need no refusal, with the reason.
ALLOWED = {
    ("registry.py", "beside"): "seam 7: the registry's bytes are the one exception, served by the protocol (step 9)",
    ("migrate.py", "migrate"): "opens with create=True, which `hub.Hub.open` refuses with HubOnly first",
}


def rows(path: Path, sql: str) -> list[tuple]:
    raw = sqlite3.connect(path)
    try:
        return raw.execute(sql).fetchall()
    finally:
        raw.close()


class OverTheWire(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.served = Served(self.root)
        self.addCleanup(self.served.stop)
        self.hub = f"127.0.0.1:{self.served.port}"
        self.connection = self.served.connect()
        self.addCleanup(self.connection.close)

    def assertHubOnly(self, verb: str, attempt) -> None:
        with self.assertRaises(remote.HubOnly) as caught:
            attempt()
        self.assertEqual((caught.exception.verb, caught.exception.hub), (verb, self.hub))
        self.assertIn(f"{verb} runs on the sd hub only", str(caught.exception))
        self.assertIn(self.hub, str(caught.exception))


class TheLocks(OverTheWire):
    def test_control_gate_refuses_a_remote_connection_and_takes_no_lock(self):
        def enter():
            with control_gate(self.connection):
                self.fail("the gate was entered over the wire")

        self.assertHubOnly("service controls and restore", enter)
        self.assertFalse((self.root / "operation-locks").exists())

    def test_the_publication_journal_refuses_a_remote_connection(self):
        self.assertHubOnly("the publication journal", lambda: publication_journal.root(self.connection))

    def test_repo_and_item_remove_refuse_a_remote_connection(self):
        self.assertHubOnly("repo and item remove", lambda: removal._store_directory(self.connection))
        self.assertHubOnly("repo and item remove", lambda: removal._main_file(self.connection))


class TheDirectories(OverTheWire):
    """Each directory beside the database, resolved over the wire, refuses."""

    def test_the_retention_prune_refuses_before_its_backup_check(self):
        self.assertHubOnly("the retention prune", lambda: retention.prune(self.connection, object()))

    def test_the_executions_prune_refuses_to_resolve_an_output(self):
        row = {"id": 1, "output_path": str(self.root / "executions" / ("0" * 32 + ".log"))}
        self.assertHubOnly("the executions prune", lambda: retention._output_files(self.connection, row))

    def test_reading_an_execution_refuses(self):
        local = database.connect(self.served.database)
        try:
            item = create_item(local, kind="work", title="exec")
            local.execute(
                "INSERT INTO note (item, timestamp, kind, body, started, output_path) VALUES (?, ?, 'exec', '{}', ?, ?)",
                (item, "2026-10-04T00:00:00+00:00", "2026-10-04T00:00:00+00:00",
                 str(self.root / "executions" / ("0" * 32 + ".log"))))
            note = local.execute("SELECT id FROM note WHERE kind = 'exec'").fetchone()[0]
            local.commit()
        finally:
            local.close()
        self.assertHubOnly("the runner's executions directory",
                           lambda: runner_exec.read_execution(self.connection, note))

    def test_runner_controls_refuse_before_they_read_the_assignment(self):
        self.assertHubOnly("runner controls", lambda: runner_controls.control(
            self.connection, 1, "cancel", expected_revision=0, who="test"))
        self.assertHubOnly("runner controls", lambda: runner_exec.reconcile(self.connection, 1))

    def test_an_execution_refuses_before_it_is_prepared_or_validated(self):
        self.assertHubOnly("the runner's executions directory", lambda: runner_exec.prepare(
            self.connection, 1, "command", {}, expected_revision=0, expected_catalog="", who="test"))
        self.assertHubOnly("the runner's executions directory",
                           lambda: runner_exec._validate(self.connection, [1], "{}"))

    def test_the_writing_cutover_refuses_before_it_reads_the_repository(self):
        self.assertHubOnly("the writing cutover", lambda: writing.cutover_pieces(
            self.connection, str(self.root / "repo"), expected_fingerprint="", who="test"))

    def test_the_writing_cutover_journal_refuses(self):
        self.assertHubOnly("the writing cutover journal",
                           lambda: writing._journal_path(self.connection, str(self.root / "repo")))


class TheSatellitePaths(Satellite):
    """The pack passes a path, not a connection: `default_path()` or its string."""

    def setUp(self):
        super().setUp()
        self.served = Served(self.root)
        self.addCleanup(self.served.stop)
        self.name_hub(self.served.port, self.served.token_file)
        self.hub = f"127.0.0.1:{self.served.port}"

    def test_the_repository_lock_refuses_the_default_path_and_creates_nothing(self):
        before = sorted(p for p in self.home.rglob("*"))
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            shapes = (default_path(self.home), default_path(), Path(str(default_path())))
            for shape in shapes:
                with self.subTest(shape=type(shape).__name__), self.assertRaises(remote.HubOnly) as caught:
                    with ship.repository_lock(shape, "example/repo"):
                        self.fail("the repository lock was taken on a satellite")
                self.assertEqual(caught.exception.verb, "the sd-ship repository lock")
                self.assertEqual(caught.exception.hub, self.hub)
        self.assertEqual(sorted(p for p in self.home.rglob("*")), before)
        self.assertFalse((self.home / ".local").exists())

    def test_control_gate_refuses_the_default_path(self):
        with self.assertRaises(remote.HubOnly):
            with control_gate(default_path(self.home)):
                self.fail("the gate was entered on a satellite")

    def test_the_backup_refuses_before_it_probes_the_destination(self):
        destination = self.root / "backups"
        with self.assertRaises(remote.HubOnly) as caught:
            take_backup(home=self.home, destination=destination)
        self.assertEqual((caught.exception.verb, caught.exception.hub), ("the backup", self.hub))
        self.assertFalse(destination.exists())
        self.assertFalse((self.home / ".local").exists())

    def test_the_restore_refuses_before_it_reads_the_snapshot(self):
        with self.assertRaises(remote.HubOnly) as caught:
            restore(self.root / "no-such-backup", home=self.home)
        self.assertEqual((caught.exception.verb, caught.exception.hub), ("the restore", self.hub))
        self.assertFalse((self.home / ".local").exists())

    def test_init_and_migrate_refuse_and_create_nothing(self):
        with self.assertRaises(remote.HubOnly) as caught:
            initialise(home=self.home)
        self.assertEqual((caught.exception.verb, caught.exception.hub), ("init and migrate", self.hub))
        self.assertFalse((self.home / ".local").exists())

    def test_another_path_is_not_the_hubs(self):
        # `sd-db.sh test --remote` serves every path; this asks about a satellite's.
        self.enterContext(mock.patch.object(database, "_opener", None))
        other = self.root / "elsewhere.db"
        self.assertIsNone(database.served_by(other, self.home))
        self.assertEqual(database.served_by(default_path(self.home)), self.hub)


class TheLedger(OverTheWire):
    def reservations(self) -> list[tuple]:
        return rows(self.served.database, "SELECT id, call_id, source, owner_pid FROM cost ORDER BY id")

    def test_reserve_refuses_naming_the_hub_and_writes_nothing(self):
        with self.assertRaises(ledger.LedgerRefused) as caught:
            ledger.reserve(self.connection, bill="open", bound=0.5, call_id="call-1", owner_pid=os.getpid())
        self.assertEqual(caught.exception.scope, "hub")
        self.assertIn("charged on the sd hub only", str(caught.exception))
        self.assertIn(self.hub, str(caught.exception))
        self.assertEqual(self.reservations(), [])

    def test_release_orphans_sweeps_nothing_and_leaves_every_reservation(self):
        local = database.connect(self.served.database)
        try:
            seed(local, parse(REGISTRY, "providers.yaml"))
            ledger.reserve(local, bill="open", bound=0.5, call_id="call-1", owner_pid=os.getpid())
        finally:
            local.close()
        before = self.reservations()
        self.assertEqual(len(before), 1)
        released = ledger.release_orphans(self.connection, is_alive=lambda pid: False)
        self.assertEqual(released, ledger.Released())
        self.assertEqual(self.reservations(), before)


@wire.hub_only
class TheLocalHub(unittest.TestCase):
    """The same verbs on the hub's own file pass: the refusal is the connection's kind."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)

    def test_control_gate_enters_on_the_hubs_own_file(self):
        connection = database.connect(self.path)
        try:
            with control_gate(connection):
                pass
        finally:
            connection.close()
        self.assertTrue((self.path.parent / "operation-locks" / "control.lock").exists())

    def test_the_repository_lock_is_taken_on_the_hubs_own_file(self):
        with ship.repository_lock(self.path, "example/repo"):
            pass
        self.assertTrue((self.path.parent / "ship-locks").is_dir())


class TheSites(unittest.TestCase):
    """Every function that reads `PRAGMA database_list` refuses first."""

    def test_each_database_list_read_is_behind_a_refusal(self):
        missing, allowed = [], set()
        for path in sorted(PACKAGE.rglob("*.py")):
            if "testing" in path.relative_to(PACKAGE).parts:
                continue
            source = path.read_text()
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.FunctionDef):
                    continue
                reads = [n.lineno for n in ast.walk(node)
                         if isinstance(n, ast.Constant) and n.value == "PRAGMA database_list"]
                if not reads:
                    continue
                key = (str(path.relative_to(PACKAGE)), node.name)
                if key in ALLOWED:
                    allowed.add(key)
                    continue
                refusals = [n.lineno for n in ast.walk(node)
                            if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None))
                            == "refuse_hub_only"]
                if not refusals or min(refusals) > min(reads):
                    missing.append(f"{key[0]}::{key[1]}")
        self.assertEqual(missing, [], "these read PRAGMA database_list without refusing a hub first")
        self.assertEqual(allowed, set(ALLOWED), "an ALLOWED entry names no function that reads the pragma")


def _tests(suite):
    for entry in suite:
        if isinstance(entry, unittest.TestSuite):
            yield from _tests(entry)
        else:
            yield entry


class TheMarks(unittest.TestCase):
    """A `hub_only` mark is honest: over the wire, the test meets a refusal.

    A refusal is a `HubOnly`, or a `LedgerRefused` with scope `hub`, built
    while the test runs; the test's own outcome does not count.
    """

    def test_every_marked_test_meets_a_hub_only_refusal_over_the_wire(self):
        loaded = unittest.TestLoader().discover(start_dir=str(HERE / "tests"), top_level_dir=str(HERE))
        marked = [test for test in _tests(loaded) if wire.is_hub_only(test)]
        self.assertTrue(marked, "no test is marked hub_only")
        with tempfile.TemporaryDirectory() as folder:
            served = Served(Path(folder))
            previous = database._opener
            refuse, refused = remote.HubOnly.__init__, ledger.LedgerRefused.__init__
            try:
                wire.install("127.0.0.1", served.port, served.token, marks=False)
                for test in marked:
                    hits = []

                    def hub_only(error, *args, **kwargs):
                        hits.append(type(error).__name__)
                        refuse(error, *args, **kwargs)

                    def ledger_refused(error, message, *args, **kwargs):
                        if kwargs.get("scope") == "hub":
                            hits.append(type(error).__name__)
                        refused(error, message, *args, **kwargs)

                    with (self.subTest(test=test.id()),
                          mock.patch.object(remote.HubOnly, "__init__", hub_only),
                          mock.patch.object(ledger.LedgerRefused, "__init__", ledger_refused)):
                        # A refused CLI verb prints its refusal; the guard reads the hits instead.
                        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                            unittest.TestSuite([test]).run(unittest.TestResult())
                        self.assertTrue(hits, f"{test.id()} is marked hub_only, but over the wire it "
                                              f"met no hub-only refusal; remove the mark")
            finally:
                database._opener = previous
                served.stop()


if __name__ == "__main__":
    unittest.main()
