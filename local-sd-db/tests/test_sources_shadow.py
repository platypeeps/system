"""The two sources that fill `shadow`: the pack's cache, and GitHub's issues.

`shadow` is shared, so each of these answers only for the rows it froze. A
row the nightly sync adds afterwards is not a row the cache lost, and the
verify has to say so or it reports a difference every time the collector
runs.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from sd_db import connect
from sd_db.migrate import initialise
from sd_db.sources import MigrationRefused, run, verify
from sd_db.sources.index_cache import Reader as CacheReader
from sd_db.sources.issues import Reader as IssueReader

from .test_shadow_sync import Gh, node

CACHE_SCHEMA = """
CREATE TABLE issue (
    id TEXT PRIMARY KEY, tracker TEXT NOT NULL, url TEXT NOT NULL,
    repo TEXT NOT NULL, number INTEGER, kind TEXT NOT NULL, title TEXT NOT NULL,
    state TEXT NOT NULL, author TEXT NOT NULL, updated_at TEXT NOT NULL,
    why TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE tracker_watermark (
    tracker TEXT PRIMARY KEY, collected_at TEXT NOT NULL, window_start TEXT NOT NULL
);
"""


class ShadowSourceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        database = self.root / "sd.db"
        initialise(database)
        self.connection = connect(database)
        self.addCleanup(self.connection.close)

    def rows(self):
        return {row["url"]: row for row in self.connection.execute("SELECT * FROM shadow")}


class TheCache(ShadowSourceCase):
    def cache(self, count=3, *, schema=CACHE_SCHEMA):
        path = self.root / "index.sqlite"
        # The pack's cache, built the way the pack builds it. This test opens
        # a database the library does not own, which is why it lives beside
        # the library rather than anywhere else.
        connection = sqlite3.connect(path)
        connection.executescript(schema)
        for number in range(1, count + 1):
            url = f"https://github.com/example-org/widget/issues/{number}"
            connection.execute(
                "INSERT INTO issue VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"github:{url}", "github", url, "example-org/widget", number, "issue",
                 f"issue {number}", "open", "octocat", "2026-09-01T00:00:00Z",
                 '["author"]', "2026-09-01", "2026-09-05"),
            )
        connection.commit()
        connection.close()
        return path

    def test_every_cached_row_lands_as_a_shadow_row(self):
        sitting = run(self.connection, CacheReader.at(self.cache()))
        self.assertTrue(sitting.clean)
        self.assertEqual(sitting.counts.seen, 3)
        self.assertEqual(sitting.counts.inserted, 3)
        self.assertEqual(len(self.rows()), 3)

    def test_a_second_run_reports_the_same_counts_with_zero_new_rows(self):
        path = self.cache()
        first = run(self.connection, CacheReader.at(path))
        second = run(self.connection, CacheReader.at(path))
        self.assertEqual(first.counts.seen, second.counts.seen)
        self.assertEqual(second.counts.inserted, 0)
        self.assertEqual(second.counts.unchanged, 3)
        self.assertEqual(len(self.rows()), 3)

    def test_one_url_under_two_trackers_lands_as_two_rows(self):
        """The cache keys on `(tracker, url)` and so does `shadow` (sd:603).

        A source keyed by url alone would drop one of these at freeze time,
        before the store's composite key could protect anything: the import
        would land one row where the pack held two, and the verify would call
        it clean because it compared the same collapsed set to itself.
        """
        path = self.cache(count=1)
        url = "https://github.com/example-org/widget/issues/1"
        connection = sqlite3.connect(path)
        connection.execute(
            "INSERT INTO issue VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"jira:{url}", "jira", url, "example-org/widget", 1, "issue",
             "the jira one", "open", "octocat", "2026-09-01T00:00:00Z",
             '["author"]', "2026-09-01", "2026-09-05"),
        )
        connection.commit()
        connection.close()
        sitting = run(self.connection, CacheReader.at(path))
        self.assertTrue(sitting.clean, sitting.report())
        self.assertEqual((sitting.counts.seen, sitting.counts.inserted), (2, 2))
        self.assertEqual(
            [tuple(row) for row in self.connection.execute(
                "SELECT tracker, url, title FROM shadow ORDER BY tracker")],
            [("github", url, "issue 1"), ("jira", url, "the jira one")])

    def test_a_padded_tracker_lands_as_the_tracker_it_is_keyed_by(self):
        """The identity trims the tracker, so the stored row must too.

        Otherwise `" github "` freezes as `github` but lands as `" github "`,
        and the next sync's `github` row for the same url is a second row
        (sd:1226).
        """
        path = self.cache(count=1)
        connection = sqlite3.connect(path)
        connection.execute("UPDATE issue SET tracker = ' github '")
        connection.commit()
        connection.close()
        sitting = run(self.connection, CacheReader.at(path))
        self.assertTrue(sitting.clean, sitting.report())
        self.assertEqual(
            [row[0] for row in self.connection.execute("SELECT tracker FROM shadow")],
            ["github"],
        )

    def test_a_tracker_that_cannot_be_keyed_is_refused(self):
        path = self.cache(count=1)
        connection = sqlite3.connect(path)
        connection.execute("UPDATE issue SET tracker = 'two words'")
        connection.commit()
        connection.close()
        with self.assertRaises(MigrationRefused) as raised:
            run(self.connection, CacheReader.at(path))
        self.assertIn("must be one word", str(raised.exception))

    def test_the_import_retires_nothing(self):
        path = self.cache()
        before = path.read_bytes()
        run(self.connection, CacheReader.at(path))
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), before)

    def test_an_absent_cache_refuses_rather_than_importing_zero(self):
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, CacheReader.at(self.root / "gone.sqlite"))
        self.assertIn("no cache at", str(caught.exception))

    def test_a_database_that_is_not_the_pack_s_index_refuses(self):
        path = self.root / "other.sqlite"
        connection = sqlite3.connect(path)
        connection.executescript("CREATE TABLE something_else (id INTEGER)")
        connection.close()
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, CacheReader.at(path))
        self.assertIn("no `issue` table", str(caught.exception))

    def test_the_verify_ignores_rows_the_cache_never_held(self):
        """`shadow` is shared with every later sync."""
        path = self.cache()
        reader = CacheReader.at(path)
        run(self.connection, reader)
        from sd_db.writes import upsert_shadow

        upsert_shadow(
            self.connection, tracker="github",
            url="https://github.com/example-org/widget/issues/99",
            title="arrived after the freeze",
        )
        frozen = reader.freeze()
        self.assertEqual(verify(self.connection, reader, frozen), [])

    def test_a_row_changed_behind_the_import_is_named(self):
        path = self.cache()
        reader = CacheReader.at(path)
        run(self.connection, reader)
        self.connection.execute(
            "UPDATE shadow SET state = 'closed' WHERE number = 2"
        )
        frozen = reader.freeze()
        differences = verify(self.connection, reader, frozen)
        self.assertEqual(len(differences), 1)
        self.assertEqual(differences[0].what, "state")
        self.assertEqual(differences[0].in_source, "open")
        self.assertEqual(differences[0].in_rows, "closed")


class TheOpenIssues(ShadowSourceCase):
    def test_open_issues_land_in_shadow_and_never_as_items(self):
        """Neither closes on GitHub, and neither becomes an item."""
        runner = Gh(nodes=[node(11), node(12)])
        sitting = run(self.connection, IssueReader(runner=runner))
        self.assertTrue(sitting.clean)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(
            list(self.connection.execute("SELECT COUNT(*) FROM item"))[0][0], 0
        )

    def test_the_query_asks_for_open_issues_the_operator_authored(self):
        runner = Gh(nodes=[])
        run(self.connection, IssueReader(runner=runner))
        self.assertEqual(runner.queries, ["author:@me is:issue is:open"])

    def test_the_repositories_are_reported_from_the_issues(self):
        """Built by reading the issues, not from a sentence in a document."""
        sitting = run(self.connection, IssueReader(runner=Gh(nodes=[node(11)])))
        self.assertIn("example-org/benchmark", "\n".join(sitting.notes))

    def test_a_second_run_reports_the_same_counts_with_zero_new_rows(self):
        first = run(self.connection, IssueReader(runner=Gh(nodes=[node(11)])))
        second = run(self.connection, IssueReader(runner=Gh(nodes=[node(11)])))
        self.assertEqual(first.counts.seen, second.counts.seen)
        self.assertEqual(second.counts.inserted, 0)
        self.assertEqual(second.counts.unchanged, 1)

    def test_a_missing_credential_refuses_rather_than_importing_zero(self):
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, IssueReader(runner=Gh(auth=False)))
        self.assertIn("cannot read GitHub", str(caught.exception))

    def test_a_truncated_search_refuses_rather_than_importing_a_partial_answer(self):
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, IssueReader(runner=Gh(nodes=[node(11)], count=900)))
        self.assertIn("cut short", str(caught.exception))
        self.assertEqual(self.rows(), {})


if __name__ == "__main__":
    unittest.main()
