"""Authenticated finite commands against a disposable database and local fixture."""

import json
from pathlib import Path
from unittest.mock import patch

from sd_db import runner_exec, workflow
from sd_db.writes import upsert_repo

from .test_workflow_actions import BrowserSession


class PaletteActions(BrowserSession):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name).resolve()
        self.checkout = root / "checkout"; self.checkout.mkdir()
        upsert_repo(self.connection, str(self.checkout), remote="https://example.invalid/repo.git")
        self.identity = self.item("Command fixture", kind="task", repo=str(self.checkout), branch="task/fixture", status="ready")
        program = root / "fixture-command"
        program.write_text("#!/usr/bin/python3\nprint('<script>fixture output</script>')\n")
        program.chmod(0o755)
        self.file = root / "commands.yaml"
        self.file.write_text("version: 1\ncommands:\n  inspect: " + json.dumps({"argv": [str(program)], "screens": ["item"], "mutates": False, "scope": "worktree"}) + "\n")
        original = runner_exec.catalog
        replacement = patch.object(runner_exec, "catalog", side_effect=lambda **options: original(**{**options, "path": options.get("path") or self.file}))
        replacement.start(); self.addCleanup(replacement.stop)

    def payload(self):
        return {"item": self.identity, "command": "inspect", "values": {}, "screen": "item",
                "revision": workflow.item_state(self.connection, self.identity)["revision"],
                "catalog": runner_exec.catalog()["sha256"]}

    def get(self, path):
        return self.request(path, headers={"Cookie": self.cookie})

    def test_auth_and_invalid_command_refuse_without_notes_or_process(self):
        before = self.snapshot()
        for endpoint in ("/api/palette/prepare", "/api/palette/1/execute", "/api/palette/1/reconcile"):
            self.assertEqual(self.post(endpoint, {}, **{"X-SD-CSRF": "0" * 64})[0], 403)
        self.assertEqual(self.request("/api/palette?screen=item")[0], 403)
        self.assertEqual(self.request("/api/executions/1")[0], 403)
        with patch("subprocess.Popen", side_effect=AssertionError("unexpected process")):
            for changes in ({"command": "unknown"}, {"argv": ["/bin/sh"]}, {"values": {"free_text": "bad"}}, {"item": True}):
                self.assertEqual(self.post("/api/palette/prepare", {**self.payload(), **changes})[0], 400)
        self.assertEqual(self.snapshot(), before)

    def test_real_readonly_command_output_and_replay_refusal(self):
        status, _, prepared = self.post("/api/palette/prepare", self.payload())
        self.assertEqual(status, 200)
        note = prepared["execution"]["note"]
        self.assertEqual(prepared["assignments"], [])
        self.assertEqual(self.post(f"/api/palette/{note}/execute", {})[0], 200)
        status, _, body = self.get(f"/api/executions/{note}")
        self.assertEqual(status, 200)
        output = json.loads(body)
        self.assertEqual(output["exit_code"], 0)
        self.assertIn("<script>fixture output</script>", output["output"])
        self.assertEqual(self.post(f"/api/palette/{note}/execute", {})[0], 400)
        self.assertEqual(list(self.checkout.iterdir()), [])
        self.assertNotIn("<script>fixture output</script>", self.request("/operations?area=commands")[2])

    def test_catalog_and_history_are_read_only_and_stale_revision_refuses(self):
        payload = self.payload(); before = self.snapshot()
        with patch("subprocess.Popen", side_effect=AssertionError("read started a process")):
            status, _, body = self.get(f"/api/palette?screen=item&item={self.identity}")
            self.assertEqual(status, 200); self.assertEqual(json.loads(body)["entries"][0]["name"], "inspect")
            self.assertEqual(self.request("/operations?area=commands")[0], 200)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.post("/api/palette/prepare", {**payload, "revision": "0" * 64})[0], 409)
        self.assertEqual(before, self.snapshot())

    def test_dialog_and_log_routes_render_and_unused_request_can_close(self):
        body = self.request(f"/item/{self.identity}")[2]
        self.assertIn('id="command-palette"', body)
        self.assertIn('data-palette-open', body)
        result = self.post("/api/palette/prepare", self.payload())[2]
        note = result["execution"]["note"]
        status, _, result = self.post(f"/api/palette/{note}/reconcile", {})
        self.assertEqual(status, 200); self.assertEqual(result["exit_code"], 125)
        self.assertEqual(self.post(f"/api/palette/{note}/execute", {})[0], 400)

    def test_item_notes_show_command_outcome_without_raw_descriptor(self):
        prepared = self.post("/api/palette/prepare", self.payload())[2]
        note = prepared["execution"]["note"]
        recorded = self.connection.execute("SELECT body FROM note WHERE id=?", (note,)).fetchone()[0]
        pending = self.request(f"/item/{self.identity}")[2]
        self.assertIn("inspect · Unfinished · outcome pending", pending)
        self.assertNotIn("registry_sha256", pending)
        self.assertNotIn("entry_sha256", pending)
        self.assertIn(f"/operations?area=commands#execution-{note}", pending)
        self.assertEqual(self.post(f"/api/palette/{note}/execute", {})[0], 200)
        finished = self.request(f"/item/{self.identity}")[2]
        self.assertIn("inspect · Finished · exit 0", finished)
        self.assertNotIn("registry_path", finished)
        self.assertEqual(self.connection.execute("SELECT body FROM note WHERE id=?", (note,)).fetchone()[0], recorded)
        history = self.request("/operations?area=commands")[2]
        self.assertIn(f'id="execution-{note}"', history)
