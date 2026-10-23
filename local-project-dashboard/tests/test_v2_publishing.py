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
import re
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
STATIC = Path(documents_screen.__file__).resolve().parent / "v2" / "static"


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
        self.assertEqual(publishing.choices_of(result)["plan"], {"notion": True, "drive": False, "obsidian": False})

    def test_a_brace_entry_gains_a_quoted_key(self):
        source = 'DOCS = [{"src": "a.md", "out": "a"}]\n'
        self.assertEqual(publishing.add_key(source, "a", "drive"), 'DOCS = [{"src": "a.md", "out": "a", "drive": {}}]\n')

    def test_what_it_cannot_do_safely_is_refused(self):
        for source, out, channel, said in (
                ("DOCS = make()\n", "plan", "notion", "builds DOCS in code"),
                (CONF, "plan", "slack", "not a destination a document designates"),
                (CONF, "missing", "notion", "no literal entry with out='missing'"),
                (CONF, "idea", "notion", "already carries notion="),
                (CONF, "idea", "drive", "switched off"),
                ("DOCS = [\n", "plan", "notion", "does not parse")):
            with self.subTest(said=said), self.assertRaisesRegex(publishing.Refused, said):
                publishing.add_key(source, out, channel)

    def test_choices_read_the_keys_without_running_the_file(self):
        self.assertEqual(publishing.choices_of(CONF), {"plan": {"notion": False, "drive": False, "obsidian": False},
                                                       "idea": {"notion": True, "drive": False, "obsidian": False}})


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
        for values, said in (({"root": "lab", "file": "plan.html", "channel": "slack"}, "notion, drive or obsidian"),
                             ({"root": "lab", "file": "plan.html"}, "root and file"),
                             ({"root": "lab", "file": "nope.html", "channel": "notion"}, "is served"),
                             ({"root": "civic", "file": "brief.html", "channel": "notion"}, "no research.conf.py"),
                             ({"root": "lab", "file": "overview.html", "channel": "notion"}, "no single literal entry")):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, said):
                publishing.request(values, config_path=self.conf, repo_root=self.base)


PINNED = '''DOCS = [
    dict(
        src="notes/PLAN.md",
        out="plan",
        item=7,
        notion=dict(page="0123456789abcdef0123456789abcdef"),
    ),
]
'''


class RemoveKey(unittest.TestCase):
    """`remove_key` drops one designation key from a literal entry, or refuses with nothing changed (sd:5489)."""

    def test_a_dict_call_entry_loses_the_key_and_its_line(self):
        result = publishing.remove_key(PINNED, "plan", "notion")
        self.assertEqual(result, PINNED.replace('\n        notion=dict(page="0123456789abcdef0123456789abcdef"),', ""))
        self.assertEqual(publishing.choices_of(result)["plan"], {"notion": False, "drive": False, "obsidian": False})

    def test_a_brace_entry_loses_its_quoted_key(self):
        source = 'DOCS = [{"src": "a.md", "out": "a", "drive": {}, "item": 3}]\n'
        self.assertEqual(publishing.remove_key(source, "a", "drive"), 'DOCS = [{"src": "a.md", "out": "a", "item": 3}]\n')

    def test_what_is_not_designated_is_refused(self):
        for source, out, channel, said in ((CONF, "plan", "notion", "not designated"), (CONF, "idea", "drive", "not designated"),
                                           (CONF, "plan", "obsidian", "not designated"), (CONF, "plan", "slack", "not a destination"),
                                           ("DOCS = make()\n", "plan", "notion", "in code")):
            with self.subTest(said=said), self.assertRaisesRegex(publishing.Refused, said):
                publishing.remove_key(source, out, channel)


class Undesignate(unittest.TestCase):
    """Unticking a document's destination (sd:5489): the pack queues the trash first, then the key goes."""

    NAME = "lab.plan.notion.json"

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.checkout = git_checkout(self.base / "lab", PINNED)
        self.conf = self.checkout / "research.conf.py"
        self.kit = publishing.kit()
        self.queue = self.base / "pending-mirror-syncs"
        self.enterContext(mock.patch.object(self.kit, "QUEUE", self.queue))

    def trust(self):
        self.kit.trust_conf(self.checkout, self.conf.read_bytes())

    def request(self):
        return json.loads((self.queue / self.NAME).read_text(encoding="utf-8"))

    def test_an_untick_queues_the_trash_then_drops_the_key(self):
        self.trust()
        answer = publishing.undesignate(self.checkout, "plan", "notion")
        self.assertEqual((answer["trash"], answer["dropped"]), ("queued", True))
        after = self.conf.read_bytes()
        self.assertNotIn(b"notion=", after)
        self.assertTrue(self.kit.conf_trusted(self.checkout, after))
        request = self.request()
        self.assertEqual((request["action"], request["page"], request["item"]), ("trash", "0123456789abcdef0123456789abcdef", 7))

    def test_an_untrusted_conf_is_refused_and_nothing_is_queued(self):
        with self.assertRaisesRegex(publishing.Refused, "sd-research-kit render"):
            publishing.undesignate(self.checkout, "plan", "notion")
        self.assertEqual(self.conf.read_text(encoding="utf-8"), PINNED)
        self.assertFalse((self.queue / self.NAME).exists())

    def test_a_pack_without_enqueue_trash_refuses_and_changes_nothing(self):
        self.trust()
        with mock.patch.object(publishing, "kit", return_value=mock.Mock(spec=["conf_trusted", "trust_conf"], conf_trusted=lambda *a: True)), \
                self.assertRaisesRegex(publishing.Refused, "enqueue_trash"):
            publishing.undesignate(self.checkout, "plan", "notion")
        self.assertEqual(self.conf.read_text(encoding="utf-8"), PINNED)

    def test_a_trash_the_pack_refuses_keeps_the_designation(self):
        self.trust()
        self.queue.mkdir()
        (self.queue / self.NAME).write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(publishing.Refused, "not known"):
            publishing.undesignate(self.checkout, "plan", "notion")
        self.assertEqual(self.conf.read_text(encoding="utf-8"), PINNED)

    def test_a_stop_after_the_trash_keeps_the_designation_says_so_and_a_second_untick_finishes(self):
        self.trust()
        with mock.patch.object(publishing, "_write", side_effect=OSError("disk full")):
            answer = publishing.undesignate(self.checkout, "plan", "notion")
        self.assertEqual((answer["trash"], answer["dropped"]), ("queued", False))
        self.assertIn("still designated", answer["lines"][-1])
        self.assertEqual(self.conf.read_text(encoding="utf-8"), PINNED)
        self.assertEqual(self.request()["action"], "trash")
        again = publishing.undesignate(self.checkout, "plan", "notion")
        self.assertEqual((again["trash"], again["dropped"]), ("queued", True))
        self.assertEqual(self.request()["page"], "0123456789abcdef0123456789abcdef")

    def test_nothing_queued_or_delivered_drops_the_key_and_queues_nothing(self):
        self.conf.write_text(PINNED.replace('dict(page="0123456789abcdef0123456789abcdef")', "dict()"), encoding="utf-8")
        self.trust()
        answer = publishing.undesignate(self.checkout, "plan", "notion")
        self.assertEqual((answer["trash"], answer["dropped"]), ("nothing", True))
        self.assertFalse((self.queue / self.NAME).exists())

    def test_the_post_names_a_research_document(self):
        document_tests.fixture(self.base)
        conf = self.base / "lab" / "research.conf.py"
        conf.write_text('DOCS = [{"src": "notes/PLAN.md", "out": "plan", "drive": {}}]\n', encoding="utf-8")
        self.kit.trust_conf(self.base / "lab", conf.read_bytes())
        with self.assertRaisesRegex(ValueError, "notion, drive or obsidian"):
            publishing.unrequest({"root": "lab", "file": "plan.html", "channel": "slack"}, config_path=self.base / "none.conf", repo_root=self.base)
        answer = publishing.unrequest({"root": "lab", "file": "plan.html", "channel": "drive"},
                                      config_path=self.base / "none.conf", repo_root=self.base)(None)
        self.assertEqual((answer["trash"], answer["dropped"]), ("nothing", True))
        self.assertNotIn("drive", conf.read_text(encoding="utf-8"))


OBSIDIAN = '''DOCS = [
    dict(
        src="notes/PLAN.md",
        out="plan",
        item=7,
        obsidian=dict(),
    ),
    {"src": "notes/IDEA.md", "out": "idea"},
]
'''


class ObsidianDocument(unittest.TestCase):
    """sd:5500: outside the vault's own repository Obsidian is a document's `obsidian=` key, ticked and unticked like
    Notion and Drive; the untick moves the vault copy into the vault's .trash through the pack's `trash_obsidian`."""

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.checkout = git_checkout(self.base / "lab", OBSIDIAN)
        self.conf = self.checkout / "research.conf.py"
        self.kit = publishing.kit()
        self.vault = self.base / "Vault"
        self.copy = self.vault / "Briefs" / "lab" / "plan.md"
        self.copy.parent.mkdir(parents=True)
        self.copy.write_text(f"---\nsource_checkout: {self.checkout}\n---\n\n# Plan\n", encoding="utf-8")
        self.enterContext(mock.patch.object(self.kit, "VAULT", str(self.vault)))
        self.enterContext(mock.patch.object(self.kit, "read_channels", return_value=(None, "")))
        self.enterContext(mock.patch.object(self.kit, "_shown", side_effect=str))
        self.enterContext(mock.patch.object(publishing, "_vault_name", return_value=self.vault))
        self.kit.trust_conf(self.checkout, self.conf.read_bytes())

    def test_a_tick_adds_the_key_and_publish_writes_the_vault_copy(self):
        runner = lambda argv, **o: subprocess.CompletedProcess(argv, 0, "obsidian: wrote 2 document(s) to x\n", "")
        answer = publishing.designate(self.checkout, "idea", "obsidian", runner=runner)
        self.assertEqual((answer["designated"], answer["wrote"], answer["queued"]), ("obsidian", True, False))
        self.assertEqual(publishing.choices_of(self.conf.read_text(encoding="utf-8"))["idea"]["obsidian"], True)

    def test_an_untick_moves_the_copy_to_the_vault_trash_then_drops_the_key(self):
        dropped = []
        answer = publishing.undesignate(self.checkout, "plan", "obsidian", drop=lambda: dropped.append(1) or (True, "item: dropped"))
        self.assertEqual((answer["trash"], answer["dropped"]), ("trashed", True))
        self.assertFalse(self.copy.exists())
        self.assertIn("# Plan", (self.vault / ".trash" / "plan.md").read_text(encoding="utf-8"))
        self.assertEqual((dropped, answer["lines"][-1]), ([1], "item: dropped"))
        after = self.conf.read_bytes()
        self.assertNotIn(b"obsidian=", after)
        self.assertTrue(self.kit.conf_trusted(self.checkout, after))

    def test_a_stop_after_the_trash_keeps_the_key_and_a_second_untick_finishes(self):
        with mock.patch.object(publishing, "_write", side_effect=OSError("disk full")):
            answer = publishing.undesignate(self.checkout, "plan", "obsidian")
        self.assertEqual((answer["trash"], answer["dropped"]), ("trashed", False))
        self.assertIn("still designated", answer["lines"][-1])
        self.assertIn("obsidian=dict()", self.conf.read_text(encoding="utf-8"))
        again = publishing.undesignate(self.checkout, "plan", "obsidian")
        self.assertEqual((again["trash"], again["dropped"]), ("nothing", True))
        self.assertEqual([p.name for p in (self.vault / ".trash").iterdir()], ["plan.md"])

    def test_a_url_not_dropped_keeps_the_key_and_a_second_untick_finishes(self):
        """From the adversarial review: a failed URL drop removed the key, so no untick could retry it."""
        answer = publishing.undesignate(self.checkout, "plan", "obsidian", drop=lambda: (False, "item: not dropped (locked)"))
        self.assertEqual((answer["trash"], answer["dropped"]), ("trashed", False))
        self.assertIn("obsidian=dict()", self.conf.read_text(encoding="utf-8"))
        again = publishing.undesignate(self.checkout, "plan", "obsidian", drop=lambda: (True, ""))
        self.assertEqual((again["trash"], again["dropped"]), ("nothing", True))
        self.assertNotIn("obsidian=", self.conf.read_text(encoding="utf-8"))

    def test_the_post_drops_the_items_obsidian_url(self):
        from sd_db import channels, connect
        from sd_db.migrate import initialise
        from sd_db.writes import create_item

        initialise(self.base / "sd.db")
        db = connect(self.base / "sd.db")
        self.addCleanup(db.close)
        for _ in range(7):
            create_item(db, kind="task", title="t")
        channels.record_url(db, 7, str(self.checkout), "plan", "Plan", "obsidian", "obsidian://open?vault=Vault&file=x", "test")
        self.assertEqual(publishing._drop_obsidian(db, 7, self.checkout, "plan"), (True, "item: obsidian link for sd:7 dropped"))
        self.assertEqual(publishing.published_fields(db, [7]), {7: {}})
        self.assertEqual(publishing._drop_obsidian(None, 7, self.checkout, "plan")[0], False)

    def test_another_checkouts_copy_refuses_and_changes_nothing(self):
        self.copy.write_text("---\nsource_checkout: ~/elsewhere/lab\n---\nx\n", encoding="utf-8")
        with self.assertRaisesRegex(publishing.Refused, "not this checkout's copy"):
            publishing.undesignate(self.checkout, "plan", "obsidian")
        self.assertEqual(self.conf.read_text(encoding="utf-8"), OBSIDIAN)

    def test_the_vaults_own_repository_keeps_obsidian_repo_wide(self):
        with mock.patch.object(publishing, "_vault_name", return_value=self.checkout):
            self.assertTrue(publishing.vault_repository(self.checkout))
            with self.assertRaisesRegex(publishing.Refused, "vault itself"):
                publishing.designate(self.checkout, "idea", "obsidian", runner=None)
        self.assertFalse(publishing.vault_repository(self.checkout))

    def test_a_trashed_copy_shows_no_icon_from_a_stale_item_url(self):
        stale = {"x::plan": {"obsidian": "obsidian://open?vault=Vault&file=Briefs%2Flab%2Fplan"}}
        found = publishing.document_copies(self.checkout, "plan", setting=None, unread="", fields=stale, vault=self.vault)
        self.assertEqual([c["channel"] for c in found["copies"]], ["obsidian"])
        self.copy.unlink()
        found = publishing.document_copies(self.checkout, "plan", setting=None, unread="", fields=stale, vault=self.vault)
        self.assertEqual(found["copies"], [])


class SeedObsidian(unittest.TestCase):
    """sd:5500's seed: each entry of a repo whose render wrote every entry into the vault gains `obsidian=dict()`, once."""

    def setUp(self):
        from sd_db import connect
        from sd_db.migrate import initialise
        from sd_db.writes import upsert_repo

        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        initialise(self.base / "sd.db")
        self.db = connect(self.base / "sd.db")
        self.addCleanup(self.db.close)
        self.kit = publishing.kit()
        self.lab = git_checkout(self.base / "lab", CONF)
        self.off = git_checkout(self.base / "off", CONF)
        self.vault = git_checkout(self.base / "Vault", CONF)
        for checkout in (self.lab, self.off, self.vault, self.base / "plain"):
            upsert_repo(self.db, str(checkout))
        repos.set_channels(self.db, self.off, {"notion": {}})
        self.db.commit()
        self.kit.trust_conf(self.lab, (self.lab / "research.conf.py").read_bytes())
        self.enterContext(mock.patch.object(publishing, "_vault_name", return_value=self.vault))

    def seed(self, apply):
        return [line.replace(str(self.base), "{base}") for line in publishing.seed_obsidian(self.db, apply=apply)]

    def test_a_dry_run_names_what_it_would_seed_and_writes_nothing(self):
        said = self.seed(False)
        self.assertIn("{base}/lab: would seed obsidian= on plan (no vault copy now)", said)
        self.assertIn("{base}/lab: would seed obsidian= on idea (no vault copy now)", said)
        self.assertTrue(any(line.startswith("{base}/off: Obsidian is off") for line in said), said)
        self.assertTrue(any(line.startswith("{base}/Vault: the vault's own repository") for line in said), said)
        self.assertEqual((self.lab / "research.conf.py").read_text(encoding="utf-8"), CONF)

    def test_apply_seeds_keeps_the_trust_and_a_rerun_seeds_nothing(self):
        said = self.seed(True)
        self.assertIn("{base}/lab: seeded obsidian= on plan (no vault copy now)", said)
        after = (self.lab / "research.conf.py").read_bytes()
        self.assertEqual(publishing.choices_of(after.decode())["plan"]["obsidian"], True)
        self.assertEqual(publishing.choices_of(after.decode())["idea"]["obsidian"], True)
        self.assertTrue(self.kit.conf_trusted(self.lab, after))
        self.assertEqual((self.off / "research.conf.py").read_text(encoding="utf-8"), CONF)
        self.assertIn("{base}/lab: every entry already designates obsidian=; nothing to seed", self.seed(True))
        self.assertEqual((self.lab / "research.conf.py").read_bytes(), after)

    def test_an_untrusted_conf_is_seeded_and_left_untrusted(self):
        (self.lab / "research.conf.py").write_text(CONF + "# edited\n", encoding="utf-8")
        said = self.seed(True)
        self.assertTrue(any("not trusted before" in line for line in said), said)
        self.assertFalse(self.kit.conf_trusted(self.lab, (self.lab / "research.conf.py").read_bytes()))

    def test_an_unread_setting_seeds_nothing(self):
        with mock.patch.object(publishing, "read_channels", return_value=(None, "hub down")):
            said = self.seed(True)
        self.assertIn("{base}/lab: held, hub down; nothing seeded", said)
        self.assertEqual((self.lab / "research.conf.py").read_text(encoding="utf-8"), CONF)


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

    def test_a_trash_request_shows_queued_for_the_trash_with_the_last_url(self):
        self.put(self.receipts, "notion", delivered="f1", url=NOTION_URL)
        self.put(self.queue, "notion", fingerprint="t1", action="trash")
        self.assertEqual(self.copies()["copies"], [
            {"channel": "notion", "url": NOTION_URL, "state": "queued", "title": "Notion: queued for the trash, not yet drained"}])

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
        # sd:5500: Obsidian is the entry's own key outside the vault's repository, whatever the repo setting says.
        self.assertEqual(pub["choice"], {"notion": False, "drive": True, "obsidian": False, "repo_wide": "", "why": ""})
        self.assertEqual([(c["channel"], c["state"]) for c in pub["copies"]], [("drive", "queued")])
        self.assertIsNone(rows["civic/brief.html"]["pub"])

    def test_documents_rows_without_a_connection_say_the_setting_was_not_read(self):
        document_tests.fixture(self.base)
        doc = documents_screen.document(now=NOW, config_path=self.base / "none.conf", repo_root=self.base)
        pub = {f"{r['key']}/{r['file']}": r for r in doc["documents"]}["lab/plan.html"]["pub"]
        self.assertIs(pub["choice"]["obsidian"], False)
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
        self.assertIn(f'<a class="pub-notion has" href="{NOTION_URL}" target="_blank" rel="noopener noreferrer" aria-label="Open in Notion" '
                      f'title="Notion: {NOTION_URL}"><svg class="i" aria-hidden="true"><use href="#i-box"/></svg></a>', rows)
        # sd:5485: a destination with no copy and no designation still shows, dim, so every destination is on the row.
        self.assertIn('<span class="pub-obsidian off" role="img" aria-label="Obsidian: not a destination" '
                      'title="Obsidian: not a destination"><svg class="i" aria-hidden="true"><use href="#i-gem"/></svg></span>', rows)

    def test_the_legend_under_the_list_names_every_icon_fill(self):
        """The Briefs list carries the destination legend Documents draws (sd:5731): one key per fill, from PUB_STATE."""
        self.page_doc = self.doc()
        out = self.run_page("R.help = String(ELS['list-help'].html); R.pub = Object.values(window.commands.PUB_STATE);")
        keys = re.findall(r'<span class="key"><span class="pub">.*?</svg></span></span> ([^<]+)</span>', out["R"]["help"])
        self.assertEqual(keys, out["R"]["pub"])

    def test_queue_copy_queues_each_designated_destination_that_has_no_copy(self):
        """A row drawn "designated, no copy yet" offers Queue copy, which posts Publish to again for each such destination."""
        self.page_doc = self.doc()
        stem, item = self.page_doc["briefs"][0]["id"], self.page_doc["briefs"][0]["item"]
        self.page_doc["briefs"][0]["pub"] = {"unread": "", "choice": {"notion": True, "drive": True, "obsidian": False, "why": ""},
                                             "copies": [{"channel": "notion", "url": NOTION_URL, "state": "published", "title": "Notion"}]}
        answer = f"""(path, body) => path === '/api/briefs/publish' ? [200, {{ designated: body.channel, already: true, queued: true, exit: 0, lines: [] }}]
  : [200, {json.dumps(self.page_doc)}]"""
        out = self.run_page(f"""R.when = cmd('brief.queue-copy').when(C.get({json.dumps(stem)}));
shellRun(cmd('brief.queue-copy'), C.get({json.dumps(stem)})); await flush();""", answer)
        self.assertIs(out["R"]["when"], True)
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/briefs/publish", {"item": item, "channel": "drive"}]])
        self.assertIn(f"Queued one copy · {stem}; Publish now drains it", [t[0] for t in out["toasts"]])

    def test_queue_copy_rereads_and_names_what_queued_when_a_later_destination_fails(self):
        """sd:5924: Drive queues, then Notion fails; the page reads again, so Publish now is on, and the toast names Drive."""
        self.page_doc = self.doc()
        stem = self.page_doc["briefs"][0]["id"]
        self.page_doc["briefs"][0]["pub"] = {"unread": "", "copies": [],
                                             "choice": {"notion": True, "drive": True, "obsidian": False, "why": ""}}
        after = json.loads(json.dumps(self.page_doc))
        after["briefs"][0]["pub"]["copies"] = [{"channel": "drive", "url": None, "state": "queued", "title": "Google Drive: queued"}]
        toasts = []
        for status, error in ((409, "the brief changed"), (500, "pack crashed")):
            answer = f"""(path, body) => path === '/api/briefs/publish' ? (body.channel === 'drive'
    ? (window.QUEUED = true, [200, {{ designated: 'drive', already: true, queued: true, exit: 0, lines: [] }}])
    : [{status}, {{ error: {json.dumps(error)} }}])
  : [200, window.QUEUED ? {json.dumps(after)} : {json.dumps(self.page_doc)}]"""
            out = self.run_page(f"""const reads = OUT.gets.length;
shellRun(cmd('brief.queue-copy'), C.get({json.dumps(stem)})); await flush();
R.reread = OUT.gets.length > reads; R.now = cmd('brief.publish-now').when(C.get({json.dumps(stem)}));""", answer)
            self.assertEqual([p[1]["channel"] for p in out["posts"]], ["drive", "notion"])
            self.assertEqual((out["R"]["reread"], out["R"]["now"]), (True, True), "no reread, so Publish now stays off")
            toasts += [t[0] for t in out["toasts"]]
        self.assertEqual(toasts, [f"Queued for Google Drive · {stem}; Notion refused: the brief changed",
                                  f"Queued for Google Drive · {stem}; whether Notion queued is not known: pack crashed"])

    def test_queue_copy_is_off_when_nothing_designated_lacks_a_copy_or_a_read_failed(self):
        self.page_doc = self.doc()
        stem = self.page_doc["briefs"][0]["id"]
        whens = []
        for pub in ({"unread": "", "choice": {"notion": True, "drive": False, "obsidian": False, "why": ""},
                     "copies": [{"channel": "notion", "url": None, "state": "queued", "title": "Notion: queued"}]},
                    {"unread": "the pack's mirror queue was not read: x", "copies": [],
                     "choice": {"notion": True, "drive": False, "obsidian": False, "why": ""}}):
            self.page_doc["briefs"][0]["pub"] = pub
            whens.append(self.run_page(f"R.when = cmd('brief.queue-copy').when(C.get({json.dumps(stem)}));")["R"]["when"])
        self.assertEqual(whens, ["every designated destination has its copy or is queued"] * 2)


class BriefDesignation(brief_tests.BriefCase):
    """Publish to on a brief: `fields.publish.<channel>` on its item, then the pack's `enqueue_briefs` (ruling note #23223)."""

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
        self.queued = []

    def enqueue(self, connection, items):
        # The pack's `enqueue_briefs` builds the request from the row; here it records what it was asked to queue.
        self.queued.append((items, json.loads(connection.execute("SELECT fields FROM item WHERE id = ?",
                                                                  (self.brief,)).fetchone()[0])["publish"]))
        return [f"mirror: queued {self.STEM} -> notion private folder, page system"]

    def fields(self):
        return json.loads(self.connection.execute("SELECT fields FROM item WHERE id = ?", (self.brief,)).fetchone()[0] or "{}")

    def test_a_tick_writes_the_designation_then_asks_the_pack_to_queue_the_brief(self):
        answer = publishing.designate_brief(self.connection, self.brief, "notion", enqueue=self.enqueue)
        self.assertEqual((answer["already"], answer["queued"]), (False, True))
        self.assertEqual(self.fields()["publish"], {"notion": {}})
        self.assertEqual(self.queued, [({self.brief}, {"notion": {}})])

    def test_a_second_tick_keeps_the_designation_and_queues_again(self):
        publishing.designate_brief(self.connection, self.brief, "notion", enqueue=self.enqueue)
        answer = publishing.designate_brief(self.connection, self.brief, "notion", enqueue=self.enqueue)
        self.assertTrue(answer["already"])
        self.assertEqual(len(self.queued), 2)

    def test_a_pack_without_enqueue_briefs_says_so_and_keeps_the_designation(self):
        with mock.patch.object(publishing, "kit", return_value=object()):
            answer = publishing.designate_brief(self.connection, self.brief, "drive")
        self.assertEqual((answer["queued"], answer["exit"]), (False, 1))
        self.assertIn("no enqueue_briefs", answer["lines"][0])
        self.assertEqual(self.fields()["publish"], {"drive": {}})

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
        self.assertEqual(brief["pub"]["choice"], {"notion": False, "drive": True, "obsidian": True,
                                                  "repo_wide": publishing.BRIEF_REPO_WIDE, "why": ""})
        self.assertEqual([(c["channel"], c["state"]) for c in brief["pub"]["copies"]], [("drive", "queued")])


class BriefUndesignation(brief_tests.BriefCase):
    """Unticking a brief's destination (sd:5489): the trash request first, with the item's URL, then fields.publish."""

    STEM = BriefDesignation.STEM
    fields = BriefDesignation.fields

    def setUp(self):
        super().setUp()
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.checkout = self.base / "system"
        self.checkout.mkdir()
        self.repo(str(self.checkout))
        self.brief = self.ids[self.STEM]
        self.connection.execute("UPDATE item SET repo = ? WHERE id = ?", (str(self.checkout), self.brief))
        self.queue = self.base / "pending-mirror-syncs"
        self.enterContext(mock.patch.object(publishing.kit(), "QUEUE", self.queue))
        from sd_db import paths

        fields = {"publish": {"notion": {}}, "published": {f"{paths.key(str(self.checkout))}::{self.STEM}": {"title": self.STEM, "notion": NOTION_URL}}}
        self.connection.execute("UPDATE item SET fields = ? WHERE id = ?", (json.dumps(fields), self.brief))
        self.connection.commit()

    def test_an_untick_queues_the_trash_with_the_item_url_then_drops_the_designation(self):
        answer = publishing.undesignate_brief(self.connection, self.brief, "notion")
        self.assertEqual((answer["trash"], answer["dropped"]), ("queued", True))
        self.assertNotIn("publish", self.fields())
        request = json.loads((self.queue / f"system.{self.STEM}.notion.json").read_text(encoding="utf-8"))
        self.assertEqual((request["action"], request["url"], request["item"]), ("trash", NOTION_URL, self.brief))

    def test_only_this_repos_url_is_named_never_another_with_the_same_document(self):
        """Codex review: a suffix match on `::<title>` took another repository's copy."""
        from sd_db import paths

        fields = {"publish": {"notion": {}}, "published": {
            f"/elsewhere/other::{self.STEM}": {"notion": "https://www.notion.so/other-page"},
            f"{paths.key(str(self.checkout))}::{self.STEM}": {"notion": NOTION_URL}}}
        self.connection.execute("UPDATE item SET fields = ? WHERE id = ?", (json.dumps(fields), self.brief))
        self.connection.commit()
        publishing.undesignate_brief(self.connection, self.brief, "notion")
        request = json.loads((self.queue / f"system.{self.STEM}.notion.json").read_text(encoding="utf-8"))
        self.assertEqual(request["url"], NOTION_URL)

    def test_a_document_url_is_read_under_its_own_repo_key(self):
        from sd_db import paths

        published = {7: {"/elsewhere/lab::plan": {"notion": "https://www.notion.so/other-page"},
                         f"{paths.key(str(self.checkout))}::plan": {"notion": NOTION_URL}}}
        self.assertEqual(publishing._document_url(published, self.checkout, "plan", "notion"), NOTION_URL)
        self.assertEqual(publishing._document_url({7: {"/elsewhere/lab::plan": {"notion": NOTION_URL}}}, self.checkout, "plan", "notion"), "")

    def test_a_brief_not_designated_for_the_channel_is_refused(self):
        with self.assertRaisesRegex(publishing.Refused, "not designated for drive"):
            publishing.undesignate_brief(self.connection, self.brief, "drive")
        self.assertFalse(self.queue.exists())

    def test_a_refused_trash_keeps_the_designation(self):
        with mock.patch.object(publishing.kit(), "enqueue_trash", return_value=("", "trash: not queued (disk)")), \
                self.assertRaisesRegex(publishing.Refused, "disk"):
            publishing.undesignate_brief(self.connection, self.brief, "notion")
        self.assertEqual(self.fields()["publish"], {"notion": {}})

    def test_the_post_checks_its_values(self):
        with self.assertRaisesRegex(ValueError, "notion or drive"):
            publishing.brief_unrequest({"item": self.brief, "channel": "obsidian"})
        answer = publishing.brief_unrequest({"item": self.brief, "channel": "notion"})(self.connection)
        self.assertTrue(answer["dropped"])


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
                      f'aria-label="Open in Notion: queued, not yet drained" title="Notion: queued, not yet drained: {NOTION_URL}">', rows)
        self.assertIn('<a class="pub-obsidian has" href="obsidian://open?vault=V&amp;file=Briefs%2Flab%2Fplan" target="_blank"', rows)
        self.assertNotIn("javascript:", rows)
        self.assertIn('<span class="pub-drive has" role="img" aria-label="Google Drive"', rows)

    def test_every_configured_destination_shows_filled_outlined_or_unknown(self):
        """sd:5300: each row shows every destination it is configured for: a copy filled, a designated one with no copy an
        outline, one whose read failed unknown with why; the hover names the destination and its link or vault path."""
        def marks(pub):
            self.plan(pub)
            rows = self.run_page("R.rows = ELS.rows.html;")["R"]["rows"]
            row = re.search(r'<tr data-id="lab/plan.html".*?</tr>', rows, re.S).group(0)
            return {m[1]: (m[0], m[2], m[3]) for m in re.findall(
                r'<(a|span) class="pub-(\w+) (\w+)"[^>]*?title="([^"]*)"', row)}

        got = marks({"unread": "", "choice": {"notion": True, "drive": True, "obsidian": True, "why": ""}, "copies": [
            {"channel": "obsidian", "url": "obsidian://open?vault=V&file=Briefs%2Flab%2Fplan", "state": "published", "title": "Obsidian"}]})
        self.assertEqual(got, {"obsidian": ("a", "has", "Obsidian: Briefs/lab/plan"),
                               "drive": ("span", "missing", "Google Drive: designated, no copy yet"),
                               "notion": ("span", "missing", "Notion: designated, no copy yet")})
        # A destination not configured shows dim (sd:5485); a failed read is not "no copy".
        got = marks({"unread": "the pack's mirror queue was not read: x", "copies": [],
                     "choice": {"notion": True, "drive": False, "obsidian": None, "why": ""}})
        self.assertEqual(got, {"drive": ("span", "off", "Google Drive: not a destination"),
                               "notion": ("span", "unread", "Notion: not read · the pack&#39;s mirror queue was not read: x"),
                               "obsidian": ("span", "unread", "Obsidian: not read · the pack&#39;s mirror queue was not read: x")})
        css = (STATIC / "shell.css").read_text(encoding="utf-8")
        for cls, want in ((".has", "background: var(--color-accent-bg)"), (".missing", "background: transparent"),
                          (".unread", "border-style: dashed"), (".queued", "border-style: dashed")):
            self.assertRegex(css, rf"(?m)^\.pub \{cls} \{{[^}}]*{re.escape(want)}", cls)

    def test_details_shows_publish_to_with_a_per_document_obsidian_box(self):
        self.plan({"unread": "", "copies": [], "choice": {"notion": True, "drive": False, "obsidian": False, "repo_wide": "", "why": ""}})
        out = self.run_page("""R.det = ELS.details.html;
R.off = ['drive', 'notion', 'obsidian'].map(ch => { const o = OBJ.get(`pub:lab/plan.html:${ch}`); return REG.find(c => c.id === 'document.publish').when(o); });
R.now = REG.find(c => c.id === 'document.publish-now').when(OBJ.get('lab/plan.html'));
const un = REG.find(c => c.id === 'document.unpublish');
R.un = ['notion', 'drive', 'obsidian'].map(ch => un.when(OBJ.get(`pub:lab/plan.html:${ch}`)));
R.vault = REG.find(c => c.id === 'document.publish').consequence(OBJ.get('pub:lab/plan.html:obsidian'));
R.says = un.consequence(OBJ.get('pub:lab/plan.html:notion'));
R.kind = [un.risk, un.askFirst];""", row="'lab/plan.html'")
        det = out["R"]["det"]
        self.assertIn('<h3>Publish to</h3>', det)
        self.assertIn('aria-checked="false" data-cmd="document.publish" data-obj="pub:lab/plan.html:drive"', det)
        # sd:5489: a ticked box unticks: it drops the designation and queues the copy for the trash, asking first.
        self.assertIn('aria-checked="true" data-cmd="document.unpublish" data-obj="pub:lab/plan.html:notion"', det)
        self.assertIn('aria-checked="false" data-cmd="document.publish" data-obj="pub:lab/plan.html:obsidian"', det)
        self.assertEqual(out["R"]["off"][0], True)
        # sd:5500: outside the vault's own repository Obsidian ticks like the others and writes the vault copy at once.
        self.assertEqual(out["R"]["off"][2], True)
        self.assertIn("writes its Markdown into the vault", out["R"]["vault"])
        self.assertEqual(out["R"]["un"], [True, "not designated for Google Drive", "not designated for Obsidian"])
        self.assertIn("trash", out["R"]["says"])
        self.assertIn("Undo", out["R"]["says"])
        self.assertEqual(out["R"]["kind"], ["undo", True])
        self.assertIn("nothing is queued", out["R"]["now"])


    def test_details_in_the_vaults_own_repository_says_obsidian_is_repo_wide(self):
        self.plan({"unread": "", "copies": [], "choice": {"notion": False, "drive": False, "obsidian": True,
                                                          "repo_wide": publishing.VAULT_REPO, "why": ""}})
        out = self.run_page("""R.det = ELS.details.html;
const o = OBJ.get('pub:lab/plan.html:obsidian');
R.when = [REG.find(c => c.id === 'document.publish').when(o), REG.find(c => c.id === 'document.unpublish').when(o)];""", row="'lab/plan.html'")
        self.assertIn("<p class=\"why\">Obsidian: this repository is the vault itself", out["R"]["det"])
        self.assertEqual(out["R"]["when"], [publishing.VAULT_REPO, publishing.VAULT_REPO])


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
  trash)
    printf '{"trashed": "t1"}' > "$FAKE_RECEIPTS/lab.plan.notion.json"
    rm -f "$FAKE_QUEUE/lab.plan.notion.json"
    echo "lab.plan.notion.json: trashed" ;;
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

    def test_claude_loads_a_server_for_every_mcp_tool_it_may_call(self):
        """sd:5942: `--restricted` drops the user's settings and their servers, so the drain had no Notion tool."""
        self.request()
        with mock.patch.dict(os.environ, {"WORKSPACE_MCP_URL": "http://192.0.2.7:8083/mcp"}):
            record = self.started()
        publish_drain.run(record)
        said = (self.base / "argv").read_text(encoding="utf-8").split("\n")
        self.assertIn("--strict-mcp-config", said)
        servers = json.loads(said[said.index("--mcp-config") + 1])["mcpServers"]
        allowed = said[said.index("--allowedTools") + 1].split(",")
        self.assertEqual(set(servers), {tool.split("__")[1] for tool in allowed if tool.startswith("mcp__")})
        self.assertEqual(servers["notion"], {"type": "http", "url": "https://mcp.notion.com/mcp"})
        self.assertEqual(servers["workspace-mcp"], {"type": "http", "url": "http://192.0.2.7:8083/mcp"})
        self.assertNotIn("servers", publish_drain.public(json.loads(record.read_text(encoding="utf-8"))))

    def test_a_run_queued_before_the_servers_loads_none_and_leaves_its_request(self):
        self.request()
        record = self.started()
        document = json.loads(record.read_text(encoding="utf-8"))
        del document["servers"]
        record.write_text(json.dumps(document), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FAKE_MODE": "leave"}):
            code = publish_drain.run(record)
        said = (self.base / "argv").read_text(encoding="utf-8").split("\n")
        self.assertEqual(said[said.index("--mcp-config") + 1], '{"mcpServers": {}}')
        self.assertEqual(code, 1)
        self.assertTrue((self.queue / "lab.plan.notion.json").is_file())

    def test_a_brief_drain_skips_step_5_and_may_edit_no_conf(self):
        self.request()
        status, run = publish_drain.start(None, self.checkout, "plan", brief=True, spawn=self.spawn)
        document = json.loads((publish_drain.state_dir() / "runs" / f"{run['id']}.json").read_text(encoding="utf-8"))
        self.assertNotIn("research.conf.py", document["allowed"])
        prompt = Path(document["prompt"]).read_text(encoding="utf-8")
        self.assertIn("Step 5: skip it: this is a routine brief", prompt)

    def trash_request(self):
        (self.queue / "lab.plan.notion.json").write_text(json.dumps({"repo": str(self.checkout), "fingerprint": "t1", "action": "trash"}),
                                                          encoding="utf-8")

    def test_a_trash_request_adds_only_the_two_trash_tools_and_names_the_rule(self):
        """sd:5489: the trash tools join the allowlist only for a drain that holds a trash request."""
        self.trash_request()
        status, run = publish_drain.start(None, self.checkout, "plan", spawn=self.spawn)
        document = json.loads((publish_drain.state_dir() / "runs" / f"{run['id']}.json").read_text(encoding="utf-8"))
        allowed = document["allowed"].split(",")
        self.assertEqual([tool for tool in allowed if any(word in tool for word in ("delete", "trash", "update_drive_file"))],
                         ["mcp__notion__notion-delete-pages", "mcp__workspace-mcp__update_drive_file"])
        prompt = Path(document["prompt"]).read_text(encoding="utf-8")
        self.assertIn("An unticked destination moves its copy to the trash", prompt)
        self.assertNotIn("Never delete, move, trash or share a page or file", prompt)

    def test_a_trashed_request_reads_done(self):
        self.trash_request()
        code, document = self.ran("trash")
        self.assertEqual((code, document["state"]), (0, "done"))
        self.assertEqual(document["results"][0]["state"], "trashed")

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
