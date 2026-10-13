"""Draft email (sd:3463): claude writes a draft from a task's title and notes, and workspace-mcp saves it; nothing sends.

`claude` is a shell double named by `CLAUDE_BIN`, which journals its argv, its stdin and the token it saw, then prints
the reply the test set. workspace-mcp is Notes' `FakeWorkspace` (test_v2_notes), which records every call. `HOME` is
a disposable folder, so the operator's own `~/.config/shell/env.sh` is never sourced and no test reaches claude or Gmail.
"""

from __future__ import annotations

import io
import os
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from sd_db import workflow
from sd_dashboard import email_draft

from test_v2_notes import FakeWorkspace
from test_workflow_actions import BrowserSession

DOUBLE = """#!/bin/sh
printf '%s\\n' "$@" > "$DOUBLE_DIR/argv"
cat > "$DOUBLE_DIR/stdin"
printf '%s' "${DOUBLE_TOKEN-unset}" > "$DOUBLE_DIR/token"
if [ -f "$DOUBLE_DIR/hang" ]; then sleep 30 & echo $! > "$DOUBLE_DIR/pid"; wait; fi
cat "$DOUBLE_DIR/reply"
exit "$(cat "$DOUBLE_DIR/exit" 2>/dev/null || echo 0)"
"""
REPLY = "Subject: The review plan is ready\n\nHi,\n\nThe plan for the review is written down.\n\nThanks\n"
DRAFTED = "Draft created! Draft ID: r-4242"


class SlowDraft(FakeWorkspace):
    """workspace-mcp that takes the draft call and answers after the client stopped waiting."""

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if b'"tools/call"' in raw:
            time.sleep(2)
        self.rfile = io.BytesIO(raw)
        try:
            super().do_POST()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client gave up first, which is the point


class DraftCase(BrowserSession):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.double, self.home = root / "double", root / "home"
        self.double.mkdir()
        self.home.mkdir()
        claude = root / "claude"
        claude.write_text(DOUBLE)
        claude.chmod(0o755)
        (self.double / "reply").write_text(REPLY)
        self.gmail = ThreadingHTTPServer(("127.0.0.1", 0), FakeWorkspace)
        self.gmail.daemon_threads = True
        self.gmail.calls, self.gmail.closed, self.gmail.delay = [], [], 0
        self.gmail.tools = {email_draft.TOOL: (DRAFTED, False)}
        threading.Thread(target=self.gmail.serve_forever, daemon=True).start()
        self.addCleanup(self.gmail.server_close)
        self.addCleanup(self.gmail.shutdown)
        environment = patch.dict(os.environ, {"CLAUDE_BIN": str(claude), "DOUBLE_DIR": str(self.double), "HOME": str(self.home),
                                              "SYSTEM_TOOLS_CONFIG": str(root / "config"), "EMAIL_FROM": "operator@example.test",
                                              "WORKSPACE_MCP_URL": f"http://127.0.0.1:{self.gmail.server_address[1]}/mcp"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("DOUBLE_TOKEN", None)

    def tools(self):
        return [(c["params"]["name"], c["params"]["arguments"]) for c in self.gmail.calls if c["method"] == "tools/call"]

    def revision(self, item):
        return workflow.item_state(self.connection, item)["revision"]

    def draft(self, item, **body):
        return self.post(f"/api/items/{item}/draft-email", {"revision": self.revision(item), **body})

    def task(self, title="Plan the review", **columns):
        item = self.item(title, kind=columns.pop("kind", "task"), **columns)
        self.note(item, "Pat wants the plan by Friday.", kind="comment")
        return item

    def last_note(self, item):
        return workflow.item_state(self.connection, item)["notes"][-1]


class TheDraft(DraftCase):
    def test_a_task_saves_a_gmail_draft_and_nothing_sends_it(self):
        item = self.task()
        status, _, state = self.draft(item)
        self.assertEqual(status, 200, state)
        self.assertEqual(self.tools(), [(email_draft.TOOL, {"user_google_email": "operator@example.test",
            "subject": "The review plan is ready", "body": "Hi,\n\nThe plan for the review is written down.\n\nThanks"})])
        self.assertEqual(self.gmail.closed, ["s-1"])
        link = "https://mail.google.com/mail/?authuser=operator%40example.test#drafts"
        self.assertEqual(state["draft"], {"subject": "The review plan is ready", "id": "r-4242", "link": link})
        note = state["notes"][-1]
        self.assertEqual((note["kind"], note["session"]), ("comment", "dashboard"))
        self.assertEqual(note["body"], f'Draft email: saved Gmail draft "The review plan is ready" (draft r-4242); it is not sent. Open it in Drafts: {link}')
        self.assertNotIn("written down", note["body"])
        self.assertEqual(state["revision"], self.revision(item))

    def test_a_followup_fits_too(self):
        self.assertEqual(self.draft(self.task("Answer Pat", kind="followup"))[0], 200)

    def test_the_model_reads_the_item_on_stdin_and_runs_with_no_tools(self):
        item = self.task(title="$(touch pwned) `id`")
        self.assertEqual(self.draft(item)[0], 200)
        argv = (self.double / "argv").read_text().split("\n")[:-1]
        self.assertEqual(argv, ["-p", "--model", "sonnet", "--restricted", "--strict-mcp-config", "--tools", "",
                                "--no-session-persistence"])
        stdin = (self.double / "stdin").read_text()
        self.assertTrue(stdin.startswith(email_draft.PROMPT), stdin)
        self.assertIn(f"Item sd:{item}\nTitle: $(touch pwned) `id`\n", stdin)
        self.assertIn("comment ", stdin)
        self.assertIn("Pat wants the plan by Friday.", stdin)
        self.assertFalse((Path(self.tmp.name) / "pwned").exists())

    def test_the_token_is_sourced_from_env_sh_on_the_same_line(self):
        (self.home / ".config" / "shell").mkdir(parents=True)
        (self.home / ".config" / "shell" / "env.sh").write_text("export DOUBLE_TOKEN=from-env-sh\necho noise\n")
        self.assertEqual(self.draft(self.task())[0], 200)
        self.assertEqual((self.double / "token").read_text(), "from-env-sh")

    def test_the_module_names_no_send_tool(self):
        source = Path(email_draft.__file__).read_text()
        self.assertNotIn("send_gmail", source)
        self.assertEqual(email_draft.TOOL, "draft_gmail_message")


class TheFailures(DraftCase):
    def test_a_model_timeout_leaves_no_draft_kills_the_run_and_records_the_failure(self):
        (self.double / "hang").write_text("")
        item = self.task()
        began = time.monotonic()
        with patch.object(email_draft, "MODEL_TIMEOUT", 1):
            status, _, answer = self.draft(item)
        self.assertLess(time.monotonic() - began, 4)
        self.assertEqual(status, 504)
        said = "Draft email failed: claude -p gave no answer in 1 s. No Gmail draft was saved."
        self.assertEqual(answer["error"], said)
        self.assertEqual(self.tools(), [])
        self.assertEqual(self.last_note(item)["body"], said)
        pid = int((self.double / "pid").read_text())
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"the model's child {pid} outlived the timeout")

    def test_a_reply_without_a_subject_line_saves_nothing(self):
        (self.double / "reply").write_text("Sure! Here is an email you could send.\n")
        item = self.task()
        status, _, answer = self.draft(item)
        self.assertEqual(status, 503)
        self.assertEqual(answer["error"], "Draft email failed: the reply did not start with a Subject line. No Gmail draft was saved.")
        self.assertEqual(self.tools(), [])
        self.assertEqual(self.last_note(item)["body"], answer["error"])

    def test_a_failed_model_run_saves_nothing_and_names_its_last_line(self):
        (self.double / "exit").write_text("1")
        (self.double / "reply").write_text("Not logged in\n")
        item = self.task()
        status, _, answer = self.draft(item)
        self.assertEqual(status, 503)
        self.assertEqual(answer["error"], "Draft email failed: claude -p exited 1: Not logged in. No Gmail draft was saved.")
        self.assertEqual(self.tools(), [])

    def test_a_refused_draft_saved_nothing(self):
        self.gmail.tools = {email_draft.TOOL: ("Gmail API error: insufficient scope", True)}
        item = self.task()
        status, _, answer = self.draft(item)
        self.assertEqual(status, 503)
        self.assertIn("workspace-mcp refused the draft", answer["error"])
        self.assertTrue(answer["error"].endswith("No Gmail draft was saved."))
        self.assertEqual(self.last_note(item)["body"], answer["error"])

    def test_workspace_mcp_down_saved_nothing(self):
        os.environ["WORKSPACE_MCP_URL"] = "http://127.0.0.1:9/mcp"
        item = self.task()
        status, _, answer = self.draft(item)
        self.assertEqual(status, 503)
        self.assertIn("workspace-mcp at http://127.0.0.1:9/mcp", answer["error"])
        self.assertTrue(answer["error"].endswith("No Gmail draft was saved."))

    def test_a_draft_call_with_no_answer_says_whether_a_draft_exists_is_not_known(self):
        item = self.task()
        self.gmail.RequestHandlerClass = SlowDraft
        with patch.object(email_draft, "GMAIL_TIMEOUT", 1):
            status, _, answer = self.draft(item)
        self.assertEqual(status, 504)
        self.assertIn('whether a Gmail draft "The review plan is ready" was saved is not known', answer["error"])
        self.assertIn("Look in Gmail Drafts before you press Draft email again.", answer["error"])
        self.assertEqual(self.last_note(item)["body"], answer["error"])

    def test_a_note_that_cannot_be_written_after_the_draft_names_the_draft(self):
        item = self.task()
        with patch.object(workflow, "add_item_note", side_effect=sqlite3.OperationalError("database is locked")):
            status, _, answer = self.draft(item)
        self.assertEqual(status, 503)
        self.assertIn('Gmail draft "The review plan is ready" (draft r-4242) was saved, but the note was not recorded: database is locked', answer["error"])
        self.assertIn(f"Record it with: sd task note {item} '", answer["error"])


class TheRefusals(DraftCase):
    def test_items_it_does_not_fit_run_nothing(self):
        report = self.item("nightly: needs attention", kind="report")
        work = self.item("Port the page", kind="work", repo=self.repo(), path="docs/work/port/prd.md")
        for item, kind in ((report, "report"), (work, "work")):
            with self.subTest(kind=kind):
                before = self.snapshot()
                status, _, answer = self.draft(item)
                self.assertEqual(status, 400)
                self.assertEqual(answer["error"], f"item {item} is a {kind}; Draft email writes about a task or followup")
                self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.double / "argv").exists())
        self.assertEqual(self.gmail.calls, [])

    def test_no_gmail_account_runs_nothing_and_names_the_remedy(self):
        os.environ.pop("EMAIL_FROM")
        item = self.task()
        before = self.snapshot()
        status, _, answer = self.draft(item)
        self.assertEqual(status, 503)
        self.assertIn("Draft email needs the Gmail account.", answer["error"])
        self.assertIn("EMAIL_FROM", answer["error"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.double / "argv").exists())

    def test_a_second_click_from_the_same_page_is_stale_and_drafts_nothing(self):
        item = self.task()
        revision = self.revision(item)
        self.assertEqual(self.post(f"/api/items/{item}/draft-email", {"revision": revision})[0], 200)
        status, _, answer = self.post(f"/api/items/{item}/draft-email", {"revision": revision})
        self.assertEqual(status, 409)
        self.assertTrue(answer["reload"])
        self.assertEqual(len(self.tools()), 1)

    def test_a_bad_body_a_missing_item_and_a_bad_origin_draft_nothing(self):
        item = self.task()
        good = {"revision": self.revision(item)}
        for payload in ({}, {"revision": "x"}, {**good, "to": "pat@example.test"}, {**good, "send": True}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(f"/api/items/{item}/draft-email", payload)[0], 400)
        self.assertEqual(self.post("/api/items/999999/draft-email", good)[0], 404)
        self.assertEqual(self.post(f"/api/items/{item}/draft-email", good, Origin="http://example.test")[0], 403)
        self.assertFalse((self.double / "argv").exists())
        self.assertEqual(self.gmail.calls, [])
