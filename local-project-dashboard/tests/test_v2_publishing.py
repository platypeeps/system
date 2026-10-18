"""Publishing (sd:5102): the published-copies read, the Publish to designation, its queue request, Publish now's drain,
and the row icons on Briefs and Documents.

Nothing here calls Notion, Drive or an MCP server: `sd-research-kit publish` is a stand-in runner, and the drain's claude
is a shell script that plays a drain's outcomes on the queue.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import repos
from sd_dashboard import documents_screen, publish_drain, publishing

import test_v2_briefs as brief_tests
import test_v2_documents as document_tests
from support import NOW, ScreenCase

CONF = '''DOCS = [
    dict(
        src="notes/PLAN.md",
        out="plan",
        item=7,
    ),
    {"src": "notes/IDEA.md", "out": "idea", "notion": dict(team=True), "drive": None},
]
'''
NOTION_URL = "https://www.notion.so/example-page-0123"
DRIVE_URL = "https://docs.google.com/document/d/example-doc/edit"


def git_checkout(path: Path, conf: str = CONF) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, stdin=subprocess.DEVNULL)
    (path / "research.conf.py").write_text(conf, encoding="utf-8")
    return path


class AddKey(unittest.TestCase):
    """`add_key` adds one designation key to a literal entry, or refuses with nothing changed."""

    def test_a_dict_call_entry_gains_the_key_on_its_own_line(self):
        result = publishing.add_key(CONF, "plan", "notion")
        self.assertIn('        item=7,\n        notion=dict(),\n    ),', result)
        self.assertEqual(publishing.choices_of(result)["plan"], {"notion": True, "drive": False})

    def test_a_brace_entry_gains_a_quoted_key(self):
        source = 'DOCS = [{"src": "a.md", "out": "a"}]\n'
        self.assertEqual(publishing.add_key(source, "a", "drive"), 'DOCS = [{"src": "a.md", "out": "a", "drive": {}}]\n')

    def test_what_it_cannot_do_safely_is_refused(self):
        for source, out, channel, said in (
                ("DOCS = make()\n", "plan", "notion", "builds DOCS in code"),
                (CONF, "plan", "obsidian", "not a destination a document designates"),
                (CONF, "missing", "notion", "no literal entry with out='missing'"),
                (CONF, "idea", "notion", "already carries notion="),
                (CONF, "idea", "drive", "switched off"),
                ("DOCS = [\n", "plan", "notion", "does not parse")):
            with self.subTest(said=said), self.assertRaisesRegex(publishing.Refused, said):
                publishing.add_key(source, out, channel)

    def test_choices_read_the_keys_without_running_the_file(self):
        self.assertEqual(publishing.choices_of(CONF), {"plan": {"notion": False, "drive": False},
                                                       "idea": {"notion": True, "drive": False}})


class Designate(unittest.TestCase):
    """`designate`: the trust check, the one-key write, the trust record, then `sd-research-kit publish`."""

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.checkout = git_checkout(self.base / "lab")
        self.conf = self.checkout / "research.conf.py"
        self.kit = publishing.kit()
        self.calls = []

    def trust(self):
        self.kit.trust_conf(self.checkout, self.conf.read_bytes())

    def runner(self, code=0, said="mirror: queued plan -> notion private folder, page lab\n"):
        def run(argv, **options):
            self.calls.append((argv, options))
            return subprocess.CompletedProcess(argv, code, said, "")
        return run

    def test_an_untrusted_conf_is_refused_and_left_as_it_was(self):
        with self.assertRaisesRegex(publishing.Refused, "sd-research-kit render"):
            publishing.designate(self.checkout, "plan", "notion", runner=self.runner())
        self.assertEqual(self.conf.read_text(encoding="utf-8"), CONF)
        self.assertEqual(self.calls, [])

    def test_a_trusted_conf_gains_the_key_is_trusted_again_and_publishes(self):
        self.trust()
        with mock.patch.dict(os.environ, {"SD_PUBLISH_FROM_WORKTREE": "1", "SD_MIRROR_DRY_RUN": "1"}):
            answer = publishing.designate(self.checkout, "plan", "notion", runner=self.runner())
        self.assertEqual((answer["designated"], answer["already"], answer["queued"], answer["exit"]), ("notion", False, True, 0))
        after = self.conf.read_bytes()
        self.assertIn(b"notion=dict()", after)
        self.assertTrue(self.kit.conf_trusted(self.checkout, after))
        argv, options = self.calls[0]
        self.assertEqual((argv[-2].rsplit("/", 1)[1], argv[-1], options["cwd"]), ("sd-research-kit", "publish", str(self.checkout)))
        self.assertEqual(options["env"]["SD_RESEARCH_TRIGGER"], "dashboard")
        self.assertNotIn("SD_PUBLISH_FROM_WORKTREE", options["env"])
        self.assertNotIn("SD_MIRROR_DRY_RUN", options["env"])
        self.assertEqual(options["timeout"], publishing.PUBLISH_SECONDS)

    def test_the_file_keeps_its_mode(self):
        self.conf.chmod(0o640)
        self.trust()
        publishing.designate(self.checkout, "plan", "drive", runner=self.runner())
        self.assertEqual(stat.S_IMODE(self.conf.stat().st_mode), 0o640)

    def test_a_conf_changed_while_read_is_refused(self):
        self.trust()
        real = publishing.add_key

        def racing(source, out, channel):
            self.conf.write_text(CONF + "# edited\n", encoding="utf-8")
            return real(source, out, channel)

        with mock.patch.object(publishing, "add_key", racing), self.assertRaisesRegex(publishing.Refused, "changed while"):
            publishing.designate(self.checkout, "plan", "notion", runner=self.runner())
        self.assertEqual(self.conf.read_text(encoding="utf-8"), CONF + "# edited\n")
        self.assertEqual(self.calls, [])

    def test_a_stop_before_the_trust_record_leaves_an_untrusted_conf(self):
        self.trust()
        with mock.patch.object(self.kit, "trust_conf", side_effect=RuntimeError("stopped")), self.assertRaises(RuntimeError):
            publishing.designate(self.checkout, "plan", "notion", runner=self.runner())
        self.assertIn("notion=dict()", self.conf.read_text(encoding="utf-8"))
        # The next tick refuses until a render by hand vouches for the file, and queues nothing meanwhile.
        with self.assertRaisesRegex(publishing.Refused, "sd-research-kit render"):
            publishing.designate(self.checkout, "plan", "notion", runner=self.runner())
        self.assertEqual(self.calls, [])

    def test_a_failed_publish_runs_again_on_the_next_tick(self):
        self.trust()
        first = publishing.designate(self.checkout, "plan", "notion", runner=self.runner(1, "render failed\n"))
        self.assertEqual((first["queued"], first["exit"], first["lines"]), (False, 1, ["render failed"]))
        again = publishing.designate(self.checkout, "plan", "notion", runner=self.runner())
        self.assertEqual((again["already"], again["queued"]), (True, True))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.conf.read_text(encoding="utf-8").count("notion=dict()"), 1)

    def test_a_publish_with_no_answer_is_not_known(self):
        self.trust()

        def hung(argv, **options):
            raise subprocess.TimeoutExpired(argv, options["timeout"])

        answer = publishing.designate(self.checkout, "plan", "notion", runner=hung)
        self.assertEqual((answer["queued"], answer["exit"]), (False, 124))
        self.assertIn("whether it queued is not known", answer["lines"][0])


class Request(unittest.TestCase):
    """POST /api/documents/publish: `{root, file, channel}` naming a research document the page serves."""

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        document_tests.fixture(self.base)
        git_checkout(self.base / "lab", 'DOCS = [{"src": "notes/PLAN.md", "out": "plan"}]\n')
        self.conf = self.base / "none.conf"

    def ask(self, **values):
        return publishing.request(values, config_path=self.conf, repo_root=self.base)

    def test_a_tick_designates_and_queues_through_publish(self):
        checkout = self.base / "lab"
        publishing.kit().trust_conf(checkout, (checkout / "research.conf.py").read_bytes())
        write = self.ask(root="lab", file="plan.html", channel="drive")
        with mock.patch.object(publishing, "_publish", return_value=(0, ["mirror: queued plan -> drive Briefs/lab"])) as ran:
            answer = write(None)
        ran.assert_called_once()
        self.assertEqual(ran.call_args.args[0], checkout)
        self.assertTrue(answer["queued"])
        self.assertIn('"drive": {}', (checkout / "research.conf.py").read_text(encoding="utf-8"))

    def test_an_untrusted_conf_answers_its_reason(self):
        with self.assertRaisesRegex(ValueError, "sd-research-kit render"):
            self.ask(root="lab", file="plan.html", channel="notion")(None)

    def test_what_names_no_research_document_is_refused(self):
        for values, said in (({"root": "lab", "file": "plan.html", "channel": "obsidian"}, "channels setting"),
                             ({"root": "lab", "file": "plan.html"}, "root and file"),
                             ({"root": "lab", "file": "nope.html", "channel": "notion"}, "is served"),
                             ({"root": "civic", "file": "brief.html", "channel": "notion"}, "no research.conf.py"),
                             ({"root": "lab", "file": "overview.html", "channel": "notion"}, "no single literal entry")):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, said):
                publishing.request(values, config_path=self.conf, repo_root=self.base)


class Copies(ScreenCase):
    """The published-icons read: queue, receipt, item fields and the vault copy, each tied to this checkout."""

    def setUp(self):
        super().setUp()
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.checkout = self.base / "repos" / "lab"
        self.checkout.mkdir(parents=True)
        self.queue, self.receipts, self.vault = self.base / "queue", self.base / "receipts", self.base / "Vault"
        for folder in (self.queue, self.receipts):
            folder.mkdir()

    def put(self, folder, channel, repo=None, **fields):
        (folder / f"lab.plan.{channel}.json").write_text(json.dumps({"repo": str(repo or self.checkout), **fields}),
                                                         encoding="utf-8")

    def copies(self, setting=None, unread="", fields=None):
        return publishing.document_copies(self.checkout, "plan", setting=setting, unread=unread, fields=fields,
                                          vault=self.vault, queue=self.queue, receipts=self.receipts)

    def test_a_delivered_receipt_is_a_published_link(self):
        self.put(self.receipts, "notion", delivered="f1", url=NOTION_URL)
        self.assertEqual(self.copies()["copies"], [{"channel": "notion", "url": NOTION_URL, "state": "published", "title": "Notion"}])

    def test_a_queued_request_shows_dimmed_with_the_last_url(self):
        self.put(self.receipts, "drive", delivered="f1", url=DRIVE_URL)
        self.put(self.queue, "drive", fingerprint="f2")
        self.put(self.queue, "notion", fingerprint="f3")
        self.assertEqual(self.copies()["copies"], [
            {"channel": "drive", "url": DRIVE_URL, "state": "queued", "title": "Google Drive: queued, not yet drained"},
            {"channel": "notion", "url": None, "state": "queued", "title": "Notion: queued, not yet drained"}])

    def test_another_checkout_of_the_same_name_is_not_this_one(self):
        other = self.base / "elsewhere" / "lab"
        self.put(self.receipts, "notion", repo=other, delivered="f1", url=NOTION_URL)
        self.put(self.queue, "drive", repo=other, fingerprint="f2")
        self.assertEqual(self.copies()["copies"], [])

    def test_the_vault_copy_follows_the_channels_folder(self):
        (self.vault / "Briefs" / "lab").mkdir(parents=True)
        (self.vault / "Briefs" / "lab" / "plan.md").write_text("# plan\n", encoding="utf-8")
        self.assertEqual(self.copies()["copies"][0]["url"], "obsidian://open?vault=Vault&file=Briefs%2Flab%2Fplan")
        # A setting naming another folder looks there; one without obsidian has no vault copy.
        self.assertEqual(self.copies(setting={"obsidian": {"folder": "Work/lab"}})["copies"], [])
        self.assertEqual(self.copies(setting={"notion": {}})["copies"], [])

    def test_an_unread_setting_leaves_obsidian_out_and_says_why(self):
        (self.vault / "Briefs" / "lab").mkdir(parents=True)
        (self.vault / "Briefs" / "lab" / "plan.md").write_text("# plan\n", encoding="utf-8")
        found = self.copies(unread="the channels setting of lab was not read: hub down")
        self.assertEqual((found["copies"], found["unread"]), ([], "the channels setting of lab was not read: hub down"))

    def test_item_fields_name_copies_and_a_link_it_may_not_open_is_dropped(self):
        fields = {"lab::plan": {"notion": NOTION_URL, "drive": "javascript:alert(1)"}, "lab::idea": {"drive": DRIVE_URL}}
        self.assertEqual(self.copies(fields=fields)["copies"], [{"channel": "notion", "url": NOTION_URL, "state": "published", "title": "Notion"}])

    def test_a_brief_row_reads_its_item_fields_and_its_vault_link(self):
        found = publishing.brief_copies({"system::brief": {"drive": DRIVE_URL}}, "obsidian://open?vault=V&file=Briefs%2Fx")
        self.assertEqual([(c["channel"], c["url"]) for c in found["copies"]],
                         [("drive", DRIVE_URL), ("obsidian", "obsidian://open?vault=V&file=Briefs%2Fx")])

    def test_documents_rows_carry_pub_with_the_choice(self):
        document_tests.fixture(self.base)
        lab = self.base / "lab"
        (lab / "research.conf.py").write_text('DOCS = [{"src": "notes/PLAN.md", "out": "plan", "drive": {}}]\n', encoding="utf-8")
        self.repo(str(lab))
        repos.set_channels(self.connection, str(lab), {"obsidian": {}, "drive": {}})
        (self.queue / "lab.plan.drive.json").write_text(json.dumps({"repo": str(lab), "fingerprint": "f"}), encoding="utf-8")
        kit = publishing.kit()
        with mock.patch.object(kit, "QUEUE", self.queue), mock.patch.object(publishing, "_vault_name", return_value=self.vault):
            doc = documents_screen.document(self.connection, now=NOW, config_path=self.base / "none.conf", repo_root=self.base)
        rows = {f"{r['key']}/{r['file']}": r for r in doc["documents"]}
        pub = rows["lab/plan.html"]["pub"]
        self.assertEqual(pub["choice"], {"notion": False, "drive": True, "obsidian": True, "why": ""})
        self.assertEqual([(c["channel"], c["state"]) for c in pub["copies"]], [("drive", "queued")])
        self.assertIsNone(rows["civic/brief.html"]["pub"])

    def test_documents_rows_without_a_connection_say_the_setting_was_not_read(self):
        document_tests.fixture(self.base)
        doc = documents_screen.document(now=NOW, config_path=self.base / "none.conf", repo_root=self.base)
        pub = {f"{r['key']}/{r['file']}": r for r in doc["documents"]}["lab/plan.html"]["pub"]
        self.assertIsNone(pub["choice"]["obsidian"])
        self.assertIn("no database connection", pub["unread"])


class BriefRows(brief_tests.BriefCase):
    """Briefs: each row's `pub` from its item's `fields.published`, and the icons the row draws."""

    run_page = brief_tests.TheScript.run_page
    STEM = "2026-09-06 - Intel Brief"

    def publish(self):
        item = self.ids[self.STEM]
        self.connection.execute("UPDATE item SET fields = json_set(COALESCE(fields, '{}'), '$.published', json(?)) WHERE id = ?",
                                (json.dumps({f"system::{self.STEM}": {"notion": NOTION_URL}}), item))
        self.connection.commit()

    def test_a_brief_published_to_notion_carries_the_copy(self):
        self.publish()
        brief = next(b for b in self.doc()["briefs"] if b["id"] == self.STEM)
        self.assertEqual(brief["pub"]["copies"][0], {"channel": "notion", "url": NOTION_URL, "state": "published", "title": "Notion"})
        self.assertEqual(brief["pub"]["unread"], "")

    def test_fields_not_read_are_unknown(self):
        with mock.patch.object(publishing, "item_fields", side_effect=OSError("disk")):
            brief = self.doc()["briefs"][0]
        self.assertIn("was not read: disk", brief["pub"]["unread"])

    def test_the_row_draws_one_link_per_copy_and_a_dimmed_queued_one(self):
        self.page_doc = self.doc()
        self.page_doc["briefs"][1]["pub"] = {"unread": "", "copies": [
            {"channel": "drive", "url": None, "state": "queued", "title": "Google Drive: queued, not yet drained"},
            {"channel": "notion", "url": NOTION_URL, "state": "published", "title": "Notion"}]}
        rows = self.run_page("R.rows = ELS.rows.html;")["R"]["rows"]
        self.assertIn('<span class="pub-drive queued" role="img" aria-label="Google Drive: queued, not yet drained" '
                      'title="Google Drive: queued, not yet drained"><svg class="i" aria-hidden="true"><use href="#i-triangle"/></svg></span>', rows)
        self.assertIn(f'<a class="pub-notion" href="{NOTION_URL}" target="_blank" rel="noopener noreferrer" aria-label="Open in Notion" '
                      'title="Notion"><svg class="i" aria-hidden="true"><use href="#i-box"/></svg></a>', rows)


class BriefDesignation(brief_tests.BriefCase):
    """Publish to on a brief: `fields.publish.<channel>` on its item, then the pack's `enqueue` (ruling note #23223)."""

    STEM = "2026-09-06 - Intel Brief"

    def setUp(self):
        super().setUp()
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.checkout = self.base / "system"
        self.checkout.mkdir()
        self.repo(str(self.checkout))
        self.brief = self.ids[self.STEM]
        self.connection.execute("UPDATE item SET repo = ? WHERE id = ?", (str(self.checkout), self.brief))
        self.connection.commit()
        self.enterContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state")}))
        self.queued = []

    def enqueue(self, repo, docs, channels):
        self.queued.append((repo, docs, channels))
        return [f"mirror: queued {docs[0]['out']} -> notion private folder, page system"]

    def fields(self):
        return json.loads(self.connection.execute("SELECT fields FROM item WHERE id = ?", (self.brief,)).fetchone()[0] or "{}")

    def test_a_tick_writes_the_designation_and_queues_the_stored_markdown(self):
        answer = publishing.designate_brief(self.connection, self.brief, "notion", enqueue=self.enqueue)
        self.assertEqual((answer["already"], answer["queued"]), (False, True))
        self.assertEqual(self.fields()["publish"], {"notion": {}})
        repo, [entry], channels = self.queued[0]
        self.assertEqual((repo, entry["out"], entry["item"], entry["notion"], channels), (self.checkout, self.STEM, self.brief, {}, None))
        self.assertIn("Three items worth reading today.", Path(entry["src"]).read_text(encoding="utf-8"))
        self.assertTrue(entry["src"].startswith(str(self.base / "state")))

    def test_a_second_tick_queues_again_and_pins_the_published_copy(self):
        publishing.designate_brief(self.connection, self.brief, "notion", enqueue=self.enqueue)
        fields = {**self.fields(), "published": {f"system::{self.STEM}": {"notion": NOTION_URL}}}
        self.connection.execute("UPDATE item SET fields = ? WHERE id = ?", (json.dumps(fields), self.brief))
        self.connection.commit()
        answer = publishing.designate_brief(self.connection, self.brief, "notion", enqueue=self.enqueue)
        self.assertTrue(answer["already"])
        self.assertEqual(self.queued[1][1][0]["notion"], {"page": NOTION_URL})

    def test_an_enqueue_that_fails_keeps_the_designation_for_the_next_tick(self):
        answer = publishing.designate_brief(self.connection, self.brief, "drive", enqueue=mock.Mock(side_effect=OSError("disk full")))
        self.assertEqual((answer["queued"], answer["exit"]), (False, 1))
        self.assertIn("disk full", answer["lines"][0])
        self.assertEqual(self.fields()["publish"], {"drive": {}})

    def test_fields_that_are_not_an_object_are_refused_and_kept(self):
        self.connection.execute("UPDATE item SET fields = '[1]' WHERE id = ?", (self.brief,))
        self.connection.commit()
        with self.assertRaisesRegex(publishing.Refused, "not a JSON object"):
            publishing.designate_brief(self.connection, self.brief, "notion", enqueue=self.enqueue)
        raw = self.connection.execute("SELECT fields FROM item WHERE id = ?", (self.brief,)).fetchone()[0]
        self.assertEqual((raw, self.queued), ("[1]", []))

    def test_a_channel_the_repo_leaves_out_or_an_unread_setting_writes_nothing(self):
        repos.set_channels(self.connection, str(self.checkout), {"obsidian": {}})
        with self.assertRaisesRegex(publishing.Refused, "not a channel of system"):
            publishing.designate_brief(self.connection, self.brief, "notion", enqueue=self.enqueue)
        with mock.patch.object(publishing, "read_channels", return_value=(None, "hub down")), \
                self.assertRaisesRegex(publishing.Refused, "hub down; nothing was designated"):
            publishing.designate_brief(self.connection, self.brief, "notion", enqueue=self.enqueue)
        self.assertNotIn("publish", self.fields())
        self.assertEqual(self.queued, [])

    def test_what_is_not_a_brief_is_refused(self):
        task = self.item("A task")
        with self.assertRaisesRegex(publishing.Refused, "not a routine brief"):
            publishing.designate_brief(self.connection, task, "notion", enqueue=self.enqueue)
        self.connection.execute("UPDATE item SET title = '../escape' WHERE id = ?", (self.brief,))
        with self.assertRaisesRegex(publishing.Refused, "no file name"):
            publishing.designate_brief(self.connection, self.brief, "notion", enqueue=self.enqueue)
        self.assertEqual(self.queued, [])
        for values in ({"item": self.brief, "channel": "obsidian"}, {"item": "7", "channel": "notion"}, {"item": self.brief}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                publishing.brief_request(values)

    def test_the_row_carries_the_choice_and_a_queued_request(self):
        queue = self.base / "queue"
        queue.mkdir()
        (queue / f"system.{self.STEM}.drive.json").write_text(json.dumps({"repo": str(self.checkout)}), encoding="utf-8")
        publishing.designate_brief(self.connection, self.brief, "drive", enqueue=self.enqueue)
        with mock.patch.object(publishing.kit(), "QUEUE", queue):
            brief = next(b for b in self.doc()["briefs"] if b["id"] == self.STEM)
        self.assertEqual(brief["pub"]["choice"], {"notion": False, "drive": True, "obsidian": True, "why": ""})
        self.assertEqual([(c["channel"], c["state"]) for c in brief["pub"]["copies"]], [("drive", "queued")])


class DocumentRows(ScreenCase):
    """Documents: the icons in a row, and the Publish to pane in Details."""

    run_page = document_tests.TheScript.run_page

    def setUp(self):
        super().setUp()
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        document_tests.fixture(base)
        self.doc = documents_screen.document(now=NOW, config_path=base / "none.conf", repo_root=base)
        for root in self.doc["roots"]:
            root["path"] = f"~/repos/{root['key']}/docs/dashboard"
        self.doc["store"] = {"available": True, "reason": ""}

    def plan(self, pub):
        for row in self.doc["documents"]:
            if row["file"] == "plan.html":
                row["pub"] = pub

    def test_the_row_draws_icons_dims_a_queued_one_and_opens_no_unsafe_link(self):
        self.plan({"unread": "", "choice": {"notion": True, "drive": False, "obsidian": True, "why": ""}, "copies": [
            {"channel": "drive", "url": "javascript:alert(1)", "state": "published", "title": "Google Drive"},
            {"channel": "notion", "url": NOTION_URL, "state": "queued", "title": "Notion: queued, not yet drained"},
            {"channel": "obsidian", "url": "obsidian://open?vault=V&file=Briefs%2Flab%2Fplan", "state": "published", "title": "Obsidian"}]})
        rows = self.run_page("R.rows = ELS.rows.html;")["R"]["rows"]
        self.assertIn(f'<a class="pub-notion queued" href="{NOTION_URL}" target="_blank" rel="noopener noreferrer" '
                      'aria-label="Open in Notion: queued, not yet drained" title="Notion: queued, not yet drained">', rows)
        self.assertIn('<a class="pub-obsidian" href="obsidian://open?vault=V&amp;file=Briefs%2Flab%2Fplan" target="_blank"', rows)
        self.assertNotIn("javascript:", rows)
        self.assertIn('<span class="pub-drive" role="img" aria-label="Google Drive"', rows)

    def test_details_shows_publish_to_with_obsidian_from_the_repo_setting(self):
        self.plan({"unread": "", "copies": [], "choice": {"notion": True, "drive": False, "obsidian": False, "why": ""}})
        out = self.run_page("""R.det = ELS.details.html;
R.off = ['drive', 'notion', 'obsidian'].map(ch => { const o = OBJ.get(`pub:lab/plan.html:${ch}`); return REG.find(c => c.id === 'document.publish').when(o); });
R.now = REG.find(c => c.id === 'document.publish-now').when(OBJ.get('lab/plan.html'));""", row="'lab/plan.html'")
        det = out["R"]["det"]
        self.assertIn('<h3>Publish to</h3>', det)
        self.assertIn('aria-checked="false" data-cmd="document.publish" data-obj="pub:lab/plan.html:drive"', det)
        self.assertIn('aria-checked="true" data-cmd="document.publish" data-obj="pub:lab/plan.html:notion"', det)
        self.assertIn('aria-checked="false" data-cmd="document.publish" data-obj="pub:lab/plan.html:obsidian"', det)
        self.assertEqual(out["R"]["off"][0], True)
        self.assertIn("remove the designation by hand", out["R"]["off"][1])
        self.assertIn("Management > Publishing", out["R"]["off"][2])
        self.assertIn("nothing is queued", out["R"]["now"])


class Drain(unittest.TestCase):
    """Publish now: the lock, the run state, the runner and the outcome read from the queue."""

    FAKE = r"""#!/bin/sh
# A stand-in for claude: records its arguments and prompt, then plays the drain outcome FAKE_MODE names.
printf '%s\n' "$@" > "$FAKE_DIR/argv"; cat > "$FAKE_DIR/prompt"
case "$FAKE_MODE" in
  deliver|newer)
    printf '{"delivered": "f1", "url": "%s"}' "$FAKE_URL" > "$FAKE_RECEIPTS/lab.plan.notion.json"
    rm -f "$FAKE_QUEUE/lab.plan.notion.json"
    if [ "$FAKE_MODE" = newer ]; then printf '{"repo": "%s", "fingerprint": "f2"}' "$FAKE_REPO" > "$FAKE_QUEUE/lab.plan.notion.json"; fi
    echo "lab.plan.notion.json: delivered $FAKE_URL" ;;
  leave) echo "lab.plan.notion.json: left: two pages carry this title"; exit 1 ;;
  sleep) sleep 30 ;;
esac
"""

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.checkout = git_checkout(self.base / "lab", 'DOCS = [{"src": "notes/PLAN.md", "out": "plan", "notion": {}}]\n')
        self.queue = self.base / "pending-mirror-syncs"
        self.queue.mkdir()
        self.receipts = self.base / "pending-mirror-syncs-receipts"
        self.receipts.mkdir()
        fake = self.base / "claude"
        fake.write_text(self.FAKE, encoding="utf-8")
        fake.chmod(0o755)
        self.enterContext(mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.base / "state"), "HOME": str(self.base / "home"), "CLAUDE_BIN": str(fake),
            "FAKE_DIR": str(self.base), "FAKE_QUEUE": str(self.queue), "FAKE_RECEIPTS": str(self.receipts),
            "FAKE_REPO": str(self.checkout), "FAKE_URL": NOTION_URL, "FAKE_MODE": "deliver"}))
        self.enterContext(mock.patch.object(publishing.kit(), "QUEUE", self.queue))
        self.spawned = []

    def request(self, fingerprint="f1"):
        (self.queue / "lab.plan.notion.json").write_text(json.dumps({"repo": str(self.checkout), "fingerprint": fingerprint}),
                                                          encoding="utf-8")

    def spawn(self, argv, **options):
        self.spawned.append((argv, options))
        return mock.Mock(wait=lambda: 0)

    def started(self):
        status, run = publish_drain.start(None, self.checkout, "plan", spawn=self.spawn)
        self.assertEqual(status, 202, run)
        return publish_drain.state_dir() / "runs" / f"{run['id']}.json"

    def ran(self, mode="deliver", timeout=None):
        record = self.started()
        if timeout:
            document = json.loads(record.read_text(encoding="utf-8"))
            record.write_text(json.dumps({**document, "timeout": timeout}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FAKE_MODE": mode}):
            code = publish_drain.run(record)
        return code, json.loads(record.read_text(encoding="utf-8"))

    def test_nothing_queued_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Nothing is queued for plan"):
            publish_drain.start(None, self.checkout, "plan", spawn=self.spawn)
        self.assertEqual(self.spawned, [])

    def test_a_request_of_another_checkout_is_not_drained_here(self):
        (self.queue / "lab.plan.notion.json").write_text(json.dumps({"repo": "/elsewhere/lab", "fingerprint": "f1"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Nothing is queued"):
            publish_drain.start(None, self.checkout, "plan", spawn=self.spawn)

    def test_a_second_start_while_one_runs_is_refused(self):
        self.request()
        state = publish_drain.state_dir()
        state.mkdir(parents=True)
        with open(state / "lock", "a+", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.write("drain-plan-20261018T000000Z")
            held.flush()
            status, answer = publish_drain.start(None, self.checkout, "plan", spawn=self.spawn)
        self.assertEqual(status, 409)
        self.assertIn("drain-plan-20261018T000000Z", answer["error"])
        self.assertEqual(self.spawned, [])

    def test_a_runner_that_does_not_start_reads_failed(self):
        self.request()

        def broken(argv, **options):
            raise OSError("no python")

        with self.assertRaises(OSError):
            publish_drain.start(None, self.checkout, "plan", spawn=broken)
        [record] = (publish_drain.state_dir() / "runs").glob("*.json")
        document = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual((document["state"], document["reason"]), ("failed", "the runner did not start: no python"))

    def test_the_runner_holds_the_lock_and_claude_gets_only_the_allowlist(self):
        self.request()
        record = self.started()
        argv, options = self.spawned[0]
        self.assertEqual((argv[2:4], options["start_new_session"]), (["run", str(record)], True))
        self.assertEqual(options["pass_fds"], (int(argv[4]),))
        publish_drain.run(record)
        said = (self.base / "argv").read_text(encoding="utf-8").split("\n")
        self.assertIn("--restricted", said)
        self.assertEqual(said[said.index("--tools") + 1], "Read,Edit,Bash")
        allowed = said[said.index("--allowedTools") + 1].split(",")
        self.assertIn("mcp__notion__notion-create-pages", allowed)
        self.assertIn("mcp__workspace-mcp__import_to_google_doc", allowed)
        self.assertIn(f"Edit(/{self.checkout}/research.conf.py)", allowed)
        self.assertFalse([tool for tool in allowed if any(word in tool for word in ("delete", "move", "share", "permission", "trash"))])
        self.assertEqual([tool for tool in allowed if tool.startswith("Bash(")],
                         [f"Bash({publish_drain_bin('sd-db')} repo channels *)", f"Bash({publish_drain_kit()} delivered *)"])
        prompt = (self.base / "prompt").read_text(encoding="utf-8")
        self.assertIn("publication-contract.md", prompt)
        self.assertIn(str(self.queue / "lab.plan.notion.json"), prompt)

    def test_a_brief_drain_skips_step_5_and_may_edit_no_conf(self):
        self.request()
        status, run = publish_drain.start(None, self.checkout, "plan", brief=True, spawn=self.spawn)
        document = json.loads((publish_drain.state_dir() / "runs" / f"{run['id']}.json").read_text(encoding="utf-8"))
        self.assertNotIn("research.conf.py", document["allowed"])
        prompt = Path(document["prompt"]).read_text(encoding="utf-8")
        self.assertIn("Step 5: skip it: this is a routine brief", prompt)

    def test_a_delivered_request_reads_done(self):
        self.request()
        code, document = self.ran("deliver")
        self.assertEqual((code, document["state"], document["reason"]), (0, "done", None))
        self.assertEqual(document["results"], [{"name": "lab.plan.notion.json", "state": "delivered", "url": NOTION_URL, "newer": None}])
        # Claude and the runner are gone, so the lock is free for the next drain.
        self.request("f9")
        self.assertEqual(publish_drain.start(None, self.checkout, "plan", now=datetime.datetime(2026, 10, 18, 1, tzinfo=datetime.timezone.utc),
                                             spawn=self.spawn)[0], 202)

    def test_a_newer_request_after_delivery_is_named(self):
        self.request()
        code, document = self.ran("newer")
        self.assertEqual((code, document["state"]), (0, "done"))
        self.assertIn("the next drain writes it", document["results"][0]["newer"])

    def test_a_request_left_in_the_queue_reads_failed(self):
        self.request()
        # A receipt of an earlier generation is not evidence for this request.
        (self.receipts / "lab.plan.notion.json").write_text(json.dumps({"delivered": "f0", "url": NOTION_URL}), encoding="utf-8")
        code, document = self.ran("leave")
        self.assertEqual((code, document["state"]), (1, "failed"))
        self.assertIn("claude exited 1 (lab.plan.notion.json: left: two pages carry this title)", document["reason"])
        self.assertIn("left in the queue: lab.plan.notion.json", document["reason"])
        self.assertTrue((self.queue / "lab.plan.notion.json").is_file())

    def test_a_drain_past_its_bound_is_killed(self):
        self.request()
        code, document = self.ran("sleep", timeout=1)
        self.assertEqual((code, document["state"]), (1, "failed"))
        self.assertIn("did not end within 1 s; its process group was killed", document["reason"])
        self.assertTrue((self.queue / "lab.plan.notion.json").is_file())

    def test_a_running_drain_whose_runner_is_gone_reads_failed(self):
        self.request()
        record = self.started()
        document = json.loads(record.read_text(encoding="utf-8"))
        dead = subprocess.Popen(["true"])
        dead.wait()
        record.write_text(json.dumps({**document, "state": "running", "pid": dead.pid}), encoding="utf-8")
        status, read = publish_drain.status(document["id"])
        self.assertEqual((status, read["state"]), (200, "failed"))
        self.assertIn("stopped without recording an outcome", read["reason"])
        self.assertNotIn("allowed", read)
        self.assertEqual(publish_drain.status("../etc")[0], 404)


def publish_drain_bin(name: str) -> str:
    from sd_dashboard import pack_bin

    return str(pack_bin() / name)


def publish_drain_kit() -> str:
    import sys

    return f"{sys.executable} {publish_drain_bin('sd-research-kit')}"


if __name__ == "__main__":
    unittest.main()
