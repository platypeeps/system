"""The `docs/work` migration: import and verify, retiring nothing.

Criterion 6 whole, criterion 7's three import clauses, and criterion 23's
rehearsal clauses. What is deliberately absent: the four retirement-refusal
tests of criterion 5, because this pull request retires nothing and there is
no refusal to assert.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import connect
from sd_db.migrate import initialise
from sd_db.repos import add
from sd_db.sources import MigrationRefused, docs_work, run, verify
from sd_db.sources.docs_work import SOURCE, Reader

from . import support


class DocsWorkCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        database = self.root / "sd.db"
        initialise(database)
        self.connection = connect(database)
        self.addCleanup(self.connection.close)
        self.checkout = support.repository(
            self.root / "one", bare=self.root / "one.git"
        )

    def register(self, path=None):
        """Register, and keep the path the row is keyed by.

        `add` resolves the path, and on macOS a temporary directory reaches
        it as `/var/folders/...` and lands as `/private/var/...`. The row's
        identity is the resolved one, so the test asks for it back rather
        than assuming the two spellings match.
        """
        self.registered_path = add(
            self.connection, path or self.checkout, home=self.home
        )
        return self.registered_path

    def identity(self, slug):
        return f"{self.registered_path}::docs/work/{slug}/prd.md"

    def reader(self):
        return Reader.from_table(self.connection)

    def items(self):
        return {
            row["external_id"]: row
            for row in self.connection.execute(
                "SELECT * FROM item WHERE source = ?", (SOURCE,)
            )
        }

    def land(self, slug="2026-07-01-an-item", **kwargs):
        support.write_item(self.checkout, slug, **kwargs)
        support.commit(self.checkout, slug)
        support.push(self.checkout)


class TheEnumerationIsBoundedByTheTable(DocsWorkCase):
    def test_an_unregistered_repository_is_not_read(self):
        self.land()
        sitting = run(self.connection, self.reader())
        self.assertEqual(sitting.counts.seen, 0)
        self.assertEqual(self.items(), {})

    def test_every_prd_in_a_registered_repository_has_a_row(self):
        """Criterion 6: enumerate from the table, join, and count the gap."""
        self.land("2026-07-01-first")
        self.land("2026-07-02-second", title="the second")
        self.register()
        run(self.connection, self.reader())
        landed = {row["path"] for row in self.items().values()}
        self.assertEqual(
            landed,
            {
                "docs/work/2026-07-01-first/prd.md",
                "docs/work/2026-07-02-second/prd.md",
            },
        )
        rows_with_no_file = [
            row for row in self.items().values()
            if not (Path(row["repo"]) / row["path"]).exists()
        ]
        self.assertEqual(rows_with_no_file, [])

    def test_a_clone_under_the_worktrees_directory_does_not_widen_it(self):
        """A runner's clone carries its own prds and is not a repository.

        The clone is made the way the runner makes one -- the whole working
        tree, `docs/work` included -- and the enumeration is unchanged,
        because it reads the table and the clone was never added to it.
        """
        self.land("2026-07-01-first")
        self.register()
        before = run(self.connection, self.reader()).counts.seen
        worktrees = self.home / ".local/share/sd/worktrees"
        worktrees.mkdir(parents=True)
        support.git(worktrees.parent, "clone", "-q", str(self.checkout), "worktrees/item")
        self.assertTrue((worktrees / "item/docs/work").is_dir())
        after = run(self.connection, self.reader())
        self.assertEqual(after.counts.seen, before)
        self.assertTrue(after.clean)


class TheImportIsIdempotent(DocsWorkCase):
    def setUp(self):
        super().setUp()
        self.land("2026-07-01-first")
        self.land("2026-07-02-second", title="the second", status="done")
        self.register()

    def test_a_second_run_reports_the_same_counts_with_zero_new_rows(self):
        """Criterion 5's import half."""
        first = run(self.connection, self.reader())
        rows = len(self.items())
        second = run(self.connection, self.reader())
        self.assertEqual(first.counts.seen, second.counts.seen)
        self.assertEqual(second.counts.inserted, 0)
        self.assertEqual(second.counts.unchanged, second.counts.seen)
        self.assertEqual(len(self.items()), rows)

    def test_a_done_item_lands_as_a_done_row_with_its_line_untouched(self):
        """Criterion 7's first import clause."""
        path = self.checkout / "docs/work/2026-07-02-second/prd.md"
        before = path.read_bytes()
        run(self.connection, self.reader())
        row = self.items()[self.identity("2026-07-02-second")]
        self.assertEqual(row["status"], "done")
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("status: done", before.decode())

    def test_the_idle_clock_is_seeded_from_the_source(self):
        run(self.connection, self.reader())
        created = {row["created_at"] for row in self.items().values()}
        self.assertEqual(created, {"2026-07-01"})

    def test_a_status_change_through_the_old_command_is_read_back(self):
        """Criterion 23: the source stays authoritative until its retire.

        The old writer edits the frontmatter line, and the next import
        carries the change into the row -- as a `transition`, so the history
        says when it happened and who asked.
        """
        run(self.connection, self.reader())
        support.write_item(
            self.checkout, "2026-07-01-first", status="in_progress", title="an item"
        )
        support.commit(self.checkout, "the old command moved it")
        support.push(self.checkout)
        second = run(self.connection, self.reader())
        row = self.items()[self.identity("2026-07-01-first")]
        self.assertEqual(row["status"], "in_progress")
        self.assertEqual(second.counts.updated, 1)
        notes = list(self.connection.execute(
            "SELECT body FROM note WHERE item = ? AND kind = 'status_change' "
            "ORDER BY id", (row["id"],)
        ))
        self.assertEqual(len(notes), 2)
        self.assertIn("planning -> in_progress", notes[-1]["body"])

    def test_the_import_retires_nothing(self):
        """Every file under the source is byte-identical afterwards."""
        source = self.checkout / "docs/work"
        before = {
            path: path.read_bytes() for path in sorted(source.rglob("*")) if path.is_file()
        }
        run(self.connection, self.reader())
        after = {
            path: path.read_bytes() for path in sorted(source.rglob("*")) if path.is_file()
        }
        self.assertEqual(before, after)


class TheSittingRefuses(DocsWorkCase):
    def test_an_uncommitted_prd_refuses_naming_it(self):
        """Criterion 7's second import clause."""
        self.land("2026-07-01-first")
        self.register()
        support.write_item(self.checkout, "2026-07-01-first", status="done")
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, self.reader())
        self.assertIn("docs/work/2026-07-01-first/prd.md", str(caught.exception))
        self.assertIn("uncommitted", str(caught.exception))

    def test_a_status_word_the_schema_does_not_name_refuses_with_the_word(self):
        self.land("2026-07-01-first", status="planning")
        support.write_item(self.checkout, "2026-07-01-first", status="pondering")
        support.commit(self.checkout, "a word nobody named")
        support.push(self.checkout)
        self.register()
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, self.reader())
        self.assertIn("'pondering'", str(caught.exception))

    def test_a_registered_path_that_is_not_a_checkout_refuses(self):
        self.land("2026-07-01-first")
        self.register()
        self.connection.execute(
            "UPDATE repo SET path = ?", (str(self.root / "gone"),)
        )
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, self.reader())
        self.assertIn("is not a git checkout", str(caught.exception))

    def test_every_unready_repository_is_named_in_one_refusal(self):
        """Not the first one met: the whole list, so one sweep clears them all.

        Stopping at the first turned a fleet-wide condition into one
        repository per run, and the enumeration never reached what was behind
        it (sd:1284).
        """
        self.land("2026-07-01-first")
        self.register()
        for name in ("gone-one", "gone-two"):
            self.connection.execute(
                "INSERT INTO repo (path, created_at, updated_at) VALUES (?, ?, ?)",
                (str(self.root / name), "2026-07-01T00:00:00+00:00",
                 "2026-07-01T00:00:00+00:00"),
            )
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, self.reader())
        message = str(caught.exception)
        self.assertIn("gone-one", message)
        self.assertIn("gone-two", message)
        self.assertIn("2 of 3", message)


class BranchesCarryItems(DocsWorkCase):
    """Criterion 7's third import clause: an item lives on its branch."""

    def branch(self, name):
        support.git(self.checkout, "checkout", "-q", "-b", name)

    def test_an_item_on_a_branch_alone_lands_from_it_with_its_commit(self):
        self.branch("feat/only-here")
        support.write_item(self.checkout, "2026-07-03-branch-only", title="branch only")
        commit = support.commit(self.checkout, "branch only")
        support.push(self.checkout, "feat/only-here")
        support.git(self.checkout, "checkout", "-q", "main")
        self.register()
        sitting = run(self.connection, self.reader())
        self.assertTrue(sitting.clean)
        row = self.items()[self.identity("2026-07-03-branch-only")]
        self.assertEqual(row["branch"], "origin/feat/only-here")
        self.assertEqual(row["source_commit"], commit)
        self.assertIn(
            "lives only on origin/feat/only-here",
            "\n".join(sitting.notes),
        )

    def test_two_branches_whose_lines_disagree_refuse_naming_both(self):
        self.land("2026-07-01-first", status="planning")
        self.branch("feat/moved-it")
        support.write_item(self.checkout, "2026-07-01-first", status="in_progress")
        support.commit(self.checkout, "moved on the branch")
        support.push(self.checkout, "feat/moved-it")
        support.git(self.checkout, "checkout", "-q", "main")
        self.register()
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, self.reader())
        message = str(caught.exception)
        self.assertIn("docs/work/2026-07-01-first/prd.md", message)
        self.assertIn("origin/main says planning", message)
        self.assertIn("origin/feat/moved-it says in_progress", message)

    def test_a_branch_that_never_touched_the_file_does_not_disagree(self):
        """The commonest shape on the real fleet, and the one that bit first.

        A branch cut before the status moved still carries the old line, and
        has never touched the file. Its copy is already in the default, so it
        is not a competing claim -- reading it as one refuses the sitting over
        a disagreement a merge already settled. Measured 2026-09-06: three
        files disagreed across ten branches and every disagreement was this.
        """
        self.land("2026-07-01-first", status="planning")
        self.branch("feat/never-touched-it")
        (self.checkout / "unrelated.txt").write_text("work\n", encoding="utf-8")
        support.commit(self.checkout, "unrelated work on the branch")
        support.push(self.checkout, "feat/never-touched-it")
        support.git(self.checkout, "checkout", "-q", "main")
        support.write_item(self.checkout, "2026-07-01-first", status="done")
        support.commit(self.checkout, "the item finished on main")
        support.push(self.checkout)
        self.register()
        sitting = run(self.connection, self.reader())
        self.assertTrue(sitting.clean)
        row = self.items()[self.identity("2026-07-01-first")]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["branch"], "origin/main")

    def test_a_branch_that_did_touch_the_file_still_disagrees(self):
        """A live branch making its own claim is the case the refusal is for."""
        self.land("2026-07-01-first", status="planning")
        self.branch("feat/its-own-claim")
        support.write_item(self.checkout, "2026-07-01-first", status="in_progress")
        support.commit(self.checkout, "moved on the branch")
        support.push(self.checkout, "feat/its-own-claim")
        support.git(self.checkout, "checkout", "-q", "main")
        support.write_item(self.checkout, "2026-07-01-first", status="done")
        support.commit(self.checkout, "finished on main")
        support.push(self.checkout)
        self.register()
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, self.reader())
        self.assertIn("origin/feat/its-own-claim says in_progress", str(caught.exception))
        self.assertIn("origin/main says done", str(caught.exception))

    def test_a_merged_branch_is_history_and_does_not_disagree(self):
        """A branch already in the default carries what it said the day it landed.

        Left in the enumeration, a stale merged branch refuses the sitting
        forever. Found against the real fleet on 2026-09-06.
        """
        self.land("2026-07-01-first", status="planning")
        self.branch("feat/landed")
        support.write_item(self.checkout, "2026-07-01-first", status="in_progress")
        support.commit(self.checkout, "moved on the branch")
        support.push(self.checkout, "feat/landed")
        support.git(self.checkout, "checkout", "-q", "main")
        support.git(self.checkout, "merge", "-q", "--ff-only", "feat/landed")
        support.push(self.checkout)
        self.register()
        sitting = run(self.connection, self.reader())
        self.assertTrue(sitting.clean)
        row = self.items()[self.identity("2026-07-01-first")]
        self.assertEqual(row["status"], "in_progress")


class RemoteBranchesMustBeFresh(DocsWorkCase):
    """C-170: live remote refs are evidence, never an invitation to fetch."""

    def setUp(self):
        super().setUp()
        self.land("2026-07-01-first")
        self.register()
        self.publisher = self.root / "publisher"
        support.git(self.root, "clone", "-q", "--branch", "main", str(self.root / "one.git"), str(self.publisher))
        support.git(self.publisher, "config", "user.email", "fixture@example.invalid")
        support.git(self.publisher, "config", "user.name", "Fixture")

    def snapshot(self):
        maintenance_lock = Path(".git/objects/maintenance.lock")
        return {relative: path.read_bytes()
                for path in self.checkout.rglob("*")
                if path.is_file()
                if (relative := path.relative_to(self.checkout)) != maintenance_lock}

    def test_snapshot_ignores_git_maintenance_lock(self):
        before = self.snapshot()
        lock = self.checkout / ".git/objects/maintenance.lock"
        lock.write_bytes(b"")
        self.assertEqual(self.snapshot(), before)

    def checked_freeze(self):
        before = self.snapshot()
        git = docs_work._git
        allowed = {"status", "remote", "for-each-ref", "ls-remote", "ls-tree", "show", "log", "merge-base"}
        def read_only(root, *arguments):
            self.assertIn(arguments[0], allowed, f"reader attempted a write: {arguments}")
            return git(root, *arguments)
        try:
            with patch.object(docs_work, "_git", side_effect=read_only) as calls:
                return self.reader().freeze()
        finally:
            self.assertEqual(self.snapshot(), before, "freeze changed checkout or Git metadata bytes")
            self.assertFalse(any("fetch" in call.args[1:] for call in calls.call_args_list))
            self.assertEqual(self.items(), {})

    def test_matching_refs_read_items_without_fetching_or_refreshing_the_index(self):
        support.git(self.checkout, "remote", "set-head", "origin", "main")
        path = self.checkout / "docs/work/2026-07-01-first/prd.md"
        # A clean file with changed stat data normally makes git status rewrite the index.
        stamp = path.stat().st_mtime_ns + 2_000_000_000
        os.utime(path, ns=(stamp, stamp))
        frozen = self.checked_freeze()
        self.assertEqual(set(frozen.records), {self.identity("2026-07-01-first")})

    def test_changed_remote_tip_refuses_naming_both_commits(self):
        cached = support.git(self.checkout, "rev-parse", "origin/main")
        support.write_item(self.publisher, "2026-07-01-first", status="done")
        remote = support.commit(self.publisher, "another writer advanced main")
        support.push(self.publisher)
        with self.assertRaises(MigrationRefused) as caught:
            self.checked_freeze()
        message = str(caught.exception)
        for text in ("origin/main", "stale", cached, remote, "git fetch --prune origin"):
            self.assertIn(text, message)

    def test_new_remote_branch_refuses_until_operator_fetches_it(self):
        support.git(self.publisher, "checkout", "-q", "-b", "feat/new-item")
        support.write_item(self.publisher, "2026-07-02-new", title="unfetched item")
        support.commit(self.publisher, "only on the remote branch")
        support.push(self.publisher, "feat/new-item")
        with self.assertRaisesRegex(MigrationRefused, "origin/feat/new-item is missing"):
            self.checked_freeze()
        support.git(self.checkout, "fetch", "-q", "--prune", "origin")
        frozen = self.checked_freeze()
        self.assertIn(self.identity("2026-07-02-new"), frozen.records)

    def test_deleted_remote_branch_refuses_before_merged_branch_filtering(self):
        support.git(self.checkout, "branch", "feat/deleted")
        support.push(self.checkout, "feat/deleted")
        support.git(self.publisher, "push", "-q", "origin", "--delete", "feat/deleted")
        with self.assertRaisesRegex(MigrationRefused, "origin/feat/deleted is stale: deleted"):
            self.checked_freeze()
        support.git(self.checkout, "fetch", "-q", "--prune", "origin")
        self.assertEqual(len(self.checked_freeze().records), 1)

    def test_each_missing_and_stale_ref_is_named_in_one_refusal(self):
        support.git(self.checkout, "branch", "feat/deleted")
        support.push(self.checkout, "feat/deleted")
        support.git(self.publisher, "push", "-q", "origin", "--delete", "feat/deleted")
        support.write_item(self.publisher, "2026-07-01-first", status="done")
        support.commit(self.publisher, "main moved")
        support.push(self.publisher)
        support.git(self.publisher, "branch", "feat/new")
        support.push(self.publisher, "feat/new")
        with self.assertRaises(MigrationRefused) as caught:
            self.checked_freeze()
        message = str(caught.exception)
        for name in ("origin/feat/deleted", "origin/feat/new", "origin/main"):
            self.assertIn(name, message)

    def test_an_empty_remote_cache_never_falls_back_to_local_branches(self):
        support.git(self.checkout, "update-ref", "-d", "refs/remotes/origin/main")
        with self.assertRaisesRegex(MigrationRefused, "origin/main is missing"):
            self.checked_freeze()

    def test_unavailable_origin_refuses_without_modifying_cached_refs(self):
        support.git(self.checkout, "remote", "set-url", "origin", str(self.root / "unavailable.git"))
        with self.assertRaisesRegex(MigrationRefused, "cannot verify live origin branches.*git fetch --prune"):
            self.checked_freeze()

    def test_remote_timeout_and_malformed_output_refuse(self):
        original = docs_work._git
        for answer in ((124, "", "timed out"), (0, "not a branch listing\n", "")):
            with self.subTest(answer=answer):
                def remote_failure(root, *arguments, answer=answer):
                    if arguments == ("ls-remote", "--heads", "origin"):
                        return answer
                    return original(root, *arguments)
                with patch.object(docs_work, "_git", side_effect=remote_failure):
                    with self.assertRaises(MigrationRefused):
                        self.checked_freeze()

    def test_repository_without_an_origin_still_reads_local_committed_items(self):
        support.git(self.checkout, "remote", "remove", "origin")
        self.assertEqual(len(self.checked_freeze().records), 1)


class TheVerify(DocsWorkCase):
    def setUp(self):
        super().setUp()
        self.land("2026-07-01-first")
        self.register()

    def test_a_clean_verify_writes_the_verified_row(self):
        sitting = run(self.connection, self.reader())
        self.assertTrue(sitting.clean)
        rows = list(self.connection.execute(
            "SELECT * FROM state WHERE kind = 'verified'"
        ))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], SOURCE)

    def test_a_seeded_difference_lifts_the_freeze_and_leaves_the_source_alone(self):
        """Criterion 23's third rehearsal clause."""
        run(self.connection, self.reader())
        self.connection.execute("UPDATE item SET title = 'edited behind the migration'")
        path = self.checkout / "docs/work/2026-07-01-first/prd.md"
        before = path.read_bytes()
        reader = self.reader()
        frozen = reader.freeze()
        differences = verify(self.connection, reader, frozen)
        self.assertEqual(len(differences), 1)
        self.assertEqual(differences[0].what, "title")
        self.assertEqual(differences[0].in_rows, "edited behind the migration")
        self.assertEqual(path.read_bytes(), before)

    def test_a_difference_writes_no_verified_row(self):
        run(self.connection, self.reader())
        self.connection.execute("DELETE FROM state WHERE kind = 'verified'")
        self.connection.execute("UPDATE item SET title = 'drifted'")
        sitting = run(self.connection, self.reader())
        # The import puts the title back, so drift the row after it instead:
        # what is asserted is that a difference produces no `verified` row.
        self.assertTrue(sitting.clean)
        self.connection.execute("DELETE FROM state WHERE kind = 'verified'")
        self.connection.execute("UPDATE item SET path = 'docs/work/moved/prd.md'")
        reader = self.reader()
        frozen = reader.freeze()
        self.assertTrue(verify(self.connection, reader, frozen))
        self.assertEqual(
            list(self.connection.execute("SELECT * FROM state WHERE kind = 'verified'")),
            [],
        )

    def test_verify_names_a_record_the_rows_do_not_have(self):
        reader = self.reader()
        frozen = reader.freeze()
        differences = verify(self.connection, reader, frozen)
        self.assertEqual(len(differences), 1)
        self.assertEqual(differences[0].what, "missing from the rows")


class TheArchiveIsTheArchiveFolder(unittest.TestCase):
    def test_a_nested_prd_outside_the_archive_is_not_counted_as_archived(self):
        """`archived` reads `docs/work/archive/`, not every prd two deep.

        A prd under `docs/work/<item>/<sub>/` is neither an active item nor a
        record of one, and the retire's "archived kept" count must not grow
        by it (sd:1226).
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = support.repository(Path(tmp) / "one")
            support.write_archived_item(root, "2026-06", "2026-06-01-old")
            stray = root / "docs/work/2026-07-01-item/notes/prd.md"
            stray.parent.mkdir(parents=True)
            stray.write_text("not an archive\n", encoding="utf-8")
            support.commit(root)
            self.assertEqual(
                docs_work.archived(root, "HEAD"),
                ["docs/work/archive/2026-06/2026-06-01-old/prd.md"],
            )


if __name__ == "__main__":
    unittest.main()
