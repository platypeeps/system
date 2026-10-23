"""Research experiments (sd:5296): a research checkout tied to a row of the team's experiments database.

What this slice promises: each run's argv allows exactly its tools and denies none of them; a run starts only under the
one lock and writes nothing when refused; a list run replaces the cache only with rows it could read; a draft leaves a
draft the page can show, with its SHA-256; an apply starts only while that draft, the link and the settings still hold,
and never twice; and each stop between an apply's steps leaves state the next apply finishes or refuses (the design's
failure table). The link is the checkout's repo declaration of kind `experiment`; until the pack knows that kind,
attach says so and every checkout reads "not linked".

No test calls claude, Notion or Drive: `realm_sweep.ask` is a fake that writes what a model run would leave, and the
spawned runner is a stub. The pack's `DECLARATION_KINDS` gains `experiment` where a test needs the pack change.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from sd_db import connect, repos, writes

from sd_dashboard import realm_sweep, research_experiments as rx, sweep

from support import ScreenCase
from test_workflow_actions import BrowserSession

SOURCE = "0000000a-0000-4000-8000-000000000001"
OTHER = "0000000b-0000-4000-8000-000000000002"
PAGE = "1" * 32
PAGE2 = "2" * 32
#: The spawned runner: it exits at once, or waits for `release` beside the state folder when `hold` is there.
STUB = r'''import pathlib, sys, time
state = pathlib.Path(sys.argv[2]).parent.parent
if (state / "hold").exists():
    for _ in range(400):
        if (state / "release").exists():
            break
        time.sleep(0.05)
'''
ROWS = [{"page": PAGE, "url": "https://example.test/one", "title": "Span sampling", "stage": "Running",
         "status": "In Progress", "edited": "2026-10-18T09:00:00Z"},
        {"page": PAGE2, "url": "https://example.test/two", "title": "Trace budgets", "stage": "Idea", "status": "Todo",
         "edited": "2026-10-17T09:00:00Z"}]
CREATE = {"title": "Span sampling", "problem": "Sampling drops the spans that matter.", "body": "## Hypothesis\nTail."}
STATUS = {"before": {"section": "## Status\n2026-10-01 started", "status": "Todo"},
          "after": {"section": "## Status\n### 2026-10-19\nRan.\n2026-10-01 started", "status": "In Progress"},
          "entry": "Ran."}
def dead_pid() -> int:
    return int(subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True,
                              text=True, check=True).stdout)


class Case(ScreenCase):
    def setUp(self):
        super().setUp()
        # Resolved: the pack keys a repository by its real path, and macOS tmp is a /var symlink.
        self.root = (Path(self.tmp.name) / "repos").resolve()
        self.alpha = self.root / "research" / "alpha"
        self.alpha.mkdir(parents=True)
        (self.alpha / "research.conf.py").write_text("DOCUMENTS = []\n")
        self.repo(str(self.alpha))
        env = mock.patch.dict(os.environ, {"REPO_ROOT": str(self.root), "SYSTEM_TOOLS_CONFIG": str(Path(self.tmp.name) / "config"),
                                           "XDG_CONFIG_HOME": str(Path(self.tmp.name) / "xdg")})
        env.start()
        self.addCleanup(env.stop)
        runner = mock.patch.object(rx, "RUNNER", (sys.executable, "-c", STUB))
        runner.start()
        self.addCleanup(runner.stop)
        # The runner's link write opens this fixture database, never the operator's.
        opened = mock.patch.object(rx, "_connect", lambda: connect(self.path))
        opened.start()
        self.addCleanup(opened.stop)
        writes.set_setting(self.connection, rx.SOURCE_KEY, f"collection://{SOURCE}")
        self.state = rx.state_dir()

    def knows(self):
        """The pack change (pack sd:5302): `experiment` is a `DECLARATION_KINDS` entry, also on a pack before it."""
        patch = mock.patch.object(repos, "DECLARATION_KINDS", (*repos.DECLARATION_KINDS, "experiment"))
        patch.start()
        self.addCleanup(patch.stop)

    def lacks(self):
        """A pack from before the change: `experiment` is not a declaration kind."""
        patch = mock.patch.object(repos, "DECLARATION_KINDS", tuple(k for k in repos.DECLARATION_KINDS if k != "experiment"))
        patch.start()
        self.addCleanup(patch.stop)

    def post(self, values):
        status, body = rx.request(values)(self.connection)
        for _ in range(200):
            if not sweep._held(self.state):
                break
            time.sleep(0.05)
        return status, body

    def cache(self, source=SOURCE, rows=ROWS):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "experiments.json").write_text(json.dumps({"read": "2026-10-19T10:00:00+00:00", "source": source, "rows": rows}))

    def link(self, page=PAGE):
        self.knows()
        repos.set_declaration(self.connection, self.alpha, "experiment", {"page": page, "data_source": SOURCE})

    def model(self, run, files=None, last="RESULT: ok", code=0, locked=True):
        """The run as the runner does it, holding the lock as `spawn` hands it over, with a fake model that leaves
        `files` in its folder."""
        seen = {}

        def ask(argv, cwd, timeout, out, keep=()):
            seen["argv"], seen["keep"] = argv, keep
            for name, value in (files or {}).items():
                (Path(cwd) / name).write_text(value if isinstance(value, str) else json.dumps(value))
            return (realm_sweep.EXIT_OK if code == 0 and last == "RESULT: ok" else realm_sweep.EXIT_ERROR), last, []

        with open(self.state / "lock", "a+", encoding="utf-8") as lock, mock.patch.object(realm_sweep, "ask", ask):
            if locked:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock = lock.fileno()
            rx.run(self.state / "runs" / run, io.StringIO(), io.StringIO())
        self.seen = seen
        return json.loads((self.state / "runs" / run / "record.json").read_text())

    def draft(self, action, body, **more):
        status, started = self.post({"verb": "draft", "project": "research/alpha", "action": action, **more})
        self.assertEqual(status, 202, started)
        record = self.model(started["run"], {"draft.json": body})
        self.assertEqual(record["state"], "done", record)
        return started["run"], record["fingerprint"]

    def apply(self, run, fingerprint, recreate=False):
        return self.post({"verb": "apply", "run": run, "fingerprint": fingerprint, "recreate": recreate})

    def prompt(self, run):
        asked = json.loads((self.state / "runs" / run / "request.json").read_text())
        return rx.prompt(asked, self.state / "runs" / run)

    def held(self):
        return (repos.declarations(self.connection, self.alpha) or {}).get("experiment")


class TheTools(Case):
    def test_each_run_allows_its_own_tools_and_denies_none_of_them(self):
        folder = Path("/state/runs/x")
        for (verb, action), mcp in rx.MCP.items():
            asked = {"verb": verb, "action": action, "checkout": "/repos/research/alpha"}
            allowed, denied = rx.tools(asked, folder)
            with self.subTest(run=(verb, action)):
                self.assertEqual(set(allowed) & set(denied), set())
                self.assertEqual([t for t in allowed if t.startswith("mcp__")], list(mcp))
                self.assertIn("Bash", denied)
                self.assertNotIn("Edit", allowed)
                # Read and Write reach the run folder; a draft also reads its checkout.
                # Claude Code matches only Edit(path) rules for Write; it ignores a Write(path) rule (sd:5751).
                self.assertIn("Edit(//state/runs/x/**)", allowed)
                self.assertEqual([t for t in allowed if t.startswith("Write")], [])
                self.assertEqual("Read(//repos/research/alpha/**)" in allowed, verb == "draft")
        # realm_sweep.DENIED denies the Notion writers; each apply allows its own and still denies the rest.
        allowed, denied = rx.tools({"verb": "apply", "action": "status"}, folder)
        self.assertIn("mcp__notion__notion-update-page", allowed)
        self.assertIn("mcp__notion__notion-create-pages", denied)
        self.assertIn("mcp__notion__notion-move-pages", denied)

    def test_each_run_loads_its_one_server_alone(self):
        # Every run loads notion; strict, so no claude.ai connector competes with the granted tools: the first draft called the claude.ai Notion fetch and was refused (sd:5751).
        for verb, action in rx.MCP:
            argv = rx.claude_argv({"verb": verb, "action": action, "checkout": "/c"},
                                  Path("/s/runs/x"), "p")
            with self.subTest(run=(verb, action)):
                self.assertIn("--strict-mcp-config", argv)
                self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
                servers = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
                self.assertEqual(list(servers), ["notion"])


class TheStart(Case):
    def test_a_second_start_while_a_run_holds_the_lock_is_refused_and_writes_no_run(self):
        self.state.mkdir(parents=True)
        (self.state / "hold").touch()
        status, first = rx.request({"verb": "list"})(self.connection)
        self.assertEqual(status, 202, first)
        status, second = rx.request({"verb": "list"})(self.connection)
        self.assertEqual(status, 409, second)
        self.assertIn("An experiments run is already running", second["error"])
        self.assertEqual([p.name for p in (self.state / "runs").iterdir()], [first["run"]])
        doc = rx.document(self.connection)
        self.assertTrue(doc["running"])
        self.assertEqual(doc["runs"][0]["state"], "running")
        (self.state / "release").touch()
        for _ in range(200):
            if not sweep._held(self.state):
                break
            time.sleep(0.05)
        # The stub wrote no record: once the lock is free, the run reads failed, naming the start log.
        run = rx.document(self.connection)["runs"][0]
        self.assertEqual(run["state"], "failed")
        self.assertIn("start.log", run["reason"])

    def test_the_model_run_inherits_the_lock_and_a_runner_without_it_refuses(self):
        status, started = self.post({"verb": "list"})
        self.model(started["run"], {"rows.json": ROWS})
        self.assertEqual(self.seen["keep"], (self.lock,))
        # A runner killed mid-run leaves its model run holding the lock: the run reads running, and nothing starts.
        realm_sweep.write_record(self.state / "runs" / started["run"] / "record.json", {"state": "running", "pid": dead_pid()})
        with open(self.state / "lock", "a+", encoding="utf-8") as writer:
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run = rx.document(self.connection)["runs"][0]
            self.assertEqual((run["state"], "still holds the lock" in run["reason"]), ("running", True))
            self.assertEqual(rx.request({"verb": "list"})(self.connection)[0], 409)
        # Started by hand, outside the lock, the runner refuses before the model runs.
        os.unlink(self.state / "lock")
        status, started = self.post({"verb": "list"})
        os.unlink(self.state / "lock")
        record = self.model(started["run"], {"rows.json": ROWS}, locked=False)
        self.assertNotIn("argv", self.seen)
        self.assertIn("holds no experiments lock", record["reason"])

    def test_an_unset_data_source_refuses_every_run_naming_it_and_an_unread_one_is_unknown(self):
        writes.set_setting(self.connection, rx.SOURCE_KEY, None)
        status, body = self.post({"verb": "list"})
        self.assertEqual(status, 409)
        self.assertIn("sd.notion_experiments is not set", body["error"])
        self.assertFalse((self.state / "runs").exists())
        with mock.patch.object(writes, "read_settings", side_effect=OSError("disk gone")):
            status, body = self.post({"verb": "list"})
        self.assertEqual(status, 409)
        self.assertIn("did not read", body["error"])
        self.assertNotIn("not set", body["error"])

    def test_a_deck_draft_is_refused_as_an_unknown_action_and_starts_no_run(self):
        self.link()
        with self.assertRaises(ValueError) as caught:
            rx.request({"verb": "draft", "project": "research/alpha", "action": "deck"})
        self.assertIn("action is one of create, status.", str(caught.exception))
        self.assertFalse((self.state / "runs").exists())

    def test_the_post_takes_only_its_fields(self):
        for values in ({"verb": "drop"}, {"verb": "list", "page": PAGE}, {"verb": "draft", "project": "p", "action": "create"},
                       {"verb": "draft", "project": "p", "action": "status", "description": "x"},
                       {"verb": "attach", "project": "p", "page": "nope"}, {"verb": "apply", "run": "x", "fingerprint": "y"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                rx.request(values)


class TheList(Case):
    def test_a_list_run_replaces_the_cache_and_a_bad_one_keeps_the_old(self):
        status, started = self.post({"verb": "list"})
        self.assertEqual(status, 202, started)
        record = self.model(started["run"], {"rows.json": ROWS})
        self.assertEqual(record["state"], "done")
        cache = rx.document(self.connection)["cache"]
        self.assertEqual((cache["source"], cache["current"], [r["page"] for r in cache["rows"]]), (SOURCE, True, [PAGE, PAGE2]))
        self.assertIn("notion-search", self.seen["argv"][2])
        for bad in ("[1", json.dumps([{"title": "no page"}])):
            status, started = self.post({"verb": "list"})
            record = self.model(started["run"], {"rows.json": bad})
            self.assertEqual(record["state"], "failed")
            self.assertEqual([r["page"] for r in rx.document(self.connection)["cache"]["rows"]], [PAGE, PAGE2])
        status, started = self.post({"verb": "list"})
        self.assertEqual(self.model(started["run"], {"rows.json": ROWS}, last="RESULT: failed, 2: notion down")["state"], "failed")


class TheAttach(Case):
    def test_attach_takes_only_a_listed_page_from_the_current_source(self):
        self.knows()
        status, body = self.post({"verb": "attach", "project": "research/alpha", "page": PAGE})
        self.assertEqual(status, 400)
        self.assertIn("refresh it first", body["error"])
        self.cache(source=OTHER)
        status, body = self.post({"verb": "attach", "project": "research/alpha", "page": PAGE})
        self.assertEqual(status, 400)
        self.cache()
        status, body = self.post({"verb": "attach", "project": "research/alpha", "page": "3" * 32})
        self.assertEqual(status, 400)
        self.assertIn("not in the experiments list", body["error"])
        self.assertIsNone(self.held())
        status, body = self.post({"verb": "attach", "project": "research/alpha", "page": PAGE})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.held(), {"page": PAGE, "data_source": SOURCE, "url": "https://example.test/one",
                                       "title": "Span sampling"})
        self.assertEqual(rx.document(self.connection, "research/alpha")["link"]["page"], PAGE)

    def test_attach_over_a_link_that_moved_since_the_read_is_stale(self):
        self.link(PAGE2)
        self.cache()
        status, body = self.post({"verb": "attach", "project": "research/alpha", "page": PAGE, "replaces": None})
        self.assertEqual(status, 409)
        self.assertTrue(body["stale"])
        status, body = self.post({"verb": "attach", "project": "research/alpha", "page": PAGE, "replaces": PAGE2})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["before"]["page"], PAGE2)

    def test_a_pack_without_the_kind_reads_not_linked_and_attach_says_to_update_it(self):
        self.lacks()
        self.cache()
        doc = rx.document(self.connection, "research/alpha")
        self.assertEqual((doc["link"], doc["link_error"]), (None, ""))
        status, body = self.post({"verb": "attach", "project": "research/alpha", "page": PAGE})
        self.assertEqual(status, 409)
        self.assertIn("does not know the experiment declaration yet; update the command pack", body["error"])

    def test_a_declaration_that_does_not_read_refuses_attach_and_is_never_overwritten(self):
        self.knows()
        # Written past the pack's own check, as an older or hand-edited row could hold it.
        self.connection.execute("UPDATE repo SET declarations = ? WHERE path = ?",
                                (json.dumps({"experiment": {"page": "x"}}), str(self.alpha)))
        self.connection.commit()
        self.cache()
        doc = rx.document(self.connection, "research/alpha")
        self.assertIn("does not read", doc["link_error"])
        status, body = self.post({"verb": "attach", "project": "research/alpha", "page": PAGE})
        self.assertEqual(status, 409)
        self.assertEqual(self.held(), {"page": "x"})

    def test_a_declaration_read_that_fails_is_unknown_never_not_linked(self):
        from sd_db.errors import SdDbError

        with mock.patch.object(repos, "declarations", side_effect=SdDbError("column unreadable")):
            doc = rx.document(self.connection, "research/alpha")
            status, body = self.post({"verb": "draft", "project": "research/alpha", "action": "status"})
        self.assertEqual((doc["link"], doc["link_error"]), (None, "not read: column unreadable"))
        self.assertEqual(status, 409)
        self.assertIn("not read: column unreadable", body["error"])
        # A store that does not answer is unknown too, never a 500 and never "not linked".
        with mock.patch.object(repos, "declarations", side_effect=sqlite3.OperationalError("database is locked")):
            doc = rx.document(self.connection, "research/alpha")
        self.assertEqual((doc["link"], doc["link_error"]), (None, "not read: database is locked"))


class TheDraft(Case):
    def test_a_draft_that_stops_or_ends_without_result_ok_or_a_shaped_draft_fails(self):
        self.link()
        status, started = self.post({"verb": "draft", "project": "research/alpha", "action": "status"})
        self.assertEqual(status, 202, started)
        self.assertEqual(self.model(started["run"], {"draft.json": STATUS}, last="")["state"], "failed")
        status, started = self.post({"verb": "draft", "project": "research/alpha", "action": "status"})
        self.assertEqual(self.model(started["run"], {"draft.json": {**STATUS, "after": {"section": "", "status": "Shipped"}}})["state"], "failed")
        # A run killed mid-way leaves `running` with a dead pid: it reads failed, never running.
        status, started = self.post({"verb": "draft", "project": "research/alpha", "action": "status"})
        realm_sweep.write_record(self.state / "runs" / started["run"] / "record.json", {"state": "running", "pid": dead_pid()})
        run = next(r for r in rx.document(self.connection)["runs"] if r["run"] == started["run"])
        self.assertEqual(run["state"], "failed")
        self.assertIn("stopped without recording an outcome", run["reason"])

    def test_a_done_draft_shows_its_body_and_hash(self):
        self.link()
        run, fingerprint = self.draft("status", STATUS)
        shown = next(r for r in rx.document(self.connection)["runs"] if r["run"] == run)
        self.assertEqual((shown["draft"], shown["fingerprint"]), (STATUS, fingerprint))
        self.assertEqual(fingerprint, hashlib.sha256((self.state / "runs" / run / "draft.json").read_bytes()).hexdigest())

    def test_create_takes_a_description_and_refuses_a_linked_project(self):
        status, body = self.post({"verb": "draft", "project": "research/alpha", "action": "create", "description": "Tail sampling"})
        self.assertEqual(status, 202, body)
        self.link()
        status, body = self.post({"verb": "draft", "project": "research/alpha", "action": "create", "description": "Again"})
        self.assertEqual(status, 409)
        self.assertIn("linked already", body["error"])


class TheApply(Case):
    def test_apply_refuses_a_changed_draft_a_moved_link_a_changed_source_and_a_second_apply(self):
        self.link()
        run, fingerprint = self.draft("status", STATUS)
        status, body = self.apply(run, "0" * 64)
        self.assertEqual(status, 409)
        self.assertIn("fingerprint", body["error"])
        repos.set_declaration(self.connection, self.alpha, "experiment", {"page": PAGE2, "data_source": SOURCE})
        status, body = self.apply(run, fingerprint)
        self.assertEqual(status, 409)
        self.assertTrue(body["error"].startswith("page:"), body)
        repos.set_declaration(self.connection, self.alpha, "experiment", {"page": PAGE, "data_source": SOURCE})
        writes.set_setting(self.connection, rx.SOURCE_KEY, f"collection://{OTHER}")
        status, body = self.apply(run, fingerprint)
        self.assertEqual(status, 409)
        self.assertTrue(body["error"].startswith("source:"), body)
        writes.set_setting(self.connection, rx.SOURCE_KEY, f"collection://{SOURCE}")
        status, started = self.apply(run, fingerprint)
        self.assertEqual(status, 202, started)
        self.assertEqual(self.model(started["run"], {"out.json": {"page": PAGE, "section": "written", "status": "written"}})["state"], "done")
        status, body = self.apply(run, fingerprint)
        self.assertEqual(status, 409)
        self.assertIn("applied", body["error"])

    def test_a_link_to_another_data_source_refuses_the_draft_and_the_apply(self):
        self.knows()
        repos.set_declaration(self.connection, self.alpha, "experiment", {"page": PAGE, "data_source": OTHER})
        status, body = self.post({"verb": "draft", "project": "research/alpha", "action": "status"})
        self.assertEqual(status, 409)
        self.assertTrue(body["error"].startswith("source:"), body)
        repos.set_declaration(self.connection, self.alpha, "experiment", {"page": PAGE, "data_source": SOURCE})
        run, fingerprint = self.draft("status", STATUS)
        repos.set_declaration(self.connection, self.alpha, "experiment", {"page": PAGE, "data_source": OTHER})
        status, body = self.apply(run, fingerprint)
        self.assertEqual(status, 409)
        self.assertTrue(body["error"].startswith("source:"), body)

    def test_create_stopped_after_out_json_links_from_it_without_a_run(self):
        self.knows()
        run, fingerprint = self.draft("create", CREATE, description="Tail sampling")
        status, started = self.apply(run, fingerprint)
        self.assertEqual(status, 202, started)
        self.assertIn("Linked from research project research/alpha, draft", self.prompt(started["run"]))
        # The model created the row and wrote out.json; the runner was killed before the link.
        folder = self.state / "runs" / started["run"]
        (folder / "out.json").write_text(json.dumps({"outcome": "created", "page": PAGE, "url": "https://example.test/one"}))
        realm_sweep.write_record(folder / "record.json", {"state": "running", "pid": dead_pid()})
        runs = len(list((self.state / "runs").iterdir()))
        status, body = self.apply(run, fingerprint)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(list((self.state / "runs").iterdir())), runs)
        self.assertEqual(self.held(), {"page": PAGE, "data_source": SOURCE, "url": "https://example.test/one", "title": "Span sampling"})

    def test_create_stopped_before_out_json_searches_and_a_miss_is_unknown_that_only_recreate_passes(self):
        self.knows()
        run, fingerprint = self.draft("create", CREATE, description="Tail sampling")
        status, first = self.apply(run, fingerprint)
        self.assertNotIn("notion-search", self.prompt(first["run"]))
        realm_sweep.write_record(self.state / "runs" / first["run"] / "record.json", {"state": "running", "pid": dead_pid()})
        status, second = self.apply(run, fingerprint)
        self.assertEqual(status, 202, second)
        text = self.prompt(second["run"])
        self.assertIn("notion-search", text)
        self.assertIn("create nothing", text)
        self.assertEqual(self.model(second["run"], {"out.json": {"outcome": "unknown"}})["state"], "unknown")
        self.assertIsNone(self.held())
        status, body = self.apply(run, fingerprint)
        self.assertEqual(status, 409)
        self.assertIn("recreate", body["error"])
        status, third = self.apply(run, fingerprint, recreate=True)
        self.assertEqual(status, 202, third)
        self.assertIn("chose to create it again", self.prompt(third["run"]))
        record = self.model(third["run"], {"out.json": {"outcome": "created", "page": PAGE, "url": "https://example.test/one"}})
        self.assertEqual(record["state"], "done", record)
        self.assertEqual(self.held()["page"], PAGE)

    def test_a_create_link_refused_keeps_out_json_for_the_next_apply(self):
        self.lacks()
        run, fingerprint = self.draft("create", CREATE, description="Tail sampling")
        status, started = self.apply(run, fingerprint)
        # The pack does not know the kind yet: the row exists, the link does not; the next apply links it.
        record = self.model(started["run"], {"out.json": {"outcome": "created", "page": PAGE, "url": "https://example.test/one"}})
        self.assertEqual(record["state"], "failed")
        self.assertIn("apply again to link it", record["reason"])
        self.knows()
        status, body = self.apply(run, fingerprint)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.held()["page"], PAGE)

    def test_status_stale_or_another_page_fails_and_a_failed_apply_does_not_block_the_next(self):
        self.link()
        run, fingerprint = self.draft("status", STATUS)
        text = self.prompt(self.apply(run, fingerprint)[1]["run"])
        self.assertIn("before.section", text)
        first = sorted((self.state / "runs").iterdir())[-1].name
        record = self.model(first, {"out.json": {"page": PAGE, "section": "stale", "status": "skipped"}})
        self.assertEqual(record["state"], "failed")
        self.assertIn("stale", record["reason"])
        status, second = self.apply(run, fingerprint)
        self.assertEqual(status, 202, second)
        record = self.model(second["run"], {"out.json": {"page": PAGE2, "section": "written", "status": "written"}})
        self.assertEqual(record["state"], "failed")
        self.assertIn(PAGE2, record["reason"])
        status, third = self.apply(run, fingerprint)
        record = self.model(third["run"], {"out.json": {"page": PAGE, "section": "skipped", "status": "written"}})
        self.assertEqual(record["state"], "done", record)

class TheOlderState(Case):
    def test_a_deck_draft_an_earlier_version_left_is_listed_but_never_applied(self):
        self.link()
        stamp = "20261019-160000-000001"
        folder = self.state / "runs" / stamp
        folder.mkdir(parents=True)
        body = json.dumps({"title": "Span sampling", "slides": []}).encode()
        (folder / "draft.json").write_bytes(body)
        realm_sweep.write_record(folder / "request.json", {"verb": "draft", "action": "deck", "project": "research/alpha",
                                                           "source": SOURCE, "page": PAGE, "account": "operator@example.test"})
        realm_sweep.write_record(folder / "record.json", {"state": "done", "pid": dead_pid()})
        shown = rx.document(self.connection, "research/alpha")["runs"][0]
        self.assertEqual((shown["run"], shown["action"], shown["state"]), (stamp, "deck", "done"))
        status, refused = self.apply(stamp, hashlib.sha256(body).hexdigest())
        self.assertEqual(status, 409, refused)
        self.assertIn("no longer applies", refused["error"])
        self.assertEqual([p.name for p in (self.state / "runs").iterdir()], [stamp])

    def test_no_state_folder_reads_never_listed_not_linked_and_no_runs(self):
        doc = rx.document(self.connection, "research/alpha")
        self.assertEqual((doc["cache"], doc["runs"], doc["link"], doc["running"]), (None, [], None, ""))
        self.assertEqual(doc["problems"], {"source": ""})


class TheRoute(BrowserSession):
    def test_the_route_answers_before_the_checkout_pattern_and_a_bad_post_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Nothing of this machine's: the state and the config are the test's.
        with mock.patch.object(rx, "state_dir", return_value=Path(tmp.name)), \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name}):
            status, _, body = self.request("/api/research/experiments", headers={"Cookie": self.cookie})
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["runs"], [])
            status, _, body = self.post("/api/research/experiments/act", {"verb": "drop"})
            self.assertEqual(status, 400, body)
            status, _, body = self.post("/api/research/experiments/act", {"verb": "draft", "project": "research/alpha", "action": "deck"})
            self.assertEqual(status, 400, body)
            self.assertIn("action is one of create, status.", body["error"])
