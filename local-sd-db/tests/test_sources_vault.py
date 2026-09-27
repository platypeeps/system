"""The vault migration: `stage` verbatim, the mapping table, and the refusal.

Criterion 5's idea clauses. The set comparison, the unmapped word, and the
mapping table read against the writing manifest -- which lives in another
repository, so the assertion runs against the real file when it is on disk
and against a fixture of the same shape either way.
"""

import json
import tempfile
import unittest
from pathlib import Path

from sd_db import connect
from sd_db.migrate import initialise
from sd_db.sources import MigrationRefused, run
from sd_db.sources.vault import (
    SOURCE,
    STAGES,
    Reader,
    manifest_path,
    manifest_targets,
    stages_in,
)

from . import support

FIXTURE_MANIFEST = Path(__file__).resolve().parent / "fixtures" / "sd-plugin.json"


class VaultCase(unittest.TestCase):
    ideas = {"first idea": "inbox", "second idea": "drafting", "third idea": "published"}
    topics = {"a topic": "active", "another topic": "parked"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        database = self.root / "sd.db"
        initialise(database)
        self.connection = connect(database)
        self.addCleanup(self.connection.close)
        self.vault = support.vault(
            self.root / "vault", ideas=self.ideas, topics=self.topics
        )

    def reader(self):
        return Reader.at(self.vault)

    def items(self):
        return {
            row["external_id"]: row
            for row in self.connection.execute(
                "SELECT * FROM item WHERE source = ?", (SOURCE,)
            )
        }


class TheImport(VaultCase):
    def test_both_bases_land_as_idea_rows(self):
        sitting = run(self.connection, self.reader())
        self.assertTrue(sitting.clean)
        self.assertEqual(sitting.counts.seen, 5)
        kinds = {row["kind"] for row in self.items().values()}
        self.assertEqual(kinds, {"idea"})

    def test_the_ladder_word_is_kept_verbatim_in_stage(self):
        run(self.connection, self.reader())
        found = {row["stage"] for row in self.items().values()}
        self.assertEqual(found, {"inbox", "drafting", "published", "active", "parked"})

    def test_the_stage_set_in_the_rows_equals_the_set_in_the_source(self):
        """Criterion 5's set comparison, run over the frozen source."""
        reader = self.reader()
        frozen = reader.freeze()
        run(self.connection, reader)
        self.assertEqual(
            stages_in(frozen.records),
            {row["stage"] for row in self.items().values()},
        )

    def test_the_status_is_this_table_s_answer_and_not_the_ladder_word(self):
        run(self.connection, self.reader())
        by_stage = {row["stage"]: row["status"] for row in self.items().values()}
        self.assertEqual(by_stage["inbox"], "planning")
        self.assertEqual(by_stage["drafting"], "in_progress")
        self.assertEqual(by_stage["published"], "done")
        self.assertEqual(by_stage["active"], "in_progress")
        self.assertEqual(by_stage["parked"], "blocked")

    def test_a_second_run_reports_the_same_counts_with_zero_new_rows(self):
        first = run(self.connection, self.reader())
        second = run(self.connection, self.reader())
        self.assertEqual(first.counts.seen, second.counts.seen)
        self.assertEqual(second.counts.inserted, 0)
        self.assertEqual(second.counts.unchanged, second.counts.seen)

    def test_the_import_retires_nothing(self):
        before = {
            path: path.read_bytes()
            for path in sorted(self.vault.rglob("*.md"))
        }
        run(self.connection, self.reader())
        after = {
            path: path.read_bytes()
            for path in sorted(self.vault.rglob("*.md"))
        }
        self.assertEqual(before, after)

    def test_the_note_body_and_fields_land_on_the_row(self):
        run(self.connection, self.reader())
        row = self.items()["blog-idea:System/Databases/Blog Ideas/first idea.md"]
        self.assertIn("the blog-idea body of first idea", json.loads(row["body"])["markdown"])
        self.assertEqual(json.loads(row["fields"])["kind"], "blog-idea")


class TheRefusals(VaultCase):
    def test_a_stage_the_table_does_not_name_refuses_with_the_word(self):
        """Criterion 5: the word, not "an unknown stage"."""
        note = self.vault / "System/Databases/Blog Ideas/first idea.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace("status: inbox", "status: marinating"),
            encoding="utf-8",
        )
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, self.reader())
        self.assertIn("'marinating'", str(caught.exception))
        self.assertEqual(self.items(), {})

    def test_a_note_with_no_status_refuses(self):
        note = self.vault / "System/Databases/Topics/a topic.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace("status: active\n", ""),
            encoding="utf-8",
        )
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, self.reader())
        self.assertIn("no `status:`", str(caught.exception))

    def test_an_absent_base_refuses_rather_than_importing_zero(self):
        for path in (self.vault / "System/Databases/Topics").iterdir():
            path.unlink()
        (self.vault / "System/Databases/Topics").rmdir()
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, self.reader())
        self.assertIn("Topics", str(caught.exception))


class TheMappingTableCoversTheManifest(unittest.TestCase):
    """Criterion 5's last clause, asserted by reading the manifest."""

    def assert_covers(self, path):
        targets = manifest_targets(path)
        missing = []
        for kind, words in targets.items():
            for word in words:
                if (kind, word) not in STAGES:
                    missing.append(f"{kind}/{word}")
        self.assertEqual(
            sorted(missing), [],
            "the mapping table does not name every status word the manifest can "
            "put a note in",
        )
        return targets

    def test_against_the_fixture_manifest_with_the_seven_target_ladder(self):
        """The shape item C's pull request produces, asserted here today.

        B's table and C's manifest land in different repositories. A table
        naming only today's four `blog-idea` targets would break the moment C
        merged, with nothing in B changing.
        """
        targets = self.assert_covers(FIXTURE_MANIFEST)
        self.assertEqual(
            targets["blog-idea"],
            {"inbox", "accepted", "researching", "drafting", "review", "ready",
             "published", "declined"},
        )

    def test_against_the_writing_pack_s_own_manifest(self):
        path = manifest_path()
        if not path.exists():
            self.skipTest(f"the writing pack's manifest is not at {path}")
        self.assert_covers(path)


if __name__ == "__main__":
    unittest.main()
