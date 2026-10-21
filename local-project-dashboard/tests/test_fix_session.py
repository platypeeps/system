"""Fix (sd:3456): a task starts a builder session and a report a debug session, through herdr.sh launch.

Every test runs the real `local-herdr/herdr.sh` against that tool's own `herdr` double (`local-herdr/tests/doubles`),
first on PATH, and reads the argv lines it journals. No test reaches the installed herdr or starts claude:
`test_the_double_is_the_herdr_the_launch_finds` asserts which `herdr` the server's PATH resolves.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

from sd_db import workflow
from sd_dashboard import fix_session

from test_workflow_actions import BrowserSession

DOUBLES = fix_session.CHECKOUT / "local-herdr" / "tests" / "doubles"
RUNNING = {"sessions": [{"name": "sd", "running": True, "socket_path": "/tmp/sd/herdr.sock", "default": False}]}


class FixCase(BrowserSession):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.fixture, self.journal, self.work = root / "fixture", root / "journal.tsv", root / "checkout"
        self.fixture.mkdir()
        self.work.mkdir()
        environment = patch.dict(os.environ, {"PATH": f"{DOUBLES}{os.pathsep}{os.environ.get('PATH', '')}",
                                              "HERDR_DOUBLE_FIXTURE": str(self.fixture), "HERDR_DOUBLE_JOURNAL": str(self.journal),
                                              "HERDR_STATE_DIR": str(root / "state"), "PYTHON": sys.executable})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("HERDR_SESSION", None)
        os.environ.pop("HERDR_SOCKET_PATH", None)

    def running(self):
        (self.fixture / "session-list.json").write_text(json.dumps(RUNNING))

    def calls(self):
        return [line.split("\t")[2:] for line in self.journal.read_text().splitlines()] if self.journal.exists() else []

    def revision(self, item):
        return workflow.item_state(self.connection, item)["revision"]

    def fix(self, item, **body):
        return self.post(f"/api/items/{item}/fix", {"revision": self.revision(item), **body})

    def task(self, title="Make the thing", **columns):
        columns.setdefault("repo", self.repo(str(self.work)))
        return self.item(title, kind="task", **columns)


class TheLaunch(FixCase):
    def test_the_double_is_the_herdr_the_launch_finds(self):
        self.assertEqual(Path(shutil.which("herdr")).parent, DOUBLES)

    def test_a_task_starts_a_builder_session_in_its_checkout_and_records_a_note(self):
        self.running()
        item = self.task()
        status, _, state = self.fix(item)
        self.assertEqual(status, 200, state)
        self.assertEqual(self.calls()[1:], [
            ["workspace", "create", "--cwd", str(self.work), "--label", f"sd-{item}", "--no-focus"],
            ["agent", "start", "sd-w9-p1", "--kind", "claude", "--pane", "w9:p1"],
            ["agent", "prompt", "sd-w9-p1", fix_session.prompt("task", item)],
        ])
        self.assertIn(f"Build sd:{item}.", fix_session.prompt("task", item))
        self.assertIn(f"`sd task show {item}`", fix_session.prompt("task", item))
        self.assertEqual(state["fix"], {"session": "builder", "pane": "w9:p1", "checkout": str(self.work)})
        note = state["notes"][-1]
        self.assertEqual((note["kind"], note["session"]), ("comment", "dashboard"))
        self.assertEqual(note["body"], f"Fix: started a builder session (claude) in herdr workspace sd-{item}, pane w9:p1, at {self.work}.")
        self.assertEqual(state["revision"], self.revision(item))

    def test_a_report_without_a_repo_starts_a_debug_session_in_this_checkout(self):
        self.running()
        item = self.item("nightly: needs attention", kind="report")
        status, _, state = self.fix(item)
        self.assertEqual(status, 200, state)
        self.assertEqual(self.calls()[1][:4], ["workspace", "create", "--cwd", str(fix_session.CHECKOUT)])
        sent = self.calls()[3]
        self.assertEqual(sent[:3], ["agent", "prompt", "sd-w9-p1"])
        self.assertIn(f"sd-debug skill on report sd:{item}", sent[3])
        self.assertIn(f"`sd reports acknowledge {item}`) only after a clean run", sent[3])
        self.assertEqual(state["fix"]["session"], "debug")

    def test_a_report_with_a_repo_starts_in_that_repo(self):
        self.running()
        item = self.item("nightly: needs attention", kind="report", repo=self.repo(str(self.work)))
        self.assertEqual(self.fix(item)[0], 200)
        self.assertEqual(self.calls()[1][3], str(self.work))

    def test_the_prompt_carries_the_id_and_never_the_title(self):
        self.running()
        item = self.task(title="$(touch pwned) `id`; rm -rf ~")
        self.assertEqual(self.fix(item)[0], 200)
        self.assertEqual(self.calls()[3][3], fix_session.prompt("task", item))
        self.assertNotIn("pwned", "".join(sum(self.calls(), [])))


class TheRefusals(FixCase):
    def test_no_running_session_is_a_503_naming_herdr_sh_start_and_writes_nothing(self):
        item = self.task()
        before = self.unjournalled()
        status, _, answer = self.fix(item)
        self.assertEqual(status, 503)
        self.assertEqual(answer["error"], "Fix failed: herdr.sh launch: session sd is not running; run herdr.sh start first")
        self.assertEqual([call[:2] for call in self.calls()], [["session", "list"]])
        self.assertEqual(self.unjournalled(), before)
        self.assertEqual(self.journalled(), [(f"Fix session · sd:{item}", 1)])  # sd:5541

    def test_herdr_not_installed_is_a_503_naming_herdr_sh_start_and_writes_nothing(self):
        bare = Path(self.tmp.name) / "bin"
        bare.mkdir()
        for tool in ("sh", "dirname"):
            (bare / tool).symlink_to(shutil.which(tool))
        os.environ["PATH"] = str(bare)
        item = self.task()
        before = self.unjournalled()
        status, _, answer = self.fix(item)
        self.assertEqual(status, 503)
        self.assertEqual(answer["error"], "Fix failed: herdr.sh launch: herdr is not installed; install it, then run herdr.sh start")
        self.assertEqual(self.unjournalled(), before)
        self.assertEqual(self.journalled(), [(f"Fix session · sd:{item}", 1)])  # sd:5541

    def test_a_refused_start_is_a_503_that_says_the_workspace_is_left(self):
        self.running()
        (self.fixture / "agent-start-fail").write_text("")
        item = self.task()
        before = self.unjournalled()
        status, _, answer = self.fix(item)
        self.assertEqual(status, 503)
        self.assertIn("w9:p1: claude did not start", answer["error"])
        self.assertIn("left at its shell prompt", answer["error"])
        self.assertEqual(self.unjournalled(), before)
        self.assertEqual(self.journalled(), [(f"Fix session · sd:{item}", 1)])  # sd:5541

    def test_no_answer_in_time_is_a_504_that_says_the_outcome_is_not_known(self):
        self.running()
        hook = self.fixture / "on-agent-start"
        hook.write_text("#!/bin/sh\nexec sleep 5\n")
        hook.chmod(0o755)
        item = self.task()
        before = self.unjournalled()
        with patch.object(fix_session, "TIMEOUT", 1):
            status, _, answer = self.fix(item)
        self.assertEqual(status, 504)
        self.assertIn("whether a session started is not known", answer["error"])
        self.assertIn(f"workspace sd-{item}", answer["error"])
        self.assertEqual(self.unjournalled(), before)
        self.assertEqual(self.journalled(), [(f"Fix session · sd:{item}", -1)])  # sd:5541

    def test_a_note_that_cannot_be_written_after_the_launch_names_the_pane(self):
        self.running()
        item = self.task()
        with patch.object(workflow, "add_item_note", side_effect=sqlite3.OperationalError("database is locked")):
            status, _, answer = self.fix(item)
        self.assertEqual(status, 503)
        self.assertIn("claude started in herdr pane w9:p1, but the note was not recorded: database is locked", answer["error"])
        self.assertIn(f"Record it with: sd task note {item} '", answer["error"])
        self.assertNotIn("--body", answer["error"])

    def test_a_second_click_from_the_same_page_is_stale_and_launches_nothing(self):
        self.running()
        item = self.task()
        revision = self.revision(item)
        self.assertEqual(self.post(f"/api/items/{item}/fix", {"revision": revision})[0], 200)
        launched = len(self.calls())
        status, _, answer = self.post(f"/api/items/{item}/fix", {"revision": revision})
        self.assertEqual(status, 409)
        self.assertTrue(answer["reload"])
        self.assertEqual(len(self.calls()), launched)

    def test_a_done_item_a_task_with_no_repo_and_a_missing_checkout_launch_nothing(self):
        self.running()
        done = self.task(status="done")
        bare = self.item("No repo", kind="task")
        gone = self.item("Moved", kind="task", repo=self.repo(str(self.work / "gone")))
        for item, says in ((done, "is done"), (bare, "names no repository"), (gone, "is not a directory on this machine")):
            with self.subTest(item=item):
                status, _, answer = self.fix(item)
                self.assertEqual(status, 400)
                self.assertIn(says, answer["error"])
        self.assertEqual(self.calls(), [])

    def test_a_bad_body_a_missing_item_and_a_bad_origin_launch_nothing(self):
        self.running()
        item = self.task()
        good = {"revision": self.revision(item)}
        for payload in ({}, {"revision": "x"}, {**good, "who": "me"}, {**good, "prompt": "anything"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(f"/api/items/{item}/fix", payload)[0], 400)
        self.assertEqual(self.post("/api/items/999999/fix", good)[0], 404)
        self.assertEqual(self.post("/api/items/0/fix", good)[0], 404)
        self.assertEqual(self.post(f"/api/items/{item}/fix", good, **{"X-SD-CSRF": "0" * 64})[0], 403)
        self.assertEqual(self.post(f"/api/items/{item}/fix", good, Origin="http://example.test")[0], 403)
        self.assertEqual(self.calls(), [])
