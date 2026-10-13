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
scheduled: 2026-10-23
recurrence: DTSTART:20250103;FREQ=WEEKLY;INTERVAL=2;BYDAY=FR
recurrence_anchor: scheduled
complete_instances:
  - "2026-10-09"
  - 2026-09-25
skipped_instances: ['2026-09-11']
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
    "juliet": """---
title: Example archived juliet
status: archived
scheduled: 2026-05-01
recurrence: DTSTART:20250501;FREQ=MONTHLY;BYMONTHDAY=1
complete_instances:
  - 2026-04-01
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
        for line in ("task files: 10", "already imported: 0", "would create: 7",
                     "  planning one-off: 1", "  in_progress one-off: 1", "  done one-off: 1",
                     "  done one-off (series ended): 1", "  cancelled one-off (series ended): 1",
                     "  planning recurring WEEKLY+BYDAY schedule: 1",
                     "  planning recurring MONTHLY completion: 1",
                     "  history entries kept in notes: 5", "refused: 3", "  no frontmatter: 1",
                     "  status 'waiting' has no sd status: 1"):
            self.assertIn(line, lines)
        self.assertTrue(any(line.startswith("  rule refused: recurrence part COUNT") for line in lines), lines)
        for title in TITLES:
            self.assertNotIn(title, out.stdout + out.stderr)
        # A file name is printed only on a refused task's line, so the operator can fix it.
        refused = {"foxtrot", "golf", "hotel"}
        for stem in TASKS:
            named = [line for line in lines if f"{stem}.md" in line]
            self.assertEqual(bool(named), stem in refused, stem)
            self.assertTrue(all(line.startswith(f"  refused TaskNotes/Tasks/{stem}.md: ") for line in named), named)

    def test_apply_maps_status_priority_dates_rule_and_history(self):
        out = self.migrate("--apply")
        # Three tasks are refused, so the run says the migration is not whole.
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("created: 7", out.stdout.splitlines())
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
                         ("FREQ=WEEKLY;INTERVAL=2;BYDAY=FR", "schedule", "2026-10-23", 2))
        history = next(n["body"] for n in delta["notes"] if n["body"].startswith("Imported from Obsidian"))
        self.assertIn("Completed occurrences (2): 2026-09-25, 2026-10-09.", history)
        self.assertIn("Skipped occurrences (1): 2026-09-11.", history)
        echo = self.item("echo")["item"]
        self.assertEqual((echo["recurrence"], echo["recurrence_anchor"], echo["due"]),
                         ("FREQ=MONTHLY", "completion", "2026-10-31"))
        india = self.item("india")
        self.assertEqual((india["item"]["status"], india["item"]["recurrence"]), ("done", None))
        self.assertTrue(any("its series ended" in n["body"] for n in india["notes"]))
        juliet = self.item("juliet")
        self.assertEqual((juliet["item"]["status"], juliet["item"]["recurrence"]), ("done", None))
        receipt = json.loads(juliet["item"]["fields"])["completion"]
        self.assertEqual((receipt["outcome"], receipt["reason"]), ("cancelled", "archived in the Obsidian vault"))
        self.assertTrue(any("Archived in the vault" in n["body"] for n in juliet["notes"]))
        for stem in ("foxtrot", "golf", "hotel"):
            self.assertIsNone(self.item(stem), stem)

    def test_completing_an_imported_byday_series_opens_the_next_friday(self):
        self.migrate("--apply")
        item = self.item("delta")["item"]["id"]
        done = workflow.change_status(self.connection(), item, "done", who="test")
        nxt = workflow.item_state(self.connection(), done["next_occurrence"])["item"]
        self.assertEqual((nxt["due"], nxt["recurrence"]), ("2026-11-06", "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR"))

    def test_a_second_run_creates_nothing_even_after_a_completion(self):
        self.migrate("--apply")
        workflow.change_status(self.connection(), self.item("delta")["item"]["id"], "done", who="test")
        before = self.count()
        out = self.migrate("--apply")
        self.assertIn("already imported: 7", out.stdout.splitlines())
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
        self.assertIn("would create: 7", out.stdout.splitlines())

    def test_archived_tasks_alone_let_apply_exit_zero(self):
        # sd:3460 ruling: archived tasks land closed with a cancelled receipt, so they are no refusal.
        for path in self.tasks.iterdir():
            if path.stem not in ("alpha", "juliet"):
                path.unlink()
        (self.tasks / "kilo.md").write_text("---\ntitle: Example tagged kilo\nstatus: open\ntags: [task, archived]\n---\n")
        out = self.migrate("--apply")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("refused: 0", out.stdout.splitlines())
        self.assertIn("  cancelled one-off (series ended): 1", out.stdout.splitlines())
        self.assertIn("  cancelled one-off: 1", out.stdout.splitlines())
        self.assertEqual(json.loads(self.item("kilo")["item"]["fields"])["completion"]["outcome"], "cancelled")

    def test_a_value_the_reader_cannot_decode_exactly_is_refused_by_key(self):
        # Review of sd:3460: never write what cannot be read exactly. Each value below would have been misread, and
        # the item's reference would have kept the damage; a refusal writes no reference, so a rerun can import it.
        head = "---\ntitle: Example refused\nstatus: open\n"
        recur = "recurrence: DTSTART:20250101;FREQ=DAILY\nscheduled: 2026-10-01\n"
        cases = {
            "escape-double": ('---\ntitle: "Example \\"quoted\\" escape"\n---\n', "title: a quoted value with an escape"),
            "escape-unicode": ('---\ntitle: "Example \\u00e9"\n---\n', "title: a quoted value with an escape"),
            "escape-single": ("---\ntitle: 'Example''s'\n---\n", "title: a quoted value with an escape"),
            "unclosed-quote": ('---\ntitle: "Example\n  more"\n---\n', "title: a quoted value with an escape"),
            "folded": ("---\ntitle: >-\n  Example folded\n  title\n---\n", "title: YAML syntax"),
            "literal": ("---\ntitle: |\n  Example literal\n---\n", "title: YAML syntax"),
            "anchor": (head + "priority: &p high\n---\n", "priority: YAML syntax"),
            "alias": (head + "priority: *p\n---\n", "priority: YAML syntax"),
            "tag": ("---\ntitle: !!str Example\n---\n", "title: YAML syntax"),
            "flow-map": ("---\ntitle: {a: b}\n---\n", "title: YAML syntax"),
            "comment": ("---\ntitle: Example # a comment\n---\n", "title: a comment or map syntax"),
            "continuation": ("---\ntitle: Example\n  continued\n---\n", "frontmatter line 2: not a line shape"),
            "map-in-list": (head + recur + "complete_instances:\n  - date: 2026-09-01\n---\n", "complete_instances: a map inside a list"),
            "flow-quoted-comma": (head + 'tags: ["a, b"]\n---\n', "tags: a quoted value with an escape"),
            "bad-date": (head + "scheduled: 2026-10-31x\n---\n", "scheduled: not a date"),
            "bad-history-date": (head + recur + "complete_instances: [2026-09-01, soon]\n---\n", "complete_instances: not a date"),
            "history-not-list": (head + recur + "complete_instances: 2026-09-01\n---\n", "complete_instances: not a list"),
            "archived-yes": (head + "archived: yes\n---\n", "archived: neither true nor false"),
            "duplicate": (head + "status: done\n---\n", "status: given twice"),
            "list-for-scalar": ("---\ntitle: Example\nstatus: [open]\n---\n", "status: a list where one value belongs"),
            "quoted-key": ('---\n"title": Example\n---\n', "frontmatter line 1"),
            "dtstart-tzid": (head + "recurrence: DTSTART;TZID=America/Denver:20250101T090000;FREQ=DAILY\nscheduled: 2026-10-01\n---\n",
                             "recurrence: a DTSTART the importer does not decode"),
        }
        for path in self.tasks.iterdir():
            path.unlink()
        for stem, (text, _) in cases.items():
            (self.tasks / f"{stem}.md").write_text(text)
        (self.tasks / "not-utf8.md").write_bytes(b"---\ntitle: Example \xff\n---\n")
        (self.tasks / "control.md").write_text('---\ntitle: "Example: a quoted colon"\nstatus: \'open\'\n---\n')
        out = self.migrate("--apply")
        lines = out.stdout.splitlines()
        for stem, (_, why) in [*cases.items(), ("not-utf8", ("", "not UTF-8"))]:
            with self.subTest(stem):
                self.assertTrue(any(line.startswith(f"  refused TaskNotes/Tasks/{stem}.md: {why}") for line in lines), lines)
                self.assertIsNone(self.item(stem))
        self.assertEqual(self.item("control")["item"]["title"], "Example: a quoted colon")
        self.assertIn("created: 1", lines)

    # Review round 2 of sd:3460: the reader is an allowlist. A value passes only when YAML reads it as the exact
    # string the importer writes; each rule below has its own test, and every case fails on the round-1 reader.
    OPEN = "---\ntitle: Example\nstatus: open\n"
    RECUR = "recurrence: DTSTART:20250101;FREQ=DAILY\nscheduled: 2026-10-01\n"

    def assert_refused(self, cases, controls=None):
        """Write `cases` (stem: (text, reason)) and `controls` (stem: text), apply, and check each outcome."""
        for path in self.tasks.iterdir():
            path.unlink()
        for stem, (text, _) in cases.items():
            (self.tasks / f"{stem}.md").write_text(text)
        for stem, text in (controls or {}).items():
            (self.tasks / f"{stem}.md").write_text(text)
        lines = self.migrate("--apply").stdout.splitlines()
        for stem, (_, why) in cases.items():
            with self.subTest(stem):
                self.assertTrue(any(line.startswith(f"  refused TaskNotes/Tasks/{stem}.md: {why}") for line in lines),
                                lines)
                self.assertIsNone(self.item(stem))
        return lines

    def test_a_plain_value_starting_with_a_yaml_indicator_is_refused(self):
        # `title: # use the filename` is YAML null plus a comment, not the title "# use the filename".
        why = "title: YAML syntax the importer does not decode (a value that starts with a YAML indicator)"
        values = {"hash": "# use the filename", "amp": "&a Example", "star": "*a", "bang": "!x Example",
                  "pipe": "|", "gt": ">-", "pct": "%x", "at": "@x", "tick": "`x`", "brace": "{a: b}",
                  "bracket": "[x", "comma": ",x", "query": "?x", "colon": ":x", "dash-space": "- x", "dash": "-"}
        self.assert_refused({f"indicator-{name}": (f"---\ntitle: {value}\n---\n", why)
                             for name, value in values.items()})

    def test_a_null_bool_or_number_where_a_string_belongs_is_refused(self):
        why = "{key}: YAML reads the value as null, a bool or a number, not a string"
        values = ["null", "Null", "NULL", "~", "true", "False", "yes", "No", "on", "OFF", "42", "-7", "3.5",
                  "1e3", ".inf", "-.Inf", ".NaN", "nan", "0x1F", "0o17"]
        cases = {f"typed-title-{n}": (f"---\ntitle: {value}\n---\n", why.format(key="title"))
                 for n, value in enumerate(values)}
        cases["typed-status"] = ("---\ntitle: Example\nstatus: ~\n---\n", why.format(key="status"))
        cases["typed-priority"] = (self.OPEN + "priority: null\n---\n", why.format(key="priority"))
        cases["typed-anchor"] = (self.OPEN + self.RECUR + "recurrence_anchor: true\n---\n",
                                 why.format(key="recurrence_anchor"))
        cases["typed-tag"] = (self.OPEN + "tags: [task, 2026]\n---\n", why.format(key="tags"))
        # A date or a bool is read by its own key: `archived: TRUE` is YAML's true.
        lines = self.assert_refused(cases, {"bool-archived": self.OPEN + "archived: TRUE\n---\n"})
        self.assertIn("  cancelled one-off: 1", lines)

    def test_leading_or_trailing_whitespace_is_refused(self):
        self.assert_refused({
            "space-quoted-leading": ('---\ntitle: "  Example"\n---\n', "title: leading or trailing whitespace"),
            "space-quoted-trailing": ("---\ntitle: 'Example '\n---\n", "title: leading or trailing whitespace"),
            "space-plain-trailing": ("---\ntitle: Example   \n---\n", "title: trailing whitespace"),
            "space-status-tab": ("---\ntitle: Example\nstatus: open\t\n---\n", "status: trailing whitespace"),
            "space-list-item": (self.OPEN + self.RECUR + "complete_instances:\n  - 2026-09-01  \n---\n",
                                "complete_instances: trailing whitespace"),
        })

    def test_an_empty_value_is_refused_except_a_title(self):
        # YAML reads an empty value as null. Only an empty title is kept: it takes the file name, as YAML intends.
        null = "an empty value, which YAML reads as null"
        lines = self.assert_refused({
            "empty-status": ("---\ntitle: Example\nstatus:\n---\n", f"status: {null}"),
            "empty-status-quoted": ('---\ntitle: Example\nstatus: ""\n---\n', f"status: {null}"),
            "empty-priority": (self.OPEN + "priority:\n---\n", f"priority: {null}"),
            "empty-scheduled": (self.OPEN + "scheduled:\n---\n", f"scheduled: {null}"),
            "empty-anchor": (self.OPEN + self.RECUR + "recurrence_anchor:\n---\n", f"recurrence_anchor: {null}"),
            "empty-history": (self.OPEN + self.RECUR + "complete_instances:\n---\n", f"complete_instances: {null}"),
            "empty-item": (self.OPEN + self.RECUR + "complete_instances:\n  -\n---\n",
                           "complete_instances: an empty list item, which YAML reads as null"),
            "empty-flow-item": (self.OPEN + "tags: [task, , x]\n---\n", "tags: an empty list item, which YAML reads as null"),
        }, {"title-empty": "---\ntitle:\nstatus: open\n---\n", "title-quoted-empty": '---\ntitle: ""\n---\n'})
        self.assertEqual(self.item("title-empty")["item"]["title"], "title-empty")
        self.assertEqual(self.item("title-quoted-empty")["item"]["title"], "title-quoted-empty")
        self.assertIn("created: 2", lines)

    def test_a_value_is_matched_exactly_not_case_folded(self):
        # `Open` is a different string from `open`; the reader no longer folds case or strips.
        self.assert_refused({
            "case-status": ("---\ntitle: Example\nstatus: Open\n---\n", "status 'Open' has no sd status"),
            "case-priority": (self.OPEN + "priority: High\n---\n", "priority 'High' has no sd priority"),
            "case-anchor": (self.OPEN + self.RECUR + "recurrence_anchor: Scheduled\n---\n",
                            "recurrence_anchor 'Scheduled' has no sd anchor"),
            "case-archived": (self.OPEN + "archived: tRuE\n---\n", "archived: neither true nor false"),
        })

    # Review round 3 of sd:3460: the reader is an allowlist of line shapes too. `  - task\n    - archived` is one
    # YAML string, not two tags; any line that is not `key: value`, `key:` or a list item at its list's indent
    # refuses the task, naming the line.
    def test_a_line_outside_the_three_shapes_is_refused_by_number(self):
        def line(n, why):
            return f"frontmatter line {n}: {why}"
        indent, shape, gap = "a list item at another indent", "not a line shape", "a blank or comment line inside a list"
        lines = self.assert_refused({
            "shape-deeper-tags": (self.OPEN + "tags:\n  - task\n    - archived\n---\n", line(5, indent)),
            "shape-deeper-history": (self.OPEN + self.RECUR + "complete_instances:\n  - 2026-09-01\n    - 2026-09-02\n---\n",
                                     line(7, indent)),
            "shape-deeper-skipped": (self.OPEN + self.RECUR + "skipped_instances:\n  - 2026-09-01\n    - 2026-09-02\n---\n",
                                     line(7, indent)),
            "shape-shallower": (self.OPEN + "tags:\n    - a\n  - b\n---\n", line(5, indent)),
            "shape-continuation": (self.OPEN + "tags:\n  - task\n    archived\n---\n", line(5, shape)),
            "shape-tab": (self.OPEN + "tags:\n\t- task\n---\n", line(4, shape)),
            "shape-blank-in-list": (self.OPEN + "tags:\n  - a\n\n  - b\n---\n", line(5, gap)),
            "shape-blank-before-item": (self.OPEN + "tags:\n\n  - a\n---\n", line(4, gap)),
            "shape-comment-in-list": (self.OPEN + "tags:\n  - a\n# note\n  - b\n---\n", line(5, gap)),
            "shape-item-after-value": (self.OPEN + "  - a\n---\n", line(3, shape)),
            "shape-fence-indented": ("---\ntitle: Example\n  ---\n---\n", line(2, shape)),
            "shape-fence-text": ("---\ntitle: Example\n--- x\n---\n", line(2, shape)),
            "shape-document-end": ("---\ntitle: Example\n...\n---\n", line(2, shape)),
            "shape-line-separator": ("---\ntitle: a\u2028status: done\n---\n", line(1, "a control character")),
            "shape-lone-cr": ("---\ntitle: a\rstatus: done\n---\n", line(1, "a control character")),
            "shape-flow-indicator": (self.OPEN + "tags: [a, b]c]\n---\n", "tags: a flow indicator inside a list item"),
        }, {"shape-column-zero-items": self.OPEN + "tags:\n- task\n- archived\n---\n",
            "shape-gap-between-keys": "---\ntitle: Example\n\n# a note\nstatus: open\n---\n"})
        self.assertIn("created: 2", lines)
        self.assertEqual(self.item("shape-column-zero-items")["item"]["status"], "done")  # the `archived` tag read

    def test_codex_round_3_findings_are_refused(self):
        # A no-break space is YAML content, not white space: `status: \u00a0done` is not `done`. And a history
        # item is read whether or not the task has a rule: `- true` is YAML's true, not a date.
        lines = self.assert_refused({
            "codex-nbsp-title": ("---\ntitle: \u00a0Pay invoice\n---\n", "title: leading or trailing whitespace"),
            "codex-nbsp-status": ("---\ntitle: Example\nstatus: \u00a0done\n---\n", "status: leading or trailing"),
            "codex-nbsp-tag": (self.OPEN + "tags: [\u00a0archived]\n---\n", "tags: leading or trailing whitespace"),
            "codex-history-bool": (self.OPEN + "complete_instances:\n  - true\n---\n", "complete_instances: not a date"),
        }, {"codex-history-no-rule": self.OPEN + "complete_instances:\n  - 2026-09-01\n---\n"})
        self.assertIn("created: 1", lines)

    def test_a_series_moved_off_its_dtstart_is_refused(self):
        # Review of sd:3460: sd keeps no DTSTART, so the due date becomes the series start. A task postponed off its
        # series would restart there: DTSTART 2026-01-31 monthly, postponed to 02-03, would recur on March 3, not 31.
        rule = "---\ntitle: Example series\nstatus: open\nscheduled: {due}\nrecurrence: DTSTART:{start};{rrule}\n---\n"
        refused = {
            "month-end-postponed": rule.format(due="2026-02-03", start="20260131", rrule="FREQ=MONTHLY"),
            "off-phase-fortnight": rule.format(due="2026-10-16", start="20250103", rrule="FREQ=WEEKLY;INTERVAL=2;BYDAY=FR"),
            "before-dtstart": rule.format(due="2026-01-01", start="20260115", rrule="FREQ=MONTHLY;BYMONTHDAY=15"),
        }
        kept = {
            "month-end-on-series": rule.format(due="2026-03-31", start="20260131", rrule="FREQ=MONTHLY"),
            "fortnight-on-phase": rule.format(due="2026-10-23", start="20250103", rrule="FREQ=WEEKLY;INTERVAL=2;BYDAY=FR"),
        }
        for path in self.tasks.iterdir():
            path.unlink()
        for stem, text in {**refused, **kept}.items():
            (self.tasks / f"{stem}.md").write_text(text)
        out = self.migrate("--apply")
        lines = out.stdout.splitlines()
        for stem in refused:
            with self.subTest(stem):
                self.assertTrue(any(line.startswith(f"  refused TaskNotes/Tasks/{stem}.md: recurrence: due date ")
                                    and "is off the series DTSTART" in line for line in lines), lines)
                self.assertIsNone(self.item(stem))
        self.assertEqual(self.item("month-end-on-series")["item"]["due"], "2026-03-31")
        nxt = workflow.change_status(self.connection(), self.item("month-end-on-series")["item"]["id"], "done", who="test")
        self.assertEqual(workflow.item_state(self.connection(), nxt["next_occurrence"])["item"]["due"], "2026-05-31")
        self.assertIsNotNone(self.item("fortnight-on-phase"))

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
        self.assertEqual(self.count(), 6)
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
