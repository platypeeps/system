"""The schema inventory and the two version refusals."""

import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from sd_db import paths
from sd_db import schema as schema_module
from sd_db.database import connect, schema_version, set_schema_version, tables
from sd_db.errors import SchemaTooNew, SchemaTooOld
from sd_db.migrate import initialise, migrate
from sd_db.schema import SCHEMA_VERSION, TABLES

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class SchemaCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".local/share/sd").mkdir(parents=True)
        self.path = self.home / ".local/share/sd/sd.db"


class TheTables(SchemaCase):
    def test_declared_tables_and_no_other(self):
        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(sorted(tables(connection)), sorted(TABLES))
        self.assertEqual(len(TABLES), 16)

    def test_report_and_dep_are_item_kinds_not_tables(self):
        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        self.assertNotIn("report", tables(connection))
        self.assertNotIn("dep", tables(connection))
        connection.execute(
            "INSERT INTO item (kind, title, status, created_at, updated_at) "
            "VALUES ('report', 't', 'planning', '', '')"
        )
        connection.execute(
            "INSERT INTO item (kind, title, status, created_at, updated_at) "
            "VALUES ('dep', 't', 'planning', '', '')"
        )

    def test_the_library_inserts_into_no_table_the_document_does_not_name(self):
        """Criterion 1's grep, run against the library rather than by hand."""
        source = subprocess.run(
            # `--untracked` before the pattern: after it, git reads it as one.
            ["git", "grep", "-hIE", "--untracked",
             "INSERT (OR IGNORE )?INTO [a-z_]+", "--", "local-sd-db/sd_db"],
            cwd=PACKAGE_ROOT.parent, capture_output=True, text=True,
        ).stdout
        written = set(re.findall(r"INSERT (?:OR IGNORE )?INTO ([a-z_]+)", source))
        self.assertTrue(written, "the grep found no inserts at all")
        self.assertEqual(written - set(TABLES), set())

    def test_no_schema_version_table(self):
        """Version bookkeeping does not need an extra table."""
        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        self.assertNotIn("schema_version", tables(connection))
        self.assertEqual(schema_version(connection), SCHEMA_VERSION)


class TheMigration(SchemaCase):
    def test_running_it_twice_is_idempotent(self):
        first = migrate(self.path)
        second = migrate(self.path)
        # Every file, not a hard-coded one: the list grows with each
        # migration, and a test naming `[1]` fails on the commit that adds
        # the second for a reason that has nothing to do with idempotency.
        self.assertEqual(first.applied, list(range(1, SCHEMA_VERSION + 1)))
        self.assertEqual(second.applied, [])
        self.assertEqual(second.before, second.after)

    def test_opening_alone_applies_nothing(self):
        raw = sqlite3.connect(self.path)
        raw.close()
        connection = connect(self.path, write=False)
        self.addCleanup(connection.close)
        self.assertEqual(schema_version(connection), 0)
        self.assertEqual(tables(connection), [])

    def test_a_migration_file_that_opens_its_own_transaction_is_refused(self):
        self.assertTrue(schema_module.migrations())
        with tempfile.TemporaryDirectory() as other:
            # One file per version, so `migrations()` sees a complete run
            # ending at SCHEMA_VERSION and gets as far as reading them. Only
            # the last one opens a transaction.
            for version in range(1, SCHEMA_VERSION + 1):
                body = (
                    "BEGIN;\nCREATE TABLE x (a);\nCOMMIT;\n"
                    if version == SCHEMA_VERSION
                    else "CREATE TABLE placeholder_%d (a);\n" % version
                )
                (Path(other) / f"{version:03d}_bad.sql").write_text(body, encoding="utf-8")
            original = schema_module.SCHEMA_DIR
            schema_module.SCHEMA_DIR = Path(other)
            self.addCleanup(setattr, schema_module, "SCHEMA_DIR", original)
            with self.assertRaises(ValueError) as raised:
                migrate(self.path)
            self.assertIn("opens its own transaction", str(raised.exception))

    def test_the_files_are_numbered_without_a_gap(self):
        versions = [version for version, _path in schema_module.migrations()]
        self.assertEqual(versions, list(range(1, SCHEMA_VERSION + 1)))

    def test_a_database_at_version_one_migrates_up_to_the_current_version(self):
        """The upgrade path, not just the fresh install.

        `initialise` applies every file at once, so a suite that only ever
        creates fresh databases never runs migration 2 against a database
        that already has migration 1's shape -- which is the only shape it
        will ever meet on a real machine.
        """
        connection = connect(self.path, create=True, write=True)
        try:
            first = schema_module.migrations()[0][1]
            connection.executescript(
                f"BEGIN;\n{first.read_text(encoding='utf-8')}\n"
                f"PRAGMA user_version = 1;\nCOMMIT;"
            )
        finally:
            connection.close()
        result = migrate(self.path)
        self.assertEqual(result.before, 1)
        self.assertEqual(result.after, SCHEMA_VERSION)
        self.assertEqual(result.applied, list(range(2, SCHEMA_VERSION + 1)))


class TheSourceCommitColumn(SchemaCase):
    """Migration 2. An item can live on a branch the default has never seen,
    and the commit it was read from is what makes that row recoverable."""

    def setUp(self):
        super().setUp()
        initialise(self.path)

    def columns(self):
        connection = connect(self.path, write=False)
        try:
            return {row[1] for row in connection.execute("PRAGMA table_info(item)")}
        finally:
            connection.close()

    def test_item_carries_source_commit(self):
        self.assertIn("source_commit", self.columns())

    def test_no_table_was_added_with_it(self):
        connection = connect(self.path, write=False)
        try:
            found = set(tables(connection))
        finally:
            connection.close()
        self.assertEqual(found, set(TABLES))


class TheShadowTrackerKey(SchemaCase):
    """Migration 8. The key becomes `(tracker, url)`, so a real machine's rows
    have to survive an index swap and a second tracker has to be able to write
    a url the first one already holds."""

    def test_rows_survive_and_a_second_tracker_may_hold_the_same_url(self):
        # Built on the version-7 shape, the only one this migration will meet
        # on a real machine, and seeded on that same connection because
        # `connect` refuses to reopen a database older than the library.
        connection = connect(self.path, create=True, write=True)
        try:
            for number, path in schema_module.migrations():
                if number > 7:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {number};\nCOMMIT;"
                )
            connection.execute(
                "INSERT INTO shadow (id, tracker, repo, url, first_seen, last_seen) "
                "VALUES (5, 'github', 'o/r', 'https://github.com/o/r/issues/1', 't1', 't2')")
            # The shape being migrated away from: one url, one row, whoever.
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO shadow (tracker, url, first_seen, last_seen) "
                    "VALUES ('jira', 'https://github.com/o/r/issues/1', 't3', 't3')")
        finally:
            connection.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (7, list(range(8, SCHEMA_VERSION + 1))))
        connection = connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(
            [tuple(row) for row in connection.execute(
                "SELECT id, tracker, repo, url FROM shadow")],
            [(5, "github", "o/r", "https://github.com/o/r/issues/1")])
        self.assertEqual(
            sorted(row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'shadow'")),
            ["shadow_by_tracker_url"])
        # The other tracker's row is its own, and a repeat of either is not.
        connection.execute(
            "INSERT INTO shadow (tracker, url, first_seen, last_seen) "
            "VALUES ('jira', 'https://github.com/o/r/issues/1', 't3', 't3')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO shadow (tracker, url, first_seen, last_seen) "
                "VALUES ('jira', 'https://github.com/o/r/issues/1', 't4', 't4')")

    def test_the_collector_no_longer_takes_another_tracker_s_row(self):
        """The defect end to end, through the writer the collector calls."""
        from sd_db.writes import upsert_shadow

        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        url = "https://github.com/o/r/issues/1"
        upsert_shadow(connection, tracker="github", url=url, title="from github")
        upsert_shadow(connection, tracker="jira", url=url, title="from jira")
        self.assertEqual(
            [tuple(row) for row in connection.execute(
                "SELECT tracker, title FROM shadow ORDER BY tracker")],
            [("github", "from github"), ("jira", "from jira")])


class TheStateCheckKind(SchemaCase):
    """Migration 7. `check` joins the `state` kinds by rebuilding the table,
    so what a real machine's database holds has to come through: every row
    with its id, both indexes, and the refusal of a kind nobody declared."""

    def test_rows_survive_the_rebuild_and_the_new_kind_is_accepted(self):
        connection = connect(self.path, create=True, write=True)
        try:
            for version, path in schema_module.migrations():
                if version > 6:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;"
                )
            connection.execute("INSERT INTO state (id, kind, key, timestamp, body) VALUES (7, 'heartbeat', 'runner', 't1', '{}')")
            connection.execute("INSERT INTO state (id, kind, key, timestamp, body, resolved_at) "
                               "VALUES (9, 'restore', '2026-09-01', 't2', '{}', 't3')")
        finally:
            connection.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (6, list(range(7, SCHEMA_VERSION + 1))))
        connection = connect(self.path)
        self.addCleanup(connection.close)
        rows = [tuple(row) for row in connection.execute(
            "SELECT id, kind, key, timestamp, body, resolved_at FROM state ORDER BY id")]
        self.assertEqual(rows, [(7, "heartbeat", "runner", "t1", "{}", None),
                                (9, "restore", "2026-09-01", "t2", "{}", "t3")])
        indexes = sorted(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'state'"))
        # `state_by_kind_key` is migration 13's, applied on the way up.
        self.assertEqual(indexes, ["runner_check", "runner_heartbeat", "state_by_kind", "state_by_kind_key"])
        connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('check', 'run-1', 't4', '{}')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('check', 'run-1', 't5', '{}')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('bored', 'x', 't6', '{}')")
        # The heartbeat's one-row-per-key index came back with the table.
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('heartbeat', 'runner', 't7', '{}')")

    def test_the_kinds_the_library_names_are_the_kinds_the_schema_accepts(self):
        from sd_db.writes import STATE_KINDS
        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        for kind in STATE_KINDS:
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES (?, ?, 't', '{}')", (kind, f"k-{kind}"))
        self.assertIn("check", STATE_KINDS)


class TheLatestStateByKindAndKey(SchemaCase):
    """Migration 13 (sd:1433). The latest row for one kind and key is read
    from an index, not found by scanning the kind and sorting it. Without
    it the dashboard's Today page took 1.5 s on the live database."""

    LATEST = "SELECT id, body FROM state WHERE kind = ? AND key = ? ORDER BY id DESC LIMIT 1"

    def plan(self, sql, parameters):
        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        return " | ".join(row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters))

    def test_the_lookup_uses_the_index_and_sorts_nothing(self):
        for kind in ("checkpoint", "heartbeat"):
            plan = self.plan(self.LATEST, (kind, "contribution:item:1"))
            self.assertIn("USING INDEX state_by_kind_key", plan)
            self.assertNotIn("TEMP B-TREE", plan)

    def test_reads_ordered_by_time_still_use_state_by_kind(self):
        # Why `state_by_kind` was kept: `unresolved_state` orders one kind by
        # timestamp, and the new index cannot do that without a sort.
        plan = self.plan("SELECT * FROM state WHERE kind = ? AND resolved_at IS NULL ORDER BY timestamp",
                         ("restore",))
        self.assertIn("USING INDEX state_by_kind ", plan + " ")
        self.assertNotIn("TEMP B-TREE", plan)


class TheKindsForPersonalAndFollowupWork(SchemaCase):
    """Migration 9. Four kinds join `item.kind`, and unlike migration 7 this
    table cannot be rebuilt: four foreign keys reference `item(id)`, two of
    them without an `ON DELETE` clause, so dropping the old copy is refused
    rather than cascaded, and `PRAGMA foreign_keys` cannot be turned off inside
    the transaction `migrate` wraps every file in. The CHECK text is therefore
    edited in place, which makes these the things to prove: nothing was lost,
    nothing that earlier migrations appended was dropped, the indexes are still
    there because no rebuild happened, and an undeclared kind is still refused.
    """

    #: Every kind this migration adds. `idea` is deliberately not here: it is
    #: the writing pack's article ladder, keyed on by `sd_db/writing.py` and by
    #: the dashboard's writing controls, so a personal or work idea filed as
    #: `idea` would enter the publishing pipeline.
    ADDED = ("personal", "followup", "work-idea", "personal-idea")

    def _at_version_eight(self):
        """A database holding item rows and a row in each table that
        references them, so the migration is measured against fan-in and not
        against an empty table."""
        connection = connect(self.path, create=True, write=True)
        try:
            for version, path in schema_module.migrations():
                if version > 8:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;"
                )
            connection.execute("INSERT INTO repo (path, created_at, updated_at) VALUES ('/repo', 't', 't')")
            connection.execute("INSERT INTO item (id, kind, repo, title, status, created_at, updated_at) "
                               "VALUES (4, 'work', '/repo', 'Existing work', 'planning', 't', 't')")
            connection.execute("INSERT INTO note (id, item, timestamp, kind, body) "
                               "VALUES (5, 4, 't', 'comment', 'a note')")
            connection.execute("INSERT INTO assignment (id, item, role, status) "
                               "VALUES (6, 4, 'merge', 'queued')")
            connection.commit()
        finally:
            connection.close()

    def test_rows_and_the_rows_that_reference_them_survive_and_the_kinds_land(self):
        self._at_version_eight()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (8, list(range(9, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(
            [tuple(row) for row in connection.execute(
                "SELECT id, kind, repo, title FROM item ORDER BY id")],
            [(4, "work", "/repo", "Existing work")])
        self.assertEqual([tuple(row) for row in connection.execute(
            "SELECT id, item, kind FROM note ORDER BY id")], [(5, 4, "comment")])
        self.assertEqual([tuple(row) for row in connection.execute(
            "SELECT id, item FROM assignment ORDER BY id")], [(6, 4)])
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        for kind in self.ADDED:
            connection.execute("INSERT INTO item (kind, title, status, created_at, updated_at) "
                               "VALUES (?, ?, 'planning', 't', 't')", (kind, f"a {kind}"))
        # The CHECK is still a CHECK: widening the list did not remove it.
        for refused in ("bored", ""):
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO item (kind, title, status, created_at, updated_at) "
                                   "VALUES (?, 'x', 'planning', 't', 't')", (refused,))

    def test_the_columns_earlier_migrations_appended_are_still_there(self):
        """The in-place edit rewrites the kind list and nothing else. A
        hand-written CREATE TABLE would have dropped every column migrations 2
        through 8 added, and the rows with them."""
        self._at_version_eight()
        before = self._columns()
        migrate(self.path)
        after = self._columns()
        # Every column survives. The only new ones are those a later
        # migration appends with `ADD COLUMN`: 012's recurrence pair.
        self.assertLessEqual(before, after)
        self.assertEqual(after - before, {"recurrence", "recurrence_anchor"})
        self.assertLessEqual({"source_commit", "piece", "parked_at",
                              "gate_generation", "ready_digest"}, after)

    def _columns(self):
        # Raw, not `connect`: before the migration this database is at the
        # older version and the library refuses to open it, by design.
        connection = sqlite3.connect(self.path)
        try:
            return {row[1] for row in connection.execute("PRAGMA table_info(item)")}
        finally:
            connection.close()

    def test_every_index_on_item_is_still_there_because_nothing_was_rebuilt(self):
        self._at_version_eight()
        connection = sqlite3.connect(self.path)
        before = sorted(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'item'"))
        connection.close()
        migrate(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        after = sorted(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'item'"))
        self.assertEqual(before, after)
        self.assertIn("item_by_piece", after)

    def test_the_guard_leaves_no_table_behind_and_writable_schema_goes_back_off(self):
        self._at_version_eight()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual([row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'migration_009%'")], [])
        self.assertEqual(connection.execute("PRAGMA writable_schema").fetchone()[0], 0)
        self.assertEqual(sorted(tables(connection)), sorted(TABLES))

    def _body(self):
        return dict(schema_module.migrations())[9].read_text(encoding="utf-8")

    def test_a_replay_onto_the_shape_it_produces_changes_nothing(self):
        """`migrate` never replays a file, but the restore path does: a
        snapshot can carry the new shape while claiming the older version, and
        `_upgrade_restore_candidate` then runs this migration over it. That must
        be a no-op, not a refusal."""
        self._at_version_eight()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        before = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'item'").fetchone()[0]
        items = connection.execute("SELECT count(*) FROM item").fetchone()[0]
        connection.executescript(f"BEGIN;\n{self._body()}\nCOMMIT;")
        self.assertEqual(connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'item'").fetchone()[0], before)
        self.assertEqual(connection.execute("SELECT count(*) FROM item").fetchone()[0], items)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(connection.execute("PRAGMA writable_schema").fetchone()[0], 0)

    def test_a_shape_this_migration_does_not_recognise_is_refused_untouched(self):
        """The reason the guard exists. An in-place schema edit that cannot find
        its pattern would otherwise widen nothing while `user_version` claimed it
        had, so the database would be a shape no number describes."""
        self._refuses(lambda sql: sql.replace(
            "'proposal', 'skill-review', 'dep'", "'proposal', 'skill-review'"))

    def test_a_shape_that_differs_only_in_case_is_refused_too(self):
        """`LIKE` is case-insensitive for ASCII, so this shape would have passed
        a `LIKE` guard while the case-sensitive `replace` changed nothing -- the
        migration would have bumped the version over a table that still rejected
        the new kinds. The guard uses `GLOB`."""
        self._refuses(lambda sql: sql.replace(
            "'proposal', 'skill-review', 'dep'", "'Proposal', 'Skill-Review', 'Dep'"))

    def _refuses(self, mangle):
        self._at_version_eight()
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        current = raw.execute("SELECT sql FROM sqlite_master WHERE name = 'item'").fetchone()[0]
        mangled = mangle(current)
        self.assertNotEqual(mangled, current, "the mangle matched nothing, so this proves nothing")
        raw.execute("PRAGMA writable_schema = ON")
        raw.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'item'", (mangled,))
        raw.execute("PRAGMA writable_schema = RESET")
        before = raw.execute("SELECT sql FROM sqlite_master WHERE name = 'item'").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            raw.executescript(f"BEGIN;\n{self._body()}\nCOMMIT;")
        try:
            raw.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        self.assertEqual(raw.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'item'").fetchone()[0], before)
        # The refusal came before `writable_schema` was switched on, so an
        # aborted migration never leaves the connection able to edit the schema.
        self.assertEqual(raw.execute("PRAGMA writable_schema").fetchone()[0], 0)
        self.assertEqual(raw.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_the_guard_writes_no_row_when_it_passes(self):
        """The guard aborts by violating the CHECK, so on the happy path it must
        insert nothing at all."""
        self._at_version_eight()
        migrate(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM item WHERE kind LIKE 'migration-%'").fetchone()[0], 0)

    def test_the_new_kinds_are_not_agent_work_even_with_a_repository_and_branch(self):
        """A personal to-do must never be picked up and run by an agent.

        The repository and the branch are the point. Seeding neither proves only
        that the query's JOIN works, which is what the first version of this test
        did; the schema lets any kind carry both, and while delivery was
        `kind != 'skill-review'` a `personal` row that had them was returned.
        """
        from sd_db.runner import delivery_candidates
        self._at_version_eight()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        for kind in self.ADDED:
            connection.execute(
                "INSERT INTO item (kind, repo, branch, title, status, created_at, updated_at) "
                "VALUES (?, '/repo', ?, ?, 'ready_to_send', 't', 't')",
                (kind, f"br-{kind}", f"a {kind}"))
        connection.commit()
        self.assertEqual(delivery_candidates(connection), [])
        # The same fixture for a kind delivery does carry, so the test would fail
        # if the query stopped returning anything at all.
        connection.execute(
            "INSERT INTO item (kind, repo, branch, title, status, created_at, updated_at) "
            "VALUES ('work', '/repo', 'br-work', 'real work', 'ready_to_send', 't', 't')")
        connection.commit()
        self.assertEqual([row["branch"] for row in delivery_candidates(connection)], ["br-work"])

    def test_a_personal_idea_is_not_a_writing_piece(self):
        """`idea` stays the article ladder. The new idea kinds carry no `piece`
        and must not appear in anything the writing pack reads."""
        from sd_db import writing
        self._at_version_eight()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        for kind in ("personal-idea", "work-idea"):
            connection.execute(
                "INSERT INTO item (kind, repo, title, status, stage, created_at, updated_at) "
                "VALUES (?, '/repo', ?, 'planning', 'drafting', 't', 't')", (kind, f"an {kind}"))
        connection.commit()
        self.assertEqual(writing.list_pieces(connection), [])


class TheVersionRefusals(SchemaCase):
    def setUp(self):
        super().setUp()
        initialise(self.path)

    def _set(self, version):
        raw = sqlite3.connect(self.path, isolation_level=None)
        set_schema_version(raw, version)
        raw.close()

    def test_a_newer_database_is_refused_on_open_naming_both(self):
        self._set(SCHEMA_VERSION + 3)
        with self.assertRaises(SchemaTooNew) as raised:
            connect(self.path)
        message = str(raised.exception)
        self.assertIn(str(SCHEMA_VERSION + 3), message)
        self.assertIn(str(SCHEMA_VERSION), message)

    def test_a_newer_database_is_refused_for_reading_too(self):
        """A wrong answer read quietly is worse than a process that stops."""
        self._set(SCHEMA_VERSION + 1)
        with self.assertRaises(SchemaTooNew):
            connect(self.path, write=False)

    def test_an_older_database_is_refused_on_write_naming_the_command(self):
        self._set(0)
        with self.assertRaises(SchemaTooOld) as raised:
            connect(self.path)
        self.assertIn("sd-db.sh migrate", str(raised.exception))

    def test_an_older_database_still_opens_for_reading(self):
        self._set(0)
        connection = connect(self.path, write=False)
        self.addCleanup(connection.close)
        self.assertEqual(schema_version(connection), 0)


class TheRunnerMergeColumn(SchemaCase):
    """Migration 10. `repo.merge_policy` becomes `repo.runner_merge`.

    The old name said what the column was, not who reads it, and it sat beside
    the pack's assistant grant under a name that read the same. The rebuild is
    007's, so the things to prove are 007's too: the recorded value survives,
    the CHECK came with the column, and the rows that reference `repo` still
    reach it rather than the copy the rebuild dropped.
    """

    def _at_version_nine(self):
        connection = connect(self.path, create=True, write=True)
        try:
            for version, path in schema_module.migrations():
                # 9 literally, never `SCHEMA_VERSION - 1`. The relative form
                # names this migration's predecessor only while 010 is the
                # last one; the next bump would quietly build a v10 database
                # and leave 010 unexercised by the test written for it.
                if version > 9:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.execute(
                "INSERT INTO repo (path, remote, merge_policy, created_at, updated_at) "
                "VALUES ('/repo', 'git@example:o/r.git', 'auto', 't', 't')")
            connection.execute(
                "INSERT INTO item (id, kind, repo, title, status, created_at, updated_at) "
                "VALUES (4, 'work', '/repo', 'Existing work', 'planning', 't', 't')")
            connection.commit()
        finally:
            connection.close()

    def test_the_column_is_renamed_and_the_recorded_policy_survives(self):
        self._at_version_nine()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (9, list(range(10, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        columns = [row["name"] for row in connection.execute("PRAGMA table_info(repo)")]
        self.assertIn("runner_merge", columns)
        self.assertNotIn("merge_policy", columns)
        self.assertEqual(
            connection.execute("SELECT runner_merge FROM repo WHERE path = '/repo'").fetchone()[0],
            "auto")

    def test_the_check_and_the_default_came_with_the_column(self):
        self._at_version_nine()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        connection.execute("INSERT INTO repo (path, created_at, updated_at) VALUES ('/fresh', 't', 't')")
        self.assertEqual(
            connection.execute("SELECT runner_merge FROM repo WHERE path = '/fresh'").fetchone()[0],
            "manual")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE repo SET runner_merge = 'whenever' WHERE path = '/fresh'")

    def test_the_rows_that_reference_the_rebuilt_table_still_reach_it(self):
        self._at_version_nine()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(
            connection.execute("SELECT title FROM item WHERE repo = '/repo'").fetchone()[0],
            "Existing work")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO item (kind, repo, title, status, created_at, updated_at) "
                "VALUES ('work', '/absent', 't', 'planning', 't', 't')")


class TheManagedColumn(SchemaCase):
    """Migration 15. `repo.managed` names the repositories the operator manages.

    Sessions inferred the answer from remotes and prose; the column makes it
    one stored fact. It is added, not rebuilt: an existing row keeps every
    value and reads 0 until something sets it, and the CHECK holds the
    column to the two values a flag has.
    """

    def _at_version_fourteen(self):
        connection = connect(self.path, create=True, write=True)
        try:
            # 014 converts paths with two functions `migrate` registers.
            paths.install(connection)
            for version, path in schema_module.migrations():
                # 14 literally, for the reason `_at_version_nine` gives.
                if version > 14:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.execute(
                "INSERT INTO repo (path, remote, runner_merge, created_at, updated_at) "
                "VALUES ('/repo', 'git@github.com:platypeeps/r.git', 'auto', 't', 't')")
            connection.commit()
        finally:
            connection.close()

    def test_the_column_arrives_at_zero_and_the_row_keeps_its_values(self):
        self._at_version_fourteen()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (14, list(range(15, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        row = connection.execute("SELECT * FROM repo WHERE path = '/repo'").fetchone()
        self.assertEqual((row["managed"], row["runner_merge"], row["remote"]),
                         (0, "auto", "git@github.com:platypeeps/r.git"))

    def test_the_check_holds_the_column_to_zero_and_one(self):
        self._at_version_fourteen()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        connection.execute("UPDATE repo SET managed = 1 WHERE path = '/repo'")
        for value in (2, -1, "yes", None):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE repo SET managed = ? WHERE path = '/repo'", (value,))
        self.assertEqual(
            connection.execute("SELECT managed FROM repo WHERE path = '/repo'").fetchone()[0], 1)


class TheCiColumn(SchemaCase):
    """Migration 16. `repo.ci` says where a repository's checks run (sd:1843).

    `github` is GitHub Actions, which every repository used before the
    column; `local` is `sd-check` run by the pack, posting `sd/local-gate`.
    It is added, not rebuilt: every existing row reads `github` and keeps its
    other values, and the CHECK holds the column to the two modes.
    """

    def _at_version_fifteen(self):
        connection = connect(self.path, create=True, write=True)
        try:
            # 014 converts paths with two functions `migrate` registers.
            paths.install(connection)
            for version, path in schema_module.migrations():
                # 15 literally, for the reason `_at_version_nine` gives.
                if version > 15:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.executemany(
                "INSERT INTO repo (path, remote, runner_merge, managed, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 't', 't')",
                [("/one", "git@github.com:platypeeps/one.git", "auto", 1),
                 ("/two", None, "manual", 0)])
            connection.commit()
        finally:
            connection.close()

    def test_every_existing_row_arrives_at_github_and_keeps_its_values(self):
        self._at_version_fifteen()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (15, list(range(16, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        rows = [tuple(row) for row in connection.execute(
            "SELECT path, ci, runner_merge, managed, remote FROM repo ORDER BY path")]
        self.assertEqual(rows, [
            ("/one", "github", "auto", 1, "git@github.com:platypeeps/one.git"),
            ("/two", "github", "manual", 0, None)])

    def test_the_check_holds_the_column_to_github_and_local(self):
        self._at_version_fifteen()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        connection.execute("UPDATE repo SET ci = 'local' WHERE path = '/one'")
        for value in ("actions", "GITHUB", "", None):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE repo SET ci = ? WHERE path = '/one'", (value,))
        self.assertEqual(
            connection.execute("SELECT ci FROM repo WHERE path = '/one'").fetchone()[0], "local")


class TheConnection(SchemaCase):
    def test_wal_and_foreign_keys_are_on(self):
        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_a_missing_database_is_not_created_silently(self):
        with self.assertRaises(FileNotFoundError) as raised:
            connect(self.home / ".local/share/sd/absent.db")
        self.assertIn("sd-db.sh init", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
