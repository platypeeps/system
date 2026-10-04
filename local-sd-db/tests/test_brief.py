"""`sd_db.brief`, which is criterion 16's library clauses.

`prd.md:1582-1591`: three followups written through the library; one
resolved and Today lists the other two while the item screen shows all
three with the resolved one marked; and the bound -- a `done` item's open
notes absent, the brief cut at eight kilobytes with the count and the
listing command, and no decision, proposal, comment or `exec` note in it.
The injection is item A's `SessionStart` hook and is not tested here; what
is tested is the text it will inject and the rows it comes from.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from sd_db import brief, connect, create_item, reads, upsert_repo
from sd_db.migrate import initialise
from sd_db.schema import migrations
from sd_db.writes import add_note, resolve_note, set_item_fields

from . import support

REPO = "/checkouts/system"
OTHER = "/checkouts/elsewhere"


class BriefCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.database = self.home / "sd.db"
        initialise(self.database)
        self.connection = connect(self.database)
        self.addCleanup(self.connection.close)
        upsert_repo(self.connection, REPO)
        upsert_repo(self.connection, OTHER)

    def item(self, title: str, *, branch: str | None = None, status: str = "in_progress",
             repo: str = REPO) -> int:
        return create_item(
            self.connection, kind="work", title=title, status=status, repo=repo, branch=branch,
        )

    def note(self, item: int, body: str, *, kind: str = "followup", at: str | None = None) -> int:
        """A note, with its timestamp pinned when the order under test needs one."""
        note = add_note(self.connection, item, kind, body)
        if at is not None:
            self.connection.execute("UPDATE note SET timestamp = ? WHERE id = ?", (at, note))
        return note

    def brief(self, *, branch: str | None = None, limit: int = brief.BRIEF_BYTES) -> brief.Brief:
        # The branch is always passed: these rows name a checkout that does
        # not exist on disk, and a None here would spawn git against it.
        return brief.note_brief(self.connection, REPO, branch=branch or "", limit=limit)


class ThreeFollowups(BriefCase):
    """Criterion 16's first clause: written, listed, one resolved, two remain."""

    def setUp(self) -> None:
        super().setUp()
        self.work = self.item("the work", branch="feat/work")
        self.first = self.note(self.work, "first thing left", at="2026-09-10T09:00:00+00:00")
        self.second = self.note(self.work, "second thing left", at="2026-09-10T10:00:00+00:00")
        self.third = self.note(self.work, "third thing left", at="2026-09-10T11:00:00+00:00")

    def test_three_followups_are_in_the_brief_newest_first(self):
        result = self.brief(branch="feat/work")
        self.assertEqual(result.scope, "branch")
        self.assertEqual(result.items, (self.work,))
        self.assertEqual((result.shown, result.cut), (3, 0))
        self.assertEqual(
            [line for line in result.text.splitlines() if line.startswith("- ")],
            [
                f"- [followup #{self.third} 2026-09-10] third thing left",
                f"- [followup #{self.second} 2026-09-10] second thing left",
                f"- [followup #{self.first} 2026-09-10] first thing left",
            ],
        )
        self.assertTrue(result.text.startswith(
            f"Open followups and questions on the work (sd:{self.work}, branch feat/work):\n"
        ))
        self.assertNotIn("more open", result.text, "nothing was cut, so no trailer")

    def test_resolving_one_leaves_two_in_the_brief_and_on_today(self):
        resolve_note(self.connection, self.second)

        result = self.brief(branch="feat/work")
        self.assertEqual((result.shown, result.cut), (2, 0))
        self.assertNotIn("second thing left", result.text)
        self.assertIn("first thing left", result.text)
        self.assertIn("third thing left", result.text)
        # Today's list is `open_followups`, which the Today screen renders
        # (`local-project-dashboard/sd_dashboard/screens.py:163`).
        self.assertEqual(
            [row["id"] for row in reads.open_followups(self.connection)],
            [self.first, self.third],
        )

    def test_the_item_screen_shows_all_three_with_the_resolved_one_marked(self):
        resolve_note(self.connection, self.second)

        # The item screen reads `item_notes`; the mark is `resolved_at`.
        history = reads.item_notes(self.connection, self.work)
        followups = [row for row in history if row["kind"] == "followup"]
        self.assertEqual([row["id"] for row in followups], [self.first, self.second, self.third])
        self.assertEqual(
            [row["resolved_at"] is not None for row in followups], [False, True, False]
        )

    def test_a_resolved_note_never_returns(self):
        resolve_note(self.connection, self.third)
        resolve_note(self.connection, self.third)
        self.assertNotIn("third thing left", self.brief(branch="feat/work").text)
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM note WHERE id = ? AND resolved_at IS NOT NULL", (self.third,)
            ).fetchone()[0],
            1,
        )


class TheBound(BriefCase):
    """Criterion 16's second clause, `prd.md:1587-1591`."""

    def setUp(self) -> None:
        super().setUp()
        self.finished = self.item("finished work", branch="feat/live", status="done")
        self.note(self.finished, "a followup on a done item, still open")
        self.note(self.finished, "a question on a done item, still open", kind="question")
        self.live = self.item("live work", branch="feat/live")
        # Every kind the schema knows but `status_change`, which only
        # `transition` writes and which `create_item` wrote already.
        for kind in ("decision", "proposal", "comment"):
            self.note(self.live, f"a {kind} that must not inject", kind=kind)
        add_note(
            self.connection, self.live, "exec", "an exec note that must not inject",
            started="2026-09-10T09:00:00+00:00", ended="2026-09-10T09:01:00+00:00",
            exit_code=0, output_path="/nowhere",
        )
        self.question = self.note(self.live, "an open question", kind="question",
                                  at="2026-09-09T05:30:00+00:00")
        # Enough followups to pass eight kilobytes several times over: two
        # hundred, each about a hundred bytes.
        self.followups = [
            self.note(self.live, f"followup {index:03d}: " + "x" * 80,
                      at=f"2026-09-{1 + index // 24:02d}T{index % 24:02d}:00:00+00:00")
            for index in range(200)
        ]

    def test_a_done_items_open_notes_are_absent(self):
        result = self.brief(branch="feat/live")
        self.assertEqual(result.items, (self.live,))
        self.assertNotIn("done item", result.text)
        # And not by way of the repository-wide case either.
        result = self.brief(branch="main")
        self.assertEqual(result.scope, "repository")
        self.assertEqual(result.items, (self.live,))
        self.assertNotIn("done item", result.text)

    def test_the_brief_is_cut_at_eight_kilobytes_with_the_count_and_the_command(self):
        result = self.brief(branch="feat/live")
        size = len(result.text.encode("utf-8"))
        self.assertLessEqual(size, brief.BRIEF_BYTES)
        self.assertGreater(size, brief.BRIEF_BYTES - 200, "the cut is greedy: one more note would not fit")
        total = len(self.followups) + 1
        self.assertEqual(result.shown + result.cut, total)
        self.assertGreater(result.cut, 0)
        self.assertEqual(result.commands, (f"sd note list {self.live}",))
        self.assertTrue(result.text.endswith(
            f"{result.cut} more open notes not shown; the rest: `sd note list {self.live}`\n"
        ))
        shown = [line for line in result.text.splitlines() if line.startswith("- ")]
        self.assertEqual(len(shown), result.shown)

    def test_one_more_note_would_not_fit(self):
        result = self.brief(branch="feat/live")
        wider = self.brief(branch="feat/live", limit=brief.BRIEF_BYTES + 200)
        self.assertGreater(wider.shown, result.shown)
        self.assertLessEqual(len(wider.text.encode("utf-8")), brief.BRIEF_BYTES + 200)

    def test_no_decision_proposal_comment_or_exec_note_is_in_it(self):
        result = self.brief(branch="feat/live")
        for word in ("decision", "proposal", "comment", "exec", "status_change", "must not inject"):
            self.assertNotIn(word, result.text)
        self.assertIn("[question #", result.text)

    def test_newest_first_puts_the_last_followup_at_the_top(self):
        result = self.brief(branch="feat/live")
        first_line = result.text.splitlines()[1]
        self.assertTrue(first_line.startswith(f"- [followup #{self.followups[-1]} "), first_line)

    def test_the_cut_renders_the_brief_once(self):
        # sd:1219 (d15d4d3e5b22): the cut rendered and encoded every prefix
        # from all notes down, so a backlog cost quadratic work in its bytes.
        for index in range(40):
            self.note(self.live, f"followup {index} " + "x" * 200)
        from unittest import mock

        with mock.patch.object(brief, "_render", wraps=brief._render) as render:
            result = self.brief(branch="feat/live", limit=2048)
        self.assertEqual(render.call_count, 1)
        self.assertGreater(result.cut, 0)
        self.assertLessEqual(len(result.text.encode("utf-8")), 2048)

    def test_a_bound_smaller_than_a_line_still_names_the_count_and_the_command(self):
        result = self.brief(branch="feat/live", limit=10)
        self.assertEqual(result.shown, 0)
        self.assertEqual(result.cut, len(self.followups) + 1)
        self.assertIn(f"sd note list {self.live}", result.text)


class WhichItems(BriefCase):
    """Requirement 7's two cases: the branch's item, or every live item."""

    def test_the_checked_out_branch_selects_its_item_and_no_other(self):
        mine = self.item("mine", branch="feat/mine")
        theirs = self.item("theirs", branch="feat/theirs")
        self.note(mine, "on mine")
        self.note(theirs, "on theirs")
        result = self.brief(branch="feat/mine")
        self.assertEqual((result.scope, result.items), ("branch", (mine,)))
        self.assertIn("on mine", result.text)
        self.assertNotIn("on theirs", result.text)

    def test_an_item_stored_on_the_remote_tracking_name_is_on_the_checked_out_branch(self):
        # `sources.docs_work` records the ref it read the prd from, which is
        # `origin/feat/mine`; `brief.checked_out_branch` answers `feat/mine`.
        # Compared exactly they never met, so a session working the branch
        # was briefed on every open item in the repository instead.
        mine = self.item("mine", branch="origin/feat/mine")
        theirs = self.item("theirs", branch="feat/theirs")
        self.note(mine, "on mine")
        self.note(theirs, "on theirs")
        result = self.brief(branch="feat/mine")
        self.assertEqual((result.scope, result.items), ("branch", (mine,)))
        self.assertIn("on mine", result.text)
        self.assertNotIn("on theirs", result.text)

    def test_no_match_reads_every_live_item_in_the_repository_and_nothing_outside_it(self):
        planning = self.item("planning", branch="feat/planning", status="planning")
        blocked = self.item("blocked", branch=None, status="blocked")
        finished = self.item("finished", branch="feat/finished", status="done")
        parked = self.item("parked", branch="feat/parked")
        set_item_fields(self.connection, parked, parked_at="2026-09-01T00:00:00+00:00")
        elsewhere = self.item("elsewhere", branch="main", repo=OTHER)
        for item, body in ((planning, "on planning"), (blocked, "on blocked"),
                           (finished, "on finished"), (parked, "on parked"),
                           (elsewhere, "on elsewhere")):
            self.note(item, body)

        result = self.brief(branch="main")
        self.assertEqual(result.scope, "repository")
        self.assertEqual(set(result.items), {planning, blocked})
        self.assertIn("on planning", result.text)
        self.assertIn("on blocked", result.text)
        for absent in ("on finished", "on parked", "on elsewhere"):
            self.assertNotIn(absent, result.text)
        # More than one item, so each line names its item.
        self.assertIn(f"(planning, sd:{planning})", result.text)
        self.assertIn(f"(blocked, sd:{blocked})", result.text)
        self.assertIn("across 2 live items", result.text)

    def test_a_done_item_on_the_checked_out_branch_does_not_match(self):
        finished = self.item("finished", branch="feat/done", status="done")
        live = self.item("live", branch="feat/live")
        self.note(finished, "on finished")
        self.note(live, "on live")
        result = self.brief(branch="feat/done")
        self.assertEqual((result.scope, result.items), ("repository", (live,)))
        self.assertNotIn("on finished", result.text)

    def test_two_live_items_on_one_branch_are_both_in(self):
        one = self.item("one", branch="feat/shared")
        two = self.item("two", branch="feat/shared")
        self.note(one, "on one")
        self.note(two, "on two")
        result = self.brief(branch="feat/shared")
        self.assertEqual(result.scope, "branch")
        self.assertEqual(set(result.items), {one, two})
        self.assertIn("on the 2 items on branch feat/shared", result.text)

    def test_the_cut_names_every_item_that_lost_a_note(self):
        one = self.item("one", branch=None)
        two = self.item("two", branch=None)
        for index in range(40):
            self.note(one, f"one {index}", at=f"2026-09-01T{index % 24:02d}:{index:02d}:00+00:00")
            self.note(two, f"two {index}", at=f"2026-09-01T{index % 24:02d}:{index:02d}:01+00:00")
        result = self.brief(branch="main", limit=600)
        self.assertGreater(result.cut, 0)
        self.assertEqual(set(result.commands), {f"sd note list {one}", f"sd note list {two}"})
        self.assertIn(f"`sd note list {one}`", result.text)
        self.assertIn(f"`sd note list {two}`", result.text)

    def test_nothing_open_is_an_empty_text(self):
        live = self.item("live", branch="feat/live")
        done_note = self.note(live, "finished already")
        resolve_note(self.connection, done_note)
        result = self.brief(branch="feat/live")
        self.assertEqual(result.text, "")
        self.assertEqual((result.shown, result.cut, result.items), (0, 0, (live,)))

    def test_a_multi_line_body_is_indented_and_the_bound_is_bytes(self):
        live = self.item("live", branch="feat/live")
        self.note(live, "first line of a longer body\nsecond line of it\nthird line of it")
        self.note(live, "ünïcödé " * 3, at="2026-09-11T00:00:00+00:00")
        result = self.brief(branch="feat/live")
        self.assertIn("first line of a longer body\n  second line of it\n  third line of it\n", result.text)
        self.assertGreater(len(result.text.encode("utf-8")), len(result.text))
        tight = self.brief(branch="feat/live", limit=len(result.text.encode("utf-8")) - 1)
        self.assertEqual(tight.shown, 1)


class BranchFromGit(unittest.TestCase):
    """`branch=None` reads the checkout; a detached HEAD is the repository case."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.repo = support.repository(home / "repo")
        initialise(home / "sd.db")
        self.connection = connect(home / "sd.db")
        self.addCleanup(self.connection.close)
        upsert_repo(self.connection, str(self.repo))
        self.mine = create_item(
            self.connection, kind="work", title="mine", status="in_progress",
            repo=str(self.repo), branch="feat/mine",
        )
        self.other = create_item(
            self.connection, kind="work", title="other", status="ready",
            repo=str(self.repo), branch="feat/other",
        )
        add_note(self.connection, self.mine, "followup", "on mine")
        add_note(self.connection, self.other, "followup", "on other")

    def test_the_branch_is_read_from_the_checkout(self):
        support.git(self.repo, "checkout", "-q", "-b", "feat/mine")
        self.assertEqual(brief.checked_out_branch(self.repo), "feat/mine")
        result = brief.note_brief(self.connection, str(self.repo))
        self.assertEqual((result.scope, result.items), ("branch", (self.mine,)))

    def test_a_detached_head_is_the_repository_case(self):
        support.git(self.repo, "checkout", "-q", "--detach", "HEAD")
        self.assertIsNone(brief.checked_out_branch(self.repo))
        result = brief.note_brief(self.connection, str(self.repo))
        self.assertEqual(result.scope, "repository")
        self.assertEqual(set(result.items), {self.mine, self.other})

    def test_a_path_that_is_not_a_repository_is_the_repository_case(self):
        self.assertIsNone(brief.checked_out_branch(Path(self.tmp.name) / "nowhere"))

    def test_a_bare_repository_is_the_repository_case(self):
        # sd:1219 (b26d51b72afb): a bare repository's HEAD still names its
        # default branch, so the brief took the branch path with nothing
        # checked out.
        bare = Path(self.tmp.name) / "bare.git"
        support.git(Path(self.tmp.name), "clone", "-q", "--bare", str(self.repo), str(bare))
        self.assertIsNone(brief.checked_out_branch(bare))


class SchemaTwo(unittest.TestCase):
    """A store from before the parking column is still readable, as `open_followups` is."""

    def test_the_brief_reads_a_store_without_parked_at(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "schema-two.db"
            raw = sqlite3.connect(path)
            for version, source in migrations():
                if version <= 2:
                    raw.executescript(source.read_text())
            raw.execute("PRAGMA user_version = 2")
            raw.execute("INSERT INTO repo (path, created_at, updated_at) VALUES (?, ?, ?)",
                        (REPO, "2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"))
            raw.execute(
                "INSERT INTO item (kind,title,status,repo,branch,created_at,updated_at) "
                "VALUES ('task','Old task','in_progress',?,'feat/old',?,?)",
                (REPO, "2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
            )
            raw.execute("INSERT INTO note (item,timestamp,kind,body) VALUES (1,?,'followup','Old followup')",
                        ("2026-09-01T00:00:00+00:00",))
            raw.commit()
            raw.close()
            connection = connect(path, write=False)
            self.addCleanup(connection.close)
            before = connection.total_changes
            result = brief.note_brief(connection, REPO, branch="feat/old")
            self.assertEqual((result.scope, result.shown), ("branch", 1))
            self.assertIn("Old followup", result.text)
            self.assertEqual(connection.total_changes, before)


if __name__ == "__main__":
    unittest.main()
