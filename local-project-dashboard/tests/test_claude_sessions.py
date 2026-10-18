"""Management > Sessions (sd:5106): each Claude session's task, last action, wait and safe to exit.

`claude_sessions.document` joins `claude agents --json`, the session transcripts, one `ps` and git. Every source is a
seam here: a Claude home in a temporary folder, and lists in place of claude, `ps`, git and the fleet. Nothing runs
the real claude, `ps` or git.
"""

import json
import os
import time
import unittest
from pathlib import Path

from sd_dashboard import claude_sessions, fix_session

from support import ScreenCase

NOW = "2026-09-06T12:00:00Z"
CLOCK = 1788696000.0  # NOW as a timestamp
LEAD = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def record(kind, *, stop=None, blocks=("text",), text="done"):
    if kind == "user":
        return {"type": "user", "message": {"role": "user", "content": text}}
    return {"type": "assistant", "message": {"role": "assistant", "stop_reason": stop,
                                             "content": [{"type": block, "text": text} if block == "text" else {"type": block}
                                                         for block in blocks]}}


FINISHED = [record("user", text="go"), record("assistant", stop="end_turn")]
MID_TURN = [record("user", text="go"), record("assistant", blocks=("tool_use",))]


def agent(session=LEAD, *, pid=100, status="idle", cwd="/work/system", name="system", waiting=None):
    row = {"pid": pid, "cwd": cwd, "kind": "interactive", "startedAt": int((CLOCK - 3600) * 1000), "sessionId": session,
           "name": name, "status": status}
    if waiting:
        row["waitingFor"] = waiting
    return row


def clean(path):
    return {"branch": "HEAD", "dirty": 0, "unpushed": 0}


class Sessions(ScreenCase):
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name) / "claude"
        (self.home / "projects" / "-work-system").mkdir(parents=True)

    def transcript(self, session=LEAD, records=FINISHED, *, age=60):
        path = self.home / "projects" / "-work-system" / f"{session}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in records))
        os.utime(path, (CLOCK - age, CLOCK - age))
        return path

    def subagent(self, session, name, records, *, age=30, kind="sd-slice-builder", description=""):
        folder = self.home / "projects" / "-work-system" / session / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / f"agent-a{name}-0.jsonl"
        log.write_text("".join(json.dumps(row) + "\n" for row in records))
        os.utime(log, (CLOCK - age, CLOCK - age))
        (folder / f"agent-a{name}-0.meta.json").write_text(json.dumps(
            {"agentType": name, "customAgentType": kind, "name": name, "description": description}))
        return log

    def read(self, agents, *, processes=list, work=clean, fleet=None, connection=True):
        trees = fleet or (lambda area: {"worktrees": []})
        return claude_sessions.document(self.connection if connection else None, now=NOW, fleet=trees,
                                        agents=lambda: agents, processes=processes, work=work, root=self.home)

    def test_an_idle_session_with_nothing_under_it_is_safe_to_exit_and_carries_its_last_action(self):
        self.transcript(age=600)
        doc = self.read([agent()])
        row = doc["sessions"][0]
        self.assertEqual((row["status"], row["waiting"], row["safe"]), ("idle", False, {"state": "yes", "why": []}))
        self.assertEqual((row["last_action"], row["age"], row["age_text"]), ("2026-09-06T11:50:00Z", 600, "10m"))
        self.assertEqual(doc["sources"], {"agents": "", "processes": "", "worktrees": ""})
        self.assertEqual(doc["builders"], {"running": 0, "why": ""})

    def test_a_session_waiting_on_the_operator_ranks_first_and_says_what_holds_it(self):
        self.transcript(age=10)
        self.transcript(OTHER, age=5)
        doc = self.read([agent(), agent(OTHER, pid=200, status="waiting", name="hoa", waiting="permission: Bash")])
        first = doc["sessions"][0]
        self.assertEqual((first["id"], first["waiting"], first["waiting_for"]), (OTHER, True, "permission: Bash"))
        self.assertEqual(first["safe"], {"state": "no", "why": ["it waits on you: permission: Bash"]})

    def test_a_running_builder_names_the_task_and_blocks_exit_and_a_stalled_one_does_neither(self):
        self.transcript()
        item = self.item("Sessions screen")
        self.subagent(LEAD, f"b{item}-sessions", MID_TURN, age=120, description="Build sessions")
        self.subagent(LEAD, "old-reader", MID_TURN, age=claude_sessions.STALL + 60, kind="general-purpose")
        self.subagent(LEAD, "done-builder", FINISHED, age=60, description="Build sd:999999")
        row = self.read([agent()])["sessions"][0]
        self.assertEqual(row["tasks"], [{"item": item, "via": f"builder b{item}-sessions", "title": "Sessions screen",
                                         "status": "planning"}])
        self.assertEqual(row["safe"], {"state": "no", "why": [f"builder b{item}-sessions is running"]})
        self.assertEqual([(a["name"], a["state"]) for a in row["agents"]],
                         [(f"b{item}-sessions", "running"), ("old-reader", "stalled")])
        # The age is the newest write of the session or any of its agents.
        self.assertEqual(row["age"], 60)
        self.assertEqual(self.read([agent()])["builders"], {"running": 1, "why": ""})

    def test_the_turn_reads_the_last_record_even_past_the_tail_and_a_question_is_still_a_turn(self):
        long = record("assistant", stop="end_turn", text="x" * (claude_sessions.TAIL_BYTES * 2))
        log = self.transcript(records=[record("user"), long])
        self.assertEqual(claude_sessions._turn(log), "idle")
        log = self.transcript(records=[record("user"), record("assistant", stop="end_turn", blocks=("text", "tool_use"))])
        self.assertEqual(claude_sessions._turn(log), "running")
        log = self.transcript(records=[record("assistant", stop="end_turn"), record("user", text="a message")])
        self.assertEqual(claude_sessions._turn(log), "running")

    def test_a_lane_watch_under_the_session_blocks_exit_and_one_under_another_does_not(self):
        self.transcript()
        ps = [(100, 1, "claude"), (101, 100, "/bin/zsh -c sd-ship lane watch --item 7"),
              (102, 101, "/usr/bin/python3 /pack/bin/sd-ship lane watch --item 7"),
              (300, 1, "sd-ship lane watch --item 8"), (103, 100, "sd-ship lane enqueue 9")]
        row = self.read([agent()], processes=lambda: ps)["sessions"][0]
        self.assertEqual([watch["pid"] for watch in row["watches"]], [101, 102])
        self.assertEqual(row["safe"]["state"], "no")

    def test_unpushed_work_in_the_cwd_or_a_builders_worktree_blocks_exit_and_names_the_checkout(self):
        self.transcript()
        item = self.item("Fleet controls")
        self.subagent(LEAD, f"b{item}", FINISHED, description=f"Build sd:{item}")
        trees = lambda area: {"worktrees": [
            {"path": "/wt/mine", "branch": f"review/sd{item}-x", "live": True},
            {"path": "/wt/theirs", "branch": "review/sd424242-x", "live": True}]}
        held = {"/work/system": {"branch": "main", "dirty": 2, "unpushed": 0},
                "/wt/mine": {"branch": f"review/sd{item}-x", "dirty": 0, "unpushed": 1},
                "/wt/theirs": {"branch": "x", "dirty": 9, "unpushed": 9}}
        row = self.read([agent()], fleet=trees, work=held.__getitem__)["sessions"][0]
        self.assertEqual(row["safe"], {"state": "no", "why": ["/work/system holds 2 uncommitted files",
                                                              "/wt/mine holds 1 unpushed commit"]})
        self.assertEqual([c["path"] for c in row["checkouts"]], ["/work/system", "/wt/mine"])

    def test_a_fix_builder_session_counts_as_a_builder_while_it_works(self):
        item = self.item("Started by Fix")
        self.transcript(records=[record("user", text=fix_session.prompt("task", item)), record("assistant", blocks=("tool_use",))])
        doc = self.read([agent(status="busy")])
        row = doc["sessions"][0]
        self.assertTrue(row["builder"])
        self.assertEqual(row["tasks"][0]["via"], "Fix builder prompt")
        self.assertEqual(doc["builders"], {"running": 1, "why": ""})
        self.transcript(records=[record("user", text=fix_session.prompt("task", item)), record("assistant", stop="end_turn")])
        self.assertEqual(self.read([agent(status="idle")])["builders"], {"running": 0, "why": ""})

    def test_the_branch_of_the_cwd_names_its_task(self):
        self.transcript()
        item = self.item("On a branch")
        row = self.read([agent()], work=lambda path: {"branch": f"sd{item}-fix", "dirty": 0, "unpushed": 0})["sessions"][0]
        self.assertEqual([(t["item"], t["via"]) for t in row["tasks"]], [(item, f"branch sd{item}-fix")])

    def test_another_sessions_agents_are_not_this_sessions(self):
        """Evidence: a session reads only the transcript and subagents under its own id."""
        self.transcript()
        self.transcript(OTHER)
        self.subagent(OTHER, "b77-other", MID_TURN)
        row = self.read([agent()])["sessions"][0]
        self.assertEqual((row["agents"], row["safe"]["state"]), ([], "yes"))

    def test_a_failed_read_is_unknown_never_safe_and_never_a_count(self):
        self.transcript()

        def broken(*args):
            raise claude_sessions.Unread("ps gave no answer in 10 s")

        row = self.read([agent()], processes=broken)["sessions"][0]
        self.assertEqual(row["safe"], {"state": "unknown", "why": ["the process table was not read: ps gave no answer in 10 s"]})
        row = self.read([agent()], work=broken)["sessions"][0]
        self.assertEqual(row["safe"]["state"], "unknown")
        # No transcript: its builders are not known, so neither is the machine's builder count.
        doc = self.read([agent(OTHER)])
        self.assertEqual(doc["sessions"][0]["safe"]["state"], "unknown")
        self.assertIsNone(doc["builders"]["running"])
        # claude itself not answering: no session list, which is not an empty one.
        doc = claude_sessions.document(None, now=NOW, agents=broken, fleet=lambda area: {"worktrees": []}, root=self.home)
        self.assertIsNone(doc["sessions"])
        self.assertEqual(doc["builders"], {"running": None, "why": "ps gave no answer in 10 s"})
        doc = claude_sessions.document(None, now=NOW, agents=lambda: {"not": "a list"}, root=self.home)
        self.assertEqual(doc["sources"]["agents"], "claude agents --json answered no session list")

    def test_a_session_id_that_is_no_id_reads_no_file(self):
        self.transcript()
        doc = self.read([agent("*")])
        self.assertEqual(doc["sessions"][0]["safe"]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
