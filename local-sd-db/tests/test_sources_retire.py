"""The `docs/work` retire: one sitting, against a real fixture repository.

Criterion 7's retire clauses and criterion 13's, which are the same clauses
read from two items. Everything here runs against a git repository built on
disk with real commits, because the freeze reads branches through git and a
fake would exercise none of it, and the fixture is diffed after each phase --
files, rows, and the commit -- which is what both criteria ask for.

What is deliberately absent, because it is item B's own work in later pull
requests: `sd restore reimport`, the `retiring` status and its note, the
refusal of a concurrent status change from a second checkout, the
branch-divergence landing, and restoring into a retired checkout.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import connect
from sd_db.migrate import initialise
from sd_db.repos import add
from sd_db.sources import MigrationRefused, docs_work, retire, run
from sd_db.sources.docs_work import MARKER, ROW, SOURCE, Reader, marker, retired

from . import support


class RetireCase(unittest.TestCase):
    """A machine: a home with a database and an installed pack, and a repo.

    The database sits where `backup.run` looks for it, because the sitting
    takes its snapshot through the ordinary backup path and a test that
    pointed that somewhere else would be proving a path nothing runs.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / ".local/share/sd").mkdir(parents=True)
        initialise(home=self.home)
        self.connection = connect(home=self.home)
        self.addCleanup(self.connection.close)
        self.checkout = support.repository(
            self.root / "one", bare=self.root / "one.git"
        )
        self.pack = support.pack(self.home)

    # ------------------------------------------------------------- fixture

    def land(self, slug, **kwargs):
        support.write_item(self.checkout, slug, **kwargs)
        support.commit(self.checkout, slug)
        support.push(self.checkout)

    def archive(self, month, slug, **kwargs):
        support.write_archived_item(self.checkout, month, slug, **kwargs)
        support.commit(self.checkout, f"archive {slug}")
        support.push(self.checkout)

    def register(self):
        self.registered = add(self.connection, self.checkout, home=self.home)
        return self.registered

    def fleet(self, active=3, archived=2):
        """Active items and archived ones, then registered and imported once.

        The shape criterion 13 names: a `done` item and open ones, plus an
        archive whose lines the retire must leave exactly where they are.
        """
        for index in range(active):
            self.land(
                f"2026-07-0{index + 1}-item-{index}",
                title=f"item {index}",
                status="done" if index == 0 else "planning",
            )
        for index in range(archived):
            self.archive("2026-06", f"2026-06-0{index + 1}-old-{index}")
        self.register()
        run(self.connection, self.reader())

    def reader(self):
        return Reader.from_table(self.connection)

    def narrowed(self):
        return self.reader().for_retire()

    def retire(self, source=None):
        """The verb's own call: the whole reader, narrowed by `retire` itself."""
        return retire(
            self.connection,
            self.reader() if source is None else source,
            token="docs-work",
            home=self.home,
            environ={},
        )

    # -------------------------------------------------------------- probes

    def active_files(self):
        return sorted(
            path for path in self.checkout.glob("docs/work/*/prd.md") if path.is_file()
        )

    def archived_files(self):
        return sorted(self.checkout.glob("docs/work/archive/*/*/prd.md"))

    def lines(self, paths):
        return {
            str(path.relative_to(self.checkout)): [
                line for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("status:")
            ]
            for path in paths
        }

    def rows(self):
        return {
            row["external_id"]: (row["status"], row["title"])
            for row in self.connection.execute(
                "SELECT * FROM item WHERE source = ?", (SOURCE,)
            )
        }

    def status_source(self):
        return self.connection.execute(
            "SELECT status_source FROM repo WHERE path = ?", (self.registered,)
        ).fetchone()["status_source"]

    def head(self):
        return support.git(self.checkout, "rev-parse", "HEAD")

    def commits(self):
        return support.git(self.checkout, "rev-list", "--count", "HEAD")

    def unchanged(self, before):
        """Assert the source is exactly as it was: lines, marker, commit."""
        self.assertEqual(self.lines(self.active_files()), before["active"])
        self.assertEqual(self.lines(self.archived_files()), before["archived"])
        self.assertEqual(self.head(), before["head"])
        self.assertIsNone(marker(self.checkout))
        self.assertFalse((self.checkout / MARKER).exists())

    def snapshot_of_source(self):
        return {
            "active": self.lines(self.active_files()),
            "archived": self.lines(self.archived_files()),
            "head": self.head(),
        }


# ------------------------------------------------------------------ refusals


class TheRetireRefuses(RetireCase):
    def test_under_a_pack_whose_sd_lib_cannot_read_the_row_naming_the_version(self):
        """Criterion 13's clause, and the whole reason `pack.py` exists."""
        self.fleet()
        support.pack(self.home, library="def status(root, item):\n    return 'no'\n")
        before = self.snapshot_of_source()
        rows = self.rows()
        with self.assertRaises(MigrationRefused) as caught:
            self.retire()
        self.assertIn("47d41245a470", str(caught.exception))
        self.assertIn("status_marker", str(caught.exception))
        self.assertIn("delivered", str(caught.exception))
        self.unchanged(before)
        self.assertEqual(self.rows(), rows)
        self.assertEqual(self.status_source(), "file")

    def test_under_a_pack_carrying_delivered_and_not_the_row_readers(self):
        """`eb7695c7`, the version a `delivered`-only guard waved through.

        The sitting must stop here with every `status:` line in place. If it
        did not, the lines would be gone and the marker written under a pack
        with no `status_marker` to read it -- the source retired under
        exactly the pack that cannot read what replaces it.
        """
        self.fleet()
        support.pack(self.home, library=support.PACK_BEFORE_THE_ROW_READERS)
        before = self.snapshot_of_source()
        with self.assertRaises(MigrationRefused) as caught:
            self.retire()
        self.assertIn("status_marker", str(caught.exception))
        self.unchanged(before)
        self.assertEqual(self.status_source(), "file")

    def test_with_no_pack_installed_at_all(self):
        """The fail-open shape: no receipt read as "nothing to check"."""
        self.fleet()
        (self.home / ".local/state/sd-ai-command-pack/installed.json").unlink()
        before = self.snapshot_of_source()
        with self.assertRaises(MigrationRefused) as caught:
            self.retire()
        self.assertIn("no receipt", str(caught.exception))
        self.unchanged(before)
        self.assertEqual(self.status_source(), "file")

    def test_a_source_with_no_retire_step_says_so_and_stops(self):
        """Four of the five, and a missing attribute is not an error message."""
        self.fleet()

        class Vaultish:
            name = "vault"

        with self.assertRaises(MigrationRefused) as caught:
            self.retire(Vaultish())
        self.assertIn("no retire step built", str(caught.exception))
        self.assertIn("vault", str(caught.exception))

    def test_a_source_never_verified_is_refused_before_anything_is_read(self):
        """No `verified` row at all: the operator is told to import first,
        rather than having an import run underneath them."""
        self.fleet()
        self.connection.execute("DELETE FROM state WHERE kind = 'verified'")
        before = self.snapshot_of_source()
        with self.assertRaises(MigrationRefused) as caught:
            self.retire()
        self.assertIn("never been verified", str(caught.exception))
        self.assertIn("sd-db.sh import", str(caught.exception))
        self.unchanged(before)

    def test_a_verify_difference_lifts_the_freeze_with_every_line_in_place(self):
        """Criterion 13's clause, from the source's side.

        A row the source no longer has is the difference an import cannot
        settle -- re-importing puts an edited title back, so the seeded drift
        has to be one the import will not overwrite.
        """
        self.fleet()
        self.connection.execute(
            "UPDATE item SET external_id = external_id || '-drifted' "
            "WHERE source = ? AND status = 'done'", (SOURCE,)
        )
        before = self.snapshot_of_source()
        with self.assertRaises(MigrationRefused) as caught:
            self.retire()
        self.assertIn("wrote no `verified` row for source hash", str(caught.exception))
        self.assertIn("no longer in the source", str(caught.exception))
        self.unchanged(before)
        self.assertEqual(self.status_source(), "file")

    def test_an_uncommitted_prd_makes_the_sitting_refuse_naming_it(self):
        """C's round eight."""
        self.fleet()
        support.write_item(self.checkout, "2026-07-02-item-1", status="in_progress")
        before = self.snapshot_of_source()
        with self.assertRaises(MigrationRefused) as caught:
            self.retire()
        self.assertIn("docs/work/2026-07-02-item-1/prd.md", str(caught.exception))
        self.assertIn("uncommitted", str(caught.exception))
        self.unchanged(before)
        self.assertEqual(self.status_source(), "file")


# ------------------------------------------------------------- the sitting


class TheSitting(RetireCase):
    def setUp(self):
        super().setUp()
        self.fleet()

    def test_the_final_import_moves_a_changed_line_into_the_row_and_verifies(self):
        """Criterion 13: the old command moved a line after the first import.

        The sitting imports once more before it retires anything, which is
        the only reason that change is not lost when the line goes.
        """
        support.write_item(
            self.checkout, "2026-07-02-item-1", title="item 1", status="done"
        )
        support.commit(self.checkout, "the old command finished it")
        support.push(self.checkout)
        self.assertEqual(self.rows()[self.identity("2026-07-02-item-1")][0], "planning")
        result = self.retire()
        self.assertTrue(result.sitting.clean)
        self.assertEqual(self.rows()[self.identity("2026-07-02-item-1")][0], "done")

    def identity(self, slug):
        return f"{self.registered}::docs/work/{slug}/prd.md"

    def test_the_archive_is_listed_once_per_repository(self):
        """`archived` runs `git ls-tree -r`; the count and the note share it."""
        with patch.object(docs_work, "archived", wraps=docs_work.archived) as spy:
            result = self.retire()
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(result.retired.kept, 2)
        self.assertIn("2 archived kept", "\n".join(result.retired.notes))

    def test_every_active_line_goes_and_every_archived_line_stays(self):
        """Both halves, asserted separately. The pack's own numbers on
        2026-09-06 were 5 active and 491 archived; the shape is what is
        asserted here, and the shape is the one-star glob."""
        archived_before = self.lines(self.archived_files())
        result = self.retire()
        self.assertEqual(
            self.lines(self.active_files()),
            {str(path.relative_to(self.checkout)): [] for path in self.active_files()},
        )
        self.assertEqual(self.lines(self.archived_files()), archived_before)
        self.assertEqual(result.retired.removed, 3)
        self.assertEqual(result.retired.kept, 2)
        self.assertNotEqual(archived_before, {})
        for names in archived_before.values():
            self.assertEqual(len(names), 1)

    def test_the_marker_is_committed_and_holds_one_line_saying_row(self):
        self.retire()
        self.assertEqual((self.checkout / MARKER).read_text(encoding="utf-8"), "row\n")
        self.assertEqual(marker(self.checkout), ROW)
        self.assertTrue(retired(self.checkout))

    def test_the_removal_and_the_marker_are_one_commit(self):
        """One commit, so a checkout is never between the two states."""
        before = int(self.commits())
        self.retire()
        self.assertEqual(int(self.commits()), before + 1)
        changed = support.git(
            self.checkout, "show", "--name-only", "--format=", "HEAD"
        ).split()
        self.assertIn(MARKER, changed)
        for path in self.active_files():
            self.assertIn(str(path.relative_to(self.checkout)), changed)
        self.assertEqual(len(changed), 4)

    def test_the_commit_says_who_wrote_it(self):
        """The pack refuses to review a commit with no `Authored-with:`.

        This migration writes commits into pack checkouts, so a message
        without the trailer lands a commit that fails the receiving
        repository's own review gate. It did, on 2026-09-07, and had to be
        repaired by hand.
        """
        self.retire()
        message = support.git(self.checkout, "log", "-1", "--format=%B")
        self.assertIn("Authored-with: human", message.splitlines())

    def test_the_repository_row_reads_status_from_the_row(self):
        self.assertEqual(self.status_source(), "file")
        result = self.retire()
        self.assertEqual(self.status_source(), ROW)
        self.assertIn(self.registered, result.retired.switched)

    def test_the_working_tree_is_clean_afterwards(self):
        """A retire that left the removal unstaged would refuse its own next
        freeze, and would have moved the source without recording it."""
        self.retire()
        self.assertEqual(
            support.git(self.checkout, "status", "--porcelain", "--", "docs/work"), ""
        )

    def test_nothing_outside_docs_work_is_touched(self):
        (self.checkout / "unrelated.txt").write_text("mine\n", encoding="utf-8")
        self.retire()
        self.assertEqual(
            (self.checkout / "unrelated.txt").read_text(encoding="utf-8"), "mine\n"
        )
        self.assertIn("unrelated.txt", support.git(
            self.checkout, "status", "--porcelain"
        ))

    def test_the_report_names_the_pack_the_snapshot_and_both_counts(self):
        result = self.retire()
        report = "\n".join(result.report())
        self.assertIn("47d41245a470", report)
        self.assertIn("3 line(s) removed, 2 archived line(s) kept", report)
        self.assertIn(str(result.snapshot), report)


class TheSnapshotComesBeforeTheRemoval(RetireCase):
    def test_the_snapshot_holds_the_rows_as_they_were_before_the_switch(self):
        """Taken before the one irreversible step, which is what makes it
        worth taking. Asserted from inside the snapshot: its `repo` row still
        says `file`, so it was written before the switch and the commit.
        """
        self.fleet()
        result = self.retire()
        self.assertTrue(result.snapshot.is_dir())
        copy = connect(result.snapshot / "sd.db", write=False)
        self.addCleanup(copy.close)
        self.assertEqual(
            copy.execute("SELECT status_source FROM repo").fetchone()[0], "file"
        )
        self.assertEqual(self.status_source(), ROW)


# ------------------------------------------------------------- resume points


class KilledAndRerun(RetireCase):
    """C's round five: two kill points, and the sitting reruns to the same end.

    Each kill is the sitting's own phases run in order and then stopped,
    rather than a signal to a subprocess: the phases are the code the sitting
    runs, and stopping between two of them is exactly what a kill does.
    """

    def setUp(self):
        super().setUp()
        self.fleet()

    def identity(self, slug):
        return f"{self.registered}::docs/work/{slug}/prd.md"

    def test_killed_after_the_import_and_before_row_reruns_to_the_same_rows(self):
        sitting = run(self.connection, self.narrowed())
        self.assertTrue(sitting.clean)
        landed = self.rows()

        # Everything is still in place until `row`.
        self.assertEqual(self.status_source(), "file")
        self.assertIsNone(marker(self.checkout))
        for names in self.lines(self.active_files()).values():
            self.assertEqual(len(names), 1)

        result = self.retire()
        self.assertEqual(self.rows(), landed)
        self.assertEqual(self.status_source(), ROW)
        self.assertEqual(marker(self.checkout), ROW)
        self.assertEqual(result.retired.removed, 3)

    def test_killed_after_row_and_before_the_commit_reruns_to_the_commit(self):
        reader = self.narrowed()
        run(self.connection, reader)
        reader.switch(self.connection)
        self.assertEqual(self.status_source(), ROW)

        # The lines are a stale copy of what the rows already say, and the
        # marker has not landed, so a reader with no database still reads them.
        self.assertIsNone(marker(self.checkout))
        for names in self.lines(self.active_files()).values():
            self.assertEqual(len(names), 1)
        landed = self.rows()
        commits = int(self.commits())

        result = self.retire()
        self.assertEqual(self.rows(), landed)
        self.assertEqual(marker(self.checkout), ROW)
        self.assertEqual(int(self.commits()), commits + 1)
        self.assertEqual(result.retired.removed, 3)

    def test_the_row_is_switched_before_the_commit_and_not_after(self):
        """The order the two kill points are the two sides of.

        Asserted by watching from inside `switch`: at the moment it runs, the
        marker must not be committed yet. A retire that committed first would
        leave a window where the lines are gone and the rows do not yet
        answer, and a successful run alone cannot tell the two orders apart.
        """
        seen = {}

        class Watched(Reader):
            def switch(inner, connection):
                seen["marker at switch"] = marker(self.checkout)
                seen["lines at switch"] = self.lines(self.active_files())
                return Reader.switch(inner, connection)

        result = retire(
            self.connection,
            Watched(paths=self.reader().paths),
            token="docs-work",
            home=self.home,
            environ={},
        )
        self.assertIsNone(seen["marker at switch"])
        for names in seen["lines at switch"].values():
            self.assertEqual(len(names), 1)
        self.assertEqual(marker(self.checkout), ROW)
        self.assertEqual(result.retired.removed, 3)

    def test_the_switch_is_idempotent(self):
        reader = self.narrowed()
        reader.switch(self.connection)
        reader.switch(self.connection)
        self.assertEqual(self.status_source(), ROW)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM repo").fetchone()[0], 1
        )


class ASecondSittingFindsNothingLeft(RetireCase):
    def setUp(self):
        super().setUp()
        self.fleet()

    def test_a_rerun_reports_the_repository_as_already_retired_and_commits_nothing(self):
        self.retire()
        commits = int(self.commits())
        result = self.retire()
        self.assertEqual(int(self.commits()), commits)
        self.assertEqual(result.retired.removed, 0)
        self.assertEqual(result.retired.commits, {})
        self.assertIn(self.registered, result.retired.already)
        self.assertIn("was already retired", "\n".join(result.report()))

    def test_the_narrowed_reader_leaves_a_retired_repository_out(self):
        self.retire()
        narrowed = self.narrowed()
        self.assertEqual(narrowed.paths, [])
        self.assertEqual(narrowed.already, [self.registered])

    def test_an_import_of_a_retired_repository_names_the_retire(self):
        """The whole-tree reader has no line to read afterwards. It says the
        answer moved, rather than "'' is not one of planning, ready, ..."
        which sends the reader looking for a mangled prd."""
        self.retire()
        # The retire commits and does not push; the branch the freeze reads
        # carries the lines until the commit merges. Merge it, the way the
        # pull request the sitting opens does.
        support.push(self.checkout)
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, self.reader())
        self.assertIn("was retired", str(caught.exception))
        self.assertIn(MARKER, str(caught.exception))
        self.assertIn("Read the row, not the file", str(caught.exception))

    def test_the_import_reader_leaves_a_retired_repository_out_and_its_rows_too(self):
        """What the `import` and `verify` verbs read after a retire.

        The rows the retire left behind are still in the table. A verify that
        held them against a freeze that read nothing would call all three "no
        longer in the source", so the narrowing has to reach `rows()` as well
        as `paths`, and the sitting has to come out clean.
        """
        self.retire()
        support.push(self.checkout)
        self.assertEqual(len(self.rows()), 3)
        narrowed = self.reader().for_import()
        self.assertEqual(narrowed.paths, [])
        self.assertEqual(narrowed.already, [self.registered])
        self.assertTrue(narrowed.nothing_left())
        self.assertEqual(narrowed.rows(self.connection), {})
        sitting = run(self.connection, narrowed)
        self.assertTrue(sitting.clean, sitting.differences)
        self.assertEqual(sitting.counts.seen, 0)
        self.assertEqual(len(self.rows()), 3)

    def test_the_import_reader_trusts_the_row_before_the_marker(self):
        """A retire killed between its switch and its commit: the row says
        `row`, the lines are still in the tree. `for_retire` reads on so the
        rerun reaches the commit; `for_import` must not, or it writes the
        lines back over the row that already answers."""
        reader = self.narrowed()
        reader.switch(self.connection)
        self.assertIsNone(marker(self.checkout))
        fresh = self.reader()
        self.assertEqual(fresh.switched, [self.registered])
        self.assertEqual(fresh.for_retire().paths, [self.registered])
        self.assertEqual(fresh.for_import().paths, [])
        self.assertEqual(fresh.for_import().already, [self.registered])


class OnlyDocsWorkHasARetireStep(unittest.TestCase):
    """The invariant the `import` verb's closing line is derived from.

    Enumerated from the modules rather than recited from a list: a sixth
    source, or a retire step landing for the vault, changes this answer
    without anybody editing this file, which is the point.
    """

    def test_four_of_the_five_carry_no_retire_and_docs_work_does(self):
        from sd_db.sources import docs_work, index_cache, issues, register, vault

        without = [
            module.__name__ for module in (index_cache, issues, register, vault)
            if not hasattr(module.Reader, "retire")
        ]
        self.assertEqual(len(without), 4, without)
        self.assertTrue(hasattr(docs_work.Reader, "retire"))


# ------------------------------------------------------- the line removal


class TheLineRemoval(unittest.TestCase):
    """`without_status` on its own: what it takes out, and what it leaves."""

    def removal(self, text):
        from sd_db.sources.docs_work import without_status

        return without_status(text)

    def test_it_removes_the_frontmatter_line_and_nothing_else(self):
        text = "---\ntitle: one\nstatus: planning\ncreated: 2026-07-01\n---\n\n# one\n"
        out, changed = self.removal(text)
        self.assertTrue(changed)
        self.assertEqual(
            out, "---\ntitle: one\ncreated: 2026-07-01\n---\n\n# one\n"
        )

    def test_it_leaves_a_status_line_in_the_body_alone(self):
        """In a prd that is prose about status, not a field."""
        text = "---\ntitle: one\nstatus: ready\n---\n\nstatus: whatever it says here\n"
        out, changed = self.removal(text)
        self.assertTrue(changed)
        self.assertIn("status: whatever it says here", out)
        self.assertNotIn("status: ready", out)

    def test_it_leaves_an_indented_status_alone(self):
        text = "---\nfields:\n  status: nested\n---\n\nbody\n"
        out, changed = self.removal(text)
        self.assertFalse(changed)
        self.assertEqual(out, text)

    def test_a_file_with_no_frontmatter_is_returned_unchanged(self):
        text = "# no block here\n\nstatus: prose\n"
        self.assertEqual(self.removal(text), (text, False))

    def test_an_unclosed_block_is_returned_unchanged(self):
        text = "---\ntitle: one\nstatus: ready\n"
        self.assertEqual(self.removal(text), (text, False))

    def test_a_block_with_no_status_line_is_returned_unchanged(self):
        text = "---\ntitle: one\n---\n\nbody\n"
        self.assertEqual(self.removal(text), (text, False))

    def test_a_file_with_no_trailing_newline_keeps_not_having_one(self):
        text = "---\ntitle: one\nstatus: ready\n---\n\nbody"
        out, changed = self.removal(text)
        self.assertTrue(changed)
        self.assertEqual(out, "---\ntitle: one\n---\n\nbody")


if __name__ == "__main__":
    unittest.main()
