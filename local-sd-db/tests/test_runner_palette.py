"""CLI uses the same catalog, guards, durable note and no-replay contract as HTTP."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sd_db import runner_exec, workflow
from sd_db.database import connect
from sd_db.migrate import initialise
from sd_db.writes import create_item, upsert_repo


class PaletteCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.database = initialise(home=self.home)
        self.db = connect(home=self.home)
        self.addCleanup(self.db.close)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        upsert_repo(self.db, str(self.repo))
        self.item = create_item(self.db, kind="task", title="CLI fixture", repo=str(self.repo), status="ready")
        self.program = self.home / "fixture-command"
        self.program.write_text("#!/usr/bin/python3\nimport sys\nprint('fixture=' + sys.argv[1])\n")
        self.program.chmod(0o700)
        self.catalog = self.home / ".local/share/sd/commands.yaml"
        self.catalog.write_text("version: 1\ncommands:\n  inspect: " + json.dumps({
            "argv": [str(self.program), "{item}"], "screens": ["item"], "scope": "worktree",
            "mutates": False, "placeholders": {"item": "item"}}) + "\n")

    def cli(self, *arguments):
        return subprocess.run([sys.executable, "-m", "sd_db.runner_palette", "--home", str(self.home), *map(str, arguments)],
            capture_output=True, text=True, check=False, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})

    def prepare(self, *extra):
        return self.cli("prepare", "--item", self.item, "--command", "inspect", "--if-revision",
            workflow.item_state(self.db, self.item)["revision"], "--catalog",
            runner_exec.catalog(home=self.home)["sha256"], "--value", f"item={self.item}", *extra)

    def test_real_catalog_prepare_execute_and_output_match_database_record(self):
        catalog = self.cli("catalog", "--item", self.item)
        self.assertEqual(catalog.returncode, 0, catalog.stderr)
        self.assertEqual(json.loads(catalog.stdout)["item"], self.item)
        prepared = self.prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        note = json.loads(prepared.stdout)["execution"]["note"]
        executed = self.cli("execute", note)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        output = self.cli("output", note)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(json.loads(output.stdout)["output"], f"fixture={self.item}\n")
        self.assertEqual(self.cli("execute", note).returncode, 1)
        self.assertEqual(self.cli("reconcile", note).returncode, 0)

    def test_invalid_or_duplicate_values_and_stale_catalog_leave_no_note(self):
        for args in (("--value", "item=1"), ("--value", "raw=sh"), ("--catalog", "0" * 64),
                     ("--target-run", "foreign")):
            result = self.prepare(*args)
            self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM note WHERE kind='exec'").fetchone()[0], 0)

    def add_entry(self, name, **changes):
        entry = {"argv": ["/bin/ls", "-la"], "screens": ["item"], "scope": "worktree", "mutates": False, **changes}
        with self.catalog.open("a") as handle:
            handle.write("  " + name + ": " + json.dumps(entry) + "\n")

    def test_a_malformed_entry_is_rejected_by_name_and_rule_and_the_rest_stand(self):
        unreadable = self.home / "not-executable"
        unreadable.write_text("plain data\n"); unreadable.chmod(0o600)
        cases = {"shell-entry": ({"argv": ["/bin/sh", "-c", "ls"]}, "shell or inline program execution is not a palette command"),
                 "relative-entry": ({"argv": ["bin/ls"]}, "worktree command needs a fixed absolute executable as argv[0]"),
                 "plain-file-entry": ({"argv": [str(unreadable)]}, "registered command is not executable"),
                 "undeclared-entry": ({"argv": ["/bin/ls", "{item}"]}, "placeholder declarations do not match arguments: argv uses item; placeholders declare none"),
                 "kitchen-entry": ({"screens": ["kitchen"]}, "invalid screen inventory; screens are backlog, item, operations, skills, today, writing")}
        for name, (changes, rule) in cases.items():
            self.add_entry(name, **changes)
        result = self.cli("catalog", "--item", self.item)
        self.assertEqual(result.returncode, 0, result.stderr)
        inventory = json.loads(result.stdout)
        self.assertTrue(inventory["configured"])
        self.assertEqual([entry["name"] for entry in inventory["entries"]], ["inspect"])
        self.assertEqual(inventory["rejected"], [{"name": name, "reason": rule} for name, (_, rule) in cases.items()])
        self.assertEqual(result.stderr.splitlines(), [f"rejected: {name}: {rule}" for name, (_, rule) in cases.items()])
        for name, (_, rule) in cases.items():
            with self.subTest(name=name):
                refused = self.prepare("--command", name)
                self.assertEqual(refused.returncode, 1)
                self.assertEqual(refused.stderr.strip(), f"commands: {name} was rejected from the catalog: {rule}")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM note WHERE kind='exec'").fetchone()[0], 0)
        prepared = self.prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)

    def test_a_clean_catalog_reports_no_rejections(self):
        result = self.cli("catalog", "--item", self.item)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["rejected"], [])
        self.assertEqual(result.stderr, "")

    def test_unused_preparation_reconciles_without_running(self):
        prepared = self.prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        note = json.loads(prepared.stdout)["execution"]["note"]
        result = self.cli("reconcile", note)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["exit_code"], 125)
        self.assertEqual(self.cli("execute", note).returncode, 1)
