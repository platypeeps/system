"""The Item page's note capture form on disposable rows, and the form parser other tests read fields with."""

from html.parser import HTMLParser

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


class NoteCapture(ScreenCase):
    """The Item page's note form. The classic Today capture panel was retired in sd:3402; Today and Tasks capture now."""

    def test_existing_item_note_command_names_its_default_kind(self):
        item = self.item("Existing note parent")
        panel = str(controls.note_capture(self.connection, item))
        self.assertIn(f'data-cli="sd task note {item} --kind comment --body TEXT"', panel)
