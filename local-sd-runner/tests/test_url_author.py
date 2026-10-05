"""sd:234 slice 8d: a `url` author is dispatched, not skipped, and the
library's one call is its session. The clone, the retained log and the
ending are the `start` path's; only the provider step differs."""
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_db import seed, transaction
from sd_db.registry import parse
from sd_runner import runtime
from sd_runner.runtime import Runner

from . import test_runtime

PACK = Path(os.environ.get("SD_ACCEPTANCE_PACK", test_runtime.ROOT.parents[1] / "pack"))
REGISTRY = """\
bills:
  open: { cost: subscription }
providers:
  mini:   { url: "https://minimax.example/v1", model: MiniMax-M3, vendor: minimax, bill: open,
            roles: [author, reviewer], max_tokens: 1000, price: { in: 1.00, out: 2.00 }, env: [MINIMAX_API_KEY] }
  kimi:   { url: "https://moonshot.example/v1", model: kimi-k3, vendor: moonshot, bill: open,
            roles: [reviewer], max_tokens: 1000, price: { in: 1.00, out: 2.00 }, env: [MOONSHOT_API_KEY] }
  claude: { start: "claude -p", vendor: anthropic, bill: open, roles: [author], reader: claude-json }
roles:
  author:   [mini]
  reviewer: [kimi]
"""
ANSWER = json.dumps({"choices": [{"message": {"role": "assistant", "content": "the work, as text"}}],
                     "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}).encode()


class Wire:
    """`test_calls.py`'s recording transport: one scripted answer per call."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        return self.script.pop(0)


class UrlAuthor(unittest.TestCase):
    def setUp(self):
        # `Fixture`'s remote, checkout, database and item, without its tests.
        test_runtime.Fixture.setUp(self)
        self.parsed = parse(REGISTRY, "providers.yaml")
        self.registry(REGISTRY)
        self.wire = Wire((200, ANSWER))
        self.runner = Runner(self.config, freezer=lambda path: None)
        self.runner.transport = self.wire
        self.resolved = (None, {}, {"provider": "mini", "vendor": "minimax", "start": False, "bill": "open", "reader": None,
                                    "entry": self.parsed.providers["mini"], "registry": self.parsed})

    def claim(self, **kw):
        assignment = store.enqueue(self.db, [self.item], who="operator", **kw)[0]
        return store.claim(self.db, assignment["id"], owner=self.runner.owner, work_root=self.config.work, retention_root=self.config.retention)

    def registry(self, text):
        """Under `home` for `provider_command`, beside the database for `enqueue`; rows seeded afresh."""
        for target in (self.root / ".local/share/sd/providers.yaml", self.database.parent / "providers.yaml"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        with transaction(self.db):
            self.db.execute("DELETE FROM provider")
            self.db.execute("DELETE FROM bill")
        seed(self.db, parse(text, "providers.yaml"))

    @unittest.skipUnless((PACK / "bin/sd_registry.py").is_file(), "needs the pack's registry reader")
    def test_provider_command_dispatches_the_first_url_author_and_a_start_entry_as_before(self):
        config = runtime.Config(**{**self.config.__dict__, "pack": PACK})
        request = self.claim()
        argv, environment, provider = runtime.provider_command(config, request, {"PATH": str(self.root / "bin")})
        self.assertEqual((argv, environment, provider["provider"], provider["start"], provider["entry"].kind, provider["entry"].url),
                         (None, {}, "mini", False, "url", "https://minimax.example/v1"))
        self.assertEqual(set(provider["registry"].providers), {"mini", "kimi", "claude"})
        # A `start` entry first in the order is resolved as it always was.
        self.registry(REGISTRY.replace("  author:   [mini]", "  author:   [claude, mini]"))
        (self.root / "bin").mkdir()
        (self.root / "bin/claude").write_text("#!/bin/sh\n")
        (self.root / "bin/claude").chmod(0o755)
        argv, environment, provider = runtime.provider_command(config, request, {"PATH": str(self.root / "bin")})
        self.assertEqual((argv[:2], provider["provider"], provider["start"], "entry" in provider),
                         ([str(self.root / "bin/claude"), "-p"], "claude", True, False))
        # The session's commits name it; the pack's hook reads the entry (sd:2544).
        self.assertEqual((environment["SD_ASSIGNMENT"], environment["SD_AUTHOR"]), (request["run"]["id"], "claude"))

    def test_a_url_author_run_ends_done_with_the_response_in_the_retained_log(self):
        with patch.object(runtime, "provider_command", return_value=self.resolved), patch.dict(os.environ, {"MINIMAX_API_KEY": "k"}):
            request = self.claim()
            result = self.runner.execute(self.db, request)
        self.assertEqual((result["end_step"], result["outcome"], result["provider"]), ("released", "done", "mini"), result)
        self.assertEqual(store.queue_state(self.db, request["id"])["status"], "done")
        log = Path(result["retained_path"]) / ".git/sd-provider.log"
        self.assertEqual(log.read_bytes(), ANSWER)
        # The prompt on the wire is the one a `start` session reads on stdin.
        sent = json.loads(self.wire.calls[0][0].data)
        self.assertEqual(sent["messages"][0]["content"], runtime.prompt(request, self.resolved[2]))
        self.assertIn("Title: fixture work", sent["messages"][0]["content"])
        note = self.db.execute("SELECT body, output_path FROM note WHERE kind = 'exec'").fetchone()
        self.assertEqual((note["body"], note["output_path"]), (result["detail"], str(log)))
        self.assertEqual([dict(row) for row in self.db.execute("SELECT source, assignment, pass FROM cost")],
                         [{"source": "run", "assignment": request["id"], "pass": request["run"]["id"]}])

    def test_a_refused_budget_ends_the_run_blocked_with_the_note_and_the_loop_leaves_it(self):
        with patch.object(runtime, "provider_command", return_value=self.resolved), patch.dict(os.environ, {"MINIMAX_API_KEY": "k"}):
            request = self.claim(budget_usd=0.001)
            result = self.runner.execute(self.db, request)
        self.assertEqual((result["end_step"], result["outcome"], self.wire.calls), ("released", "blocked", []), result)
        self.assertIn(f"budget spent: assignment {request['id']} has", result["detail"])
        self.assertEqual([row["body"] for row in self.db.execute("SELECT body FROM note WHERE kind = 'followup'")], [result["detail"]])
        self.assertEqual(store.queue_state(self.db, request["id"])["status"], "blocked")
        self.assertEqual(store.queued(self.db), [])

    def test_an_exhausted_time_budget_is_refused_before_the_wire_and_the_timeout_is_the_remaining_time(self):
        with patch.object(runtime, "provider_command", return_value=self.resolved), patch.dict(os.environ, {"MINIMAX_API_KEY": "k"}):
            request = self.claim()
            request["run"] = self.runner.persist(self.db, request["run"]["id"], provider="mini", vendor="minimax", start_step="started")
            (Path(request["run"]["work_path"]) / ".git").mkdir(parents=True)
            with self.assertRaisesRegex(store.RunnerRefused, "assignment time budget exceeded"):
                self.runner.answer(self.db, request, self.resolved[2], deadline=time.monotonic() - 1)
            self.assertEqual(self.wire.calls, [])
            self.runner.answer(self.db, request, self.resolved[2], deadline=time.monotonic() + 0.5)
        self.assertEqual(len(self.wire.calls), 1)
        self.assertLess(self.wire.calls[0][1], 1.0)

    def test_an_unwritable_log_ends_the_run_blocked_with_the_call_settled(self):
        class Unwritable(type(Path())):
            def write_text(self, *args, **kw):
                raise OSError("disk full")
        with patch.object(runtime, "provider_command", return_value=self.resolved), patch.dict(os.environ, {"MINIMAX_API_KEY": "k"}), \
                patch.object(runtime, "Path", Unwritable):
            request = self.claim()
            result = self.runner.execute(self.db, request)
        self.assertEqual((result["end_step"], result["outcome"], result["detail"]), ("released", "blocked", "disk full"), result)
        self.assertEqual(store.queue_state(self.db, request["id"])["status"], "blocked")
        # The call was made and settled; its cost stands on the assignment.
        self.assertEqual([dict(row) for row in self.db.execute("SELECT source, usd FROM cost")], [{"source": "run", "usd": 0.002}])


if __name__ == "__main__":
    unittest.main()
