"""Tasks > Start next (sd:5107): builders on the next ready items, within the builder cap and the load cap.

`start_next.plan` is the preview and `start_next.run` the start. The Claude sessions, the gate and herdr are seams:
the sessions document is a dict, the gate's answer a dict, and the launch is the real `fix_session.start` with its
`herdr.sh launch` call replaced, so the revision check and the Fix note are the real ones. Nothing starts claude,
runs `sd gate` or reaches herdr.
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import workflow
from sd_dashboard import fix_session, start_next

from support import NOW, ScreenCase


def sessions(running=0, named=(), why=""):
    rows = [{"tasks": [{"item": item, "via": "Fix builder prompt"} for item in named]}]
    return lambda connection, *, now: {"sessions": rows if running is not None else None,
                                       "builders": {"running": running, "why": why}}


def gate(load5=4.0, load_max=40.0):
    return lambda: {"load": [load5 + 1, load5, load5 - 1], "load_max": load_max}


class Crash(Exception):
    """The server process stopping between two launches."""


class StartNext(ScreenCase):
    def setUp(self):
        super().setUp()
        self.work = Path(self.tmp.name) / "checkout"
        self.work.mkdir()
        self.repo_path = self.repo(str(self.work))
        self.launched = []
        herdr = patch.object(fix_session.subprocess, "run", self.herdr)
        herdr.start()
        self.addCleanup(herdr.stop)

    def herdr(self, argv, **options):
        self.launched.append(argv[3])
        return subprocess.CompletedProcess(argv, 0, stdout=f"launched w1:p{len(self.launched)}: claude in {argv[2]}\n", stderr="")

    def ready(self, title, **columns):
        columns.setdefault("repo", self.repo_path)
        return self.item(title, kind="task", status="ready", **columns)

    def plan(self, **seams):
        seams.setdefault("sessions", sessions())
        seams.setdefault("gate", gate())
        return start_next.plan(self.connection, now=NOW, **seams)

    def run_plan(self, items, **seams):
        seams.setdefault("sessions", sessions())
        seams.setdefault("gate", gate())
        return start_next.run(self.connection, now=NOW, items=items, who="dashboard", **seams)

    def test_the_plan_names_the_next_ready_items_with_their_revisions_and_why_others_are_left_out(self):
        first, second = self.ready("First"), self.ready("Second")
        self.item("Planning only", kind="task", status="planning", repo=self.repo_path)
        nowhere = self.ready("No checkout", repo=self.repo("/repos/not-on-this-mac"))
        launched = self.ready("Fixed already")
        workflow.add_item_note(self.connection, launched, kind="comment", who="dashboard",
                               body=fix_session.NOTE.format(session="builder", item=launched, pane="p9", where="~/x"))
        named = self.ready("A session has it")
        out = self.plan(sessions=sessions(running=2, named=[named]))
        self.assertEqual((out["cap"], out["running"], out["room"], out["refusal"]), (6, 2, 2, ""))
        self.assertEqual([(row["id"], row["revision"]) for row in out["start"]],
                         [(first, workflow.item_state(self.connection, first)["revision"]),
                          (second, workflow.item_state(self.connection, second)["revision"])])
        why = {row["id"]: row["why"] for row in out["skipped"]}
        self.assertIn("not a directory on this machine", why[nowhere])
        self.assertEqual(why[launched], "Fix started a session for it already")
        self.assertEqual(why[named], "a live Claude session works on it")

    def test_the_room_is_the_cap_less_the_running_builders(self):
        items = [self.ready(f"Task {n}") for n in range(8)]
        out = self.plan(sessions=sessions(running=3))
        self.assertEqual((out["room"], [row["id"] for row in out["start"]]), (3, items[:3]))

    def test_no_room_when_the_cap_is_full_the_load_is_high_or_either_was_not_read(self):
        self.ready("Waiting")
        cases = [
            (dict(sessions=sessions(running=6)), "6 builders run already; the cap is 6"),
            (dict(gate=gate(load5=41.5)), "the five-minute load is 41.5, above the cap of 40"),
            (dict(sessions=sessions(running=None, why="claude agents --json gave no answer in 10 s")),
             "the running builders were not counted: claude agents --json gave no answer in 10 s"),
            (dict(gate=lambda: {"load": []}), "the load was not read: sd gate status gave no load or load cap"),
            (dict(gate=gate(load5=float("nan"))), "the load was not read: sd gate status gave no load or load cap"),
            (dict(gate=gate(load_max=float("inf"))), "the load was not read: sd gate status gave no load or load cap"),
        ]
        for seams, refusal in cases:
            out = self.plan(**seams)
            self.assertEqual((out["room"], out["start"], out["refusal"]), (0, [], refusal))
            with self.assertRaisesRegex(workflow.WorkflowError, "Nothing started"):
                self.run_plan([{"id": out["eligible"][0]["id"], "revision": out["eligible"][0]["revision"]}], **seams)
        self.assertEqual(self.launched, [])

    def test_a_run_starts_each_item_with_its_revision_and_writes_its_note(self):
        first, second = self.ready("First"), self.ready("Second")
        out = self.run_plan([{"id": row["id"], "revision": row["revision"]} for row in self.plan()["start"]])
        self.assertEqual(out["failed"], None)
        self.assertEqual([(row["id"], row["pane"]) for row in out["started"]], [(first, "w1:p1"), (second, "w1:p2")])
        self.assertEqual(self.launched, [f"sd-{first}", f"sd-{second}"])
        self.assertEqual(self.plan()["refusal"], "no ready item is left to start")

    def test_the_first_item_that_does_not_start_stops_the_run(self):
        first, second, third = self.ready("First"), self.ready("Second"), self.ready("Third")
        rows = [{"id": row["id"], "revision": row["revision"]} for row in self.plan()["start"]]
        workflow.add_item_note(self.connection, second, kind="comment", who="human", body="changed since the plan")
        out = self.run_plan(rows)
        self.assertEqual([row["id"] for row in out["started"]], [first])
        self.assertEqual((out["failed"]["id"], out["failed"]["status"]), (second, 409))
        self.assertEqual(out["not_started"], [third])
        self.assertEqual(self.launched, [f"sd-{first}"])

    def test_nothing_starts_when_an_item_left_the_plan_or_more_were_asked_than_fit(self):
        first, second = self.ready("First"), self.ready("Second")
        rows = [{"id": row["id"], "revision": row["revision"]} for row in self.plan()["start"]]
        with self.assertRaisesRegex(workflow.WorkflowError, "1 builders fit now, not 2"):
            self.run_plan(rows, sessions=sessions(running=5))
        workflow.change_status(self.connection, second, "in_progress", who="human",
                               expected_revision=workflow.item_state(self.connection, second)["revision"])
        with self.assertRaisesRegex(workflow.WorkflowError, f"#{second} is not ready"):
            self.run_plan(rows)
        self.assertEqual(self.launched, [])
        self.assertEqual(first, rows[0]["id"])

    def test_a_run_stopped_between_two_launches_leaves_the_started_item_out_of_the_next_plan(self):
        """Steps: the note lands with each launch, so a rerun neither starts the first item twice nor skips the second."""
        first, second = self.ready("First"), self.ready("Second")
        rows = [{"id": row["id"], "revision": row["revision"]} for row in self.plan()["start"]]
        real = fix_session.start

        def crash_after_first(connection, item, **options):
            if item == second:
                raise Crash("the server stopped")
            return real(connection, item, **options)

        with self.assertRaises(Crash):
            self.run_plan(rows, launch=crash_after_first)
        again = self.plan()
        self.assertEqual([row["id"] for row in again["start"]], [second])
        self.assertIn(first, {row["id"] for row in again["skipped"]})
        out = self.run_plan([{"id": row["id"], "revision": row["revision"]} for row in again["start"]])
        self.assertEqual([row["id"] for row in out["started"]], [second])
        self.assertEqual(self.launched, [f"sd-{first}", f"sd-{second}"])

    def test_a_stalled_or_unread_builder_holds_its_slot_and_its_item(self):
        """Review class (sd:5106 round 1): the real sessions document, with one builder stalled past `STALL` and five
        whose logs hold no record, leaves no room, and the stalled builder's item is not offered again."""
        import json
        import os
        from sd_dashboard import claude_sessions
        held, other = self.ready("Held by a stalled builder"), self.ready("Next")
        home = Path(self.tmp.name) / "claude"
        folder = home / "projects" / "-w" / "lead-session-1" / "subagents"
        folder.mkdir(parents=True)
        (home / "projects" / "-w" / "lead-session-1.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "go"}}) + "\n")
        mid_turn = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}) + "\n"
        for n in range(6):
            log = folder / f"agent-a{n}.jsonl"
            log.write_text(mid_turn if n == 0 else "")
            then = 1788696000 - 3 * 3600 - 60 * n  # NOW less 3 h and n minutes: stalled, or unread
            os.utime(log, (then, then))
            (folder / f"agent-a{n}.meta.json").write_text(json.dumps(
                {"customAgentType": "sd-slice-builder", "name": f"builder-{chr(97 + n)}", "description": f"Build sd:{held}" if n == 0 else ""}))
        agents = [{"pid": 1, "cwd": str(self.work), "sessionId": "lead-session-1", "name": "lead", "status": "idle"}]

        def document(connection, *, now):
            return claude_sessions.document(connection, now=now, agents=lambda: agents, processes=list, root=home,
                                            work=lambda path: None, fleet=lambda area: {"worktrees": []})

        out = self.plan(sessions=document)
        # Operator ruling (sd:5106 round 1): a full cap names the sessions holding it, stalled first, and where to end them.
        self.assertEqual((out["running"], out["room"]), (6, 0))
        self.assertEqual(out["refusal"], "6 builders run already; the cap is 6. Stalled: checkout builder-a (3h 0m). Unknown: "
                                         "checkout builder-f (3h 5m), checkout builder-e (3h 4m), checkout builder-d (3h 3m), "
                                         "checkout builder-c (3h 2m), checkout builder-b (3h 1m). End a session in Management > "
                                         "Sessions to free its slots")
        self.assertEqual(out["holders"][0], {"session": "lead", "repo": "checkout", "agent": "builder-a", "state": "stalled",
                                             "age": 10800})
        self.assertEqual({row["id"]: row["why"] for row in out["skipped"]}[held], "a live Claude session works on it")
        self.assertEqual([row["id"] for row in out["eligible"]], [other])

    def test_the_post_takes_only_distinct_id_and_revision_pairs_up_to_the_cap(self):
        good = {"id": 1, "revision": "r"}
        self.assertTrue(callable(start_next.request({"items": [good]})))
        for body in ({}, {"items": []}, {"items": [good, good]}, {"items": [{"id": "1", "revision": "r"}]},
                     {"items": [dict(good, id=n) for n in range(1, 8)]}, {"items": [good], "n": 1},
                     {"items": [{"id": 1}]}, {"items": [{"id": True, "revision": "r"}]}):
            with self.assertRaises(ValueError, msg=body):
                start_next.request(body)


if __name__ == "__main__":
    unittest.main()
