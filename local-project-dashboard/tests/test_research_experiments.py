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
DECK = {"title": "Span sampling", "style": {"accent": "#000000"},
        "slides": [{"id": f"sdexp_{n}", "message": f"m{n}", "visual": "a band", "notes": "n"} for n in range(1, 8)]}
IDS = [f"sdexp_{n}" for n in range(1, 8)]


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
        self.kit = Path(self.tmp.name) / "kit" / "design.md"
        self.kit.parent.mkdir()
        self.kit.write_text("# Kit\n")
        self.kit = self.kit.resolve()
        env = mock.patch.dict(os.environ, {"REPO_ROOT": str(self.root), "SYSTEM_TOOLS_CONFIG": str(Path(self.tmp.name) / "config"),
                                           "XDG_CONFIG_HOME": str(Path(self.tmp.name) / "xdg"), "RESEARCH_BRAND_KIT": str(self.kit)})
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
        writes.set_setting(self.connection, rx.ACCOUNT_KEY, "operator@example.test")
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
            asked = {"verb": verb, "action": action, "checkout": "/repos/research/alpha", "kit": str(self.kit)}
            allowed, denied = rx.tools(asked, folder)
            with self.subTest(run=(verb, action)):
                self.assertEqual(set(allowed) & set(denied), set())
                self.assertEqual([t for t in allowed if t.startswith("mcp__")], list(mcp))
                self.assertIn("Bash", denied)
                self.assertNotIn("Edit", allowed)
                # Read and Write reach the run folder; a draft also reads its checkout; only the deck draft the kit.
                self.assertIn("Write(//state/runs/x/**)", allowed)
                self.assertEqual("Read(//repos/research/alpha/**)" in allowed, verb == "draft")
                self.assertEqual(f"Read(/{self.kit})" in allowed, (verb, action) == ("draft", "deck"))
        allowed, denied = rx.tools({"verb": "draft", "action": "deck", "checkout": "/c", "kit": str(self.kit)}, folder)
        self.assertIn(f"Read(/{self.kit.parent}/known-issues.md)", denied)
        self.assertIn(f"Read(/{self.kit.parent}/internal/**)", denied)
        self.assertIn(f"Read(/{self.kit.parent}/tests/**)", denied)
        # realm_sweep.DENIED denies the Notion writers; each apply allows its own and still denies the rest.
        allowed, denied = rx.tools({"verb": "apply", "action": "status"}, folder)
        self.assertIn("mcp__notion__notion-update-page", allowed)
        self.assertIn("mcp__notion__notion-create-pages", denied)
        self.assertIn("mcp__notion__notion-move-pages", denied)

    def test_only_the_deck_apply_loads_workspace_mcp_alone(self):
        argv = rx.claude_argv({"verb": "apply", "action": "deck"}, Path("/s/runs/x"), "p")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--strict-mcp-config", rx.claude_argv({"verb": "list", "action": None}, Path("/s/runs/x"), "p"))


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

    def test_a_deck_draft_without_the_brand_kit_is_off_naming_the_variable_and_both_remedies(self):
        self.link()
        with mock.patch.dict(os.environ, {"RESEARCH_BRAND_KIT": ""}):
            del os.environ["RESEARCH_BRAND_KIT"]
            status, body = self.post({"verb": "draft", "project": "research/alpha", "action": "deck"})
            doc = rx.document(self.connection, "research/alpha")
        self.assertEqual(status, 409)
        self.assertIn("RESEARCH_BRAND_KIT is not set. Export it, or copy local-project-dashboard/.env.example", body["error"])
        self.assertIn("RESEARCH_BRAND_KIT", doc["problems"]["kit"])

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
        status, started = self.post({"verb": "draft", "project": "research/alpha", "action": "deck"})
        self.assertEqual(self.model(started["run"], {"draft.json": {**DECK, "slides": DECK["slides"][:3]}})["state"], "failed")
        # A run killed mid-way leaves `running` with a dead pid: it reads failed, never running.
        status, started = self.post({"verb": "draft", "project": "research/alpha", "action": "status"})
        realm_sweep.write_record(self.state / "runs" / started["run"] / "record.json", {"state": "running", "pid": dead_pid()})
        run = next(r for r in rx.document(self.connection)["runs"] if r["run"] == started["run"])
        self.assertEqual(run["state"], "failed")
        self.assertIn("stopped without recording an outcome", run["reason"])

    def test_a_done_draft_shows_its_body_and_hash_and_the_deck_draft_reads_the_kit(self):
        self.link()
        run, fingerprint = self.draft("deck", DECK)
        self.assertIn(f"Read(/{self.kit})", self.seen["argv"][self.seen["argv"].index("--allowedTools") + 1])
        shown = next(r for r in rx.document(self.connection)["runs"] if r["run"] == run)
        self.assertEqual((shown["draft"], shown["fingerprint"]), (DECK, fingerprint))
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

    def test_a_deck_resumes_the_presentation_in_out_json_or_searches_its_exact_title(self):
        self.link()
        run, fingerprint = self.draft("deck", DECK)
        # The deck is bound to the account it was drafted for too.
        writes.set_setting(self.connection, rx.ACCOUNT_KEY, "other@example.test")
        status, body = self.apply(run, fingerprint)
        self.assertEqual(status, 409)
        self.assertTrue(body["error"].startswith("account:"), body)
        writes.set_setting(self.connection, rx.ACCOUNT_KEY, "operator@example.test")
        status, first = self.apply(run, fingerprint)
        self.assertEqual(status, 202, first)
        argv = rx.claude_argv(json.loads((self.state / "runs" / first["run"] / "request.json").read_text()),
                              self.state / "runs" / first["run"], "p")
        # The apply reads its own folder only: never the kit, so the confirmed spec is all it writes.
        self.assertFalse([t for t in argv[argv.index("--allowedTools") + 1].split(",") if t.startswith("Read(") and first["run"] not in t])
        realm_sweep.write_record(self.state / "runs" / first["run"] / "record.json", {"state": "running", "pid": dead_pid()})
        status, second = self.apply(run, fingerprint)
        text = self.prompt(second["run"])
        self.assertIn(f'named exactly "Span sampling - draft {run}"', text)
        (self.state / "runs" / second["run"] / "out.json").write_text(json.dumps({"outcome": "created", "presentation": "p1",
                                                                                 "url": "https://example.test/deck"}))
        realm_sweep.write_record(self.state / "runs" / second["run"] / "record.json", {"state": "running", "pid": dead_pid()})
        status, third = self.apply(run, fingerprint)
        text = self.prompt(third["run"])
        self.assertIn("created presentation p1: resume it", text)
        self.assertIn("only when\n   that shape holds no text", text)
        # The model said ok with the deck only created: fewer checked slides than drafted is failed, and resumable.
        # The model said ok short of the end: slides not all checked, in another order, or the blank one left. Each is
        # failed, and the next apply resumes the deck.
        made = {"outcome": "created", "presentation": "p1", "url": "https://example.test/deck"}
        for left in ({}, {"slides": 7, "cleared": True}, {"slides": IDS[::-1], "cleared": True}, {"slides": IDS}):
            record = self.model(third["run"], {"out.json": {**made, **left}})
            with self.subTest(left=left):
                self.assertEqual(record["state"], "failed")
                self.assertIn("apply again to resume it", record["reason"])
            status, third = self.apply(run, fingerprint)
            self.assertIn("created presentation p1: resume it", self.prompt(third["run"]))
        record = self.model(third["run"], {"out.json": {**made, "slides": IDS, "cleared": True}})
        self.assertEqual(record["deck"], {"presentation": "p1", "url": "https://example.test/deck", "slides": 7})


class TheDeckContract(Case):
    """Each Slides or Drive call the deck apply's prompt asks for, with the shape the API takes (the lane review's
    class pass): the slide ids, the first slide, the blank slide, the title search, the notes shape and the check."""

    def test_every_slide_id_a_draft_may_hold_is_a_slides_object_id_and_the_old_ids_are_refused(self):
        for count in (6, 7, 8):
            deck = {**DECK, "slides": [{**DECK["slides"][0], "id": f"sdexp_{n}"} for n in range(1, count + 1)]}
            rx._shape("deck", deck)
            for request in rx.slide_requests(deck):
                with self.subTest(count=count, request=request):
                    self.assertRegex(request["createSlide"]["objectId"], rx.OBJECT_ID)
        with self.assertRaisesRegex(ValueError, "id sdexp_1"):
            rx._shape("deck", {**DECK, "slides": [{**slide, "id": f"s{n}"} for n, slide in enumerate(DECK["slides"], 1)]})

    def test_the_apply_makes_every_spec_slide_the_first_too_and_removes_only_the_blank_one(self):
        self.link()
        run, fingerprint = self.draft("deck", DECK)
        status, started = self.apply(run, fingerprint)
        text = self.prompt(started["run"])
        for n, slide in enumerate(IDS):
            self.assertIn(f"{slide}: " + json.dumps({"createSlide": {"objectId": slide, "insertionIndex": n,
                                                                     "slideLayoutReference": {"predefinedLayout": "BLANK"}}}), text)
        self.assertNotIn("placeholder", text)
        self.assertIn("each slide whose\n   objectId does not start with sdexp_", text)
        self.assertIn("speakerNotesObjectId in\n   slideProperties.notesPage.notesProperties", text)
        self.assertIn(json.dumps(IDS), text)

    def test_a_title_with_quotes_still_makes_a_drive_name_query(self):
        title = rx.deck_title({"draft": "20261019-160000-000001"}, {"title": 'It\'s "fast" \\ cheap'})
        self.assertEqual(title, "Its fast  cheap - draft 20261019-160000-000001")
        self.assertEqual(rx.deck_title({"draft": "d"}, {"title": "'\""}), "Experiment - draft d")


class TheOlderState(Case):
    def test_no_state_folder_reads_never_listed_not_linked_and_no_runs(self):
        doc = rx.document(self.connection, "research/alpha")
        self.assertEqual((doc["cache"], doc["runs"], doc["link"], doc["running"]), (None, [], None, ""))
        self.assertEqual(doc["problems"], {"source": "", "account": "", "kit": ""})


class TheRoute(BrowserSession):
    def test_the_route_answers_before_the_checkout_pattern_and_a_bad_post_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Nothing of this machine's: the state, the kit and the account config are the test's.
        with mock.patch.object(rx, "state_dir", return_value=Path(tmp.name)), \
                mock.patch.object(rx, "brand_kit", return_value=("", "unset here")), \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name}):
            status, _, body = self.request("/api/research/experiments", headers={"Cookie": self.cookie})
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["runs"], [])
            status, _, body = self.post("/api/research/experiments/act", {"verb": "drop"})
            self.assertEqual(status, 400, body)
