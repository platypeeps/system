"""The vault migration: `stage` verbatim, the declared status maps, and the refusals.

The kinds, their bases and their status maps come from the pack's
`sd plugin list --json` (sd:1425). The unit tests build `Reader` from a
fixture payload of that shape and cover each row of the design's failure
table; `TheWritingDeclaration` registers the pinned writing manifest with the
pinned pack and imports through the real command.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from sd_db import connect
from sd_db.migrate import initialise
from sd_db.sources import MigrationRefused, run
from sd_db.sources.vault import SOURCE, Reader, plugin_entries, stages_in

from . import support

SYSTEM = Path(__file__).resolve().parents[2]
PACK = Path(os.environ.get("SD_ACCEPTANCE_PACK", SYSTEM.parent / "pack"))
WRITING_MANIFEST = SYSTEM / ".github/fixtures/writing-sd-plugin.json"

BLOG_IDEA, TOPIC, entry = support.BLOG_IDEA, support.TOPIC, support.plugin_entry


class VaultCase(unittest.TestCase):
    ideas = {"first idea": "inbox", "second idea": "drafting", "third idea": "published"}
    topics = {"a topic": "active", "another topic": "parked"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        database = self.root / "sd.db"
        initialise(database)
        self.connection = connect(database)
        self.addCleanup(self.connection.close)
        self.vault = support.vault(
            self.root / "vault", ideas=self.ideas, topics=self.topics
        )
        self.environ = {"OBSIDIAN_VAULT": str(self.vault)}

    def reader(self, entries=None):
        return Reader.from_plugins([entry()] if entries is None else entries,
                                   environ=self.environ)

    def refused(self, entries=None):
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, self.reader(entries))
        self.assertEqual(self.items(), {})
        return str(caught.exception)

    def items(self):
        return {
            row["external_id"]: row
            for row in self.connection.execute(
                "SELECT * FROM item WHERE source = ?", (SOURCE,)
            )
        }

    def edit(self, relative, old, new):
        note = self.vault / relative
        note.write_text(note.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


class TheImport(VaultCase):
    def test_both_bases_land_as_idea_rows(self):
        sitting = run(self.connection, self.reader())
        self.assertTrue(sitting.clean)
        self.assertEqual(sitting.counts.seen, 5)
        kinds = {row["kind"] for row in self.items().values()}
        self.assertEqual(kinds, {"idea"})

    def test_the_ladder_word_is_kept_verbatim_in_stage(self):
        run(self.connection, self.reader())
        found = {row["stage"] for row in self.items().values()}
        self.assertEqual(found, {"inbox", "drafting", "published", "active", "parked"})

    def test_the_stage_set_in_the_rows_equals_the_set_in_the_source(self):
        """Criterion 5's set comparison, run over the frozen source."""
        reader = self.reader()
        frozen = reader.freeze()
        run(self.connection, reader)
        self.assertEqual(
            stages_in(frozen.records),
            {row["stage"] for row in self.items().values()},
        )

    def test_the_status_is_the_declared_map_s_answer_and_not_the_ladder_word(self):
        run(self.connection, self.reader())
        by_stage = {row["stage"]: row["status"] for row in self.items().values()}
        self.assertEqual(by_stage["inbox"], "planning")
        self.assertEqual(by_stage["drafting"], "in_progress")
        self.assertEqual(by_stage["published"], "done")
        self.assertEqual(by_stage["active"], "in_progress")
        self.assertEqual(by_stage["parked"], "blocked")

    def test_a_changed_declaration_changes_the_status(self):
        declared = entry(workflow={"blog-idea": {"status": {**BLOG_IDEA, "drafting": "blocked"}},
                                   "topic": {"status": dict(TOPIC)}})
        run(self.connection, self.reader([declared]))
        row = self.items()["blog-idea:System/Databases/Blog Ideas/second idea.md"]
        self.assertEqual(row["status"], "blocked")

    def test_a_second_run_reports_the_same_counts_with_zero_new_rows(self):
        first = run(self.connection, self.reader())
        second = run(self.connection, self.reader())
        self.assertEqual(first.counts.seen, second.counts.seen)
        self.assertEqual(second.counts.inserted, 0)
        self.assertEqual(second.counts.unchanged, second.counts.seen)

    def test_the_import_retires_nothing(self):
        before = {
            path: path.read_bytes()
            for path in sorted(self.vault.rglob("*.md"))
        }
        run(self.connection, self.reader())
        after = {
            path: path.read_bytes()
            for path in sorted(self.vault.rglob("*.md"))
        }
        self.assertEqual(before, after)

    def test_the_note_body_and_fields_land_on_the_row(self):
        run(self.connection, self.reader())
        row = self.items()["blog-idea:System/Databases/Blog Ideas/first idea.md"]
        self.assertIn("the blog-idea body of first idea", json.loads(row["body"])["markdown"])
        self.assertEqual(json.loads(row["fields"])["kind"], "blog-idea")

    def test_only_the_declared_kinds_are_imported(self):
        declared = entry(workflow={"topic": {"status": dict(TOPIC)}})
        sitting = run(self.connection, self.reader([declared]))
        self.assertEqual(sitting.counts.seen, 2)
        self.assertEqual({json.loads(row["fields"])["kind"] for row in self.items().values()},
                         {"topic"})


class TheDueField(VaultCase):
    def declared(self):
        return entry(workflow={"blog-idea": {"status": dict(BLOG_IDEA), "due-field": "due"},
                               "topic": {"status": dict(TOPIC)}})

    def test_the_declared_field_lands_in_item_due(self):
        self.edit("System/Databases/Blog Ideas/first idea.md",
                  "status: inbox\n", "status: inbox\ndue: 2026-11-02\n")
        sitting = run(self.connection, self.reader([self.declared()]))
        self.assertTrue(sitting.clean)
        rows = self.items()
        self.assertEqual(rows["blog-idea:System/Databases/Blog Ideas/first idea.md"]["due"],
                         "2026-11-02")
        self.assertIsNone(rows["blog-idea:System/Databases/Blog Ideas/second idea.md"]["due"])

    def test_a_kind_without_a_due_field_lands_null(self):
        self.edit("System/Databases/Topics/a topic.md",
                  "status: active\n", "status: active\ndue: 2026-11-02\n")
        run(self.connection, self.reader([self.declared()]))
        self.assertIsNone(self.items()["topic:System/Databases/Topics/a topic.md"]["due"])

    def test_a_due_value_that_is_not_a_date_refuses_naming_note_field_and_value(self):
        self.edit("System/Databases/Blog Ideas/first idea.md",
                  "status: inbox\n", "status: inbox\ndue: next week\n")
        message = self.refused([self.declared()])
        self.assertIn("first idea.md", message)
        self.assertIn("`due`", message)
        self.assertIn("'next week'", message)

    def test_an_impossible_date_refuses(self):
        self.edit("System/Databases/Blog Ideas/first idea.md",
                  "status: inbox\n", "status: inbox\ndue: 2026-02-30\n")
        self.assertIn("'2026-02-30'", self.refused([self.declared()]))


class TheRefusals(VaultCase):
    def test_a_word_the_declared_map_does_not_name_refuses_with_the_word_and_plugin(self):
        """Criterion 5: the word, not "an unknown stage"."""
        self.edit("System/Databases/Blog Ideas/first idea.md", "status: inbox", "status: marinating")
        message = self.refused()
        self.assertIn("'marinating'", message)
        self.assertIn("sdw", message)

    def test_a_note_with_no_status_refuses(self):
        self.edit("System/Databases/Topics/a topic.md", "status: active\n", "")
        self.assertIn("no `status:`", self.refused())

    def test_an_absent_base_refuses_rather_than_importing_zero(self):
        shutil.rmtree(self.vault / "System/Databases/Topics")
        self.assertIn("Topics", self.refused())

    def test_an_unreadable_root_refuses_naming_it_and_why(self):
        unreadable = {"root": "/plugins/gone", "readable": False, "why": "no sd-plugin.json there"}
        message = self.refused([entry(), unreadable])
        self.assertIn("/plugins/gone", message)
        self.assertIn("no sd-plugin.json there", message)

    def test_a_manifest_error_refuses_naming_the_prefix_and_error(self):
        broken = entry(prefix="bad", workflow={}, manifestError="`workflow['topic'].status` maps nothing")
        message = self.refused([entry(), broken])
        self.assertIn("bad", message)
        self.assertIn("maps nothing", message)

    def test_a_pack_that_predates_workflow_refuses_through_its_dashboard_error(self):
        """An older pack reports the unknown key from its first `try` block."""
        older = {"root": "/plugins/sdw", "readable": True, "prefix": "sdw",
                 "dashboardError": "manifest carries key(s) outside the closed vocabulary: workflow"}
        message = self.refused([older])
        self.assertIn("sdw", message)
        self.assertIn("closed vocabulary: workflow", message)

    def test_a_plugin_with_no_workflow_block_is_a_note_not_a_failure(self):
        sitting = run(self.connection, self.reader([entry(), entry(prefix="sys", workflow={})]))
        self.assertTrue(sitting.clean)
        self.assertIn("sys: declares no dated kinds; nothing imported", sitting.notes)

    def test_no_plugin_declaring_any_kind_refuses(self):
        self.assertIn("no registered plugin declares", self.refused([entry(workflow={})]))

    def test_no_plugin_at_all_refuses(self):
        self.assertIn("no registered plugin declares", self.refused([]))

    def test_two_plugins_declaring_one_kind_refuse_naming_both(self):
        message = self.refused([entry(), entry(prefix="sdx", workflow={"topic": {"status": dict(TOPIC)}})])
        self.assertIn("'topic'", message)
        self.assertIn("sdw", message)
        self.assertIn("sdx", message)

    def test_an_unset_root_variable_refuses_naming_it_and_the_prefix(self):
        message = self.refused([entry(root="$SOME_OTHER_VAULT")])
        self.assertIn("$SOME_OTHER_VAULT", message)
        self.assertIn("sdw", message)

    def test_a_set_root_variable_other_than_obsidian_vault_is_read(self):
        self.environ = {"SOME_OTHER_VAULT": str(self.vault)}
        sitting = run(self.connection, self.reader([entry(root="$SOME_OTHER_VAULT")]))
        self.assertEqual(sitting.counts.seen, 5)


class ThePackCommand(unittest.TestCase):
    """`plugin_entries`: the receipt, the checkout's `bin/sd`, and its output."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.environ = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    def sd(self, script):
        checkout = support.pack(self.home)
        (checkout / "bin/sd").write_text(textwrap.dedent(script), encoding="utf-8")
        return checkout

    def refused(self):
        with self.assertRaises(MigrationRefused) as caught:
            plugin_entries(home=self.home, environ=self.environ)
        return str(caught.exception)

    def test_no_receipt_refuses_quoting_the_reason(self):
        support.pack(self.home, receipt=False)
        self.assertIn("no receipt", self.refused())

    def test_a_checkout_without_bin_sd_refuses(self):
        support.pack(self.home)
        self.assertIn("bin/sd", self.refused())

    def test_a_failing_list_refuses_with_its_exit_code_and_first_stderr_line(self):
        self.sd("import sys\nsys.stderr.write('sd: the registry is broken\\nmore\\n')\nsys.exit(3)\n")
        message = self.refused()
        self.assertIn("exited 3", message)
        self.assertIn("sd: the registry is broken", message)
        self.assertNotIn("more", message)

    def test_output_that_is_not_json_refuses(self):
        self.sd("print('sdw  /plugins/sdw')\n")
        self.assertIn("not JSON", self.refused())

    def test_the_list_runs_with_this_interpreter_and_its_entries_return(self):
        self.sd("""\
            import json, sys
            assert sys.argv[1:] == ["plugin", "list", "--json"], sys.argv
            print(json.dumps([{"root": "/plugins/sdw", "readable": True, "prefix": "sdw",
                               "interpreter": sys.executable}]))
            """)
        found = plugin_entries(home=self.home, environ=self.environ)
        self.assertEqual(found[0]["prefix"], "sdw")
        self.assertEqual(found[0]["interpreter"], sys.executable)


class TheWritingDeclaration(unittest.TestCase):
    """The pinned writing manifest, registered with the pinned pack, imports.

    The design's integration test: a temporary checkout holding the fixture
    manifest and an empty file at each template path, registered in an
    isolated `XDG_CONFIG_HOME`, read back through `Reader.at` and the real
    `sd plugin list --json`.
    """

    def test_the_pinned_pack_accepts_the_declaration_and_both_kinds_land(self):
        self.assertTrue((PACK / "bin/sd").is_file(),
                        "set SD_ACCEPTANCE_PACK to the companion pack checkout")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            checkout = root / "writing"
            checkout.mkdir()
            manifest = json.loads(WRITING_MANIFEST.read_text(encoding="utf-8"))
            (checkout / "sd-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
            for kind in manifest["kinds"].values():
                template = checkout / kind["sections"]["template"]
                template.parent.mkdir(parents=True, exist_ok=True)
                template.touch()
            home = root / "home"
            support.pack(home, checkout=PACK, library=None)
            vault = support.vault(root / "vault", ideas={"an idea": "inbox"},
                                  topics={"a topic": "candidate"})
            environ = {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(root / "config"),
                "OBSIDIAN_VAULT": str(vault),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
            added = subprocess.run([sys.executable, str(PACK / "bin/sd"), "plugin", "add", str(checkout)],
                                   capture_output=True, text=True, env=environ, check=False)
            self.assertEqual(added.returncode, 0, added.stderr)
            database = root / "sd.db"
            initialise(database)
            connection = connect(database)
            self.addCleanup(connection.close)
            sitting = run(connection, Reader.at(home=home, environ=environ))
            self.assertTrue(sitting.clean)
            found = {row["external_id"]: row["status"] for row in connection.execute(
                "SELECT external_id, status FROM item WHERE source = ?", (SOURCE,))}
            self.assertEqual(found, {
                "blog-idea:System/Databases/Blog Ideas/an idea.md": "planning",
                "topic:System/Databases/Topics/a topic.md": "planning",
            })


if __name__ == "__main__":
    unittest.main()
