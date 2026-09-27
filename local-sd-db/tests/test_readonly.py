"""Reading cannot become a database write, including on an older schema."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from sd_db.database import connect, schema_version
from sd_db.migrate import initialise
from sd_db.writes import create_item


class ReadOnlyConnection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "db?#.sqlite"
        initialise(self.path)

    def test_read_connection_refuses_data_and_schema_writes(self):
        reader = connect(self.path, write=False)
        self.addCleanup(reader.close)
        for statement in (
            "DELETE FROM item", "CREATE TABLE forbidden (value)",
            "PRAGMA user_version = 99",
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.OperationalError):
                reader.execute(statement)
        with self.assertRaises(sqlite3.OperationalError):
            create_item(reader, kind="task", title="Must not be written")
        self.assertEqual(reader.execute("SELECT count(*) FROM item").fetchone()[0], 0)

    def test_reading_older_schema_does_not_bypass_write_refusal(self):
        writer = connect(self.path)
        writer.execute("PRAGMA user_version = 0")
        writer.close()
        reader = connect(self.path, write=False)
        self.addCleanup(reader.close)
        self.assertEqual(schema_version(reader), 0)
        with self.assertRaises(sqlite3.OperationalError):
            create_item(reader, kind="task", title="Wrong version")

    def test_read_open_does_not_change_main_bytes_or_journal_mode(self):
        writer = sqlite3.connect(self.path)
        self.assertEqual(writer.execute("PRAGMA journal_mode = DELETE").fetchone()[0], "delete")
        writer.close()
        before = self.path.read_bytes()
        listing = set(self.path.parent.iterdir())
        reader = connect(self.path, write=False)
        self.assertEqual(reader.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        reader.execute("SELECT * FROM item").fetchall()
        reader.close()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(set(self.path.parent.iterdir()), listing)

    def test_reading_a_live_wal_sees_committed_rows_without_changing_it(self):
        writer = connect(self.path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        item = create_item(writer, kind="task", title="Still in the WAL")
        wal = self.path.with_name(self.path.name + "-wal")
        self.assertGreater(wal.stat().st_size, 0)
        before = (self.path.read_bytes(), wal.read_bytes())
        reader = connect(self.path, write=False)
        self.addCleanup(reader.close)
        self.assertEqual(reader.execute("SELECT title FROM item WHERE id = ?", (item,)).fetchone()[0], "Still in the WAL")
        self.assertEqual((self.path.read_bytes(), wal.read_bytes()), before)

    def test_readonly_creation_is_refused_without_creating_anything(self):
        missing = self.path.parent / "absent" / "db"
        with self.assertRaises(ValueError):
            connect(missing, write=False, create=True)
        self.assertFalse(missing.parent.exists())

    def test_turning_off_query_only_still_cannot_write_main(self):
        reader = connect(self.path, write=False)
        self.addCleanup(reader.close)
        reader.execute("PRAGMA query_only = OFF")
        with self.assertRaises(sqlite3.OperationalError):
            reader.execute("DELETE FROM item")


if __name__ == "__main__":
    unittest.main()
