"""Import to Slides (pack sd:5525, #25693 option A): a row's built deck goes to Google Slides through workspace-mcp.

workspace-mcp is Notes' `FakeWorkspace` (test_v2_notes), which records every call; no test reaches Google. The work
account is `work@example.test`, as `realm_sweep.held` would read it from the workflow database.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from sd_dashboard import realm_sweep, research_screen, research_slides

from test_research_deck import brief
from test_v2_notes import FakeWorkspace
from test_workflow_actions import BrowserSession

IMPORTED = ("✅ Successfully imported 'alpha: status deck' as Google Slides presentation\n   Presentation ID: p-1\n"
            "   Source format: application/vnd.openxmlformats-officedocument.presentationml.presentation\n   Folder: root\n"
            "   Link: https://docs.google.com/presentation/d/p-1/edit")
WORK = {"google.accounts.work.email": "work@example.test"}


class SlidesCase(BrowserSession):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.workspace = ThreadingHTTPServer(("127.0.0.1", 0), FakeWorkspace)
        self.workspace.daemon_threads = True
        self.workspace.calls, self.workspace.closed, self.workspace.delay = [], [], 0
        self.workspace.tools = {research_slides.TOOL: (IMPORTED, False)}
        threading.Thread(target=self.workspace.serve_forever, daemon=True).start()
        self.addCleanup(self.workspace.server_close)
        self.addCleanup(self.workspace.shutdown)
        self.url = f"http://127.0.0.1:{self.workspace.server_address[1]}/mcp"
        self.enterContext(mock.patch.dict(os.environ, {"REPO_ROOT": str(self.root), "WORKSPACE_MCP_URL": self.url}))
        self.held = self.enterContext(mock.patch.object(realm_sweep, "held", return_value=(dict(WORK), "")))
        super().setUp()
        self.repo = brief(self.root, "research/alpha")
        self.deck = self.repo / research_screen.BUILT
        self.deck.parent.mkdir(parents=True)
        self.deck.write_bytes(b"pptx one")

    def tools(self):
        return [(c["params"]["name"], c["params"]["arguments"]) for c in self.workspace.calls if c["method"] == "tools/call"]

    def press(self, key="research/alpha"):
        status, _, body = self.post("/api/research/slides", {"repo": key})
        return status, body

    def ready(self):
        status, _, body = self.request("/api/research/slides/status", headers={"Cookie": self.cookie})
        return status, json.loads(body)


class TheImport(SlidesCase):
    def test_the_deck_goes_as_base64_with_its_sha256_on_the_work_account_and_the_row_reads_its_link(self):
        status, body = self.press()
        self.assertEqual(status, 200, body)
        self.assertEqual((body["slides"]["id"], body["slides"]["link"], body["reused"]), ("p-1", "https://docs.google.com/presentation/d/p-1/edit", False))
        [(name, arguments)] = self.tools()
        self.assertEqual(name, "import_to_google_slides")
        self.assertEqual(arguments, {"user_google_email": "work@example.test", "file_name": "alpha: status deck", "source_format": "pptx",
                                     "base64_content": base64.b64encode(b"pptx one").decode(), "base64_sha256": hashlib.sha256(b"pptx one").hexdigest()})
        self.assertEqual(len(self.workspace.closed), 1)
        status, read = self.ready()
        self.assertEqual((status, read["ready"], read["why"]), (200, True, ""))
        self.assertEqual(read["imports"]["research/alpha"]["link"], "https://docs.google.com/presentation/d/p-1/edit")
        self.assertEqual(read["imports"]["research/alpha"]["built"], research_screen.experiment(self.repo)["built"])
        self.assertNotIn("account", read["imports"]["research/alpha"])

    def test_a_second_press_on_an_unchanged_deck_reuses_its_slides_file_and_calls_nothing(self):
        self.press()
        status, body = self.press()
        self.assertEqual((status, body["reused"], body["slides"]["id"]), (200, True, "p-1"))
        self.assertEqual(len(self.tools()), 1)

    def test_a_changed_deck_or_another_account_imports_again_and_names_the_file_it_replaces(self):
        self.press()
        self.deck.write_bytes(b"pptx two")
        self.workspace.tools[research_slides.TOOL] = (IMPORTED.replace("p-1", "p-2"), False)
        status, body = self.press()
        self.assertEqual((status, body["slides"]["id"], body["replaces"]["id"]), (200, "p-2", "p-1"))
        self.held.return_value = ({"google.accounts.work.email": "other@example.test"}, "")
        status, body = self.press()
        self.assertEqual((status, body["reused"]), (200, False))
        self.assertEqual([a["user_google_email"] for _, a in self.tools()], ["work@example.test", "work@example.test", "other@example.test"])


class TheRefusals(SlidesCase):
    def test_no_built_deck_or_no_checkout_imports_nothing(self):
        self.deck.unlink()
        for key in ("research/alpha", "research/gone", "../research/alpha"):
            with self.subTest(key=key):
                status, body = self.press(key)
                self.assertEqual(status, 400)
                self.assertIn("has no built deck: press Build deck first", body["error"])
        self.assertEqual(self.workspace.calls, [])

    def test_no_work_account_turns_it_off_and_imports_nothing(self):
        self.held.return_value = ({"google.accounts.work.email": None}, "")
        status, body = self.press()
        self.assertEqual(status, 503)
        self.assertIn("needs the work Google account: the workflow database's google.accounts.work.email setting is unset", body["error"])
        status, read = self.ready()
        self.assertEqual((read["ready"], read["why"]), (False, "no work Google account: the workflow database's google.accounts.work.email setting is unset"))
        self.assertEqual(self.workspace.calls, [])

    def test_workspace_mcp_that_does_not_answer_turns_it_off_and_made_nothing(self):
        self.workspace.shutdown()
        self.workspace.server_close()
        status, read = self.ready()
        self.assertFalse(read["ready"])
        self.assertIn(f"workspace-mcp at {self.url} did not answer", read["why"])
        status, body = self.press()
        self.assertEqual(status, 503)
        self.assertTrue(body["error"].endswith("No Slides file was made."), body)

    def test_a_refused_import_made_nothing_and_an_unclear_answer_says_to_look_in_drive(self):
        self.workspace.tools[research_slides.TOOL] = ("Unsupported source_format", True)
        status, body = self.press()
        self.assertEqual(status, 503)
        self.assertIn("workspace-mcp refused the import", body["error"])
        self.workspace.tools[research_slides.TOOL] = ("Done.", False)
        status, body = self.press()
        self.assertEqual(status, 504)
        self.assertIn("Look in Drive before you press Import to Slides again", body["error"])
        self.assertFalse(research_slides.store().exists())

    def test_an_import_whose_record_is_not_written_names_its_link(self):
        with mock.patch.object(research_slides, "_write", side_effect=OSError("disk full")):
            status, body = self.press()
        self.assertEqual(status, 503)
        self.assertIn("imported as https://docs.google.com/presentation/d/p-1/edit, but its record was not written: disk full", body["error"])

    def test_an_unreadable_record_is_unknown_not_empty(self):
        research_slides.store().parent.mkdir(parents=True, exist_ok=True)
        research_slides.store().write_text("[]", encoding="utf-8")
        status, body = self.press()
        self.assertEqual(status, 503)
        self.assertIn("Nothing was imported", body["error"])
        self.assertEqual(self.ready()[0], 503)
        self.assertEqual(self.tools(), [])

    def test_a_request_names_one_checkout(self):
        for values in ({}, {"repo": 1}, {"repo": "research/alpha", "folder_id": "x"}):
            with self.subTest(values=values):
                status, body = self.post("/api/research/slides", values)[::2]
                self.assertEqual(status, 400)
                self.assertIn("Send", body["error"])
