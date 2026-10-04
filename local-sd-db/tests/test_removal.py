"""The plans, the record and the apply of `item remove` and `repo remove` (sd:754, PR 1 and PR 2).

A plan names every row a remove would take, every reason it must refuse,
the journal files the apply would move and the files and references it
would leave, and hashes the rows and refusal keys into a fingerprint. The
apply plans again under `BEGIN IMMEDIATE`, files the record, deletes, commits
and moves the journal files. These tests build their own stores in a
temporary directory, outside `default_path(home)`, and never open the live
store. Each refusal has a test that a mutation of exactly its guard fails
(`implement.md` steps 2 to 5). The journal move against the runner's own
readers is `local-sd-runner/tests/test_removal_journal.py`.
"""
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import sd_db
from sd_db import contribution_sync, contributions, removal, reporting, retention, runner_journal
from sd_db.contribution_sync import QUEUE
from sd_db.migrate import initialise
from sd_db.writes import (add_note, create_assignment, create_item, record_state, resolve_note, resolve_state,
                          upsert_repo)

backup = removal.backups

STAMP = "2026-09-09T12:00:00+00:00"
WHO = {"who": "alex", "reason": "the probe is finished", "session": None, "principal": "alex",
       "program": "sd-db.sh item remove"}


class Store(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"; self.home.mkdir()
        self.store = self.root / "store"
        initialise(self.store / "sd.db")
        self.db = sd_db.connect(self.store / "sd.db"); self.addCleanup(self.db.close)
        self.retained = self.root / "retained"; self.retained.mkdir()

    # -- fixture rows ----------------------------------------------------

    def repo(self, name="source"):
        path = str(self.root / "checkouts" / name)
        upsert_repo(self.db, path)
        return path

    def item(self, repo=None, **kwargs):
        kwargs = {"kind": "task", "title": "an item", "status": "done", **kwargs}
        return create_item(self.db, repo=repo, **kwargs)

    def assignment(self, item, status="done", **kwargs):
        return create_assignment(self.db, role="author", status=status, item=item, **kwargs)

    def newer(self):
        """An assignment on another item, newer than every one before it, so A1 clears."""
        return self.assignment(self.item(title="later work"))

    def attempt(self, assignment, repo, *, number=1, released=True, clone=False, retained_path=None, root=None):
        ident = uuid.uuid4().hex
        item = self.db.execute("SELECT item FROM assignment WHERE id=?", (assignment,)).fetchone()[0]
        retained = retained_path or str((root or self.retained) / str(assignment) / str(number) / "clone")
        if clone:
            Path(retained).mkdir(parents=True)
        self.db.execute(
            "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, created_at,"
            " updated_at, released_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ident, assignment, number, repo, "sd/probe", "runner", str(self.root / "work" / str(item)
             / f"{assignment}-{number}-{ident}"), retained, STAMP, STAMP, STAMP if released else None))
        return ident

    def lease(self, run, repo, *, released=True):
        self.db.execute("INSERT INTO runner_lease (run, repo, branch, exclusive, acquired_at, released_at)"
                        " VALUES (?,?,?,?,?,?)", (run, repo, "sd/probe", 1, STAMP, STAMP if released else None))

    # -- reading a plan ---------------------------------------------------

    def plan(self, item):
        return removal.plan_item(self.db, item, home=self.home)

    def plan_repo(self, path, with_items=True):
        return removal.plan_repo(self.db, path, with_items=with_items, home=self.home)

    @staticmethod
    def refused(plan, code):
        return [refusal for refusal in plan["refusals"] if refusal["code"] == code]

    @staticmethod
    def keys(plan):
        return [(entry["table"], entry["row"]["id" if "id" in entry["row"] else
                                             "path" if entry["table"] == "repo" else
                                             "repo" if entry["table"] == "repo_protection" else "run"])
                for entry in plan["rows"]]

    # -- the apply --------------------------------------------------------

    def journal(self, runs):
        """Each run's journal pair, written as the runner writes it, so `backup.run` accepts the store."""
        for run in runs:
            row = dict(self.db.execute("SELECT * FROM runner_run WHERE id=?", (run,)).fetchone())
            runner_journal.persist(self.store / "sd.db", row)
        return self.store / "runner-journal"

    def counts(self, connection=None):
        connection = connection or self.db
        return {name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def apply(self, kind, target, fingerprint, **changes):
        return removal.apply(self.db, kind, target, fingerprint=fingerprint, home=self.home, with_items=True,
                             **{**WHO, **changes})

    def record(self, ident):
        row = self.db.execute("SELECT * FROM item WHERE id=?", (ident,)).fetchone()
        return dict(row), json.loads(row["fields"]), json.loads(row["body"])["text"]

    def chunk_rows(self, record):
        """The rows in the record's chunk notes, in order, each header's sha256 checked."""
        rows = []
        for (body,) in self.db.execute("SELECT body FROM note WHERE item=? AND kind='comment' ORDER BY id", (record,)):
            header, _, rest = body.partition("\n")
            lines = rest.split("\n")
            digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
            self.assertRegex(header, r"^removed rows \d+ of \d+, sha256 " + digest + "$")
            rows += [json.loads(line) for line in lines]
        return rows


class ActorIsChecked(Store):
    """G4, raised by `check_actor`; the plans take no actor values (C-61)."""

    def test_each_blank_or_long_actor_value_is_refused(self):
        long = "x" * 201
        cases = {"who blank": {"who": "  "}, "reason blank": {"reason": ""}, "who long": {"who": long},
                 "reason long": {"reason": long}, "session long": {"session": long},
                 "principal long": {"principal": long}, "program long": {"program": long}}
        for name, change in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(removal.RemovalRefused, "G4"):
                removal.check_actor(**{**WHO, **change})

    def test_a_blank_or_missing_principal_or_program_is_refused(self):
        """The #407 review: `apply` stores `program` as the record's `source_path` and
        `principal` beside `who` (D9a), so neither may be empty. `session` may be `None`."""
        for name in ("principal", "program"):
            for value in ("", "  ", None):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(removal.RemovalRefused, "G4"):
                    removal.check_actor(**{**WHO, name: value})
        removal.check_actor(**{**WHO, "session": None})

    def test_values_at_the_limit_pass(self):
        removal.check_actor(**{**WHO, "who": "w" * 200, "reason": "r" * 200, "session": "s" * 200,
                               "principal": "p" * 200, "program": "g" * 200})

    def test_a_line_break_or_control_character_in_who_or_reason_is_refused(self):
        """The #336 review: `_record` writes both into manifest lines, so a line break
        would let a caller forge a `removed:` line that `records` then matches."""
        for name in ("who", "reason"):
            for text in ("alex\nremoved:\nitem 1", "alex\r", "a\x85b", "a b", "a b", "a\tb",
                         "a\x1eb", "a\x7fb", "a\x1bb"):
                with self.subTest(name=name, text=text), self.assertRaisesRegex(removal.RemovalRefused, "G4"):
                    removal.check_actor(**{**WHO, name: text})
        removal.check_actor(**{**WHO, "who": "Alex Morgan", "reason": "done — the probe is retired"})

    def test_the_apply_refuses_a_line_break_before_it_reads_anything(self):
        item = self.item()
        self.newer()
        fingerprint = self.plan(item)["fingerprint"]
        before = self.counts()
        with self.assertRaisesRegex(removal.RemovalRefused, "G4"):
            self.apply("item", item, fingerprint, reason="retired\nremoved:\nitem 1")
        self.assertEqual(self.counts(), before)


class ItemRows(Store):

    def test_an_item_lists_exactly_its_rows(self):
        item = self.item()
        opening = self.db.execute("SELECT id FROM note WHERE item=?", (item,)).fetchone()[0]
        decision = add_note(self.db, item, "decision", "go", session="alex")
        execution = add_note(self.db, item, "exec", "{}", session="runner", started=STAMP, ended=STAMP, exit_code=0)
        work = self.assignment(item)
        self.newer()
        plan = self.plan(item)
        self.assertEqual(self.keys(plan), [("item", item), ("note", opening), ("note", decision),
                                           ("note", execution), ("assignment", work)])
        self.assertEqual(plan["counts"], {"item": 1, "note": 3, "assignment": 1})
        self.assertEqual(plan["refusals"], [])
        row = plan["rows"][0]["row"]
        self.assertEqual(set(row), {r[1] for r in self.db.execute("PRAGMA table_info(item)")})

    def test_the_plan_writes_nothing(self):
        item = self.item()
        self.assignment(item)
        before = tuple(self.db.iterdump())
        self.plan(item)
        self.assertEqual(before, tuple(self.db.iterdump()))

    def test_a_note_added_after_a_plan_changes_the_fingerprint(self):
        item = self.item()
        first = self.plan(item)["fingerprint"]
        self.assertEqual(self.plan(item)["fingerprint"], first)
        add_note(self.db, item, "comment", "one more", session="alex")
        self.assertNotEqual(self.plan(item)["fingerprint"], first)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_every_foreign_key_into_the_four_parents_has_a_rule(self):
        # The parents are `removal.SCANNED`, compared lower-cased as `_unknown_children`
        # compares them, so neither a fifth parent nor `REFERENCES ITEM` slips past (the #400 review).
        def children():
            return {(row[0], row[1]) for row in self.db.execute(
                "SELECT m.name, p.\"from\" FROM sqlite_master m JOIN pragma_foreign_key_list(m.name) p"
                f" WHERE m.type='table' AND lower(p.\"table\") IN ({','.join('?' * len(removal.SCANNED))})",
                [parent.lower() for parent in removal.SCANNED])}
        found = children()
        self.assertTrue(found)
        self.assertEqual(found - set(removal.REFERENCES), set())
        self.assertTrue(all(removal.REFERENCES[key] for key in found))
        # A migration that adds a child table without a rule is caught, however it spells the parent.
        self.db.execute("CREATE TABLE fixture_child (item INTEGER REFERENCES item(id))")
        self.db.execute("CREATE TABLE fixture_upper (run INTEGER REFERENCES RUNNER_RUN(id))")
        self.assertEqual(children() - set(removal.REFERENCES), {("fixture_child", "item"), ("fixture_upper", "run")})


class GuardsOnEitherVerb(Store):

    def test_g1_a_store_with_an_orphan_is_refused(self):
        item = self.item()
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.db.execute("INSERT INTO note (item, timestamp, kind, body) VALUES (9999, ?, 'comment', 'orphan')",
                        (STAMP,))
        self.db.execute("PRAGMA foreign_keys=ON")
        self.assertEqual([r["table"] for r in self.refused(self.plan(item), "G1")], ["note"])

    def test_g2_an_unresolved_restore_is_refused(self):
        item = self.item()
        self.assertEqual(self.refused(self.plan(item), "G2"), [])
        record_state(self.db, "restore", body={"snapshot": "x"})
        self.assertEqual(len(self.refused(self.plan(item), "G2")), 1)

    def test_g6_a_record_for_this_fingerprint_refuses_and_another_does_not(self):
        item = self.item()
        fingerprint = self.plan(item)["fingerprint"]
        for run_id, expected in (("b" * 64, 0), (fingerprint, 1)):
            with self.subTest(run_id=run_id[:4]):
                reporting.ingest(self.db, job="item-remove", run_id=run_id, started=STAMP, ended=STAMP,
                                 exit_code=0, text="remove\n", source_path="test", record="item-remove")
                plan = self.plan(item)
                self.assertEqual(len(self.refused(plan, "G6")), expected)
                self.assertEqual(plan["fingerprint"], fingerprint)
        self.assertIn("put back", self.refused(self.plan(item), "G6")[0]["message"])


class APlanReadsOneSnapshot(Store):
    """The #350 review: every read of one plan sees the store as it stood at one moment.

    A second connection writes while the plan is half read. Each test puts the
    write between two of the plan's reads, and the row it writes is one a
    later read would see: without one snapshot, the plan mixes the two states.
    """

    def writer(self):
        other = sd_db.connect(self.store / "sd.db"); self.addCleanup(other.close)
        return other

    def during(self, name, write):
        """Run `write` once, right after the plan's first call to `removal.<name>` returns."""
        real = getattr(removal, name)
        done = []

        def wrapped(*args, **kwargs):
            result = real(*args, **kwargs)
            if not done:
                done.append(True)
                write()
            return result

        return mock.patch.object(removal, name, side_effect=wrapped)

    def test_an_item_plan_does_not_see_a_restore_that_lands_part_way(self):
        item = self.item()
        self.newer()
        before = self.plan(item)
        other = self.writer()
        with self.during("_runs", lambda: record_state(other, "restore", body={"snapshot": "x"})):
            during = self.plan(item)
        self.assertEqual(self.refused(during, "G2"), [])
        self.assertEqual(during["fingerprint"], before["fingerprint"])
        self.assertEqual(len(self.refused(self.plan(item), "G2")), 1)

    def test_a_repo_plan_does_not_see_an_item_that_lands_part_way(self):
        path = self.repo()
        before = self.plan_repo(path, with_items=False)
        other = self.writer()
        with self.during("_guards", lambda: create_item(other, repo=path, kind="task", title="late", status="done")):
            during = self.plan_repo(path, with_items=False)
        self.assertEqual(self.refused(during, "P3"), [])
        self.assertEqual(during["fingerprint"], before["fingerprint"])
        self.assertEqual(len(self.refused(self.plan_repo(path, with_items=False), "P3")), 1)

    def test_a_plan_takes_no_write_lock_and_leaves_no_transaction_open(self):
        item = self.item()
        other = self.writer()
        with self.during("_runs", lambda: add_note(other, item, "comment", "while reading", session="alex")):
            self.plan(item)
        self.assertFalse(self.db.in_transaction)
        read_only = sd_db.connect(self.store / "sd.db", write=False); self.addCleanup(read_only.close)
        self.assertEqual(removal.plan_item(read_only, item, home=self.home)["fingerprint"], self.plan(item)["fingerprint"])
        self.assertFalse(read_only.in_transaction)


class AssignmentIdsAreNotReused(Store):
    """A1: a removed assignment id must stay below a surviving one (C-2, C-23)."""

    def test_the_newest_assignment_is_refused(self):
        self.assignment(self.item(title="older"))
        item = self.item()
        self.assignment(item)
        self.assertEqual(len(self.refused(self.plan(item), "A1")), 1)

    def test_a_newer_assignment_on_another_item_clears_it(self):
        item = self.item()
        self.assignment(item)
        self.newer()
        self.assertEqual(self.refused(self.plan(item), "A1"), [])

    def test_no_surviving_assignment_is_refused(self):
        item = self.item()
        self.assignment(item)
        self.assertEqual(len(self.refused(self.plan(item), "A1")), 1)

    def test_with_no_survivor_the_message_names_no_id(self):
        gone = self.assignment(self.item(title="gone"))
        item = self.item()
        self.assignment(item)
        self.db.execute("DELETE FROM assignment WHERE id=?", (gone,))
        [refusal] = self.refused(self.plan(item), "A1")
        self.assertNotRegex(refusal["message"], r"reuse id \d")

    def test_an_item_with_no_assignment_is_not_refused(self):
        self.assertEqual(self.refused(self.plan(self.item()), "A1"), [])


class ItemRefusals(Store):

    def test_i1_no_such_item(self):
        plan = self.plan(9999)
        self.assertEqual([(r["table"], r["key"]) for r in self.refused(plan, "I1")], [("item", 9999)])
        self.assertEqual(plan["rows"], [])

    def claim(self, ident, item, active):
        self.db.execute("PRAGMA ignore_check_constraints=ON")
        self.db.execute("INSERT INTO publication_claim (id, item, active_item, payload, state, created_at,"
                        " updated_at) VALUES (?,?,?,'{}','{}',?,?)", (ident, item, active, STAMP, STAMP))
        self.db.execute("PRAGMA ignore_check_constraints=OFF")

    def test_i2_a_publication_claim_as_item_and_as_active_item(self):
        item, other = self.item(), self.item(title="other")
        self.claim("as-item", item, None)
        self.assertEqual([r["key"] for r in self.refused(self.plan(item), "I2")], ["as-item"])
        self.db.execute("DELETE FROM publication_claim")
        # Only `active_item` names it; the schema's CHECK is bypassed to build this.
        self.claim("as-active", other, item)
        self.assertEqual([r["key"] for r in self.refused(self.plan(item), "I2")], ["as-active"])

    def test_i3_each_unfinished_status_is_refused(self):
        for status in ("queued", "running", "ending", "blocked"):
            with self.subTest(status=status):
                item = self.item()
                work = self.assignment(item, status=status)
                self.newer()
                self.assertEqual([r["key"] for r in self.refused(self.plan(item), "I3")], [work])
        item = self.item()
        self.assignment(item, status="cancelled")
        self.newer()
        self.assertEqual(self.refused(self.plan(item), "I3"), [])

    def test_i4_a_cost_row_on_its_assignment(self):
        item = self.item()
        work = self.assignment(item)
        self.newer()
        self.assertEqual(self.refused(self.plan(item), "I4"), [])
        self.db.execute("INSERT INTO cost (timestamp, assignment, source) VALUES (?, ?, 'run')", (STAMP, work))
        self.assertEqual(len(self.refused(self.plan(item), "I4")), 1)

    def test_i5_another_items_assignment_points_at_it(self):
        for column in ("after", "parent"):
            with self.subTest(column=column):
                item = self.item()
                work = self.assignment(item)
                child = self.assignment(self.item(title="dependent"), **{column: work})
                self.assertEqual([r["key"] for r in self.refused(self.plan(item), "I5")], [child])

    def test_i5_a_chain_inside_the_item_is_not_refused(self):
        item = self.item()
        self.assignment(item, after=self.assignment(item))
        self.newer()
        self.assertEqual(self.refused(self.plan(item), "I5"), [])

    def test_i7_an_open_followup_or_question(self):
        for kind in ("followup", "question"):
            with self.subTest(kind=kind):
                item = self.item()
                note = add_note(self.db, item, kind, "open", session="alex")
                self.assertEqual([r["key"] for r in self.refused(self.plan(item), "I7")], [note])
                resolve_note(self.db, note)
                self.assertEqual(self.refused(self.plan(item), "I7"), [])

    def test_i8_each_way_a_routine_command_recreates_the_row(self):
        checkout = self.root / "checkouts" / "source"
        prd = checkout / "docs/work/an-item/prd.md"
        repo = self.repo()
        item = self.item(repo=repo, kind="work", status="planning", path="docs/work/an-item/prd.md",
                         source="docs/work", external_id=f"{repo}::docs/work/an-item/prd.md")
        self.assertEqual(self.refused(self.plan(item), "I8"), [])
        prd.parent.mkdir(parents=True); prd.write_text("---\n")
        self.assertEqual(len(self.refused(self.plan(item), "I8")), 1)
        piece = self.item()
        self.db.execute("UPDATE item SET piece='a-piece' WHERE id=?", (piece,))
        self.assertEqual(len(self.refused(self.plan(piece), "I8")), 1)
        for name in ("contribution", "skill_review"):
            with self.subTest(field=name):
                other = self.item(fields={name: {"x": 1}})
                self.assertEqual([r["message"] for r in self.refused(self.plan(other), "I8")],
                                 [f"item {other} carries fields.{name}, which a routine command reads back"])

    def test_i9_any_record_marker_is_refused(self):
        for job in ("item-remove", "repo-remove", "reports-acknowledge"):
            with self.subTest(job=job):
                state = reporting.ingest(self.db, job=job, run_id=uuid.uuid4().hex, started=STAMP, ended=STAMP,
                                         exit_code=0, text="x\n", source_path="test", record=job)
                self.assertEqual(len(self.refused(self.plan(state["item"]["id"]), "I9")), 1)

    def test_i9_a_json_null_marker_is_a_record_and_an_empty_object_is_not(self):
        report = {"job": "nightly", "run_id": "r"}
        item = self.item(kind="report")
        for fields, expected in (({}, 0), ({"attention": False, "record": None, "report": report}, 1)):
            with self.subTest(fields=fields):
                self.db.execute("UPDATE item SET fields=? WHERE id=?", (json.dumps(fields), item))
                refusals = self.refused(self.plan(item), "I9")
                self.assertEqual(len(refusals), expected)

    def queue(self, pending):
        self.db.execute("DELETE FROM state WHERE key=?", (QUEUE,))
        saved = record_state(self.db, "checkpoint", key=QUEUE, body={"pending": pending})
        resolve_state(self.db, saved)
        return saved

    def test_i10_a_dependent_names_it(self):
        item = self.item()
        dependent = self.item(title="dependent", fields={"contribution": {"depends_on": [
            {"kind": "merge", "url": "https://github.com/o/r/pull/1"}, {"kind": "item", "item": item}]}})
        self.assertEqual([(r["table"], r["key"]) for r in self.refused(self.plan(item), "I10")],
                         [("item", dependent)])

    def test_i10_the_entry_contribution_sync_plan_writes_names_it(self):
        """The main case: the queue entry as `contribution_sync.plan` builds it (`source:local-sd-db/sd_db/contribution_sync.py::plan`).

        It names the item under `item` and in the key. design.md section 3 says
        `item_id`, the projection's name; the plan accepts either (a plan divergence).
        """
        draft = self.root / "draft.md"; draft.write_text("# Bug\n")
        item = contributions.capture(self.db, title="Draft", who="operator", changes={
            "target_repo": "example/project", "draft_title": "Bug",
            "draft_path": {"path": str(draft), "sha256": hashlib.sha256(draft.read_bytes()).hexdigest()},
            "depends_on": [{"kind": "issue", "url": "https://github.com/example/project/issues/9"}]})["item"]["id"]
        other = self.item(title="other")
        pending = contribution_sync.plan(self.db)["pending"]
        [entry] = [entry for entry in pending if entry.get("depends_on")]
        self.assertEqual((entry["key"], entry["item"], "item_id" in entry), (f"item:{item}", item, False))
        saved = self.queue(pending)
        self.assertEqual([(r["table"], r["key"]) for r in self.refused(self.plan(item), "I10")], [("state", saved)])
        self.assertEqual(self.refused(self.plan(other), "I10"), [])

    def test_i10_an_entry_named_by_one_field_or_a_dependency_names_it(self):
        """Each way of naming the item refuses on its own, so no match hides another.

        `contribution_sync.plan` writes the id in both `item` and the key, so
        the real-shape test cannot tell the two apart; here each stands alone.
        `item_id` is not a shape main writes: it is design.md section 3's name,
        kept because a wider match only refuses more.
        """
        item = self.item()
        self.queue([{"key": "github:https://github.com/o/r/pull/3", "url": "u", "why": ["author"]}])
        self.assertEqual(self.refused(self.plan(item), "I10"), [])
        entries = {
            "by item only": {"key": "github:https://github.com/o/r/pull/4", "item": item},
            "by the key only": {"key": f"item:{item}"},
            "as item_id, not written by main": {"key": "github:https://github.com/o/r/pull/2", "item_id": item},
            "as a dependency": {"key": "item:99999", "item": 99999, "depends_on": [{"kind": "item", "item": item}]},
        }
        for name, entry in entries.items():
            with self.subTest(entry=name):
                self.queue([entry])
                self.assertEqual([r["table"] for r in self.refused(self.plan(item), "I10")], ["state"])

    def test_i11_malformed_fields_or_queue_refuse_without_raising(self):
        item, other = self.item(), self.item(title="other")
        self.db.execute("UPDATE item SET fields='{not json' WHERE id=?", (item,))
        self.assertEqual([r["key"] for r in self.refused(self.plan(item), "I11")], [item])
        self.db.execute("UPDATE item SET fields=NULL WHERE id=?", (item,))
        self.assertEqual(self.refused(self.plan(item), "I11"), [])
        self.db.execute("UPDATE item SET fields='{not json' WHERE id=?", (other,))
        self.assertEqual([r["key"] for r in self.refused(self.plan(item), "I11")], [other])
        self.db.execute("UPDATE item SET fields=NULL WHERE id=?", (other,))
        resolve_state(self.db, record_state(self.db, "checkpoint", key=QUEUE, body="{not json"))
        self.assertEqual([r["table"] for r in self.refused(self.plan(item), "I11")], ["state"])

    def test_i11_a_queue_of_the_wrong_shape_is_not_called_invalid_json(self):
        item = self.item()
        for body in ('{"pending": 5}', '{"pending": [5]}'):
            with self.subTest(body=body):
                self.db.execute("DELETE FROM state WHERE key=?", (QUEUE,))
                resolve_state(self.db, record_state(self.db, "checkpoint", key=QUEUE, body=body))
                [refusal] = self.refused(self.plan(item), "I11")
                self.assertNotIn("not valid JSON", refusal["message"])
                self.assertIn("pending", refusal["message"])

    def test_i11_a_bytes_value_in_a_planned_row_refuses_without_raising(self):
        item = self.item()
        note = add_note(self.db, item, "decision", "went fine", session="alex")
        self.db.execute("UPDATE note SET body=? WHERE id=?", (b"\x00\xff", note))
        plan = self.plan(item)
        self.assertEqual([(r["table"], r["key"]) for r in self.refused(plan, "I11")], [("note", note)])
        self.assertRegex(plan["fingerprint"], r"^[0-9a-f]{64}$")


class RunsAndClones(Store):
    """I6: runs, leases and retained clones (D4, option a)."""

    def setUp(self):
        super().setUp()
        self.source = self.repo()
        self.item_id = self.item(repo=self.source)
        self.work = self.assignment(self.item_id)
        self.newer()

    def i6(self):
        return self.refused(self.plan(self.item_id), "I6")

    def test_an_unreleased_run_and_an_open_lease(self):
        run = self.attempt(self.work, self.source, released=False)
        self.assertEqual([r["key"] for r in self.i6()], [run])
        self.db.execute("UPDATE runner_run SET released_at=?", (STAMP,))
        self.assertEqual(self.i6(), [])
        self.lease(run, self.source, released=False)
        self.assertEqual([(r["table"], r["key"]) for r in self.i6()], [("runner_lease", run)])

    def test_leases_are_in_run_order(self):
        runs = sorted((self.attempt(self.work, self.source, number=number) for number in (1, 2, 3)), reverse=True)
        for run in runs:
            self.lease(run, self.source)
        plan = self.plan(self.item_id)
        self.assertEqual([entry["row"]["run"] for entry in plan["rows"] if entry["table"] == "runner_lease"],
                         sorted(runs))

    def remedy(self, assignment=None):
        """sd:1793: the runner's verb, not a raw `chflags -R nouchg` and `rm -rf`."""
        return f"runner.sh retained-remove --clone-only --assignment {assignment or self.work} --who NAME"

    def test_a_retained_clone_prints_the_retained_remove_command(self):
        run = self.attempt(self.work, self.source, clone=True)
        clone = str(self.retained / str(self.work) / "1" / "clone")
        refusal, = self.i6()
        self.assertEqual(refusal["commands"], [self.remedy()])
        message = refusal["message"]
        self.assertLess(message.index(clone), message.index(self.remedy()))
        self.assertIn("with your name for NAME", message)
        # The scope of the pair it replaces: the clone goes, the preserved outputs stay.
        self.assertIn(f"removes only the retained clones of assignment {self.work}"
                      " and keeps kept.tar, archives and ignored outputs", message)
        self.assertNotIn("chflags", message)
        self.assertNotIn("rm -rf", message)
        self.assertEqual(refusal["key"], run)
        shutil.rmtree(clone)
        self.assertEqual(self.i6(), [])

    def test_a_clone_path_with_a_space_prints_the_same_command(self):
        """The path is no longer on the command line, so no quoting can split it."""
        root = self.root / "re tained"; root.mkdir()
        self.attempt(self.work, self.source, clone=True, root=root)
        clone = str(root / str(self.work) / "1" / "clone")
        refusal, = self.i6()
        self.assertEqual([shlex.split(command) for command in refusal["commands"]],
                         [["runner.sh", "retained-remove", "--clone-only", "--assignment", str(self.work),
                           "--who", "NAME"]])
        self.assertIn(clone, refusal["message"])

    def test_an_interrupted_runner_prune_refuses_until_its_leftover_is_gone(self):
        """sd:770: a prune renames the clone to `.pruning-clone` first; what it leaves still refuses."""
        run = self.attempt(self.work, self.source)
        leftover = self.retained / str(self.work) / "1" / ".pruning-clone"
        self.assertEqual(self.i6(), [])
        leftover.mkdir(parents=True)
        refusal, = self.i6()
        self.assertEqual((refusal["key"], refusal["commands"]), (run, [self.remedy()]))
        self.assertIn("an interrupted runner prune left part of a retained clone", refusal["message"])
        self.assertNotIn("retained clone still on disk", refusal["message"])
        leftover.rmdir()
        self.assertEqual(self.i6(), [])

    def test_a_clone_and_a_leftover_print_one_command(self):
        """retained-remove finishes the leftover and removes the clone in one run."""
        self.attempt(self.work, self.source, clone=True)
        clone = self.retained / str(self.work) / "1" / "clone"
        leftover = clone.parent / ".pruning-clone"; leftover.mkdir()
        refusal, = self.i6()
        self.assertEqual(refusal["commands"], [self.remedy()])
        self.assertIn(str(clone), refusal["message"])
        self.assertIn(str(leftover), refusal["message"])

    def test_a_linked_leftover_has_an_unexpected_shape(self):
        run = self.attempt(self.work, self.source)
        elsewhere = self.root / "elsewhere"; elsewhere.mkdir()
        run_directory = self.retained / str(self.work) / "1"; run_directory.mkdir(parents=True)
        (run_directory / ".pruning-clone").symlink_to(elsewhere)
        refusal, = self.i6()
        self.assertEqual((refusal["key"], refusal["commands"]), (run, []))
        self.assertIn("retained path has an unexpected shape", refusal["message"])

    def test_a_path_of_another_assignment_has_an_unexpected_shape(self):
        other = self.retained / "999" / "1" / "clone"; other.mkdir(parents=True)
        self.attempt(self.work, self.source, retained_path=str(other))
        refusal, = self.i6()
        self.assertIn("retained path has an unexpected shape", refusal["message"])
        self.assertEqual(refusal["commands"], [])

    def test_a_symlink_component_has_an_unexpected_shape(self):
        elsewhere = self.root / "elsewhere"
        (elsewhere / "1" / "clone").mkdir(parents=True)
        (self.retained / str(self.work)).symlink_to(elsewhere)
        self.attempt(self.work, self.source)
        refusal, = self.i6()
        self.assertIn("retained path has an unexpected shape", refusal["message"])
        self.assertEqual(refusal["commands"], [])

    def test_a_symlink_inside_the_root_has_an_unexpected_shape(self):
        """The symlink check alone: the clone resolves inside the root, so containment passes."""
        inside = self.retained / "inside"
        (inside / "1" / "clone").mkdir(parents=True)
        (self.retained / str(self.work)).symlink_to(inside)
        run = self.attempt(self.work, self.source)
        path = self.retained / str(self.work) / "1" / "clone"
        self.assertTrue(path.resolve().is_relative_to(self.retained.resolve()))
        refusal, = self.i6()
        self.assertEqual((refusal["key"], refusal["commands"]), (run, []))
        self.assertIn("retained path has an unexpected shape", refusal["message"])

    def test_resolving_outside_the_root_refuses_without_the_symlink_check(self):
        """A symlink the component check misses (a race between the checks) still fails containment."""
        elsewhere = self.root / "elsewhere"
        (elsewhere / "1" / "clone").mkdir(parents=True)
        (self.retained / str(self.work)).symlink_to(elsewhere)
        self.attempt(self.work, self.source)
        with mock.patch.object(Path, "is_symlink", return_value=False):
            refusal, = self.i6()
        self.assertIn("retained path has an unexpected shape", refusal["message"])
        self.assertEqual(refusal["commands"], [])

    def test_an_absent_retention_root_is_an_unmounted_volume(self):
        root = self.root / "Volumes" / "sd-work" / "retained"
        self.attempt(self.work, self.source, root=root)
        refusal, = self.i6()
        self.assertIn("retained volume is not mounted", refusal["message"])
        self.assertIn(str(root), refusal["message"])
        root.mkdir(parents=True)
        self.assertEqual(self.i6(), [])


class RepoRefusals(Store):

    def setUp(self):
        super().setUp()
        self.source = self.repo()

    def test_p1_no_such_repo(self):
        plan = self.plan_repo(str(self.root / "nowhere"))
        self.assertEqual([r["code"] for r in plan["refusals"]], ["P1"])
        self.assertEqual(plan["rows"], [])

    def test_p2_a_retiring_source(self):
        for column in ("status_source", "pieces_source"):
            with self.subTest(column=column):
                self.db.execute(f"UPDATE repo SET status_source='file', pieces_source='file', {column}='retiring'")
                self.assertEqual([r["message"] for r in self.refused(self.plan_repo(self.source), "P2")],
                                 [f"repo {self.source} has {column} retiring"])

    def test_p3_an_item_without_with_items(self):
        item = self.item(repo=self.source)
        self.assertEqual([r["key"] for r in self.refused(self.plan_repo(self.source, with_items=False), "P3")],
                         [item])
        plan = self.plan_repo(self.source, with_items=True)
        self.assertEqual(self.refused(plan, "P3"), [])
        self.assertIn(("item", item), self.keys(plan))

    def test_a_repo_with_no_items_lists_itself_and_its_protection(self):
        self.db.execute("INSERT INTO repo_protection (repo, observed_at, status) VALUES (?, ?, 'unknown')",
                        (self.source, STAMP))
        plan = self.plan_repo(self.source, with_items=False)
        self.assertEqual(self.keys(plan), [("repo", self.source), ("repo_protection", self.source)])
        self.assertEqual(plan["refusals"], [])

    def test_p4_a_clone_on_the_repo_prints_its_commands(self):
        item = self.item(repo=self.source)
        work = self.assignment(item)
        self.newer()
        run = self.attempt(work, self.source, clone=True)
        clone = str(self.retained / str(work) / "1" / "clone")
        refusal, = self.refused(self.plan_repo(self.source), "P4")
        self.assertEqual((refusal["key"], refusal["commands"]),
                         (run, [f"runner.sh retained-remove --clone-only --assignment {work} --who NAME"]))
        self.assertIn(clone, refusal["message"])

    def test_p4_a_path_of_the_wrong_shape_prints_nothing(self):
        work = self.assignment(self.item(repo=self.source))
        self.newer()
        other = self.retained / "999" / "1" / "clone"; other.mkdir(parents=True)
        self.attempt(work, self.source, retained_path=str(other))
        refusal, = self.refused(self.plan_repo(self.source), "P4")
        self.assertIn("retained path has an unexpected shape", refusal["message"])
        self.assertEqual(refusal["commands"], [])

    def test_p4_an_absent_retention_root(self):
        work = self.assignment(self.item(repo=self.source))
        self.newer()
        self.attempt(work, self.source, root=self.root / "unmounted")
        refusal, = self.refused(self.plan_repo(self.source), "P4")
        self.assertIn("retained volume is not mounted", refusal["message"])

    def test_p4_an_unreleased_run_and_an_open_lease_on_the_repo(self):
        work = self.assignment(self.item(repo=self.source))
        self.newer()
        run = self.attempt(work, self.source, released=False)
        self.lease(run, self.source, released=False)
        self.assertEqual(sorted(r["table"] for r in self.refused(self.plan_repo(self.source), "P4")),
                         ["runner_lease", "runner_run"])

    def test_p4_a_lease_on_the_repo_whose_run_is_on_another_repo(self):
        other = self.repo("other")
        run = self.attempt(self.assignment(self.item(repo=other, title="elsewhere")), other)
        self.newer()
        self.lease(run, self.source)
        plan = self.plan_repo(self.source)
        self.assertIn(("runner_lease", run), self.keys(plan))
        self.assertEqual(self.refused(plan, "P4"), [])
        self.db.execute("UPDATE runner_lease SET released_at=NULL WHERE run=?", (run,))
        self.assertEqual([(r["table"], r["key"]) for r in self.refused(self.plan_repo(self.source), "P4")],
                         [("runner_lease", run)])

    def test_p5_a_conf_that_cannot_be_read_refuses_without_raising(self):
        directory = self.root / "conf-directory"; directory.mkdir()
        undecodable = self.root / "undecodable.conf"; undecodable.write_bytes(b"\xff\xfe platypeeps\n")
        environ = {key: value for key, value in os.environ.items() if key != "SD_REPO_ROOT"}
        for conf in (directory, undecodable):
            with self.subTest(conf=conf.name), mock.patch("sd_db.repos.conf_path", return_value=conf), \
                    mock.patch.dict(os.environ, environ, clear=True):
                self.assertEqual([r["table"] for r in self.refused(self.plan_repo(self.source), "P5")], ["repo"])

    def test_p5_a_present_checkout_in_the_conf(self):
        conf = self.root / "repos.personal.conf"
        conf.write_text("platypeeps platypeeps/source\n")
        checkout = self.home / "repos" / "platypeeps" / "source"
        upsert_repo(self.db, str(checkout))
        environ = {key: value for key, value in os.environ.items() if key != "SD_REPO_ROOT"}
        with mock.patch("sd_db.repos.conf_path", return_value=conf), mock.patch.dict(os.environ, environ, clear=True):
            self.assertEqual(self.refused(self.plan_repo(str(checkout)), "P5"), [])
            (checkout / ".git").mkdir(parents=True)
            self.assertEqual(len(self.refused(self.plan_repo(str(checkout)), "P5")), 1)
            self.assertEqual(self.refused(self.plan_repo(self.source), "P5"), [])

    def test_p6_a_run_whose_item_is_not_in_the_plan(self):
        work = self.assignment(self.item(repo=None))
        self.newer()
        run = self.attempt(work, self.source)
        self.assertEqual([r["key"] for r in self.refused(self.plan_repo(self.source), "P6")], [run])

    def test_a1_over_the_merged_plan(self):
        for number in range(2):
            self.assignment(self.item(repo=self.source))
        plan = self.plan_repo(self.source)
        self.assertEqual(len(self.refused(plan, "A1")), 1)
        self.newer()
        self.assertEqual(self.refused(self.plan_repo(self.source), "A1"), [])

    def test_g6_a_repo_remove_record_for_this_fingerprint(self):
        fingerprint = self.plan_repo(self.source)["fingerprint"]
        reporting.ingest(self.db, job="repo-remove", run_id=fingerprint, started=STAMP, ended=STAMP,
                         exit_code=0, text="remove\n", source_path="test", record="repo-remove")
        self.assertEqual(len(self.refused(self.plan_repo(self.source), "G6")), 1)

    def test_one_refused_item_refuses_the_repo(self):
        item = self.item(repo=self.source)
        add_note(self.db, item, "followup", "open", session="alex")
        self.assertEqual(len(self.refused(self.plan_repo(self.source), "I7")), 1)


class MovedItemRuns(Store):
    """sd:2581: a run of an item moved to another repo no longer holds the old repo's remove.

    `sd task edit N --belongs-to` moves the item and leaves its finished runs
    naming the old repo. The remove detaches each one (migration 018): the row
    stays with its item and `repo` goes NULL. Its journal is left as it was,
    naming the repo, and the record names it too.
    """

    def setUp(self):
        super().setUp()
        self.source = self.repo()
        self.target = self.repo("target")
        self.moved = self.item(repo=self.target, title="moved away")
        self.work = self.assignment(self.moved)
        self.newer()
        self.run = self.attempt(self.work, self.source)
        self.lease(self.run, self.source)

    def test_the_preview_detaches_the_run_instead_of_refusing(self):
        plan = self.plan_repo(self.source)
        self.assertEqual(plan["refusals"], [])
        self.assertEqual(plan["detach"], [{"run": self.run, "repo": self.source, "item": self.moved,
                                           "item_repo": self.target}])
        self.assertNotIn(("runner_run", self.run), self.keys(plan))
        # Its released lease names the repo, so it goes with it, as P4 has it.
        self.assertIn(("runner_lease", self.run), self.keys(plan))

    def test_a_detached_run_is_still_held_to_p4(self):
        self.db.execute("UPDATE runner_run SET released_at=NULL WHERE id=?", (self.run,))
        self.assertEqual([r["key"] for r in self.refused(self.plan_repo(self.source), "P4")],
                         [self.run])

    def test_the_apply_keeps_the_run_with_its_item_and_leaves_its_journal(self):
        journal = self.journal([self.run])
        before = (journal / f"{self.run}.json").read_bytes()
        version = self.db.execute("SELECT journal_version FROM runner_run WHERE id=?", (self.run,)).fetchone()[0]
        fingerprint = self.plan_repo(self.source)["fingerprint"]
        result = self.apply("repo", self.source, fingerprint)
        self.assertEqual(result["detached"], [self.run])
        row = self.db.execute("SELECT * FROM runner_run WHERE id=?", (self.run,)).fetchone()
        self.assertEqual((row["repo"], row["assignment"], row["journal_version"]), (None, self.work, version))
        self.assertIsNone(self.db.execute("SELECT 1 FROM repo WHERE path=?", (self.source,)).fetchone())
        self.assertEqual(list(self.db.execute("PRAGMA foreign_key_check")), [])
        # The journal keeps the repository as provenance; the remove never writes it.
        self.assertEqual((journal / f"{self.run}.json").read_bytes(), before)
        _, _, text = self.record(result["record"])
        self.assertIn(f"detached:\nrunner_run {self.run} repo {self.source}\n", text)
        # The nightly backup checks every row against its journal: it must pass.
        removal.backups.run(home=self.home, database=self.store / "sd.db", keep=None)

    def two_detached(self):
        """A second released run of the moved item, and both runs' journals as bytes."""
        second = self.attempt(self.work, self.source, number=2)
        self.lease(second, self.source)
        journal = self.journal([self.run, second])
        return [self.run, second], {run: (journal / f"{run}.json").read_bytes() for run in (self.run, second)}

    def test_a_journal_write_that_fails_on_the_second_run_leaves_every_journal_as_it_was(self):
        """Review on sd:2581: a file write cannot roll back with the transaction, so the remove makes none."""
        runs, before = self.two_detached()
        fingerprint = self.plan_repo(self.source)["fingerprint"]
        real = runner_journal.persist
        calls = []

        def second_fails(database, record):
            calls.append(record["id"])
            if len(calls) == 2:
                raise OSError("disk full on the second journal")
            return real(database, record)
        with mock.patch.object(runner_journal, "persist", side_effect=second_fails):
            try:
                self.apply("repo", self.source, fingerprint)
            except (OSError, removal.RemovalRefused):
                pass
        after = {run: (self.store / "runner-journal" / f"{run}.json").read_bytes() for run in runs}
        self.assertEqual(after, before)
        repos = {row["repo"] for row in self.db.execute("SELECT repo FROM runner_run WHERE id IN (?, ?)", runs)}
        self.assertEqual(len(repos), 1)  # the rows moved together, or not at all

    def test_a_removal_that_rolls_back_leaves_the_rows_and_the_journals(self):
        runs, before = self.two_detached()
        fingerprint = self.plan_repo(self.source)["fingerprint"]
        with mock.patch.object(removal, "_delete", side_effect=removal.RemovalRefused("stopped after the detach")), \
                self.assertRaisesRegex(removal.RemovalRefused, "stopped after the detach"):
            self.apply("repo", self.source, fingerprint)
        self.assertEqual([row["repo"] for row in self.db.execute("SELECT repo FROM runner_run WHERE id IN (?, ?)", runs)],
                         [self.source, self.source])
        self.assertEqual({run: (self.store / "runner-journal" / f"{run}.json").read_bytes() for run in runs}, before)

    def test_the_removals_backup_restores_against_the_live_journal(self):
        """Review on sd:2581: restoring the pre-removal backup must not meet a journal that says NULL."""
        journal = self.journal([self.run])
        fingerprint = self.plan_repo(self.source)["fingerprint"]
        result = self.apply("repo", self.source, fingerprint)
        other = self.root / "other-home"
        state = other / ".local/share/sd"
        state.mkdir(parents=True)
        shutil.copytree(journal, state / "runner-journal")  # the live journal after the removal
        restored = backup.restore(Path(result["backup"]), home=other)
        connection = sd_db.connect(restored); self.addCleanup(connection.close)
        row = connection.execute("SELECT repo FROM runner_run WHERE id=?", (self.run,)).fetchone()
        self.assertEqual(row["repo"], self.source)
        backup._check_runner_records(connection, state)


class TheProbe(Store):
    """sd:744's rows, rebuilt from `design.md` section 7, and the files beside them."""

    def setUp(self):
        super().setUp()
        self.older = self.item(title="task38-queue-only")
        self.first = self.assignment(self.older)
        self.source = self.repo("provisioning-probe")
        self.probe = self.item(repo=self.source, title="runner provisioning probe")
        for kind in ("decision", "exec", "decision", "exec", "decision"):
            if kind == "exec":
                add_note(self.db, self.probe, kind, "{}", session="runner", started=STAMP, ended=STAMP, exit_code=0)
            else:
                add_note(self.db, self.probe, kind, "went fine", session="alex")
        self.db.execute("INSERT INTO note (item, timestamp, kind, body) VALUES (?, ?, 'status_change',"
                        " 'in_progress -> done by alex')", (self.probe, STAMP))
        self.works = [self.assignment(self.probe), self.assignment(self.probe)]
        self.runs = []
        for work in self.works:
            run = self.attempt(work, self.source, clone=True)
            self.lease(run, self.source)
            self.runs.append(run)

    def clear_clones(self):
        for work in self.works:
            shutil.rmtree(self.retained / str(work) / "1" / "clone")

    def test_fifteen_rows_and_the_three_refusals_of_2026_09_10(self):
        plan = self.plan_repo(self.source)
        self.assertEqual(plan["counts"], {"repo": 1, "item": 1, "note": 7, "assignment": 2, "runner_run": 2,
                                          "runner_lease": 2})
        self.assertEqual(len(plan["rows"]), 15)
        self.assertEqual(sorted({r["code"] for r in plan["refusals"]}), ["A1", "I6", "P4"])
        p4 = self.refused(plan, "P4")
        self.assertEqual(len(p4), 2)
        for refusal, work in zip(p4, self.works):
            clone = str(self.retained / str(work) / "1" / "clone")
            self.assertIn(clone, refusal["message"])
            self.assertEqual(refusal["commands"],
                             [f"runner.sh retained-remove --clone-only --assignment {work} --who NAME"])
        self.clear_clones()
        plan = self.plan_repo(self.source)
        self.assertEqual(self.refused(plan, "P4"), [])
        self.assertEqual([r["code"] for r in plan["refusals"]], ["A1"])
        self.newer()
        self.assertEqual(self.plan_repo(self.source)["refusals"], [])

    def test_journal_files_are_listed_to_move_and_left_unchanged(self):
        self.clear_clones()
        self.newer()
        journal = self.store / "runner-journal"; journal.mkdir()
        files = []
        for run in self.runs:
            for suffix in (".json", ".lock"):
                path = journal / f"{run}{suffix}"
                path.write_text(json.dumps({"record": {"id": run}}) if suffix == ".json" else "")
                files.append(path)
        (self.retained / str(self.works[0]) / "1" / "retention.json").write_text("{}")
        receipts = self.store / "runner-reconciliation"; receipts.mkdir()
        receipt = receipts / f"{'c' * 64}.json"
        receipt.write_text(json.dumps({"run": self.runs[0], "operation": "database-from-journal"}))
        (receipts / f"{'d' * 64}.json").write_text(json.dumps({"run": uuid.uuid4().hex}))
        before = {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in files}
        plan = self.plan_repo(self.source)
        self.assertEqual(plan["refusals"], [])
        self.assertEqual(plan["move"], {"files": [str(path) for path in files], "to": str(
            self.store / "runner-recovery-evidence" / f"removed-{plan['fingerprint']}")})
        self.assertEqual(before, {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                                  for path in files})
        self.assertIn(f"file {self.retained / str(self.works[0]) / '1' / 'retention.json'}", plan["left"])
        self.assertIn(f"file {receipt}", plan["left"])
        self.assertEqual([line for line in plan["left"] if "runner-reconciliation" in line], [f"file {receipt}"])

    def test_no_journal_directory_lists_nothing_to_move(self):
        self.clear_clones()
        self.newer()
        plan = self.plan_repo(self.source)
        self.assertEqual(plan["move"], {"files": [], "to": None})

    def test_state_cost_and_completion_references_are_left(self):
        self.clear_clones()
        self.newer()
        contribution = record_state(self.db, "checkpoint", key=f"contribution:item:{self.probe}", body={})
        ship = record_state(self.db, "checkpoint", key="ship:" + "e" * 64, body={"item": self.probe})
        record_state(self.db, "checkpoint", key="ship:" + "f" * 64, body={"item": self.first})
        delivery = record_state(self.db, "checkpoint", key=f"runner-delivery:{self.runs[0]}", body={})
        completes = self.item(title="completes", fields={"completion": {"item": self.probe}})
        cost = self.db.execute("INSERT INTO cost (timestamp, repo, source) VALUES (?, ?, 'run')",
                               (STAMP, self.source)).lastrowid
        left = self.plan_repo(self.source)["left"]
        for line in (f"reference state {contribution} contribution:item:{self.probe}",
                     f"reference state {ship} ship:{'e' * 64}",
                     f"reference item {completes} fields.completion item {self.probe}",
                     f"reference state {delivery} runner-delivery:{self.runs[0]}",
                     f"reference cost {cost} repo"):
            with self.subTest(line=line):
                self.assertIn(line, left)
        self.assertEqual([line for line in left if " ship:" in line], [f"reference state {ship} ship:{'e' * 64}"])

    def test_palette_scopes_are_left_as_references(self):
        self.clear_clones()
        self.newer()
        scope = "palette:" + json.dumps({"version": 1, "item": self.probe, "repo": self.source}, sort_keys=True)
        self.db.execute("UPDATE assignment SET scope=? WHERE id IN (?, ?)", (scope, *self.works))
        self.db.execute("UPDATE assignment SET scope='palette:{not json' WHERE id=?", (self.first,))
        left = self.plan_repo(self.source)["left"]
        self.assertIn(f"reference assignment.scope item {self.probe}", left)
        self.assertIn(f"reference assignment.scope repo {self.source}", left)
        left = self.plan(self.probe)["left"]
        self.assertEqual([line for line in left if "assignment.scope" in line],
                         [f"reference assignment.scope item {self.probe}"])


class ImportedSources(Store):
    """The warning's source set is the importers' own (the #350 review, N-1)."""

    def test_imported_is_every_source_module_and_the_report_writer(self):
        import importlib
        import sd_db.sources
        declared = set()
        for module_file in sorted(Path(sd_db.sources.__file__).parent.glob("*.py")):
            if module_file.name == "__init__.py":
                continue
            module = importlib.import_module(f"sd_db.sources.{module_file.stem}")
            if hasattr(module, "Reader"):
                # A sixth importer without a `SOURCE` constant fails here, by name.
                self.assertTrue(hasattr(module, "SOURCE"), f"{module_file.name} has a Reader and no SOURCE")
                declared.add(module.SOURCE)
        state = reporting.ingest(self.db, job="probe", run_id="r1", started=STAMP, ended=STAMP, exit_code=1,
                                 text="failed\n", source_path="test")
        filed = self.db.execute("SELECT source FROM item WHERE source IS NOT NULL AND external_id='probe:r1'").fetchone()
        self.assertIsNotNone(filed, state)
        self.assertEqual(set(removal.IMPORTED), declared | {filed["source"]})
        self.assertEqual(len(set(removal.IMPORTED)), len(removal.IMPORTED))

    def test_an_item_from_each_importer_is_reported_and_one_from_drafts_is_not(self):
        for source in ("docs/work", "vault", "register", "github-issues", "index.sqlite", "cron-report"):
            with self.subTest(source=source):
                item = self.item(source=source, external_id=f"probe:{source}")
                self.assertEqual(self.plan(item)["warnings"],
                                 [f"item {item} was imported from {source} as probe:{source}; "
                                  f"the next import can bring it back"])
        for source, external in (("drafts", "probe:drafts"), ("register", None), (None, "probe:none")):
            with self.subTest(source=source, external=external):
                self.assertEqual(self.plan(self.item(source=source, external_id=external))["warnings"], [])


class UnknownChildTables(Store):
    """G7: a foreign key into one of the four parents the plan has no rule for (the #350 review, N-4)."""

    def test_a_store_on_the_current_schema_has_no_unknown_child(self):
        item = self.item(repo=self.repo())
        self.assertEqual(self.refused(self.plan(item), "G7"), [])
        self.assertEqual(self.refused(self.plan_repo(self.repo()), "G7"), [])

    def test_a_probe_cascade_child_of_item_is_refused_by_name_on_both_verbs(self):
        source = self.repo()
        item = self.item(repo=source)
        self.db.execute("CREATE TABLE fixture_child (id INTEGER PRIMARY KEY, owner INTEGER REFERENCES item(id) ON DELETE CASCADE)")
        for plan in (self.plan(item), self.plan_repo(source)):
            with self.subTest(kind=plan["kind"]):
                refusal, = self.refused(plan, "G7")
                self.assertEqual((refusal["table"], refusal["key"]), ("fixture_child", "owner"))
                self.assertIn("fixture_child.owner references item", refusal["message"])
                self.assertIn("REFERENCES", refusal["message"])

    def test_a_probe_child_of_repo_is_refused_and_a_grandchild_is_not(self):
        source = self.repo()
        item = self.item(repo=source)
        self.db.execute("CREATE TABLE fixture_repo_child (checkout TEXT REFERENCES repo)")
        self.db.execute("CREATE TABLE fixture_grandchild (note INTEGER REFERENCES note(id) ON DELETE CASCADE)")
        refusal, = self.refused(self.plan_repo(source), "G7")
        self.assertEqual((refusal["table"], refusal["key"]), ("fixture_repo_child", "checkout"))
        self.assertIn("fixture_repo_child.checkout references repo", refusal["message"])
        self.assertEqual([r["table"] for r in self.refused(self.plan(item), "G7")], ["fixture_repo_child"])

    def test_a_probe_cascade_child_of_assignment_or_runner_run_is_refused_by_name_on_both_verbs(self):
        # The plans delete assignment and runner_run rows too, so a child of
        # either is taken from underneath just like a child of item (sd:956, 1).
        source = self.repo()
        item = self.item(repo=source)
        self.db.execute("CREATE TABLE fixture_assignment_child (owner INTEGER REFERENCES assignment(id) ON DELETE CASCADE)")
        self.db.execute("CREATE TABLE fixture_run_child (run TEXT REFERENCES runner_run(id) ON DELETE CASCADE)")
        for plan in (self.plan(item), self.plan_repo(source)):
            with self.subTest(kind=plan["kind"]):
                refusals = self.refused(plan, "G7")
                self.assertEqual([(r["table"], r["key"]) for r in refusals],
                                 [("fixture_assignment_child", "owner"), ("fixture_run_child", "run")])
                self.assertIn("fixture_assignment_child.owner references assignment(id)", refusals[0]["message"])
                self.assertIn("fixture_run_child.run references runner_run(id)", refusals[1]["message"])

    def test_a_parent_declared_in_upper_case_is_the_same_parent(self):
        # SQLite identifiers are case-insensitive; `pragma_foreign_key_list`
        # reports the parent as declared (sd:956, 2).
        source = self.repo()
        item = self.item(repo=source)
        self.db.execute('CREATE TABLE fixture_child (owner INTEGER REFERENCES "ITEM"(id) ON DELETE CASCADE)')
        self.assertEqual(self.db.execute("SELECT count(*) FROM pragma_foreign_key_list('fixture_child')"
                                         " WHERE \"table\"='ITEM'").fetchone()[0], 1)
        for plan in (self.plan(item), self.plan_repo(source)):
            with self.subTest(kind=plan["kind"]):
                refusal, = self.refused(plan, "G7")
                self.assertEqual((refusal["table"], refusal["key"]), ("fixture_child", "owner"))

    def test_the_refusal_is_in_the_fingerprint(self):
        item = self.item()
        before = self.plan(item)["fingerprint"]
        self.db.execute("CREATE TABLE fixture_child (owner INTEGER REFERENCES item(id))")
        self.assertNotEqual(self.plan(item)["fingerprint"], before)


class QuotedTextIsCapped(Store):
    """A refusal quotes at most 200 characters of stored text (the #350 review, N-5)."""

    def setUp(self):
        super().setUp()
        self.source = self.repo()
        self.item_id = self.item(repo=self.source)
        self.work = self.assignment(self.item_id)
        self.newer()

    def test_the_helper_keeps_200_characters_and_marks_the_cut(self):
        self.assertEqual(removal._quoted("x" * 200), "x" * 200)
        self.assertEqual(removal._quoted("x" * 201), "x" * 200 + "...")
        self.assertEqual(removal._quoted(Path("/y")), "/y")
        self.assertEqual(removal.QUOTED, 200)

    def test_a_100_001_character_retained_path_gives_a_short_refusal_that_names_the_run(self):
        tail = f"/{self.work}/2/clone"
        # One path of no shape at all, and one of the right shape under a root
        # that is not mounted: both messages quote the path.
        for number, retained_path in enumerate(("/" + "a" * 100_000, "/" + "a" * (100_000 - len(tail)) + tail), 1):
            with self.subTest(shape=retained_path[-6:]):
                run = self.attempt(self.work, self.source, number=number, retained_path=retained_path)
                self.assertEqual(len(retained_path), 100_001)
                plan = self.plan(self.item_id)
                self.db.execute("DELETE FROM runner_run WHERE id=?", (run,))
                refusal, = self.refused(plan, "I6")
                self.assertEqual(refusal["key"], run)
                self.assertLess(len(refusal["message"]), 1_000)
                self.assertIn(retained_path[:200] + "...", refusal["message"])

    def test_a_100_001_character_repo_path_gives_short_p1_p2_and_p3_refusals(self):
        # `repo.path` is unconstrained TEXT (sd:956, 3). `key` keeps the full
        # path, as `key` does for every refusal; the message quotes 200.
        path = "/" + "r" * 100_000
        self.assertEqual(len(path), 100_001)
        upsert_repo(self.db, path, status_source="retiring", pieces_source="file")
        item = self.item(repo=path)
        self.newer()
        plans = {"P1": self.plan_repo(path + "/absent"), "P2": self.plan_repo(path),
                 "P3": self.plan_repo(path, with_items=False)}
        for code, plan in plans.items():
            with self.subTest(code=code):
                refusal, = self.refused(plan, code)
                self.assertEqual(refusal["key"], item if code == "P3" else path if code == "P2" else path + "/absent")
                self.assertLess(len(refusal["message"]), 1_000)
                self.assertIn(path[:200] + "...", refusal["message"])
                self.assertEqual(refusal["commands"], [])
        self.assertEqual([r["message"] for r in self.refused(plans["P2"], "P2")],
                         [f"repo {path[:200]}... has status_source retiring"])

    def test_a_repo_row_too_large_for_a_note_gives_a_short_r1_refusal(self):
        # The #407 review: R1 named the row by its whole key, so a long
        # `repo.path` landed whole in the message. `key` keeps it whole.
        path = "/" + "r" * 100_000
        upsert_repo(self.db, path)
        with mock.patch.object(removal, "MAX_REPORT", 50_000):
            plan = self.plan_repo(path)
        refusal, = [r for r in self.refused(plan, "R1") if r["table"] == "repo"]
        self.assertEqual(refusal["key"], path)
        self.assertLess(len(refusal["message"]), 1_000)
        self.assertIn(f"repo row {path[:200]}... is ", refusal["message"])

    def test_other_stored_text_in_refusals_is_capped_too(self):
        long = "/".join(["p" * 100] * 5)
        item = self.item(repo=self.source, path=f"docs/work/{long}/prd.md")
        self.db.execute("UPDATE item SET piece=? WHERE id=?", ("p" * 500, item))
        Path(self.source, "docs/work", long).mkdir(parents=True)
        Path(self.source, "docs/work", long, "prd.md").write_text("still here")
        messages = [r["message"] for r in self.refused(self.plan(item), "I8")]
        self.assertEqual(len(messages), 2)
        for message in messages:
            self.assertLess(len(message), 400)
            self.assertIn("...", message)
            self.assertNotIn("p" * 201, message)
        self.assertIn(f"is writing piece {'p' * 200}...", messages[1])
        record_state(self.db, "checkpoint", key=QUEUE, body={"pending": [{"key": "k" * 500, "item": item}]})
        resolve_state(self.db, self.db.execute("SELECT max(id) FROM state").fetchone()[0])
        message, = [r["message"] for r in self.refused(self.plan(item), "I10")]
        self.assertIn("k" * 200 + "...", message)
        self.assertNotIn("k" * 201, message)



class TheRecord(Store):
    """`_record`: the manifest, the chunks and R1-R3 (`design.md` sections 4.1 and 4.2; step 4)."""

    def five_rows(self, width=1):
        """An item, its opening note and three comments: five rows, one line each."""
        item = self.item(title="five rows")
        for text in ("one", "two", "three"):
            add_note(self.db, item, "comment", text * width, session="alex")
        self.newer()
        return self.plan(item)

    @staticmethod
    def parse(body):
        header, _, rest = body.partition("\n")
        return header, rest.split("\n")

    def test_a_note_limit_that_fits_two_comments_gives_two_whole_chunks(self):
        plan = self.five_rows(width=200)
        self.assertEqual(len(plan["rows"]), 5)
        lines = [removal._line(entry) for entry in plan["rows"]]
        sizes = [len(line.encode()) for line in lines]
        # The three comments are the longest rows. A limit that fits two of
        # them takes the item row, its opening note and the first comment as
        # one chunk, and the other two comments as the second: 1 of 2, 2 of 2.
        self.assertGreater(min(sizes[2:]), max(sizes[:2]))
        header = len(f"removed rows 16 of 16, sha256 {'0' * 64}") + 1
        limit = header + max(sizes[3] + 1 + sizes[4], sizes[0] + 1 + sizes[1] + 1 + sizes[2]) + 1
        self.assertGreater(sum(sizes[:4]) + 3 + 1, limit - header)
        _, chunks, refusals = removal._record(plan, who="alex", reason="r", backup="b", note_limit=limit)
        self.assertEqual(refusals, [])
        self.assertEqual(len(chunks), 2)
        for number, (chunk, expected) in enumerate(zip(chunks, (lines[:3], lines[3:])), 1):
            with self.subTest(chunk=number):
                head, rows = self.parse(chunk)
                self.assertEqual(rows, expected)
                self.assertEqual(head, f"removed rows {number} of 2, sha256 "
                                       f"{hashlib.sha256(chr(10).join(rows).encode()).hexdigest()}")
                self.assertLessEqual(len(chunk.encode()), limit)
                for row in rows:
                    json.loads(row)
        self.assertEqual(plan["record"], {"notes": 1})

    def test_a_newline_and_a_non_ascii_character_round_trip(self):
        item = self.item(title="round trip", body={"text": "läuft\nnoch ✓"})
        note = add_note(self.db, item, "comment", "zeile eins\nzeile zwei ü", session="alex")
        self.newer()
        plan = self.plan(item)
        _, chunks, _ = removal._record(plan, who="alex", reason="r", backup="b")
        self.assertEqual(len(chunks), 1)
        _, lines = self.parse(chunks[0])
        rows = [json.loads(line) for line in lines]
        stored = {("item", item): dict(self.db.execute("SELECT * FROM item WHERE id=?", (item,)).fetchone()),
                  ("note", note): dict(self.db.execute("SELECT * FROM note WHERE id=?", (note,)).fetchone())}
        for row in rows:
            key = (row["table"], row["row"]["id"])
            if key in stored:
                with self.subTest(table=row["table"]):
                    self.assertEqual(row["row"], stored[key])
        self.assertIn("ü", chunks[0])
        self.assertNotIn("\\u00fc", chunks[0])

    def test_r1_a_row_larger_than_a_note(self):
        plan = self.five_rows()
        longest = max(len(removal._line(entry).encode()) for entry in plan["rows"])
        header = len(f"removed rows 16 of 16, sha256 {'0' * 64}") + 1
        _, _, refusals = removal._record(plan, who="s", reason="r", backup="b", note_limit=header + longest - 1)
        self.assertEqual([r["code"] for r in refusals][:1], ["R1"])
        self.assertIn("more than one record note can hold", refusals[0]["message"])
        with mock.patch.object(removal, "MAX_REPORT", header + longest - 1):
            self.assertTrue(self.refused(self.five_rows(), "R1"))

    def test_r2_a_plan_needing_more_notes_than_the_limit(self):
        plan = self.five_rows()
        limit = (len(f"removed rows 16 of 16, sha256 {'0' * 64}") + 1
                 + max(len(removal._line(e).encode()) for e in plan["rows"]))
        # How many chunks the plan needs is read, not assumed: every column a
        # migration adds to `item` lengthens the item row, which is the
        # longest here, and so changes how many short notes share a chunk.
        _, needed, _ = removal._record(plan, who="s", reason="r", backup="b",
                                       note_limit=limit, max_notes=9)
        self.assertGreater(len(needed), 1)
        _, chunks, refusals = removal._record(plan, who="s", reason="r", backup="b",
                                              note_limit=limit, max_notes=len(needed) - 1)
        self.assertEqual(len(chunks), len(needed))
        self.assertEqual([r["code"] for r in refusals], ["R2"])
        self.assertEqual((refusals[0]["table"], refusals[0]["key"]), ("item", plan["target"]))
        with mock.patch.object(removal, "MAX_RECORD_NOTES", 0):
            self.assertTrue(self.refused(self.five_rows(), "R2"))

    def test_r3_a_manifest_over_its_limit(self):
        plan = self.five_rows()
        manifest, _, refusals = removal._record(plan, who="s", reason="r", backup="b")
        self.assertEqual(refusals, [])
        _, _, refusals = removal._record(plan, who="s", reason="r", backup="b",
                                         note_limit=len(manifest.encode()) - 1, max_notes=1000)
        self.assertEqual([r["code"] for r in refusals if r["code"] == "R3"], ["R3"])
        self.assertEqual((refusals[-1]["table"], refusals[-1]["key"]), ("item", plan["target"]))
        # A plan cannot know the actor lines, so it reserves room for them.
        manifest, _, refusals = removal._record(plan, who=None, reason=None, backup=None)
        self.assertEqual(refusals, [])
        self.assertNotIn("who:", manifest)
        _, _, refusals = removal._record(plan, who=None, reason=None, backup=None,
                                         note_limit=len(manifest.encode()) + removal.MANIFEST_HEADROOM - 1,
                                         max_notes=1000)
        self.assertEqual([r["code"] for r in refusals if r["code"] == "R3"], ["R3"])
        with mock.patch.object(removal, "MAX_REPORT", len(manifest.encode()) + removal.MANIFEST_HEADROOM - 1):
            self.assertTrue(self.refused(self.plan(plan["target"]), "R3"))

    def test_the_headroom_holds_a_who_and_a_reason_of_200_non_ascii_characters(self):
        """R3 measures bytes; the headroom reserves four bytes per character of each actor field."""
        plan = self.five_rows()
        preview, _, refusals = removal._record(plan, who=None, reason=None, backup=None)
        self.assertEqual(refusals, [])
        limit = len(preview.encode()) + removal.MANIFEST_HEADROOM
        _, _, refusals = removal._record(plan, who=None, reason=None, backup=None, note_limit=limit, max_notes=1000)
        self.assertEqual(refusals, [])
        who = reason = "\U0001F418" * removal.MAX_ACTOR
        removal.check_actor(who=who, reason=reason, program="p", principal="u", session=None)
        self.assertEqual(len(who.encode()), 4 * removal.MAX_ACTOR)
        manifest, _, refusals = removal._record(plan, who=who, reason=reason, backup="/" + "b" * 4095,
                                                note_limit=limit, max_notes=1000)
        self.assertEqual([r["code"] for r in refusals], [], manifest[:200])
        self.assertLessEqual(len(manifest.encode()), limit)

    def test_the_manifest_lines_are_in_the_stated_order(self):
        plan = self.five_rows()
        manifest, _, _ = removal._record(plan, who="alex", reason="why", backup="/b")
        lines = manifest.split("\n")
        self.assertEqual(lines[:6], [f"remove item {plan['target']}", "reason: why", "who: alex",
                                     f"fingerprint: {plan['fingerprint']}", "backup: /b", "rows: 5 in 1 note(s)"])
        self.assertEqual(lines[6], "removed:")
        self.assertEqual(lines[7:12], [f"{e['table']} {e['row']['id']}" for e in plan["rows"]])
        self.assertEqual(lines[-1], "")


class TheApply(Store):
    """`apply` on the probe fixture (step 5), with real backups of the fixture store."""

    def setUp(self):
        super().setUp()
        self.older = self.item(title="task38-queue-only")
        self.first = self.assignment(self.older)
        self.source = self.repo("provisioning-probe")
        self.probe = self.item(repo=self.source, title="runner provisioning probe")
        for kind in ("decision", "exec", "decision", "exec", "decision"):
            if kind == "exec":
                add_note(self.db, self.probe, kind, "{}", session="runner", started=STAMP, ended=STAMP, exit_code=0)
            else:
                add_note(self.db, self.probe, kind, "went fine", session="alex")
        self.db.execute("INSERT INTO note (item, timestamp, kind, body) VALUES (?, ?, 'status_change',"
                        " 'in_progress -> done by alex')", (self.probe, STAMP))
        self.works = [self.assignment(self.probe), self.assignment(self.probe)]
        self.runs = [self.attempt(work, self.source) for work in self.works]
        for run in self.runs:
            self.lease(run, self.source)
        # The newest assignment sits on the older item, so A1 clears while
        # the probe stays the largest item id (the id-reuse test).
        self.newest = self.assignment(self.older)
        self.journal_dir = self.journal(self.runs)
        self.fingerprint = self.plan_repo(self.source)["fingerprint"]
        self.before = self.counts()

    def snapshots(self):
        root = self.home / "Documents" / "sd-backups"
        return sorted(root.iterdir()) if root.is_dir() else []

    def only_record(self):
        rows = self.db.execute("SELECT id FROM item WHERE source='cron-report'").fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0]["id"]

    def test_the_fifteen_rows_go_and_one_record_says_who_and_what(self):
        self.assertEqual(self.plan_repo(self.source)["refusals"], [])
        result = self.apply("repo", self.source, self.fingerprint)
        self.assertEqual(list(self.db.execute("PRAGMA foreign_key_check")), [])
        self.assertEqual(self.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        after = self.counts()
        # The record is one item and three notes: opened, one chunk, done.
        self.assertEqual({t: self.before[t] - after[t] for t in ("repo", "item", "note", "assignment", "runner_run",
                                                                   "runner_lease")},
                         {"repo": 1, "item": 0, "note": 7 - 3, "assignment": 2, "runner_run": 2, "runner_lease": 2})
        self.assertEqual(result["notes"], 1)
        self.assertIsNone(self.db.execute("SELECT 1 FROM item WHERE id=?", (self.probe,)).fetchone())
        self.assertIsNone(self.db.execute("SELECT 1 FROM repo WHERE path=?", (self.source,)).fetchone())
        record = self.only_record()
        self.assertEqual(result["record"], record)
        row, fields, _ = self.record(record)
        self.assertEqual(row["external_id"], f"repo-remove:{self.fingerprint}")
        self.assertIsNone(row["repo"])
        self.assertEqual(fields["report"]["removed"], {"repo": 1, "item": 1, "note": 7, "assignment": 2,
                                                       "runner_run": 2, "runner_lease": 2})
        self.assertEqual(result["removed"], fields["report"]["removed"])
        actor = fields["report"]["actor"]
        self.assertEqual((actor["who"], actor["reason"]), (WHO["who"], WHO["reason"]))
        self.assertEqual((actor["pid"], actor["ppid"]), (os.getpid(), os.getppid()))
        self.assertEqual(actor["principal"], WHO["principal"])

    def test_a_stray_partial_is_refused_by_the_preview(self):
        """The #350 review: the move lists only the pair, so the plan has to name what else is there."""
        (self.journal_dir / f"{self.runs[0]}.partial").write_text("{")
        plan = self.plan_repo(self.source)
        refusals = [r for r in plan["refusals"] if r["table"] == "runner_run" and r["key"] == self.runs[0]
                    and f"{self.runs[0]}.partial" in r["message"]]
        self.assertEqual(sorted(r["code"] for r in refusals), ["I6", "P4"])
        self.assertEqual([r for r in self.plan(self.probe)["refusals"] if ".partial" in r["message"]][0]["code"], "I6")

    def test_a_partial_that_appears_after_the_backup_is_refused_before_the_commit(self):
        """The re-plan inside the transaction sees it, so no row goes and the journal stays whole."""
        partial = self.journal_dir / f"{self.runs[0]}.partial"
        real = backup.run

        def run(**kwargs):
            snapshot = real(**kwargs)
            partial.write_text("{")
            return snapshot

        with mock.patch.object(backup, "run", side_effect=run), \
                self.assertRaisesRegex(removal.RemovalRefused, r"\.partial"):
            self.apply("repo", self.source, self.fingerprint)
        self.assertFalse(self.db.in_transaction)
        # The backup writes its own checkpoint `state` row; no other table moves.
        self.assertEqual({k: v for k, v in self.counts().items() if k != "state"},
                         {k: v for k, v in self.before.items() if k != "state"})
        self.assertEqual(self.db.execute("SELECT count(*) FROM item WHERE source='cron-report'").fetchone()[0], 0)
        self.assertTrue(partial.exists())
        self.assertTrue(all((self.journal_dir / f"{run}.json").exists() for run in self.runs))

    def test_the_record_marker_and_status(self):
        record = self.apply("repo", self.source, self.fingerprint)["record"]
        row, fields, _ = self.record(record)
        self.assertEqual(fields["record"], "repo-remove")
        self.assertIs(fields["attention"], False)
        self.assertEqual(row["status"], "done")
        notes = self.db.execute("SELECT kind, body FROM note WHERE item=? ORDER BY id", (record,)).fetchall()
        self.assertEqual([n["kind"] for n in notes if n["kind"] == "followup"], [])
        self.assertEqual([n["body"] for n in notes if n["kind"] == "status_change"][-1],
                         f"planning -> done by {WHO['who']}: removal record")
        other = self.item(title="an item")
        self.newer()
        record = self.apply("item", other, self.plan(other)["fingerprint"])["record"]
        self.assertEqual(self.record(record)[1]["record"], "item-remove")

    def test_nothing_sweeps_the_record(self):
        record = self.apply("repo", self.source, self.fingerprint)["record"]
        before = sd_db.workflow.item_state(self.db, record)["revision"]
        later = datetime.now(UTC) + timedelta(days=30)
        retention.settle_clean_reports(self.db, now=later)
        self.assertEqual(sd_db.workflow.item_state(self.db, record)["revision"], before)
        self.assertNotIn(record, reporting.clean_candidates(self.db, before=later.isoformat()))

    def test_the_chunk_notes_rebuild_every_row_and_records_finds_the_record(self):
        plan = self.plan_repo(self.source)
        record = self.apply("repo", self.source, self.fingerprint)["record"]
        rows = self.chunk_rows(record)
        self.assertEqual(rows, [{"table": e["table"], "row": e["row"]} for e in plan["rows"]])
        self.assertEqual(len(rows), 15)
        self.assertEqual(removal.records(self.db, "item", self.probe), [record])
        self.assertEqual(removal.records(self.db, "repo", self.source), [record])
        self.assertEqual(removal.records(self.db, "item", self.probe + 1000), [])

    def test_no_id_is_reused(self):
        largest = self.db.execute("SELECT max(id) FROM item").fetchone()[0]
        self.assertEqual(largest, self.probe)
        result = self.apply("repo", self.source, self.fingerprint)
        self.assertGreater(result["record"], largest)
        fresh = self.assignment(self.item(title="after"))
        self.assertGreater(fresh, max(self.works))

    def test_a1_is_read_again_inside_the_transaction(self):
        """The newest assignment goes between the first plan and the re-plan: A1 from the second one."""
        real = removal.operations.control_gate

        @removal.operations.__dict__["contextmanager"]
        def gate(database):
            self.db.execute("DELETE FROM assignment WHERE id=?", (self.newest,))
            with real(database):
                yield

        with mock.patch.object(removal.operations, "control_gate", gate), \
                self.assertRaisesRegex(removal.RemovalRefused, "A1"):
            self.apply("repo", self.source, self.fingerprint)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.counts()["runner_run"], self.before["runner_run"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM item WHERE source='cron-report'").fetchone()[0], 0)

    def test_an_assignment_chain_inside_the_plan_is_removed(self):
        self.db.execute("UPDATE assignment SET after=? WHERE id=?", (self.works[0], self.works[1]))
        fingerprint = self.plan_repo(self.source)["fingerprint"]
        self.apply("repo", self.source, fingerprint)
        self.assertEqual(self.db.execute("SELECT count(*) FROM assignment WHERE id IN (?, ?)", self.works).fetchone()[0], 0)

    def test_a_note_added_between_plan_and_apply_is_g3_and_writes_nothing(self):
        add_note(self.db, self.probe, "comment", "one more", session="alex")
        before = self.counts()
        with self.assertRaisesRegex(removal.RemovalRefused, "^G3"):
            self.apply("repo", self.source, self.fingerprint)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.db.execute("SELECT count(*) FROM item WHERE source='cron-report'").fetchone()[0], 0)

    def test_a_note_added_inside_the_gate_is_g3_from_the_in_transaction_plan(self):
        """The second compare (C-26): the store changes after the first compare passed."""
        real = removal.operations.control_gate

        @removal.operations.__dict__["contextmanager"]
        def gate(database):
            add_note(self.db, self.probe, "comment", "late", session="alex")
            with real(database):
                yield

        with mock.patch.object(removal.operations, "control_gate", gate), \
                self.assertRaisesRegex(removal.RemovalRefused, "^G3"):
            self.apply("repo", self.source, self.fingerprint)
        self.assertEqual(self.db.execute("SELECT count(*) FROM item WHERE source='cron-report'").fetchone()[0], 0)
        self.assertEqual(self.counts()["runner_run"], self.before["runner_run"])

    def test_a_forced_foreign_key_violation_rolls_the_record_and_the_deletes_back(self):
        # G7 (the #399 review) refuses an unknown child at plan time; this
        # test is about the in-transaction check, so G7 is patched off here.
        # The constraint is deferred, so the delete passes and only
        # `PRAGMA foreign_key_check` sees the orphan before the commit.
        self.db.execute("CREATE TABLE fixture_child (id INTEGER PRIMARY KEY,"
                        " owner INTEGER REFERENCES item(id) DEFERRABLE INITIALLY DEFERRED)")
        self.db.execute("INSERT INTO fixture_child (owner) VALUES (?)", (self.probe,))
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.journal_dir.iterdir()}
        with mock.patch.object(removal, "_unknown_children", return_value=[]), \
                self.assertRaisesRegex(removal.RemovalRefused, "foreign key violation.*fixture_child"):
            fingerprint = self.plan_repo(self.source)["fingerprint"]
            self.apply("repo", self.source, fingerprint)
        self.assertFalse(self.db.in_transaction)
        after = self.counts()
        self.assertEqual({k: v for k, v in after.items() if k not in ("state", "fixture_child")},
                         {k: v for k, v in self.before.items() if k != "state"})
        self.assertEqual(after["fixture_child"], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM item WHERE source='cron-report'").fetchone()[0], 0)
        self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.journal_dir.iterdir()}, before)

    def test_a_concurrent_writer_holding_the_lock_is_waited_for(self):
        held = threading.Event()

        def hold():
            other = sd_db.connect(self.store / "sd.db")
            try:
                other.execute("BEGIN IMMEDIATE")
                held.set()
                time.sleep(1.0)
                other.execute("COMMIT")
            finally:
                other.close()

        writer = threading.Thread(target=hold); writer.start(); self.addCleanup(writer.join)
        held.wait(5)
        self.apply("repo", self.source, self.fingerprint)
        writer.join()
        self.assertEqual(list(self.db.execute("PRAGMA foreign_key_check")), [])
        self.only_record()

    def test_g5_a_backup_that_raises_writes_nothing_and_a_retry_is_safe(self):
        with mock.patch.object(backup, "run", side_effect=OSError("disk full")), \
                self.assertRaisesRegex(removal.RemovalRefused, "^G5.*disk full"):
            self.apply("repo", self.source, self.fingerprint)
        self.assertEqual({k: v for k, v in self.counts().items() if k != "state"},
                         {k: v for k, v in self.before.items() if k != "state"})
        self.assertEqual(self.snapshots(), [])
        self.apply("repo", self.source, self.fingerprint)
        self.only_record()

    def test_g5_a_snapshot_with_violations_writes_nothing(self):
        snapshot = backup.Snapshot(directory=self.home / "x", run_id="r", violations=[("note", 1, "item", 0)])
        with mock.patch.object(backup, "run", return_value=snapshot), \
                self.assertRaisesRegex(removal.RemovalRefused, "^G5.*violation"):
            self.apply("repo", self.source, self.fingerprint)
        self.assertEqual({k: v for k, v in self.counts().items() if k != "state"},
                         {k: v for k, v in self.before.items() if k != "state"})

    def test_a_stale_fingerprint_takes_no_backup(self):
        add_note(self.db, self.probe, "comment", "after the preview", session="alex")
        with mock.patch.object(backup, "run", wraps=backup.run) as run, \
                self.assertRaisesRegex(removal.RemovalRefused, "^G3"):
            self.apply("repo", self.source, self.fingerprint)
        self.assertEqual(run.call_count, 0)
        self.assertEqual(self.snapshots(), [])
        # The same for a refusal that appears after the preview.
        fingerprint = self.plan_repo(self.source)["fingerprint"]
        add_note(self.db, self.probe, "followup", "open", session="alex")
        with mock.patch.object(backup, "run", wraps=backup.run) as run, \
                self.assertRaisesRegex(removal.RemovalRefused, "I7"):
            self.apply("repo", self.source, fingerprint)
        self.assertEqual(run.call_count, 0)
        self.assertEqual(self.snapshots(), [])

    def test_the_backup_is_of_the_applys_store(self):
        self.assertNotEqual(self.store / "sd.db", sd_db.database.default_path(self.home))
        with mock.patch.object(backup, "run", wraps=backup.run) as run:
            self.apply("repo", self.source, self.fingerprint)
        self.assertEqual(run.call_args.kwargs["database"], self.store / "sd.db")
        self.assertEqual(run.call_args.kwargs["home"], self.home)
        memory = sd_db.migrate.initialise_memory() if hasattr(sd_db.migrate, "initialise_memory") else None
        if memory is None:
            memory = sqlite3.connect(":memory:"); memory.row_factory = sqlite3.Row
            sd_db.paths.install(memory)
            for number, path in sd_db.schema.migrations():
                memory.executescript(path.read_text(encoding="utf-8"))
        self.addCleanup(memory.close)
        with mock.patch.object(removal.operations, "control_gate", wraps=removal.operations.control_gate) as gate, \
                self.assertRaisesRegex(removal.RemovalRefused, "^G5.*no main file"):
            removal.apply(memory, "item", 1, fingerprint="x", home=self.home, **WHO)
        self.assertEqual(gate.call_count, 0)

    def test_g4_is_checked_before_the_backup(self):
        for name, value in (("program", "x" * 201), ("session", "x" * 201), ("program", ""), ("principal", "")):
            with self.subTest(name=name, value=value[:1]), mock.patch.object(backup, "run", wraps=backup.run) as run, \
                    self.assertRaisesRegex(removal.RemovalRefused, "^G4"):
                self.apply("repo", self.source, self.fingerprint, **{name: value})
            self.assertEqual(run.call_count, 0)
            self.assertEqual(self.snapshots(), [])

    def test_the_snapshot_restores_the_fifteen_rows_into_another_home(self):
        result = self.apply("repo", self.source, self.fingerprint)
        directory = Path(result["backup"])
        self.assertTrue(directory.is_dir())
        self.assertIn(f"backup: {directory}", self.record(result["record"])[2].split("\n"))
        other = self.root / "other-home"; other.mkdir()
        restored = backup.restore(directory, home=other)
        connection = sd_db.connect(restored); self.addCleanup(connection.close)
        backup._check_restore(connection)
        self.assertEqual(connection.execute("SELECT count(*) FROM item WHERE id=?", (self.probe,)).fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT count(*) FROM runner_run").fetchone()[0], 2)
        self.assertEqual(connection.execute("SELECT count(*) FROM repo WHERE path=?", (self.source,)).fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM runner_run").fetchone()[0], 0)

    def test_an_unknown_plan_kind_is_refused_before_the_backup(self):
        """Copilot on #407: a typo such as `repos` must not select the repo plan."""
        with self.assertRaisesRegex(removal.RemovalRefused, "unknown plan kind 'repos'"):
            self.apply("repos", self.source, self.fingerprint)
        self.assertEqual(self.snapshots(), [])
        self.assertEqual(self.counts(), self.before)

    def test_r3_from_the_applys_own_manifest_is_refused_inside_the_transaction(self):
        """Copilot on #407: the preview reserves headroom; the apply checks the manifest it stores."""
        plan = self.plan_repo(self.source)
        manifest, _, _ = removal._record(plan, who=None, reason=None, backup=None)
        with mock.patch.object(removal, "MANIFEST_HEADROOM", 0), \
                mock.patch.object(removal, "MAX_REPORT", len(manifest.encode()) + 10):
            self.assertEqual(self.plan_repo(self.source)["fingerprint"], self.fingerprint)
            with self.assertRaisesRegex(removal.RemovalRefused, "^R3: the manifest is"):
                self.apply("repo", self.source, self.fingerprint)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(len(self.snapshots()), 1)
        after = self.counts()
        self.assertEqual({k: v for k, v in after.items() if k != "state"},
                         {k: v for k, v in self.before.items() if k != "state"})
        self.assertEqual(self.db.execute("SELECT count(*) FROM item WHERE source='cron-report'").fetchone()[0], 0)

    def test_who_and_reason_are_required(self):
        for name in ("who", "reason"):
            with self.subTest(name=name), self.assertRaises(TypeError):
                arguments = {**WHO}; del arguments[name]
                removal.apply(self.db, "repo", self.source, fingerprint=self.fingerprint, home=self.home,
                              with_items=True, **arguments)

    def test_a_second_apply_is_refused_and_files_no_second_report(self):
        self.apply("repo", self.source, self.fingerprint)
        with self.assertRaisesRegex(removal.RemovalRefused, "P1"):
            self.apply("repo", self.source, self.fingerprint)
        self.only_record()
        other = self.item(title="an item")
        self.newer()
        fingerprint = self.plan(other)["fingerprint"]
        self.apply("item", other, fingerprint)
        with self.assertRaisesRegex(removal.RemovalRefused, "I1"):
            self.apply("item", other, fingerprint)
        self.assertEqual(self.db.execute("SELECT count(*) FROM item WHERE source='cron-report'").fetchone()[0], 2)

    def test_the_record_cannot_be_removed(self):
        record = self.apply("repo", self.source, self.fingerprint)["record"]
        plan = self.plan(record)
        self.assertEqual([r["code"] for r in plan["refusals"]], ["I9"])
        with self.assertRaisesRegex(removal.RemovalRefused, "I9"):
            self.apply("item", record, plan["fingerprint"])

    def test_more_runs_than_the_fd_limit_admits_are_all_moved(self):
        """sd:968: the move takes one ending lock at a time, so `RLIMIT_NOFILE` does not bound the run count.

        Holding every run's ending-lock fd for the whole move met `EMFILE`
        after the commit on a repo with more runs than the soft limit (256
        on a default macOS shell), and the apply exited 4 with the lines
        printed. The limit is lowered inside this process to a number above
        what it already holds and below the run count, and restored after.
        The import sits here so no line above the keyed citation at
        `contribution_sync.py` moves (`tests/test_citations.py`'s ratchet).
        """
        import resource
        late = [self.attempt(self.works[0], self.source, number=number) for number in range(2, 50)]
        for run in late:
            self.lease(run, self.source)
        self.journal(late)
        runs = self.runs + late
        fingerprint = self.plan_repo(self.source)["fingerprint"]
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        limit = len(os.listdir("/dev/fd")) + 24
        self.assertLess(limit, soft)
        self.assertLess(limit, len(runs))
        resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))
        try:
            result = self.apply("repo", self.source, fingerprint)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        self.assertIsNone(result["move_error"])
        self.assertEqual(result["move_commands"], [])
        self.assertEqual(sorted(result["moved"]), sorted(str(self.journal_dir / f"{run}.{suffix}")
                                                         for run in runs for suffix in ("json", "lock")))
        self.assertEqual(sorted(self.journal_dir.iterdir()), [])
        quarantine = self.store / "runner-recovery-evidence" / f"removed-{fingerprint}"
        self.assertEqual(len(list(quarantine.iterdir())), 2 * len(runs) + 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM runner_run").fetchone()[0], 0)


if __name__ == "__main__": unittest.main()
