"""The command journal reads every `exec` note its writers leave (sd:2183).

Two writers exist: the palette's `prepare`, a `version` 1 descriptor, and
`runner.release`, a run's outcome in plain text. Each fixture comes from the
real writer; the notes no writer leaves are added by hand and must be
counted and skipped, never raised.
"""

import json
import unittest

from sd_db import runner, runner_exec
from sd_db.testing.wire import hub_only
from sd_db.writes import add_note

from .test_runner_exec import PaletteFixture

#: The keys a palette row carried before sd:2183; its output stays exactly this.
PALETTE_KEYS = {"id", "item", "timestamp", "started", "ended", "exit_code", "session", "title",
                "command", "scope", "output_expired"}


class Journal(PaletteFixture):
    def released(self, *, outcome="blocked", detail="provider exited successfully but authored no commits"):
        """A run released by the runner, which writes its `exec` note."""
        queued = runner.enqueue(self.db, [self.item], who="operator")[0]
        claim = runner.claim(self.db, queued["id"], owner="fixture", work_root=self.root / "work",
                             retention_root=self.root / "retained")
        ident = claim["run"]["id"]
        runner.begin_ending(self.db, ident, outcome=outcome, detail=detail)
        runner.update_run(self.db, ident, end_step="retained")
        runner.release(self.db, ident, output_path=str(self.root / "retained/sd-provider.log"), exit_code=0)
        note = self.db.execute("SELECT id FROM note WHERE kind='exec' AND session='runner' ORDER BY id DESC").fetchone()
        return queued["id"], note["id"]

    @hub_only
    def test_a_palette_row_is_listed_exactly_as_before(self):
        prepared = self.prepare()["execution"]
        journal = runner_exec.execution_journal(self.db)
        self.assertEqual(journal["skipped"], {})
        [row] = journal["executions"]
        self.assertEqual(set(row), PALETTE_KEYS)
        self.assertEqual((row["id"], row["item"], row["command"], row["scope"], row["session"], row["title"]),
                         (prepared["note"], self.item, "inspect", "worktree", "operator", "Fixture"))
        self.assertIsNone(row["output_expired"])
        self.assertEqual(runner_exec.executions(self.db), journal["executions"])

    def test_a_runner_release_is_listed_with_its_role_and_detail(self):
        assignment, note = self.released()
        journal = runner_exec.execution_journal(self.db)
        self.assertEqual(journal["skipped"], {})
        [row] = journal["executions"]
        self.assertEqual(row["id"], note)
        self.assertEqual((row["command"], row["scope"], row["source"], row["assignment"]),
                         ("runner author", "runner", "runner", assignment))
        self.assertEqual(row["detail"], "provider exited successfully but authored no commits")
        self.assertEqual((row["session"], row["exit_code"], row["title"]), ("runner", 0, "Fixture"))
        self.assertIsNotNone(row["started"]); self.assertIsNotNone(row["ended"])
        self.assertIsNone(row["output_expired"])

    def test_a_runner_detail_that_parses_as_json_is_still_a_runner_row(self):
        _, note = self.released(detail="3")
        [row] = runner_exec.executions(self.db)
        self.assertEqual((row["id"], row["command"], row["detail"]), (note, "runner author", "3"))

    def test_a_runner_row_whose_run_is_gone_still_lists(self):
        _, note = self.released()
        self.db.execute("UPDATE note SET started='2000-01-01T00:00:00+00:00' WHERE id=?", (note,))
        [row] = runner_exec.executions(self.db)
        self.assertEqual((row["id"], row["command"], row["assignment"]), (note, "runner", None))

    def test_two_runs_with_the_note_s_times_name_no_role(self):
        _, first = self.released()
        _, second = self.released()
        times = self.db.execute("SELECT started,ended FROM note WHERE id=?", (first,)).fetchone()
        self.db.execute("UPDATE runner_run SET created_at=?,released_at=?", tuple(times))
        self.db.execute("UPDATE note SET started=?,ended=? WHERE id=?", (*times, second))
        rows = runner_exec.executions(self.db)
        self.assertEqual([(row["id"], row["command"], row["assignment"]) for row in rows],
                         [(second, "runner", None), (first, "runner", None)])

    @hub_only
    def test_both_writers_list_newest_first(self):
        prepared = self.prepare()["execution"]
        _, released = self.released()
        rows = runner_exec.executions(self.db)
        self.assertEqual([row["id"] for row in rows], [released, prepared["note"]])

    @hub_only
    def test_notes_no_writer_leaves_are_counted_and_skipped(self):
        prepared = self.prepare()["execution"]
        other = json.dumps({"version": 1, "note": prepared["note"], "command": "inspect"})
        unknown = {
            add_note(self.db, self.item, "exec", json.dumps({"version": 2, "command": "future"}), session="operator"),
            add_note(self.db, self.item, "exec", "{}", session="operator"),
            add_note(self.db, self.item, "exec", other, session="operator"),
            add_note(self.db, self.item, "exec", "free text from nobody", session="operator"),
            add_note(self.db, self.item, "exec", "not ended", session="runner"),
        }
        journal = runner_exec.execution_journal(self.db)
        self.assertEqual([row["id"] for row in journal["executions"]], [prepared["note"]])
        self.assertFalse(unknown & {row["id"] for row in journal["executions"]})
        self.assertEqual(journal["skipped"], {"unknown version": 2, "palette record for another note": 1,
                                              "unknown writer": 1, "runner outcome without an end": 1})


if __name__ == "__main__":
    unittest.main()
