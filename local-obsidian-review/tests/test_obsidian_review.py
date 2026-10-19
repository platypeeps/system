"""The digest must be the same digest with Jev as without it.

Jev is experimental, so the thing worth testing is not that it ranks well --
nothing here can know that -- but that the tool is unchanged when it is off,
unavailable or broken, and that turning it on moves cards around without
adding, dropping or re-wiring one. The twelve-card cap is the sharp edge:
Blog Ideas holds fourteen notes in the fixture, and the same twelve must
reach the brief either way. The stub in `harness.py` stands in for `jev.sh`,
so no test here touches the network.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness  # noqa: E402

GOLDEN_LIST = (harness.FIXTURES / "golden_list.txt").read_text()
GOLDEN_BRIEF = (harness.FIXTURES / "golden_body.md").read_text().rstrip("\n")

#: Unset is on, and unset is what every deployed run has, so the fixtures
#: run the stage that way (sd:1183). `1` is checked once, explicitly, below.
ON: dict = {}
#: `JEV_OBSIDIAN_REVIEW` switches the stage off and nothing switches it on:
#: unset is on. Yesterday's digest is therefore the run that sets it to 0.
OFF = {"JEV_OBSIDIAN_REVIEW": "0"}


def shown(text: str):
    """{queue: [note stems shown]}, in the order the digest shows them."""
    out, current = {}, None
    for line in text.splitlines():
        m = re.match(r"^(.+): \d+ awaiting a decision$", line)
        if m:
            current = m.group(1)
            out[current] = []
        elif line.startswith("- ") and current:
            out[current].append(line[2:].split(" (score")[0])
    return out


def sections(text: str):
    return [m.group(1) for m in
            re.finditer(r"^(.+): \d+ awaiting a decision$", text, re.M)]


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = harness.build_root(self.tmp.name)

    def digest(self, env=None):
        out, brief, rc = harness.run(self.root, env)
        self.assertEqual(rc, 0)
        return harness.templated(out), harness.templated(brief)

    # --- the three ways Jev stays out of the way -------------------------

    def test_the_stage_switched_off_is_byte_for_byte_yesterdays_digest(self):
        out, brief = self.digest(OFF)
        self.assertEqual(out, GOLDEN_LIST)
        self.assertEqual(brief, GOLDEN_BRIEF)

    def test_the_golden_holds_on_any_day_the_run_is_pinned_to(self):
        """The golden is yesterday's digest on every day, not only today.

        The ages in it ("(14d ago)") are not templated, so they hold only
        while the script counts from the same day the fixture was written
        on. Neither date here is the real one: one crosses a leap day and
        one a year end. A script that read the clock instead of
        OBSIDIAN_REVIEW_TODAY fails both, which is the midnight race that
        broke runs 35935415274 and 35935854678.
        """
        for day in (datetime.date(2028, 3, 1), datetime.date(2027, 1, 5)):
            with self.subTest(day=day.isoformat()), \
                    tempfile.TemporaryDirectory() as tmp:
                root = harness.build_root(tmp, today=day)
                out, brief, rc = harness.run(root, OFF, today=day)
                self.assertEqual(rc, 0)
                self.assertEqual(harness.templated(out, day), GOLDEN_LIST)
                self.assertEqual(harness.templated(brief, day), GOLDEN_BRIEF)

    def test_the_stage_unset_orders_like_the_stage_switched_on(self):
        # The flip: every one of these integrations was opt-in, and an opt-in
        # that defaults to off makes each one added after it silently never
        # run. Unset now reaches Jev; the machine-wide answers still decide.
        self.assertEqual(self.digest(), self.digest({"JEV_OBSIDIAN_REVIEW": "1"}))
        self.assertNotEqual(self.digest(), self.digest(OFF))

    def test_jev_disabled_is_byte_for_byte_yesterdays_digest(self):
        out, brief = self.digest({**ON, "JEV_STUB_ENABLED_EXIT": "3"})
        self.assertEqual(out, GOLDEN_LIST)
        self.assertEqual(brief, GOLDEN_BRIEF)

    def test_a_failing_jev_leaves_the_digest_intact(self):
        out, brief = self.digest({**ON, "JEV_STUB_ASK_FAIL": "1"})
        self.assertEqual(out, GOLDEN_LIST)
        self.assertEqual(brief, GOLDEN_BRIEF)

    def test_a_failing_jev_says_so_on_stderr(self):
        env = dict(os.environ)
        env.update({
            **harness.sd_stub(self.root),
            "JEV_STUB_ASK_FAIL": "1",
        })
        env.pop("JEV_OBSIDIAN_REVIEW", None)
        proc = subprocess.run(
            ["sh", str(self.root / "local-obsidian-review" / "obsidian-review.sh"),
             "run"],
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("jev did not rank this digest", proc.stderr)
        self.assertIn("keeping the usual order", proc.stderr)

    # --- the rows come from the workflow database ------------------------

    def test_the_digest_reads_the_database_rows_not_the_vault_notes(self):
        """sd:5134: the database is the source since pack sd:4627, and the notes are going away."""
        folder = self.root / "vault" / "System" / "Databases" / "Blog Ideas"
        folder.mkdir(parents=True)
        (folder / "vault-only.md").write_text(
            "---\nstatus: inbox\nscore: 99\ndateCreated: 2026-01-01\n---\n")
        out, brief = self.digest({**OFF, "OBSIDIAN_VAULT": str(self.root / "vault")})
        self.assertNotIn("vault-only", out)
        self.assertEqual(shown(out), harness.EXPECTED_SHOWN)

    def test_each_card_opens_its_row_on_the_dashboards_tasks_page(self):
        ids = harness.item_ids(self.root)
        _, brief = self.digest(OFF)
        for queue, stems in harness.EXPECTED_SHOWN.items():
            for stem in stems:
                card = brief.split(f"- **{stem}** · ", 1)[1].split("\n", 1)[0]
                self.assertTrue(card.startswith(f"what {stem} is about · "), card)
                self.assertTrue(card.endswith(f" · [\u2197 Open](/tasks?row={ids[stem]})"), card)
        self.assertNotIn("obsidian://", brief)

    def test_an_unreadable_database_stops_the_run_and_files_nothing(self):
        """A read that fails is unknown, never "all decision queues empty"."""
        from sd_db.database import local_path

        local_path(self.root / "home").unlink()
        proc, brief = harness.run_proc(self.root, OFF)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("the base rows could not be read", proc.stderr)
        self.assertNotIn("all decision queues empty", proc.stdout)
        self.assertEqual(brief, "")
        self.assertFalse((self.root / "sd.args").exists())

    # --- and the one way it does something -------------------------------

    def test_jev_reorders_the_cards_and_the_sections(self):
        out, brief = self.digest(ON)
        self.assertEqual(sections(out),
                         list(reversed(harness.EXPECTED_SECTIONS)))
        for queue, notes in harness.EXPECTED_SHOWN.items():
            self.assertEqual(shown(out)[queue], list(reversed(notes)))
        self.assertNotEqual(out, GOLDEN_LIST)
        self.assertIn("Ordered by Jev", brief)
        self.assertIn("(ordered by Jev, most worth deciding first)", out)

    def test_the_item_set_never_changes(self):
        for env in (None, {**ON, "JEV_STUB_ENABLED_EXIT": "3"},
                    {**ON, "JEV_STUB_ASK_FAIL": "1"}, ON):
            out, brief = self.digest(env)
            self.assertEqual(out.splitlines()[0], "20 awaiting decisions")
            picked = shown(out)
            self.assertEqual(sorted(picked), sorted(harness.EXPECTED_SHOWN))
            for queue, notes in harness.EXPECTED_SHOWN.items():
                self.assertEqual(sorted(picked[queue]), sorted(notes))
            # the two notes over the twelve-card cap stay over it
            self.assertNotIn("blog-01", out)
            self.assertNotIn("blog-02", out)
            self.assertIn("… and 2 more on Tasks", brief)
            # and nothing whose status is not a pending decision appears
            for hidden in ("blog-accepted", "skill-done", "tip-ready-a"):
                self.assertNotIn(hidden, out)
            self.assertIn("2 tip(s) are `ready`", brief)

    def test_the_links_are_untouched(self):
        plain = self.digest()[1]
        ranked = self.digest(ON)[1]
        hrefs = lambda doc: sorted(re.findall(r"\]\(([^)]+)\)", doc))
        self.assertTrue(hrefs(plain))
        self.assertEqual(hrefs(plain), hrefs(ranked))
        self.assertEqual(sorted(re.findall(r"\[([^\]]+)\]\(", plain)),
                         sorted(re.findall(r"\[([^\]]+)\]\(", ranked)))

    # --- the digest is a brief on Today, not a mail ------------------------

    def test_the_digest_is_filed_as_the_days_review_queue_brief(self):
        """Operator ruling 2026-10-17: what the reports view shows is raised on Today, not mailed."""
        out, brief, rc = harness.run(self.root, OFF)
        self.assertEqual(rc, 0)
        self.assertIn("brief filed", out)
        args = (self.root / "sd.args").read_text().strip()
        self.assertEqual(args, f"reports brief add --belongs-to {self.root} --replace "
                               + args.split(" --replace ", 1)[1])
        self.assertTrue(args.endswith(f"/{harness.TODAY.isoformat()} - Review Queue.md"), args)
        self.assertTrue(brief.startswith("---\njob: obsidian-review-daily\n---\n\n**20** item(s) across 5 queue(s)"))
        self.assertFalse((self.root / "local-notify").exists())

    def test_a_brief_that_cannot_be_filed_exits_1(self):
        proc, _ = harness.run_proc(self.root, {**OFF, "SD_EXIT": "2"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("brief NOT filed", proc.stderr)

    def test_list_files_nothing(self):
        env = {**os.environ, **harness.sd_stub(self.root), **OFF}
        proc = subprocess.run(["sh", str(self.root / "local-obsidian-review" / "obsidian-review.sh"), "list"],
                              capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.root / "sd.args").exists())

    # --- what leaves the machine -----------------------------------------

    def test_only_titles_queues_and_dates_leave_the_machine(self):
        record = self.root / "jev.request.json"
        self.digest({**ON, "JEV_STUB_RECORD": str(record)})
        sent = json.loads(record.read_text())
        payload = json.dumps(sent)
        self.assertNotIn("A body nobody outside this machine", payload)
        self.assertNotIn("what blog-14 is about", payload)   # description
        self.assertNotIn("Base/", payload)  # no row path
        self.assertNotIn(str(self.root), payload)
        self.assertNotIn("obsidian://", payload)
        for item in sent["state"]["items"]:
            self.assertEqual(sorted(item), ["added", "id", "queue", "title"])

    def test_the_ask_names_the_digest_by_hash_and_the_run_is_one_id(self):
        """`--subject` hashes the stems, never a title; both calls share `JEV_RUN` (sd:2953)."""
        calls = self.root / "jev.calls"
        self.digest({**ON, "JEV_STUB_CALLS": str(calls)})
        lines = calls.read_text().splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["enabled", "ask"])
        found = re.search(r"--subject (\S+) run=", lines[1])
        self.assertIsNotNone(found, lines[1])
        self.assertRegex(found.group(1), r"^obsidian-review:[0-9a-f]{16}$")
        runs = {line.rsplit("run=", 1)[1] for line in lines}
        self.assertEqual(len(runs), 1, runs)
        self.assertRegex(runs.pop(), r"^obsidian-review-\d{8}T\d{6}-[0-9a-f]{4}$")

    def test_every_shown_note_is_one_question_in_one_request(self):
        record = self.root / "jev.request.json"
        self.digest({**ON, "JEV_STUB_RECORD": str(record)})
        sent = json.loads(record.read_text())
        expected = sum(len(v) for v in harness.EXPECTED_SHOWN.values())
        self.assertEqual(len(sent["questions"]), expected)
        self.assertEqual(len(sent["state"]["items"]), expected)


if __name__ == "__main__":
    unittest.main()
