"""`obsidian-tasks.sh migrate` against a synthetic vault and a scratch sd database (sd:3460).

Every run gets its own HOME, so `sd_db.connect` opens a database this suite
made, and its own vault under a temp dir; the real ones are never read.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
TOOL = HERE.parent / "obsidian-tasks.sh"

_spec = importlib.util.spec_from_file_location("obsidian_migrate", HERE.parent / "migrate.py")
migrate = sys.modules[_spec.name] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)  # puts the pack's lib/ on sys.path

import sd_db  # noqa: E402
from sd_db import workflow, writes  # noqa: E402
from sd_db.migrate import migrate as make_database  # noqa: E402

# Synthetic tasks, one per shape the importer maps or refuses.
TASKS = {
    "alpha": """---
title: Example one-off alpha
status: open
priority: high
scheduled: 2026-11-02
due: 2026-11-05T17:00
---
Body text of alpha.
""",
    "bravo": """---
title: Example one-off bravo
status: in-progress
priority: low
---
""",
    "charlie": """---
title: "Example one-off charlie"
status: done
priority: none
completedDate: 2026-09-30
---
""",
    "delta": """---
title: Example fortnightly delta
status: open
priority: normal
scheduled: 2026-10-16
recurrence: DTSTART:20250103;FREQ=WEEKLY;INTERVAL=2;BYDAY=FR
recurrence_anchor: scheduled
complete_instances:
  - "2026-10-02"
  - 2026-09-18
skipped_instances: ['2026-09-04']
---
""",
    "echo": """---
title: Example monthly echo
status: open
scheduled: 2026-10-31T09:00
recurrence: "DTSTART:20250131;FREQ=MONTHLY;INTERVAL=1"
recurrence_anchor: completion
complete_instances: []
---
""",
    "foxtrot": """---
title: Example refused foxtrot
status: open
scheduled: 2026-10-20
recurrence: DTSTART:20250101;FREQ=MONTHLY;COUNT=3
---
""",
    "golf": """---
title: Example refused golf
status: waiting
---
""",
    "hotel": "No frontmatter here.\n",
    "india": """---
title: Example ended series india
status: done
scheduled: 2026-08-01
recurrence: DTSTART:20250801;FREQ=YEARLY
complete_instances: [2025-08-01]
---
""",
}
TITLES = [line.split(":", 1)[1].strip().strip('"') for text in TASKS.values()
          for line in text.splitlines() if line.startswith("title:")]


class Migrate(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        make_database(home=self.home)
        self.vault = self.root / "vault"
        self.tasks = self.vault / "TaskNotes" / "Tasks"
        self.tasks.mkdir(parents=True)
        for stem, text in TASKS.items():
            (self.tasks / f"{stem}.md").write_text(text)

    def env(self):
        env = dict(os.environ, HOME=str(self.home), OBSIDIAN_VAULT=str(self.vault),
                   SYSTEM_TOOLS_CONFIG=str(self.root / "config"))
        env.pop("OBSIDIAN_TASKS_SUBDIR", None)
        return env

    def migrate(self, *args):
        return subprocess.run(["sh", str(TOOL), "migrate", *args], env=self.env(),
                              capture_output=True, text=True, timeout=120)

    def connection(self):
        connection = sd_db.connect(home=self.home)
        self.addCleanup(connection.close)
        return connection

    def item(self, stem):
        row = writes.item_by_external(self.connection(), "task-ref", f"obsidian:TaskNotes/Tasks/{stem}.md")
        return None if row is None else workflow.item_state(self.connection(), row["id"])

    def count(self):
        return self.connection().execute("SELECT count(*) FROM item").fetchone()[0]

    def test_a_dry_run_writes_nothing_and_prints_counts_not_names(self):
        out = self.migrate()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.count(), 0)
        lines = out.stdout.splitlines()
        for line in ("task files: 9", "already imported: 0", "would create: 6",
                     "  planning one-off: 1", "  in_progress one-off: 1", "  done one-off: 1",
                     "  done one-off (series ended): 1",
                     "  planning recurring WEEKLY+BYDAY schedule: 1",
                     "  planning recurring MONTHLY completion: 1",
                     "  history entries kept in notes: 4", "refused: 3", "  no frontmatter: 1",
                     "  status 'waiting' has no sd status: 1"):
            self.assertIn(line, lines)
        self.assertTrue(any(line.startswith("  rule refused: recurrence part COUNT") for line in lines), lines)
        for title in TITLES:
            self.assertNotIn(title, out.stdout + out.stderr)
        for stem in TASKS:
            self.assertNotIn(f"{stem}.md", out.stdout + out.stderr)

    def test_apply_maps_status_priority_dates_rule_and_history(self):
        out = self.migrate("--apply")
        # Three tasks are refused, so the run says the migration is not whole.
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("created: 6", out.stdout.splitlines())
        self.assertIn("failed: 0", out.stdout.splitlines())

        alpha = self.item("alpha")
        self.assertEqual((alpha["item"]["kind"], alpha["item"]["repo"], alpha["item"]["status"],
                          alpha["item"]["priority"], alpha["item"]["due"], json.loads(alpha["item"]["body"])["text"]),
                         ("task", None, "planning", 1, "2026-11-02", "Body text of alpha."))
        self.assertTrue(any("The vault's due date was 2026-11-05" in n["body"] for n in alpha["notes"]))
        bravo = self.item("bravo")["item"]
        self.assertEqual((bravo["status"], bravo["priority"], bravo["due"]), ("in_progress", 3, None))
        charlie = self.item("charlie")
        self.assertEqual((charlie["item"]["title"], charlie["item"]["status"], charlie["item"]["priority"]),
                         ("Example one-off charlie", "done", None))
        self.assertTrue(any("Completed in the vault on 2026-09-30." in n["body"] for n in charlie["notes"]))

        delta = self.item("delta")
        self.assertEqual((delta["item"]["recurrence"], delta["item"]["recurrence_anchor"], delta["item"]["due"],
                          delta["item"]["priority"]),
                         ("FREQ=WEEKLY;INTERVAL=2;BYDAY=FR", "schedule", "2026-10-16", 2))
        history = next(n["body"] for n in delta["notes"] if n["body"].startswith("Imported from Obsidian"))
        self.assertIn("Completed occurrences (2): 2026-09-18, 2026-10-02.", history)
        self.assertIn("Skipped occurrences (1): 2026-09-04.", history)
        echo = self.item("echo")["item"]
        self.assertEqual((echo["recurrence"], echo["recurrence_anchor"], echo["due"]),
                         ("FREQ=MONTHLY", "completion", "2026-10-31"))
        india = self.item("india")
        self.assertEqual((india["item"]["status"], india["item"]["recurrence"]), ("done", None))
        self.assertTrue(any("its series ended" in n["body"] for n in india["notes"]))
        for stem in ("foxtrot", "golf", "hotel"):
            self.assertIsNone(self.item(stem), stem)

    def test_completing_an_imported_byday_series_opens_the_next_friday(self):
        self.migrate("--apply")
        item = self.item("delta")["item"]["id"]
        done = workflow.change_status(self.connection(), item, "done", who="test")
        nxt = workflow.item_state(self.connection(), done["next_occurrence"])["item"]
        self.assertEqual((nxt["due"], nxt["recurrence"]), ("2026-10-30", "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR"))

    def test_a_second_run_creates_nothing_even_after_a_completion(self):
        self.migrate("--apply")
        workflow.change_status(self.connection(), self.item("delta")["item"]["id"], "done", who="test")
        before = self.count()
        out = self.migrate("--apply")
        self.assertIn("already imported: 6", out.stdout.splitlines())
        self.assertIn("created: 0", out.stdout.splitlines())
        self.assertEqual(self.count(), before)
        self.assertIn("would create: 0", self.migrate().stdout.splitlines())

    def test_a_reference_to_another_file_does_not_count_as_imported(self):
        connection = self.connection()
        item = workflow.capture_task(connection, title="Another", who="test")["item"]["id"]
        with sd_db.transaction(connection):
            writes.set_item_fields(connection, item, source="task-ref", external_id="obsidian:TaskNotes/Tasks/alpha.md.old")
        self.assertIsNotNone(writes.item_by_external(self.connection(), "task-ref", "obsidian:TaskNotes/Tasks/alpha.md.old"))
        out = self.migrate()
        self.assertIn("already imported: 0", out.stdout.splitlines())
        self.assertIn("would create: 6", out.stdout.splitlines())

    def test_the_vault_is_only_read(self):
        def files():
            return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.vault.rglob("*") if p.is_file()}

        before = files()
        self.migrate("--apply")
        self.assertEqual(files(), before)
        self.assertEqual(len(before), len(TASKS))

    def test_a_task_stopped_between_its_writes_is_imported_whole_on_the_next_run(self):
        # The note is the last write of a task; failing it must leave no half-imported item behind.
        real = workflow.add_item_note
        calls = []

        def fail_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("stopped")
            return real(*args, **kwargs)

        with mock.patch.dict(os.environ, self.env(), clear=True), \
                mock.patch.object(workflow, "add_item_note", fail_once), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(migrate.run(["--apply"]), 1)
        self.assertIn("  RuntimeError: stopped: 1", printed.getvalue().splitlines())
        self.assertIsNone(self.item("alpha"))
        self.assertEqual(self.count(), 5)
        out = self.migrate("--apply")
        self.assertIn("created: 1", out.stdout.splitlines())
        notes = [n["body"] for n in self.item("alpha")["notes"]]
        self.assertEqual(sum(body.startswith("Imported from Obsidian") for body in notes), 1)

    def test_an_unreadable_database_is_an_error_not_zero_imported(self):
        (self.home / ".local/share/sd/sd.db").unlink()
        out = self.migrate()
        self.assertEqual(out.returncode, 1)
        self.assertIn("cannot open the sd database", out.stderr)
        self.assertNotIn("already imported", out.stdout)


if __name__ == "__main__":
    unittest.main()
