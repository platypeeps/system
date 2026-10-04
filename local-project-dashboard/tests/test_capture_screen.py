"""Capture types, parent identity and inactive fields on disposable rows."""

import html
import re
from html.parser import HTMLParser
from unittest.mock import patch

from sd_db import reads
from sd_dashboard import controls

from support import ScreenCase


class Fields(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.fields = {}
        self.labels = {}
        self.options = {}
        self.select = None
        self.option = None
        self.label = None
        self.feed(markup)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag in ("input", "select", "textarea"):
            self.fields[attributes.get("name")] = attributes
        if tag == "select":
            self.select = attributes["name"]
            self.options[self.select] = []
        if tag == "option":
            self.option = {"attributes": attributes, "text": ""}
        if tag == "label":
            self.label = attributes.get("for")
            self.labels[self.label] = ""

    def handle_data(self, text):
        if self.option is not None:
            self.option["text"] += text
        if self.label is not None:
            self.labels[self.label] += text

    def handle_endtag(self, tag):
        if tag == "option":
            self.options[self.select].append(self.option)
            self.option = None
        if tag == "select":
            self.select = None
        if tag == "label":
            self.label = None


class CaptureScreen(ScreenCase):
    def panel(self, path):
        page = self.render(path)
        return re.search(r'<section[^>]*id="capture"[^>]*>.*?</section>', page).group(0)

    def test_today_and_backlog_start_with_a_task_and_inactive_note_fields(self):
        for path in ("/classic/today", "/classic/backlog"):
            panel = self.panel(path)
            parsed = Fields(panel)
            self.assertIn("<h2>Capture</h2>", panel)
            self.assertNotIn("<h2>Capture a task</h2>", panel)
            self.assertIn("Get it out of your head. Add the details when you need them. CLI: ", panel)
            self.assertIn('sd task add "Title"', html.unescape(panel))
            self.assertNotIn("CLI equivalents", panel)
            options = parsed.options["capture_type"]
            # sd:719 step 6a: the word means one thing per control. "Followup
            # item" files an item, the kind `sd task add --kind followup`
            # creates; "Followup note" attaches a note, as before.
            self.assertEqual([entry["text"] for entry in options],
                             ["Task", "Followup item", "Followup note", "Comment", "Question", "Decision", "Proposal"])
            self.assertEqual([entry["attributes"]["value"] for entry in options],
                             ["task", "followup_item", "followup", "comment", "question", "decision", "proposal"])
            self.assertIn("A task or a followup item stands alone.", panel)
            self.assertEqual([entry["attributes"]["value"] for entry in options if "selected" in entry["attributes"]], ["task"])
            self.assertEqual(parsed.labels["capture-type"], "Type")
            self.assertEqual(parsed.labels["capture-related"], "Related item")
            self.assertEqual(parsed.fields["title"]["maxlength"], "500")
            self.assertIn("required", parsed.fields["title"])
            self.assertNotIn("disabled", parsed.fields["title"])
            self.assertEqual(parsed.fields["body"]["maxlength"], "50000")
            for name in ("body", "related_item", "revision"):
                self.assertIn("disabled", parsed.fields[name])
                self.assertNotIn("required", parsed.fields[name])
            self.assertIn("No related items yet. Capture a task first.", panel)
            self.assertRegex(panel, r'<div[^>]*hidden[^>]*data-capture-note')
            self.assertRegex(panel, r'<button[^>]*data-capture-refresh[^>]*hidden')

    def test_all_parents_are_disambiguated_and_completed_parked_are_explicit(self):
        first_repo, second_repo = self.repo("/repos/first/research"), self.repo("/repos/second/research")
        first = self.item("Shared <title>", kind="task", repo=first_repo, status="planning")
        second = self.item("Shared <title>", kind="work", repo=second_repo, status="done")
        parked = self.item("Parked idea", kind="idea")
        self.connection.execute("UPDATE item SET parked_at = '2026-09-01T12:00:00Z', body = 'PRIVATE BODY' WHERE id = ?", (parked,))
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        with patch.object(reads, "capture_items", wraps=reads.capture_items) as inventory, \
                patch("sd_db.workflow.item_state", side_effect=AssertionError("capture fetched per-item revision")):
            panel = str(controls.capture(self.connection))
        self.assertEqual(inventory.call_count, 1)
        options = Fields(panel).options["related_item"]
        labels = {entry["attributes"]["value"]: entry["text"] for entry in options}
        self.assertEqual(set(labels), {"", str(first), str(second), str(parked)})
        self.assertIn(f"#{first} · Shared <title> · task · {first_repo} [planning]", labels[str(first)])
        self.assertIn(f"#{second} · Shared <title> · work · {second_repo} [completed]", labels[str(second)])
        self.assertIn("parked]", labels[str(parked)])
        self.assertIn("Shared &lt;title&gt;", panel)
        self.assertNotIn("PRIVATE BODY", panel)
        self.assertNotIn("Shared <title>", panel)
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_existing_item_note_command_names_its_default_kind(self):
        item = self.item("Existing note parent")
        panel = str(controls.note_capture(self.connection, item))
        self.assertIn(f'data-cli="sd task note {item} --kind comment --body TEXT"', panel)

    def test_the_script_tells_the_followup_item_from_the_followup_note(self):
        from pathlib import Path

        from sd_dashboard import server

        script = (Path(server.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn('"followup_item"', script)
        self.assertIn('var noteKinds = ["followup", "comment", "question", "decision", "proposal"]', script)
        self.assertIn("sd task add --kind followup", script)
        self.assertIn('"Add followup item"', script)
