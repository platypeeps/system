"""`sd_db.reads`, on the parts that read git rather than the database.

Focused on `missing_trailers`, which is the one read in this module that
parses a subprocess's output rather than a row, and is therefore the one that
can be wrong about a commit that is perfectly fine.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import connect, create_item, reads, reporting, upsert_repo, workflow
from sd_db.errors import SdDbError
from sd_db.migrate import initialise
from sd_db.schema import migrations
from sd_db.writes import add_note, set_item_fields, transition, upsert_shadow
from sd_db.writing import cutover_pieces, cutover_preview, import_piece, list_pieces, park_piece

from . import support

#: A fixed instant, so the week window does not depend on when the suite runs.
NOW = "2026-09-06T12:00:00Z"
WHEN = "2026-09-04T09:00:00+00:00"

TRAILERED = "Authored-with: claude/anthropic"


def commit_with(root: Path, message: str) -> None:
    """A commit at a fixed date, with whatever trailers `message` carries.

    Both dates are pinned: `git log --since/--until` filters on the committer
    date, and a fixture that only pinned the author date would drift back into
    depending on the clock.
    """
    environment = dict(os.environ, GIT_AUTHOR_DATE=WHEN, GIT_COMMITTER_DATE=WHEN)
    (root / f"{abs(hash(message)) % 10**8}.txt").write_text(message, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", message],
        check=True, capture_output=True, env=environment,
    )


class TheItemShadow(unittest.TestCase):
    """`shadow` is keyed by `(tracker, url)`, so a url names a row only once
    you say whose. The read either takes the tracker or refuses to guess."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "sd.db"
        initialise(path)
        self.connection = connect(path)
        self.addCleanup(self.connection.close)
        self.url = "https://github.com/o/r/issues/1"
        self.item = create_item(
            self.connection, kind="task", title="A thing",
            source="github", external_id=self.url)
        upsert_shadow(self.connection, tracker="github", url=self.url, title="from github")

    def test_one_tracker_needs_no_predicate(self):
        self.assertEqual(reads.item_shadow(self.connection, self.item)["title"], "from github")

    def test_the_named_tracker_gets_its_own_row(self):
        upsert_shadow(self.connection, tracker="jira", url=self.url, title="from jira")
        self.assertEqual(
            reads.item_shadow(self.connection, self.item, tracker="github")["title"],
            "from github")
        self.assertEqual(
            reads.item_shadow(self.connection, self.item, tracker="jira")["title"],
            "from jira")

    def test_a_tracker_with_no_such_row_gets_nothing_rather_than_the_other_one(self):
        self.assertIsNone(reads.item_shadow(self.connection, self.item, tracker="jira"))

    def test_an_unnamed_tracker_on_a_contested_url_is_refused(self):
        upsert_shadow(self.connection, tracker="jira", url=self.url, title="from jira")
        with self.assertRaises(SdDbError) as raised:
            reads.item_shadow(self.connection, self.item)
        self.assertIn("github, jira", str(raised.exception))


class MissingTrailers(unittest.TestCase):
    """A trailer that is not the first trailer is still a trailer.

    The bug: the count was parsed by splitting `%H%x00%(trailers)` on newlines
    and skipping any line without a NUL. `%(trailers)` is *one field spanning
    several lines*, so only a commit's first trailer line carried the NUL --
    and a commit whose `Authored-with:` sat on the second line had that line
    thrown away and was reported as missing. Every commit in this fleet that
    also carries a `Co-Authored-By:` or a `Reviewed-by:` above the trailer was
    counted as a violation.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)

        self.repo = support.repository(home / "repo")
        # `repository()` opens with an untrailered commit; amend it so the
        # only untrailered commit in this fixture is the one put there on
        # purpose, and the expected count is 1 rather than "1 plus setup".
        environment = dict(
            os.environ, GIT_AUTHOR_DATE=WHEN, GIT_COMMITTER_DATE=WHEN
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-q", "--amend",
             "-m", f"first\n\n{TRAILERED}"],
            check=True, capture_output=True, env=environment,
        )

        self.database = home / "sd.db"
        initialise(self.database)
        self.connection = connect(self.database)
        self.addCleanup(self.connection.close)

    def count(self) -> int:
        return reads.missing_trailers(
            self.connection, now=NOW, repo_paths=[str(self.repo)]
        )

    def test_a_trailer_that_is_not_the_first_trailer_still_counts(self):
        """The regression, in the shape it was reported in.

        Two commits: one whose `Authored-with:` is its *second* trailer, one
        with no trailers at all. One of them is missing the trailer. The old
        parse said two.
        """
        commit_with(
            self.repo,
            "a commit with two trailers\n\n"
            "Reviewed-by: someone <someone@example.invalid>\n"
            f"{TRAILERED}\n",
        )
        commit_with(self.repo, "a commit with no trailers at all\n")

        self.assertEqual(
            self.count(), 1,
            "the commit whose trailer is not on the first trailer line was "
            "counted as missing",
        )

    def test_the_trailer_is_found_wherever_it_sits_in_the_block(self):
        """First line, last line, and buried in the middle: all trailered."""
        commit_with(self.repo, f"trailer first\n\n{TRAILERED}\nReviewed-by: a\n")
        commit_with(self.repo, f"trailer last\n\nReviewed-by: b\n{TRAILERED}\n")
        commit_with(
            self.repo,
            "trailer in the middle\n\n"
            f"Reviewed-by: c\n{TRAILERED}\nCo-Authored-By: d <d@example.invalid>\n",
        )
        self.assertEqual(self.count(), 0)

    def test_a_commit_with_no_trailers_is_counted(self):
        """The other direction: the fix must not stop counting anything."""
        commit_with(self.repo, "no trailers here\n")
        commit_with(self.repo, "nor here\n")
        self.assertEqual(self.count(), 2)

    def test_a_multi_line_trailer_value_does_not_split_the_record(self):
        """A folded trailer continues on an indented line and is still one
        trailer; a parse that splits on newlines sees the continuation as a
        record of its own."""
        commit_with(
            self.repo,
            "a folded trailer\n\n"
            "Reviewed-by: someone with\n  a wrapped value\n"
            f"{TRAILERED}\n",
        )
        self.assertEqual(self.count(), 0)

    def test_a_walk_past_its_budget_is_refused_not_a_partial_count(self):
        """One slow repository spends a caller's budget: the read raises, it does not return 0."""
        stub = Path(self.tmp.name) / "bin"
        stub.mkdir()
        (stub / "git").write_text("#!/bin/sh\nexec sleep 5\n")
        (stub / "git").chmod(0o755)
        path = os.environ["PATH"]
        os.environ["PATH"] = f"{stub}{os.pathsep}{path}"
        self.addCleanup(os.environ.__setitem__, "PATH", path)
        with self.assertRaisesRegex(reads.OverBudget, r"^the trailer count ran past its budget of 0\.3 seconds$"):
            reads.missing_trailers(self.connection, now=NOW, repo_paths=[str(self.repo)], within=0.3)
        # A spent budget starts no git at all.
        with mock.patch.object(reads.subprocess, "run") as run, self.assertRaises(reads.OverBudget):
            reads.missing_trailers(self.connection, now=NOW, repo_paths=[str(self.repo)], within=0)
        run.assert_not_called()

    def test_a_budget_the_walk_fits_changes_nothing(self):
        commit_with(self.repo, "no trailers here\n")
        self.assertEqual(reads.missing_trailers(self.connection, now=NOW, repo_paths=[str(self.repo)], within=30), 1)

    def test_a_repository_that_cannot_be_read_is_not_a_crash(self):
        missing = Path(self.tmp.name) / "not-a-repository"
        self.assertEqual(
            reads.missing_trailers(
                self.connection, now=NOW, repo_paths=[str(missing)]
            ),
            0,
        )


class ParkedWritingReads(unittest.TestCase):
    def test_due_date_does_not_put_parked_writing_back_in_today(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            connection = connect(path)
            self.addCleanup(connection.close)
            item = create_item(connection, kind="idea", title="Parked due idea", status="planning", due="2026-09-01")
            set_item_fields(connection, item, parked_at=WHEN)
            self.assertEqual(reads.today_items(connection, now=NOW), [])
            self.assertEqual(reads.backlog_items(connection, now=NOW), [])

    def test_park_and_revive_change_today_and_backlog_but_preserve_explicit_writing_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "writing"
            index = repo / "content/2026/a-piece/index.md"
            index.parent.mkdir(parents=True)
            index.write_text("---\ntitle: A piece\nstatus: review\npublished: null\n---\n\n## Draft\nDraft prose.\n")
            path = root / "sd.db"
            initialise(path)
            connection = connect(path)
            self.addCleanup(connection.close)
            upsert_repo(connection, str(repo))
            piece = import_piece(connection, str(repo), "2026/a-piece", who="operator")["item"]["id"]
            followup = add_note(connection, piece, "followup", "Resume the parked piece only when revived")
            preview = cutover_preview(connection, str(repo))
            cutover_pieces(connection, str(repo), expected_fingerprint=preview["fingerprint"], who="operator")
            task = create_item(connection, kind="task", title="Due ordinary task", status="planning", due="2026-09-01")
            for query in (reads.today_items, reads.backlog_items):
                self.assertEqual({row["id"] for row in query(connection, now=NOW)}, {piece, task})
            self.assertEqual([row["id"] for row in reads.open_followups(connection)], [followup])
            park_piece(connection, piece, require_row=True, who="operator")
            for query in (reads.today_items, reads.backlog_items):
                self.assertEqual([row["id"] for row in query(connection, now=NOW)], [task])
            self.assertEqual(reads.open_followups(connection), [])
            self.assertEqual(list_pieces(connection, str(repo)), [])
            self.assertEqual([row["id"] for row in list_pieces(connection, str(repo), include_parked=True)], [piece])
            park_piece(connection, piece, parked=False, require_row=True, who="operator")
            for query in (reads.today_items, reads.backlog_items):
                self.assertEqual({row["id"] for row in query(connection, now=NOW)}, {piece, task})
            self.assertEqual([row["id"] for row in reads.open_followups(connection)], [followup])

    def test_schema_two_readonly_queries_remain_available_without_parked_column(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "schema-two.db"
            raw = sqlite3.connect(path)
            for version, source in migrations():
                if version <= 2:
                    raw.executescript(source.read_text())
            raw.execute("PRAGMA user_version = 2")
            raw.execute("INSERT INTO item (kind,title,status,created_at,updated_at) VALUES ('task','Old active task','in_progress',?,?)", (WHEN, WHEN))
            raw.execute("INSERT INTO note (item,timestamp,kind,body) VALUES (1,?,'followup','Old followup')", (WHEN,))
            raw.commit()
            raw.close()
            connection = connect(path, write=False)
            self.addCleanup(connection.close)
            self.assertNotIn("parked_at", {row[1] for row in connection.execute("PRAGMA table_info(item)")})
            before = connection.total_changes
            for query in (reads.today_items, reads.backlog_items):
                self.assertEqual([row["title"] for row in query(connection, now=NOW)], ["Old active task"])
            self.assertEqual([row["body"] for row in reads.open_followups(connection)], ["Old followup"])
            captured = reads.capture_items(connection)
            self.assertEqual([row["title"] for row in captured], ["Old active task"])
            self.assertIsNone(captured[0]["parked_at"])
            self.assertEqual(connection.total_changes, before)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)


class TheUnscopedBacklog(unittest.TestCase):
    """One pane of glass over the items that belong to no repository. An empty
    `repo` means "every repository", so before `NO_REPO` the personal rows
    could only be read mixed into everything."""

    def test_no_repository_is_selectable_and_is_not_the_same_as_no_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            writer = connect(path)
            repo = str(Path(temporary) / "alpha")
            upsert_repo(writer, repo)
            scoped = create_item(writer, kind="work", title="Scoped work",
                                 status="planning", repo=repo)
            chore = create_item(writer, kind="personal", title="Fix the fence",
                                status="planning")
            spark = create_item(writer, kind="personal-idea",
                                title="A thought that is not an article",
                                status="planning")
            errand = create_item(writer, kind="followup", title="Chase the quote",
                                 status="planning")
            writer.commit()
            writer.close()
            connection = connect(path, write=False)
            self.addCleanup(connection.close)

            everything = [row["id"] for row in reads.backlog_items(connection)]
            self.assertEqual(sorted(everything), sorted([scoped, chore, spark, errand]))

            unscoped = [row["id"] for row in
                        reads.backlog_items(connection, repo=reads.NO_REPO)]
            self.assertEqual(sorted(unscoped), sorted([chore, spark, errand]))

            self.assertEqual([row["id"] for row in
                              reads.backlog_items(connection, repo=repo)], [scoped])

            # Facets still compose, which is what makes the pane of glass work:
            # one kind within the unscoped rows.
            self.assertEqual([row["id"] for row in reads.backlog_items(
                connection, repo=reads.NO_REPO, kind="personal")], [chore])

    def test_a_followup_with_a_repository_is_read_with_it_and_an_old_one_stays_unscoped(self):
        """sd:809. A followup took no repository before, so no checkout's brief
        and no scoped backlog listed one. The two ways it now takes one, a task
        reclassified with its repository and a followup moved onto a checkout,
        put it in both readers; a followup filed before then stays unscoped."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            writer = connect(path)
            repo = str(Path(temporary) / "alpha")
            upsert_repo(writer, repo)
            older = create_item(writer, kind="followup", title="Filed before sd:809",
                                status="planning")
            reclassified = workflow.capture_task(
                writer, title="A review finding", repo=repo, who="alex")["item"]["id"]
            workflow.edit_item(writer, reclassified, {"kind": "followup"}, who="alex")
            moved = create_item(writer, kind="followup", title="Filed off the checkout",
                                status="planning")
            workflow.edit_item(writer, moved, {"repo": repo}, who="alex")
            writer.commit()
            writer.close()
            connection = connect(path, write=False)
            self.addCleanup(connection.close)

            self.assertEqual(sorted(row["id"] for row in reads.brief_items(connection, repo)),
                             sorted([reclassified, moved]))
            self.assertEqual(sorted(row["id"] for row in reads.backlog_items(
                connection, repo=repo, kind="followup")), sorted([reclassified, moved]))
            self.assertEqual([row["id"] for row in reads.backlog_items(
                connection, repo=reads.NO_REPO, kind="followup")], [older])

    def test_a_repository_registered_under_the_token_does_not_shadow_the_selector(self):
        """The collision, tested through the API that would cause it.

        `upsert_repo` validates nothing and `repo.path` carries no CHECK, so a
        repository really can be registered under the word the URL uses. The
        selector is an object, so identity keeps the two apart: the repository
        named `none` is one filter and "no repository at all" is another."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            writer = connect(path)
            upsert_repo(writer, reads.NO_REPO_TOKEN)
            named = create_item(writer, kind="work", title="In a repo called none",
                                status="planning", repo=reads.NO_REPO_TOKEN)
            unscoped = create_item(writer, kind="personal", title="Fix the fence",
                                   status="planning")
            writer.commit()
            writer.close()
            connection = connect(path, write=False)
            self.addCleanup(connection.close)
            self.assertEqual([row["id"] for row in reads.backlog_items(
                connection, repo=reads.NO_REPO)], [unscoped])
            self.assertEqual([row["id"] for row in reads.backlog_items(
                connection, repo=reads.NO_REPO_TOKEN)], [named])
            self.assertIsNot(reads.NO_REPO, reads.NO_REPO_TOKEN)
            self.assertNotEqual(reads.NO_REPO, reads.NO_REPO_TOKEN)


class QuietRunReportsAreNotBacklog(unittest.TestCase):
    """sd:708 defect 2. A clean cron report is a log line that was given an
    item because there was nowhere else to put it; sd:739 built the somewhere
    else. Until it leaves the shared row set it is a row in the list, the board
    and the matrix for fourteen days -- seven `planning` before `retention`
    settles it, seven more inside `include_done_days`.

    Measured on the live store the day this was written: 310 of 739 rows.
    """

    def store(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "sd.db"
        initialise(path)
        writer = connect(path)
        self.addCleanup(writer.close)
        return writer

    def report(self, writer, *, attention, job="fixture-job"):
        """Through `ingest`, the real producer, so `fields` is the real shape.

        Building the row with `create_item` and a hand-written `fields` dict
        would test the test's idea of the JSON rather than the one the cron
        path writes.
        """
        state = reporting.ingest(
            writer, job=job, run_id=f"{job}-{attention}", started="2026-09-13T00:00:00+00:00",
            ended="2026-09-13T00:01:00+00:00", exit_code=1 if attention else 0,
            text="fixture run\n", source_path="/tmp/log", attention=attention)
        return state["item"]["id"]

    def test_a_clean_report_is_not_in_the_set_and_an_attention_report_is(self):
        writer = self.store()
        quiet = self.report(writer, attention=False, job="quiet-job")
        loud = self.report(writer, attention=True, job="loud-job")
        chore = create_item(writer, kind="task", title="A real task", status="planning")
        writer.commit()

        ids = [row["id"] for row in reads.backlog_items(writer)]
        self.assertNotIn(quiet, ids)
        self.assertIn(loud, ids)
        self.assertIn(chore, ids)

    def test_the_exclusion_is_by_attention_and_not_by_kind(self):
        """The whole point, and the reason a blanket `kind = 'report'` clause
        is wrong: `is_urgent` promotes an attention report into the matrix's
        urgent quadrant by name, so excluding the kind would leave that
        function dead in the same file it lives in."""
        writer = self.store()
        loud = self.report(writer, attention=True, job="loud-job")
        writer.commit()

        rows = {row["id"]: row for row in reads.backlog_items(writer)}
        self.assertIn(loud, rows)
        self.assertTrue(reads.is_urgent(rows[loud]))

    def test_asking_for_the_report_kind_returns_the_ones_that_need_a_person(self):
        writer = self.store()
        self.report(writer, attention=False, job="quiet-job")
        loud = self.report(writer, attention=True, job="loud-job")
        writer.commit()

        self.assertEqual([row["id"] for row in reads.backlog_items(writer, kind="report")],
                         [loud])

    def test_a_settled_clean_report_does_not_return_inside_the_done_window(self):
        """`done` is the other door into the set. A report that `retention`
        settles is `done` with a fresh `status_since`, which is exactly what
        `include_done_days` lets back in -- so the clause has to hold on both
        sides or the row leaves for seven days and comes back."""
        writer = self.store()
        quiet = self.report(writer, attention=False, job="quiet-job")
        transition(writer, quiet, "done", who="retention")
        writer.commit()

        self.assertNotIn(quiet, [row["id"] for row in reads.backlog_items(writer)])

    def test_a_numeric_attention_is_quiet_and_agrees_with_is_urgent(self):
        """The two readers must not disagree about the same row.

        `json_extract` returns 1 for the JSON boolean `true` and for the JSON
        number `1` alike, while `is_urgent` accepts only the boolean. Before
        this was tightened the row below was in the backlog and not in the
        urgent quadrant -- the shared-row contract broken by the clause written
        to protect it. `ingest` cannot produce this row (`reporting.py:25`
        refuses a non-bool `attention`), so it is a hand-edited or legacy one,
        and quiet is the safe direction for those.
        """
        writer = self.store()
        quiet = self.report(writer, attention=False, job="quiet-job")
        writer.execute("UPDATE item SET fields=? WHERE id=?",
                       (json.dumps({"attention": 1, "report": {}}), quiet))
        writer.commit()

        rows = {row["id"]: row for row in reads.backlog_items(writer)}
        self.assertNotIn(quiet, rows)
        # The agreement is the assertion, not the exclusion on its own: both
        # readers have to reach the same verdict about this row.
        row = writer.execute("SELECT * FROM item WHERE id=?", (quiet,)).fetchone()
        self.assertFalse(reads.is_urgent(row))

    def test_a_report_whose_fields_cannot_speak_is_not_in_the_backlog_on_its_own(self):
        """sd:873. NULL, '' and `{not json` are one case at every site: a
        report whose `fields` cannot say it is clean is for a person to look
        at. Retention refuses to settle it and names it in the prune report,
        an attention report that IS in this backlog; the bulk clean declines
        it; the page withholds the one-click acknowledge. The backlog lists a
        report on its own word, and this row has none, so it is not listed
        on its own -- it reaches the person through the prune report, not
        by being called clean. The same three inputs are pinned at the other
        three sites, so the predicate cannot drift at one and stay green.
        """
        writer = self.store()
        for index, value in enumerate((None, "", "{not json")):
            with self.subTest(fields=value):
                quiet = self.report(writer, attention=False, job=f"quiet-job-{index}")
                writer.execute("UPDATE item SET fields=? WHERE id=?", (value, quiet))
                writer.commit()
                self.assertNotIn(quiet, [row["id"] for row in reads.backlog_items(writer)])
                row = writer.execute("SELECT * FROM item WHERE id=?", (quiet,)).fetchone()
                self.assertFalse(reads.is_urgent(row))

    def test_a_null_fields_report_is_quiet(self):
        """`item.fields` is nullable, and `json_valid(NULL)` is NULL rather
        than 0, so this path is distinct from malformed text and needs its own
        case -- otherwise a future rewrite of the clause could let NULL rows
        back in with every other test still green. Not in the backlog is not
        the same as clean: retention never settles this row and the prune
        report names it (sd:873, the three-input test above)."""
        writer = self.store()
        quiet = self.report(writer, attention=False, job="quiet-job")
        writer.execute("UPDATE item SET fields=NULL WHERE id=?", (quiet,))
        writer.commit()

        self.assertNotIn(quiet, [row["id"] for row in reads.backlog_items(writer)])

    def test_a_report_whose_fields_will_not_parse_is_treated_as_quiet(self):
        """`json_extract` and `json_type` do not return NULL for malformed
        JSON: they raise `sqlite3.OperationalError: malformed JSON`, and one
        such row would take down the whole backlog query. `json_valid` is
        0 for it, so the guard in `_NOT_A_QUIET_REPORT` short-circuits
        before `json_type` runs, and the row falls out. This is the test
        that proves the guard. Falling out is the safe direction: a report
        that cannot say it needs attention is not evidence that it does."""
        writer = self.store()
        quiet = self.report(writer, attention=False, job="quiet-job")
        writer.execute("UPDATE item SET fields='{not json' WHERE id=?", (quiet,))
        writer.commit()

        self.assertNotIn(quiet, [row["id"] for row in reads.backlog_items(writer)])


class CaptureItems(unittest.TestCase):
    def test_all_parents_have_explicit_fields_stable_order_and_one_readonly_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sd.db"
            initialise(path)
            writer = connect(path)
            repositories = (str(Path(temporary) / "alpha"), str(Path(temporary) / "beta"))
            for repo in repositories:
                upsert_repo(writer, repo)
            fixtures = (
                ("task", "planning", repositories[0], "2026-09-01", None),
                ("work", "in_progress", repositories[1], "2026-09-01", None),
                ("proposal", "ready", None, "2026-09-02", None),
                ("task", "done", repositories[0], "2026-09-05", None),
                ("idea", "planning", repositories[1], "2026-09-06", WHEN),
                ("idea", "done", repositories[0], "2026-09-04", WHEN),
                ("report", "planning", None, "2026-08-31", None),
                ("skill-review", "planning", None, "2026-08-31", None),
                ("dep", "blocked", None, "2026-08-31", None),
            )
            ids = []
            for kind, status, repo, updated, parked in fixtures:
                item = create_item(writer, kind=kind, title="Same title", status=status,
                                   repo=repo, body={"text": "Private body omitted from selector"})
                ids.append(item)
                writer.execute("UPDATE item SET updated_at=?, parked_at=? WHERE id=?", (updated, parked, item))
            writer.commit()
            writer.close()
            connection = connect(path, write=False)
            self.addCleanup(connection.close)
            before = tuple(connection.iterdump())
            statements = []
            connection.set_trace_callback(statements.append)
            rows = reads.capture_items(connection)
            connection.set_trace_callback(None)
            self.assertEqual(len(statements), 1)
            self.assertTrue(statements[0].lstrip().startswith("SELECT"))
            self.assertEqual([row["id"] for row in rows], [ids[i] for i in (2, 1, 0, 8, 7, 6, 4, 3, 5)])
            self.assertEqual({row["repo"] for row in rows}, {*repositories, None})
            self.assertTrue(all(row.keys() == ["id", "title", "kind", "status", "repo", "parked_at"] for row in rows))
            self.assertEqual(connection.total_changes, 0)
            self.assertEqual(tuple(connection.iterdump()), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
