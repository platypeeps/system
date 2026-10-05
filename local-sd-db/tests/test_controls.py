"""Operator intents validate whole batches and leave source checkouts unchanged."""
import ast
import getpass
import hashlib
import inspect
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sd_db
from sd_db import (
    ledger,
    provider_controls,
    registry,
    reporting,
    retention,
    runner,
    runner_controls,
    skills_catalog,
    workflow,
    writes,
)
from sd_db.database import connect
from sd_db.errors import RegistryError, SdDbError
from sd_db.migrate import initialise
from sd_db.operations import LABEL_PREFIX
from sd_db.testing.wire import hub_only
from sd_db.writes import (
    add_note,
    create_item,
    record_skill_use,
    resolve_note,
    set_item_fields,
    transition,
    upsert_repo,
)

REGISTRY = """bills:
  a: {cost: subscription}
  b: {cost: subscription}
  c: {cost: plan}
providers:
  claude: {start: "claude -p", vendor: anthropic, bill: a, roles: [author, reviewer], reader: claude-json}
  codex: {start: "codex exec", vendor: openai, bill: b, roles: [author, reviewer], reader: codex-json}
  minimax: {url: "https://example.invalid/v1", model: fixture, vendor: minimax, bill: c, roles: [reviewer]}
roles:
  author: [claude, codex]
  reviewer: [codex, claude, minimax]
"""

class Controls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "sd.db"; initialise(self.path)
        self.db = connect(self.path); self.addCleanup(self.db.close)
        self.providers = self.root / "providers.yaml"; self.providers.write_text(REGISTRY)
        original = registry.read
        self.registry_patch = patch.object(registry, "read", side_effect=lambda path=None, **kw: original(path or self.providers, **kw))
        self.registry_patch.start(); self.addCleanup(self.registry_patch.stop)
        self.repo = self.root / "pack"; self.repo.mkdir()
        self.git("init", "-b", "main"); self.git("config", "user.name", "Fixture"); self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "skills").mkdir(); (self.repo / "contrib/sd-test").mkdir(parents=True)
        self.skill = self.repo / "contrib/sd-test/SKILL.md"
        self.skill.write_text("---\nname: sd-test\ndescription: Use when testing a workflow.\n---\n## When to use\nFor a bounded fixture.\n")
        (self.repo / "skills/paths.json").write_text(json.dumps({"paths": {"build": {"skills": []}}}))
        self.git("add", "."); self.git("commit", "-m", "Fixture\n\nAuthored-with: codex/openai")
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "clone", "--bare", str(self.repo), str(self.remote)], capture_output=True, check=True)
        upsert_repo(self.db, str(self.repo), remote=str(self.remote), status_source="row")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    def snapshot(self):
        return tuple(self.db.iterdump())

    def catalog(self):
        return skills_catalog.catalog(self.db, root=self.repo, home=self.root)

    def test_provider_replacement_is_atomic_and_guarded(self):
        state = provider_controls.snapshot(self.db)
        enabled = {p["name"]: p["enabled"] for p in state["providers"]}; enabled["codex"] = False
        before = self.snapshot()
        with self.assertRaisesRegex(SdDbError, "first in both"):
            provider_controls.configure(self.db, enabled=enabled, orders=state["orders"], expected_revision=state["revision"], who="operator")
        self.assertEqual(before, self.snapshot())
        orders = {**state["orders"], "reviewer": ["minimax", "claude", "codex"]}
        changed = provider_controls.configure(self.db, enabled=enabled, orders=orders, expected_revision=state["revision"], who="operator")
        self.assertFalse(registry.read(connection=self.db).providers["codex"].enabled)
        self.assertEqual(registry.read(connection=self.db).resolve("reviewer").name, "minimax")
        self.assertEqual(self.providers.read_text(), REGISTRY)
        before = self.snapshot()
        with self.assertRaises(workflow.StaleItem):
            provider_controls.configure(self.db, enabled=enabled, orders=orders, expected_revision=state["revision"], who="operator")
        self.assertEqual(before, self.snapshot()); self.assertNotEqual(changed["revision"], state["revision"])

    def file_hash(self):
        return hashlib.sha256(self.providers.read_bytes()).hexdigest()

    def test_provider_subset_keeps_capability_and_enabled_state(self):
        state = provider_controls.snapshot(self.db)
        enabled = {entry["name"]: entry["enabled"] for entry in state["providers"]}
        orders = {**state["orders"], "reviewer": ["codex", "claude"]}
        changed = provider_controls.configure(self.db, enabled=enabled, orders=orders,
            expected_revision=state["revision"], who="operator")
        self.assertEqual(changed["orders"], orders)
        minimax = registry.read(connection=self.db).providers["minimax"]
        self.assertTrue(minimax.enabled)
        self.assertEqual(minimax.roles, ("reviewer",))
        self.assertEqual(minimax.ranks, {})
        self.assertEqual(changed["bills"], state["bills"])
        self.assertEqual(self.providers.read_text(), REGISTRY)
        self.assertEqual(changed["order_policy"], {"schema_version": 1,
            "explicit_providers": ["claude", "codex", "minimax"]})
        restored = provider_controls.configure(self.db, enabled=enabled, orders=state["orders"],
            expected_revision=changed["revision"], who="operator")
        self.assertEqual(restored["orders"], state["orders"])

    def test_first_explicit_provider_save_changes_revision_then_identical_save_writes_nothing(self):
        state = provider_controls.snapshot(self.db)
        self.db.execute("UPDATE provider SET reviewer_rank=NULL WHERE name='minimax'")
        self.assertEqual(provider_controls.snapshot(self.db), state)
        self.assertIsNone(state["order_policy"])
        enabled = {entry["name"]: entry["enabled"] for entry in state["providers"]}
        changed = provider_controls.configure(self.db, enabled=enabled, orders=state["orders"],
            expected_revision=state["revision"], who="operator")
        self.assertEqual(changed["orders"], state["orders"])
        self.assertNotEqual(changed["revision"], state["revision"])
        before, writes_before = self.snapshot(), self.db.total_changes
        self.assertEqual(provider_controls.configure(self.db, enabled=enabled, orders=changed["orders"],
            expected_revision=changed["revision"], who="operator"), changed)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.db.total_changes, writes_before)
        with self.assertRaises(workflow.StaleItem):
            provider_controls.configure(self.db, enabled=enabled, orders=state["orders"],
                expected_revision=state["revision"], who="operator")
        with self.assertRaises(workflow.StaleItem):
            provider_controls.set_cap(self.db, "c", 1.0,
                expected_revision=state["revision"], who="operator")

    def test_provider_rank_only_change_preserves_disabled_reason(self):
        provider_controls.snapshot(self.db)
        self.db.execute("UPDATE provider SET enabled=0, reason='Operator paused this endpoint' WHERE name='minimax'")
        state = provider_controls.snapshot(self.db)
        enabled = {entry["name"]: entry["enabled"] for entry in state["providers"]}
        changed = provider_controls.configure(self.db, enabled=enabled,
            orders={**state["orders"], "reviewer": ["codex", "claude"]},
            expected_revision=state["revision"], who="different operator")
        before_entry = next(entry for entry in state["providers"] if entry["name"] == "minimax")
        after_entry = next(entry for entry in changed["providers"] if entry["name"] == "minimax")
        self.assertEqual({**after_entry, "ranks": before_entry["ranks"]}, before_entry)

    def test_invalid_provider_membership_refuses_before_writes(self):
        state = provider_controls.snapshot(self.db)
        enabled = {entry["name"]: entry["enabled"] for entry in state["providers"]}
        bad_orders = ({"author": ["claude"]}, {**state["orders"], "reviewer": []},
            {**state["orders"], "reviewer": ["codex", "codex"]},
            {**state["orders"], "reviewer": ["unknown"]},
            {**state["orders"], "author": ["minimax"]},
            {**state["orders"], "reviewer": ["claude"]},
            {**state["orders"], "reviewer": [1]},
            {**state["orders"], "reviewer": "codex"})
        before, writes_before = self.snapshot(), self.db.total_changes
        for orders in bad_orders:
            with self.subTest(orders=orders), self.assertRaises(SdDbError):
                provider_controls.configure(self.db, enabled=enabled, orders=orders,
                    expected_revision=state["revision"], who="operator")
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.db.total_changes, writes_before)
        with self.assertRaises(SdDbError):
            provider_controls.configure(self.db, enabled={**enabled, "minimax": False},
                orders={**state["orders"], "reviewer": ["minimax"]},
                expected_revision=state["revision"], who="operator")
        self.assertEqual(self.snapshot(), before)

    def test_provider_rank_and_membership_writes_rollback_together(self):
        state = provider_controls.snapshot(self.db)
        enabled = {entry["name"]: entry["enabled"] for entry in state["providers"]}
        before = self.snapshot()
        original = writes.record_state
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated interruption after checkpoint write")
        with patch.object(writes, "record_state", side_effect=interrupted), self.assertRaises(RuntimeError):
            provider_controls.configure(self.db, enabled=enabled,
                orders={**state["orders"], "reviewer": ["codex", "claude"]},
                expected_revision=state["revision"], who="operator")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(provider_controls.snapshot(self.db), state)

    def test_provider_membership_survives_temporary_yaml_removal(self):
        state = provider_controls.snapshot(self.db)
        enabled = {entry["name"]: entry["enabled"] for entry in state["providers"]}
        orders = {**state["orders"], "reviewer": ["codex", "claude"]}
        provider_controls.configure(self.db, enabled=enabled, orders=orders,
            expected_revision=state["revision"], who="operator")
        reduced = "\n".join(line for line in REGISTRY.splitlines() if not line.startswith("  minimax:"))
        self.providers.write_text(reduced.replace("[codex, claude, minimax]", "[codex, claude]"))
        state = provider_controls.snapshot(self.db)
        changed = provider_controls.configure(self.db, enabled={"claude": True, "codex": True},
            orders=orders, expected_revision=state["revision"], who="operator")
        self.assertIn("minimax", changed["order_policy"]["explicit_providers"])
        self.providers.write_text(REGISTRY)
        self.assertEqual(provider_controls.snapshot(self.db)["orders"], orders)

    def test_a_cap_write_is_a_bill_row_and_the_file_keeps_its_hash(self):
        """Criterion 13's cap clause: the row carries the number, the file's
        hash is the snapshot's own `configuration_sha256`, before and after."""
        state = provider_controls.snapshot(self.db)
        self.assertEqual(state["configuration_sha256"], self.file_hash())
        self.assertEqual([bill["name"] for bill in state["bills"]], ["a", "b", "c"])
        self.assertIsNone(next(bill["cap_usd_month"] for bill in state["bills"] if bill["name"] == "c"))
        changed = provider_controls.set_cap(self.db, "c", 12.5, expected_revision=state["revision"], who="operator")
        self.assertEqual(registry.read(connection=self.db).bills["c"].cap_usd_month, 12.5)
        self.assertEqual(self.db.execute("SELECT cap_usd_month FROM bill WHERE name='c'").fetchone()[0], 12.5)
        self.assertEqual(next(bill["cap_usd_month"] for bill in changed["bills"] if bill["name"] == "c"), 12.5)
        self.assertEqual(self.providers.read_text(), REGISTRY)
        self.assertEqual(changed["configuration_sha256"], self.file_hash())
        self.assertEqual(changed["configuration_sha256"], state["configuration_sha256"])
        self.assertNotEqual(changed["revision"], state["revision"])
        cleared = provider_controls.set_cap(self.db, "c", None, expected_revision=changed["revision"], who="operator")
        self.assertIsNone(registry.read(connection=self.db).bills["c"].cap_usd_month)
        self.assertEqual(cleared["configuration_sha256"], self.file_hash())

    def test_a_cap_on_a_bill_a_start_entry_holds_is_the_readers_refusal(self):
        """Clause 15.15: the refusal names the entry and the bill in the
        reader's words, and nothing is written -- not even a row the
        transaction then rolls back, which `total_changes` would count."""
        state = provider_controls.snapshot(self.db)
        before, changes = self.snapshot(), self.db.total_changes
        with self.assertRaises(RegistryError) as refused:
            provider_controls.set_cap(self.db, "a", 5.0, expected_revision=state["revision"], who="operator")
        message = str(refused.exception)
        self.assertEqual(self.db.total_changes, changes, "the refusal comes before any write")
        self.assertIn("provider 'claude' is a 'start' entry on the capped bill 'a'", message)
        self.assertIn("nothing enforces", message)
        with self.assertRaises(RegistryError) as reader:
            registry.parse(REGISTRY.replace("a: {cost: subscription}", "a: {cost: subscription, cap_usd_month: 5.0}"), self.providers)
        self.assertEqual(message, str(reader.exception))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(provider_controls.snapshot(self.db), state)
        self.assertEqual(self.providers.read_text(), REGISTRY)

    def test_a_stale_revision_an_unknown_bill_and_a_nan_cap_write_nothing(self):
        state = provider_controls.snapshot(self.db)
        provider_controls.set_cap(self.db, "c", 1.0, expected_revision=state["revision"], who="operator")
        before = self.snapshot()
        with self.assertRaises(workflow.StaleItem):
            provider_controls.set_cap(self.db, "c", 2.0, expected_revision=state["revision"], who="operator")
        current = provider_controls.snapshot(self.db)
        with self.assertRaisesRegex(SdDbError, "no bill 'd'"):
            provider_controls.set_cap(self.db, "d", 2.0, expected_revision=current["revision"], who="operator")
        for bad in (float("nan"), -1.0, True):
            with self.assertRaises(ledger.LedgerRefused):
                provider_controls.set_cap(self.db, "c", bad, expected_revision=current["revision"], who="operator")
        self.assertEqual(before, self.snapshot())

    def test_a_legacy_row_cap_on_a_start_bill_is_reported_and_cleared(self):
        """#433's review: a cap a bare `UPDATE` left on a `start` entry's
        bill must not fail every read, and `set_cap(..., None)` is the
        repair -- it clears the row the merged view no longer shows."""
        provider_controls.snapshot(self.db)  # seeds the rows the bare UPDATE below needs
        self.assertEqual(self.db.execute("UPDATE bill SET cap_usd_month = 5.0 WHERE name = 'a'").rowcount, 1)
        state = provider_controls.snapshot(self.db)
        self.assertEqual(len(state["warnings"]), 1)
        self.assertIn("provider 'claude' is a 'start' entry on the capped bill 'a'", state["warnings"][0])
        self.assertIsNone(next(bill["cap_usd_month"] for bill in state["bills"] if bill["name"] == "a"))
        with self.assertRaises(RegistryError):
            provider_controls.set_cap(self.db, "a", 6.0, expected_revision=state["revision"], who="operator")
        cleared = provider_controls.set_cap(self.db, "a", None, expected_revision=state["revision"], who="operator")
        self.assertIsNone(self.db.execute("SELECT cap_usd_month FROM bill WHERE name='a'").fetchone()[0])
        self.assertEqual(cleared["warnings"], [])
        self.assertNotEqual(cleared["revision"], state["revision"])
        self.assertEqual(provider_controls.snapshot(self.db), cleared)

    @hub_only
    def test_raising_a_cap_lets_a_refused_bill_reserve_again(self):
        """Criterion 13's raised-cap test: a bill the ledger passed over at
        its cap takes the same reservation once the cap is raised."""
        state = provider_controls.snapshot(self.db)
        state = provider_controls.set_cap(self.db, "c", 0.10, expected_revision=state["revision"], who="operator")
        ledger.reserve(self.db, bill="c", bound=0.10, call_id="one", owner_pid=os.getpid())
        ledger.claim(self.db, "one")
        self.assertTrue(ledger.settle(self.db, "one", usd=0.10))
        with self.assertRaises(ledger.LedgerRefused) as refused:
            ledger.reserve(self.db, bill="c", bound=0.10, call_id="two", owner_pid=os.getpid())
        self.assertEqual(refused.exception.scope, "bill")
        self.assertEqual(refused.exception.name, "c")
        provider_controls.set_cap(self.db, "c", 1.00, expected_revision=state["revision"], who="operator")
        row = ledger.reserve(self.db, bill="c", bound=0.10, call_id="two", owner_pid=os.getpid())
        self.assertEqual(self.db.execute("SELECT source, bill FROM cost WHERE id=?", (row,)).fetchone()[:2], ("reserved", "c"))

    def test_use_rows_come_back_oldest_first_with_their_time_skill_and_surface(self):
        record_skill_use(self.db, "sd-later", surface="codex", mode="direct", timestamp="2026-09-22T10:00:00Z")
        record_skill_use(self.db, "sd-earlier", surface="claude", mode="direct", timestamp="2026-09-01T10:00:00Z")
        rows = skills_catalog.use_rows(self.db)
        self.assertEqual([(row["skill"], row["surface"]) for row in rows], [("sd-earlier", "claude"), ("sd-later", "codex")])
        self.assertEqual(sorted(rows[0]), ["skill", "surface", "timestamp"])

    def test_catalog_reads_files_and_usage_without_mutation(self):
        record_skill_use(self.db, "sd-test", surface="codex", mode="direct")
        before = self.snapshot()
        skill = self.catalog()["skills"][0]
        self.assertEqual(skill["status"], "contrib"); self.assertIn("bounded fixture", skill["when"])
        self.assertEqual(skill["uses"], [{"surface": "codex", "mode": "direct", "count": 1}])
        self.assertEqual(before, self.snapshot())
        started = skills_catalog.trial(self.db, "sd-test", expected_revision=skill["revision"], root=self.repo, home=self.root)
        self.assertEqual(started["status"], "trial"); self.assertTrue(self.skill.exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_catalog_reads_block_descriptions_without_consuming_other_metadata(self):
        lines = [
            "Remove signs of AI-generated writing from text. Use when editing or reviewing",
            "text to make it sound more natural and human-written.",
            "",
            "Detects and fixes filler phrases.",
        ]
        for style, expected in (
            ("|", "\n".join(lines)),
            ("|-", "\n".join(lines)),
            ("|2-", "\n".join(lines)),
            (">", " ".join(lines[:2]) + "\n" + lines[3]),
            (">+", " ".join(lines[:2]) + "\n" + lines[3]),
            (">-2 # Wrapped description", " ".join(lines[:2]) + "\n" + lines[3]),
        ):
            with self.subTest(style=style):
                self.skill.write_text(
                    "---\nname: sd-test\ndescription: " + style + "\n"
                    + "\n".join("  " + line for line in lines)
                    + "\nlicense: MIT\nmetadata:\n  version: \"2.9.1\"\n---\n# Skill\n"
                )
                before = self.snapshot()
                skill = self.catalog()["skills"][0]
                self.assertEqual(skill["description"], expected)
                self.assertEqual(skill["when"], " ".join(expected.split()))
                self.assertEqual(before, self.snapshot())

    def test_catalog_empty_description_does_not_read_the_next_field(self):
        for description in ("description:\n", "description: |\n", ""):
            with self.subTest(description=description):
                self.skill.write_text(
                    "---\nname: sd-test\n" + description
                    + "license: MIT\n---\n# Skill\n"
                )
                skill = self.catalog()["skills"][0]
                self.assertEqual(skill["description"], "No description declared.")

    def test_catalog_folded_description_preserves_indented_examples(self):
        self.skill.write_text(
            "---\nname: sd-test\ndescription: >-\n"
            "  Run the example:\n    sd status\n  Then inspect the result.\n"
            "license: MIT\n---\n# Skill\n"
        )
        skill = self.catalog()["skills"][0]
        self.assertEqual(skill["description"], "Run the example:\n  sd status\nThen inspect the result.")

    def test_review_accept_many_queues_one_and_preserves_dirty_checkout(self):
        (self.repo / "unrelated.txt").write_text("Operator work")
        before_git = self.git("status", "--porcelain")
        state = skills_catalog.request(self.db, "sd-test", "review", root=self.repo, home=self.root, who="operator")
        item, assignment = state["item"]["id"], state["assignments"][0]["id"]
        self.assertEqual(state["assignments"][0]["role"], "reviewer")
        registry.seed(self.db, registry.read(connection=self.db))
        self.db.execute("UPDATE assignment SET status='running',provider='claude' WHERE id=?", (assignment,))
        source = json.loads(state["item"]["fields"])["skill_review"]
        document = {"version": 1, "item": item, "source_sha256": source["source_sha256"], "proposals": [
            {"path": "contrib/sd-test/SKILL.md", "line_start": 5, "line_end": 6, "body": "Clarify entry condition"},
            {"path": "contrib/sd-test/SKILL.md", "line_start": 6, "line_end": 6, "body": "Give a bounded example"}]}
        before = self.snapshot()
        bad = {**document, "proposals": [document["proposals"][0], {**document["proposals"][1], "line_end": 900}]}
        with self.assertRaises(workflow.WorkflowError):
            skills_catalog.record_review_proposals(self.db, item, assignment, "claude", bad)
        self.assertEqual(self.snapshot(), before)
        state = skills_catalog.record_review_proposals(self.db, item, assignment, "claude", document)
        notes = [note["id"] for note in state["notes"] if note["kind"] == "proposal"]
        with self.assertRaisesRegex(workflow.WorkflowError, "wait"):
            skills_catalog.apply_proposals(self.db, item, notes, expected_revision=state["revision"], who="operator")
        self.db.execute("UPDATE assignment SET status='done' WHERE id=?", (assignment,))
        forged = add_note(self.db, item, "proposal", json.dumps(document["proposals"][0]))
        state = workflow.item_state(self.db, item)
        with self.assertRaises(workflow.WorkflowError):
            skills_catalog.apply_proposals(self.db, item, [forged], expected_revision=state["revision"], who="operator")
        result = skills_catalog.apply_proposals(self.db, item, notes, expected_revision=state["revision"], who="operator")
        self.assertEqual(len(result["assignments"]), 1)
        self.assertEqual(result["assignments"][0]["scope"], "skill-apply")
        self.assertEqual(self.db.execute("SELECT count(*) FROM assignment").fetchone()[0], 2)
        self.assertEqual(self.git("status", "--porcelain"), before_git)
        with self.assertRaises(workflow.StaleItem):
            skills_catalog.apply_proposals(self.db, item, notes, expected_revision=state["revision"], who="operator")

    def test_unknown_authorship_and_changed_skill_refuse_before_queue(self):
        self.skill.write_text(self.skill.read_text() + "Changed\n")
        before = self.snapshot()
        with self.assertRaisesRegex(workflow.WorkflowError, "commit"):
            skills_catalog.request(self.db, "sd-test", "promote", path_name="build", root=self.repo, home=self.root, who="operator")
        self.assertEqual(before, self.snapshot())
        self.git("add", "."); self.git("commit", "-m", "Unattributed")
        with self.assertRaisesRegex(workflow.WorkflowError, "Authored-with"):
            skills_catalog.request(self.db, "sd-test", "review", root=self.repo, home=self.root, who="operator")
        self.assertEqual(before, self.snapshot())

    def test_setup_owned_work_and_atomic_stale_batch(self):
        items = [create_item(self.db, kind="task", title=str(n)) for n in range(2)]
        for item in items:
            runner_controls.configure_item(self.db, item, repo=str(self.repo), branch="work/new", expected_revision=workflow.item_state(self.db, item)["revision"], who="operator")
        fields = json.loads(workflow.item_state(self.db, items[0])["item"]["fields"])
        self.assertEqual(fields["runner_branch"], {"source_commit": self.git("rev-parse", "HEAD"), "source_branch": "main", "remote_absent": True})
        self.assertEqual(self.git("branch", "--format=%(refname:short)"), "main")
        revisions = {str(item): workflow.item_state(self.db, item)["revision"] for item in items}
        set_item_fields(self.db, items[1], title="Changed")
        before = self.snapshot()
        with self.assertRaises(SdDbError):
            runner_controls.enqueue(self.db, items, revisions=revisions, who="operator")
        self.assertEqual(before, self.snapshot())
        upsert_repo(self.db, str(self.repo), status_source="file")
        work = create_item(self.db, kind="work", title="Owned", repo=str(self.repo))
        with self.assertRaisesRegex(workflow.WorkflowError, "file owner"):
            runner_controls.configure_item(self.db, work, repo=str(self.repo), branch="main", expected_revision=workflow.item_state(self.db, work)["revision"], who="operator")

    def install_runner(self, *, database=None):
        launcher = self.root / "service/local-sd-runner/runner.sh"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexit 0\n"); launcher.chmod(0o755)
        (launcher.parent / "sd_runner").mkdir()
        (launcher.parent / "sd_runner/bootstrap.py").write_text("# installed fixture\n")
        config = self.root / "runner.json"; config.write_text(json.dumps({"database": str(database or self.path)}))
        plist = self.root / f"Library/LaunchAgents/{LABEL_PREFIX}.sd-runner.plist"; plist.parent.mkdir(parents=True)
        plist.write_bytes(plistlib.dumps({"Label": LABEL_PREFIX + ".sd-runner", "ProgramArguments": [str(launcher), "serve", "--config", str(config)],
                                         "EnvironmentVariables": {"SD_RUNNER_PYTHON": sys.executable}}))
        return plist

    @hub_only
    def test_installed_control_binds_database_revision_and_attempt(self):
        item = create_item(self.db, kind="task", title="Owned", repo=str(self.repo), branch="main", status="ready")
        assignment = runner.enqueue(self.db, [item], who="operator")[0]["id"]
        runner.claim(self.db, assignment, owner="fixture", work_root=self.root / "work", retention_root=self.root / "retained")
        state = runner.queue_state(self.db, assignment)
        plist = self.install_runner()
        calls = []
        def backend(installation, verb, assignment, **options):
            calls.append((installation, verb, assignment, options)); return {"accepted": True}
        before = self.snapshot()
        with self.assertRaises(workflow.StaleItem):
            runner_controls.control(self.db, assignment, "cancel", expected_revision="0" * 64, backend=backend, home=self.root, who="operator")
        self.assertEqual(calls, [])
        runner_controls.control(self.db, assignment, "cancel", expected_revision=state["revision"], backend=backend, home=self.root, who="operator")
        self.assertEqual(calls[0][3]["run"], state["run"]["id"])
        self.assertEqual(calls[0][0]["database"], str(self.path))
        self.assertEqual(before, self.snapshot())
        record = plistlib.loads(plist.read_bytes()); record["ProgramArguments"] += ["--arbitrary", "shell"]
        plist.write_bytes(plistlib.dumps(record))
        with self.assertRaisesRegex(workflow.WorkflowError, "unsupported"):
            runner_controls.control(self.db, assignment, "cancel", expected_revision=state["revision"], backend=backend, home=self.root, who="operator")
        self.assertEqual(len(calls), 1)

    def test_multi_author_review_excludes_every_vendor(self):
        self.skill.write_text(self.skill.read_text() + "Example\n")
        self.git("add", "."); self.git("commit", "-m", "Several authors\n\nAuthored-with: codex/openai\nAuthored-with: claude/anthropic")
        state = skills_catalog.request(self.db, "sd-test", "review", root=self.repo, home=self.root, who="operator")
        source = json.loads(state["item"]["fields"])["skill_review"]
        self.assertEqual(source["latest_author_vendors"], ["anthropic", "openai"])
        self.assertIsNone(source["latest_author_vendor"])

    def test_report_replay_attention_and_acknowledgement(self):
        log = self.root / "job.log"; log.write_text("Earlier\nSD_REPORT_ATTENTION: Check a stale reference\n")
        args = {"job": "brief", "run_id": "one", "started": "2026-09-08T00:00:00Z", "ended": "2026-09-08T00:01:00Z", "exit_code": 0, "log_path": log, "offset": 8}
        state = reporting.ingest_log(self.db, **args)
        self.assertTrue(json.loads(state["item"]["fields"])["attention"])
        self.assertNotIn("Earlier", state["item"]["body"])
        before = self.snapshot(); reporting.ingest_log(self.db, **args); self.assertEqual(before, self.snapshot())
        item = state["item"]["id"]
        with self.assertRaisesRegex(workflow.WorkflowError, "followups"):
            reporting.acknowledge(self.db, item, expected_revision=state["revision"], who="operator")
        for note in state["notes"]:
            if note["kind"] == "followup": resolve_note(self.db, note["id"])
        state = workflow.item_state(self.db, item)
        self.assertEqual(reporting.acknowledge(self.db, item, expected_revision=state["revision"], who="operator")["item"]["status"], "done")
        self.assertIn("planning -> done by operator",
                      [note["body"] for note in workflow.item_state(self.db, item)["notes"]])
        log.write_text("Changed")
        with self.assertRaisesRegex(workflow.WorkflowError, "different evidence"):
            reporting.ingest_log(self.db, **args)


class AnAcknowledgementNamesItsOperator(unittest.TestCase):
    """`reporting.acknowledge` has no default `who` (sd:747).

    281 report items went `planning -> done by user` inside one second, every
    note carrying the same timestamp: one loop, one call site, and a store
    that cannot say what ran it. `"user"` was the default on `acknowledge`, so
    it is the name a caller gets for supplying nothing -- and both shipped
    callers supply something, which leaves the literal reachable only from a
    library call that declined to say who was asking. Removing the default
    makes `acknowledge` refuse that call the way `writes.transition`, which it
    is a thin wrapper over, already did.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "sd.db"; initialise(self.path)
        self.db = connect(self.path); self.addCleanup(self.db.close)

    def report(self):
        """One clean report, acknowledgeable: no followup stands in the way."""
        log = self.root / "job.log"; log.write_text("all quiet\n")
        item = create_item(self.db, kind="report", title="nightly: run report",
                           status="planning", source="cron-report",
                           external_id=f"nightly:{id(self)}-{self.db.total_changes}",
                           fields={"attention": False}, body={"text": "all quiet"},
                           session="cron")
        return item, workflow.item_state(self.db, item)["revision"]

    def statuses(self, item):
        return [note["body"] for note in workflow.item_state(self.db, item)["notes"]
                if note["kind"] == "status_change"]

    def test_omitting_who_is_a_type_error_and_moves_nothing(self):
        """The refusal is the point, and it has to land before the write.

        A `TypeError` raised after the transition would still leave an
        unattributed row behind, so the status is asserted too.
        """
        item, revision = self.report()
        with self.assertRaises(TypeError) as raised:
            reporting.acknowledge(self.db, item, expected_revision=revision)
        self.assertIn("who", str(raised.exception))
        state = workflow.item_state(self.db, item)
        self.assertEqual(state["item"]["status"], "planning")
        self.assertEqual(self.statuses(item), ["opened as planning"])

    def test_the_default_that_produced_the_batch_cannot_be_reached_by_omission(self):
        """No caller gets `"user"` for free; a caller that wants it says so.

        Asserting on the signature and not only on the call keeps the guard
        honest against a default reintroduced under a different literal.
        """
        who = inspect.signature(reporting.acknowledge).parameters["who"]
        self.assertIs(who.default, inspect.Parameter.empty)
        self.assertIs(who.kind, inspect.Parameter.KEYWORD_ONLY)
        beneath = inspect.signature(transition).parameters["who"]
        self.assertIs(who.default, beneath.default)
        self.assertIs(who.kind, beneath.kind)

    def test_both_shipped_callers_still_acknowledge_under_their_own_name(self):
        """`server.py` sends `"dashboard"`; `bin/sd_controls.py` sends the login account.

        Neither passes through the removed default, so neither changes; the
        names they send are what the history has to read back.
        """
        for name in ("dashboard", getpass.getuser()):
            with self.subTest(who=name):
                item, revision = self.report()
                state = reporting.acknowledge(self.db, item, expected_revision=revision, who=name)
                self.assertEqual(state["item"]["status"], "done")
                self.assertEqual(self.statuses(item),
                                 ["opened as planning", f"planning -> done by {name}"])


class AnAcknowledgementResolvesTheIngestsOwnFollowup(unittest.TestCase):
    """`reporting.acknowledge` can resolve the followup `ingest` wrote (sd:921).

    Every report `ingest` opens for a failed run carries one unresolved
    followup, written by the ingest itself under session `"cron"`. The
    refusal at `acknowledge` then blocks every one of them: 51 watchdog
    reports on the live store on 2026-09-15, none acknowledgeable from the
    command line. `resolve_ingest_followups=True` resolves those notes, and
    only those, inside the same transaction as the transition, and the
    status note records that it did under the same `who`. A followup a
    person wrote is somebody's owed work and still blocks, keyword or not;
    the refusal names the notes and the verb that resolves one.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "sd.db"; initialise(self.path)
        self.db = connect(self.path); self.addCleanup(self.db.close)

    def failed_run(self, run="one"):
        """One failed run, as `cron-jobs.sh` files it: born with the ingest's followup."""
        log = self.root / f"{run}.log"; log.write_text("boom\n")
        state = reporting.ingest_log(self.db, job="watchdog", run_id=run, started="2026-09-15T00:00:00Z",
                                     ended="2026-09-15T00:01:00Z", exit_code=1, log_path=log)
        return state["item"]["id"], state["revision"]

    def followups(self, item):
        return [(note["id"], note["session"], note["resolved_at"] is not None)
                for note in workflow.item_state(self.db, item)["notes"] if note["kind"] == "followup"]

    def statuses(self, item):
        return [(note["body"], note["session"]) for note in workflow.item_state(self.db, item)["notes"]
                if note["kind"] == "status_change"]

    def test_the_refusal_names_the_followup_and_the_verb_that_resolves_it(self):
        item, revision = self.failed_run()
        (note, session, resolved), = self.followups(item)
        self.assertEqual((session, resolved), ("cron", False))
        with self.assertRaises(workflow.WorkflowError) as raised:
            reporting.acknowledge(self.db, item, expected_revision=revision, who="operator")
        self.assertIn(f"sd note resolve {note}", str(raised.exception))
        self.assertEqual(workflow.item_state(self.db, item)["item"]["status"], "planning")
        self.assertEqual(self.followups(item), [(note, "cron", False)])

    def test_the_keyword_resolves_the_ingests_followup_and_the_status_note_says_so(self):
        item, revision = self.failed_run()
        (note, _, _), = self.followups(item)
        state = reporting.acknowledge(self.db, item, expected_revision=revision, who="operator",
                                      resolve_ingest_followups=True)
        self.assertEqual(state["item"]["status"], "done")
        self.assertEqual(self.followups(item), [(note, "cron", True)])
        self.assertEqual(self.statuses(item), [("opened as planning", "cron"),
                                               (f"planning -> done by operator: resolved the ingest's followup {note}", "operator")])

    def test_a_followup_a_person_wrote_still_blocks_and_nothing_is_resolved(self):
        item, _ = self.failed_run()
        owed = add_note(self.db, item, "followup", "check the disk too", session="alex")
        revision = workflow.item_state(self.db, item)["revision"]
        (cron, _, _), _ = self.followups(item)
        with self.assertRaises(workflow.WorkflowError) as raised:
            reporting.acknowledge(self.db, item, expected_revision=revision, who="operator",
                                  resolve_ingest_followups=True)
        self.assertIn(f"sd note resolve {owed}", str(raised.exception))
        self.assertNotIn(f"sd note resolve {cron}", str(raised.exception))
        self.assertEqual(workflow.item_state(self.db, item)["item"]["status"], "planning")
        self.assertEqual(self.followups(item), [(cron, "cron", False), (owed, "alex", False)])

    def test_a_person_writing_under_the_ingests_session_still_blocks(self):
        """`workflow.add_item_note` writes its `who` as `note.session`, so `"cron"` is a word anyone can say.

        The ingest's followup is its session and its text together, the text
        derived from the report's own provenance; a followup that only borrows
        the session is somebody's note and stays open.
        """
        item, _ = self.failed_run()
        borrowed = workflow.add_item_note(self.db, item, body="check the disk too", kind="followup", who="cron")["note"]["id"]
        revision = workflow.item_state(self.db, item)["revision"]
        (cron, _, _), _ = self.followups(item)
        with self.assertRaises(workflow.WorkflowError) as raised:
            reporting.acknowledge(self.db, item, expected_revision=revision, who="operator",
                                  resolve_ingest_followups=True)
        self.assertIn(f"sd note resolve {borrowed}", str(raised.exception))
        self.assertEqual(self.followups(item), [(cron, "cron", False), (borrowed, "cron", False)])

    def test_unreadable_fields_refuse_with_the_note_rather_than_resolving_on_a_guess(self):
        """The text is derived from `fields.report`; fields that cannot say match nothing.

        The default path never reads `fields` (the dashboard's notice sends
        such a report to the command line on that promise), so it is checked
        here too: the same row finishes once the note is resolved by hand.
        """
        item, _ = self.failed_run()
        (note, _, _), = self.followups(item)
        self.db.execute("UPDATE item SET fields=? WHERE id=?", ("{not json", item)); self.db.commit()
        revision = workflow.item_state(self.db, item)["revision"]
        with self.assertRaises(workflow.WorkflowError) as raised:
            reporting.acknowledge(self.db, item, expected_revision=revision, who="operator",
                                  resolve_ingest_followups=True)
        self.assertIn(f"sd note resolve {note}", str(raised.exception))
        self.assertEqual(self.followups(item), [(note, "cron", False)])
        resolve_note(self.db, note)
        revision = workflow.item_state(self.db, item)["revision"]
        self.assertEqual(reporting.acknowledge(self.db, item, expected_revision=revision, who="operator")["item"]["status"], "done")

    def test_the_keyword_is_exactly_a_bool_because_it_changes_what_is_persisted(self):
        item, revision = self.failed_run()
        (note, _, _), = self.followups(item)
        for value in ("false", "yes", 1, 0, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(workflow.WorkflowError, "exactly True or False"):
                    reporting.acknowledge(self.db, item, expected_revision=revision, who="operator",
                                          resolve_ingest_followups=value)
        self.assertEqual(workflow.item_state(self.db, item)["item"]["status"], "planning")
        self.assertEqual(self.followups(item), [(note, "cron", False)])

    def test_the_resolve_and_the_transition_are_one_transaction(self):
        """A transition that fails leaves the followup open: no half-acknowledged row."""
        item, revision = self.failed_run()
        (note, _, _), = self.followups(item)
        with patch.object(reporting, "transition", side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):
                reporting.acknowledge(self.db, item, expected_revision=revision, who="operator",
                                      resolve_ingest_followups=True)
        self.assertEqual(self.followups(item), [(note, "cron", False)])
        self.assertEqual(workflow.item_state(self.db, item)["item"]["status"], "planning")

    def test_a_clean_report_acknowledges_the_same_with_the_keyword(self):
        """Nothing to resolve, nothing recorded: the status note is the plain one."""
        item = create_item(self.db, kind="report", title="nightly: run report", status="planning",
                           source="cron-report", external_id="nightly:clean", fields={"attention": False},
                           body={"text": "all quiet"}, session="cron")
        revision = workflow.item_state(self.db, item)["revision"]
        state = reporting.acknowledge(self.db, item, expected_revision=revision, who="operator",
                                      resolve_ingest_followups=True)
        self.assertEqual(state["item"]["status"], "done")
        self.assertEqual(self.statuses(item), [("opened as planning", "cron"), ("planning -> done by operator", "operator")])

    def test_the_keyword_is_off_by_default_and_who_still_has_no_default(self):
        parameters = inspect.signature(reporting.acknowledge).parameters
        self.assertIs(parameters["resolve_ingest_followups"].default, False)
        self.assertIs(parameters["resolve_ingest_followups"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parameters["who"].default, inspect.Parameter.empty)


class NoVerbNamesItsOperatorForTheCaller(unittest.TestCase):
    """No library function names the operator on the caller's behalf (sd:749).

    sd:747 removed `who="user"` from `reporting.acknowledge`; forty-two more
    functions in `sd_db` and two in `sd_runner.controls` carried the same
    default, `"user"` on most, `"sd-ship"` on `progress.deliver_work`,
    `"import"` on `writing.import_piece`. Each let a call that named nobody
    write a row naming somebody. Fixing them one at a time closes those
    instances and leaves the next verb free to reopen the hole, so this walks
    the source of both packages instead of listing the verbs.

    `principal` gets the first two rules with `who` (sd:755): it is what the
    caller's channel authenticated, and a default or a fallback there would
    record a login nobody gave.

    It refuses three shapes of the same thing: a default on the parameter; a
    fallback in the body (`who = who or "user"`); and a literal `"user"` handed
    to a `who=` argument, including one hidden as the default of a lookup
    (`os.environ.get("SD_SESSION", "user")`, which `sd-db work register` used).
    Every function is read, private helpers and nested ones included: a
    default on `_import_piece` writes the same row as one on `import_piece`.

    `local-sd-runner` is walked from here and not from its own suite so that
    one test owns the rule for the whole library the operator verbs live in;
    both folders are in the same checkout wherever this suite runs, and a
    missing one fails rather than skipping.
    """

    SYSTEM = Path(__file__).resolve().parents[2]
    ROOTS = (SYSTEM / "local-sd-db/sd_db", SYSTEM / "local-sd-runner/sd_runner")

    @staticmethod
    def functions(tree):
        return [node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]

    @staticmethod
    def who_parameters(function):
        arguments = function.args
        positional = arguments.posonlyargs + arguments.args
        defaults = [None] * (len(positional) - len(arguments.defaults)) + list(arguments.defaults)
        pairs = list(zip(positional, defaults)) + list(zip(arguments.kwonlyargs, arguments.kw_defaults))
        return [(argument, default) for argument, default in pairs if argument.arg in ("who", "principal")]

    def sources(self):
        for root in self.ROOTS:
            self.assertTrue(root.is_dir(), f"{root} is part of this checkout; the walk cannot cover it")
        return [(path.relative_to(self.SYSTEM).as_posix(), ast.parse(path.read_text(), str(path)))
                for root in self.ROOTS for path in sorted(root.rglob("*.py"))]

    @staticmethod
    def where(name, node, function=None):
        label = getattr(function, "name", "<lambda>") if function is not None else ""
        return f"{name}:{node.lineno} {label}".rstrip()

    def findings(self):
        seen, defaulted, fallbacks, literals = [], [], [], []
        for name, tree in self.sources():
            for function in self.functions(tree):
                parameters = self.who_parameters(function)
                seen.extend(f"{name} {getattr(function, 'name', '<lambda>')}" for _ in parameters)
                defaulted.extend(f"{self.where(name, function, function)} {argument.arg}={ast.unparse(default)}"
                                 for argument, default in parameters if default is not None)
                if not parameters:
                    continue
                for node in ast.walk(function):
                    if (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
                            and any(isinstance(value, ast.Name) and value.id in ("who", "principal")
                                    for value in node.values)
                            and any(isinstance(value, ast.Constant) and isinstance(value.value, str)
                                    for value in node.values)):
                        fallbacks.append(f"{self.where(name, node, function)}: {ast.unparse(node)}")
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        if keyword.arg == "who" and any(isinstance(part, ast.Constant) and part.value == "user"
                                                        for part in ast.walk(keyword.value)):
                            literals.append(f"{name}:{node.lineno}: who={ast.unparse(keyword.value)}")
        return seen, defaulted, fallbacks, literals

    def test_the_walk_sees_both_packages(self):
        """An empty walk passes every rule below, so it has to have found the verbs."""
        seen = self.findings()[0]
        self.assertGreaterEqual(len(seen), 40, seen)
        self.assertIn("local-sd-db/sd_db/writes.py transition", seen)
        self.assertIn("local-sd-runner/sd_runner/controls.py cancel", seen)
        self.assertEqual(seen.count("local-sd-db/sd_db/reporting.py acknowledge_clean"), 2, "who and principal")

    def test_no_who_parameter_has_a_default(self):
        self.assertEqual(self.findings()[1], [], "a default `who` names every caller and identifies none; "
                                                 "make it keyword-only with no default and let callers say who they are")

    def test_no_function_supplies_who_from_a_literal_in_its_body(self):
        self.assertEqual(self.findings()[2], [], "`who or \"...\"` is the removed default moved one line down")

    def test_no_call_passes_the_user_literal_as_who(self):
        self.assertEqual(self.findings()[3], [], "`\"user\"` names every caller; pass the account or session that is asking")


class AQuietTickWritesNoItem(unittest.TestCase):
    """The heartbeat-or-report split in `reporting.ingest_log` (sd:739)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "sd.db"; initialise(self.path)
        self.db = connect(self.path); self.addCleanup(self.db.close)
        self.log = self.root / "watchdog.log"
        self.log.write_text("")

    def tick(self, run, *, exit_code=0, text="nothing to do\n"):
        """One run of a quarter-hourly job, at the offset the previous one ended."""
        offset = self.log.stat().st_size
        with self.log.open("a") as stream:
            stream.write(text)
        return reporting.ingest_log(
            self.db, job="watchdog", run_id=run, started="2026-09-13T00:00:00Z",
            ended="2026-09-13T00:00:30Z", exit_code=exit_code, log_path=self.log, offset=offset)

    def items(self):
        return [dict(row) for row in self.db.execute(
            "SELECT id,title FROM item WHERE kind='report' ORDER BY id")]

    def beats(self):
        return [json.loads(row["body"]) for row in self.db.execute(
            "SELECT body FROM state WHERE kind='heartbeat' AND key=? ORDER BY id",
            (reporting.HEARTBEAT_KEY + "watchdog",))]

    def test_a_clean_tick_records_a_heartbeat_and_no_item(self):
        result = self.tick("one")
        self.assertEqual(result["recorded"], "heartbeat")
        self.assertEqual(self.items(), [])
        self.assertEqual([beat["healthy"] for beat in self.beats()], [True])

    def test_a_hundred_clean_ticks_leave_one_row_and_no_items(self):
        """The defect this change exists for, at the rate that produced it.

        `claude-mem-pro-watchdog` runs every fifteen minutes and had 102 open
        report items. A hundred ticks is that, and the assertion is on the
        count rather than on the newest row: a heartbeat per tick would pass
        any check that only reads the latest one.
        """
        for number in range(100):
            self.tick(f"run-{number}")
        self.assertEqual(self.items(), [])
        self.assertEqual(len(self.beats()), 100)
        # Which is what `retention.compact_heartbeats` is for, and it is the
        # rule the module documents rather than one this change invents.
        self.assertEqual(retention.compact_heartbeats(self.db), 99)
        self.assertEqual(len(self.beats()), 1)

    def test_a_failing_tick_still_files_a_report(self):
        state = self.tick("bad", exit_code=1, text="broke\n")
        self.assertEqual(state["recorded"], "report")
        self.assertTrue(json.loads(state["item"]["fields"])["attention"])
        self.assertEqual(len(self.items()), 1)
        self.assertEqual([beat["healthy"] for beat in self.beats()], [False])

    def test_a_clean_tick_that_declares_findings_still_files_a_report(self):
        state = self.tick("noisy", text="SD_REPORT_ATTENTION: two stale pins\n")
        self.assertEqual(state["recorded"], "report")
        self.assertEqual(json.loads(state["item"]["fields"])["report"]["attention_basis"],
                         "two stale pins")
        self.assertEqual(len(self.items()), 1)

    def test_the_recovery_files_a_report_and_the_tick_after_it_does_not(self):
        """The transition a quiet job would otherwise hide.

        A failing report stays `planning` and never says it stopped being true,
        so without a row here the operator reads a failure that was fixed hours
        ago and has no way to tell.
        """
        self.tick("bad", exit_code=1, text="broke\n")
        recovered = self.tick("good")
        self.assertEqual(recovered["recorded"], "report")
        self.assertEqual(recovered["why"], "the job recovered")
        self.assertFalse(json.loads(recovered["item"]["fields"])["attention"])
        self.assertEqual(len(self.items()), 2)
        # And the job is quiet again from here.
        self.assertEqual(self.tick("also-good")["recorded"], "heartbeat")
        self.assertEqual(len(self.items()), 2)

    def test_a_first_ever_clean_tick_is_a_baseline_and_not_a_recovery(self):
        self.assertIsNone(reporting.health(self.db, "watchdog"))
        self.assertEqual(self.tick("first")["recorded"], "heartbeat")
        self.assertEqual(self.items(), [])

    def test_replaying_one_run_writes_nothing_the_second_time(self):
        """`cron-jobs.sh` re-runs the ingest when the first attempt cannot reach
        the database, so a replay has to be a no-op on both paths."""
        self.tick("one")
        before = tuple(self.db.iterdump())
        again = reporting.ingest_log(
            self.db, job="watchdog", run_id="one", started="2026-09-13T00:00:00Z",
            ended="2026-09-13T00:00:30Z", exit_code=0, log_path=self.log,
            offset=self.log.stat().st_size)
        self.assertEqual(again["recorded"], "nothing")
        self.assertEqual(before, tuple(self.db.iterdump()))

    def test_one_job_going_dark_does_not_silence_another(self):
        """The heartbeat key is per job, so health is not a global flag."""
        self.tick("bad", exit_code=1, text="broke\n")
        other = self.root / "other.log"; other.write_text("fine\n")
        state = reporting.ingest_log(
            self.db, job="other", run_id="one", started="2026-09-13T00:00:00Z",
            ended="2026-09-13T00:00:30Z", exit_code=0, log_path=other, offset=0)
        self.assertEqual(state["recorded"], "heartbeat")
        self.assertIs(reporting.health(self.db, "other"), True)
        self.assertIs(reporting.health(self.db, "watchdog"), False)


if __name__ == "__main__": unittest.main()
