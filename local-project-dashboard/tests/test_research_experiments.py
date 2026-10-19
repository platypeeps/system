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
#: Seven slides of three elements: a filled band, a styled text box, and a plain shape.
DECK = {"title": "Span sampling", "slides": [{"id": f"sdexp_{n}", "message": f"m{n}", "elements": [
    {"shape": "RECTANGLE", "x": 0, "y": 300, "w": 720, "h": 40, "fill": "#112233"},
    {"shape": "TEXT_BOX", "x": 40, "y": 40, "w": 640, "h": 60, "text": f"say {n}", "color": "#FFFFFF", "size": 28,
     "font": "Inter", "bold": True},
    {"shape": "ELLIPSE", "x": 600, "y": 200, "w": 80, "h": 80}]} for n in range(1, 8)]}
IDS = [f"sdexp_{n}" for n in range(1, 8)]
SHAPES = {sid: [f"{sid}_{k}" for k in (1, 2, 3)] for sid in IDS}


class FakeSlides:
    """Google Slides and the model that relays to it, for the deck apply: each call creates or finds the deck as the
    prompt says, sends `batches.json` as the API would (each batch whole or not at all), and writes `sent.json` and
    `deck.json`. The API's rules it keeps: a created objectId is 5 to 50 of [A-Za-z0-9_:-], new to the deck; every id a
    request names exists, made before it in the same batch or earlier; insertionIndex is within the deck.

    `edits(deck, round)` runs before a round's batches, as an operator would between runs; `refuse` names a batch
    index the API refuses; `stop` ends the model after the create, before it reads anything."""

    def __init__(self, deck=None, edits=None, refuse=None, stop=False):
        self.deck = deck  # [{"id", "elements": [...]}] or None until created
        self.edits, self.refuse, self.stop, self.round, self.sent = edits, refuse, stop, 0, []

    def ask(self, argv, cwd, timeout, out, keep=()):
        cwd, text = Path(cwd), argv[argv.index("-p") + 1]
        made = json.loads((cwd / "out.json").read_text()) if (cwd / "out.json").exists() else {}
        if "Create the presentation" in text:
            self.deck = [{"id": "p", "elements": ["i0", "i1"]}]
            (cwd / "out.json").write_text(json.dumps({"outcome": "created", "presentation": "p1",
                                                      "url": "https://example.test/deck"}))
            if self.stop:
                return realm_sweep.EXIT_ERROR, "RESULT: failed, 2: stopped", []
        elif "Search My Drive" in text:
            (cwd / "out.json").write_text(json.dumps({"outcome": "found", "presentation": "p1", "url": "https://example.test/deck"}
                                                     if self.deck is not None else {"outcome": "unknown"}))
            if self.deck is None:
                return realm_sweep.EXIT_OK, "RESULT: ok", []
        elif made.get("presentation") != "p1":
            return realm_sweep.EXIT_ERROR, "RESULT: failed, 1: no deck", []
        if self.edits:
            self.edits(self.deck, self.round)
        self.round += 1
        sent = []
        for n, batch in enumerate(json.loads((cwd / "batches.json").read_text())):
            error = "refused" if n == self.refuse else self.apply(batch)
            self.sent.append(batch)
            sent.append({"batch": n, "ok": True} if error is None else {"batch": n, "ok": False, "error": error})
            if error is not None:
                break
        (cwd / "sent.json").write_text(json.dumps(sent))
        (cwd / "deck.json").write_text(json.dumps({"slides": self.deck}))
        return realm_sweep.EXIT_OK, "RESULT: ok", []

    def apply(self, batch):
        """One batchUpdate: the deck after it, or the API's error and the deck untouched."""
        deck = [{"id": slide["id"], "elements": list(slide["elements"])} for slide in self.deck]
        def ids():
            return {slide["id"] for slide in deck} | {e for slide in deck for e in slide["elements"]}

        for request in batch:
            (kind, body), = request.items()
            made = body.get("objectId") if kind in ("createSlide", "createShape") else None
            if made is not None and (not rx.OBJECT_ID.fullmatch(made) or made in ids()):
                return f"{kind}: objectId {made!r} is not a new valid id"
            if kind == "createSlide":
                if not 0 <= body["insertionIndex"] <= len(deck):
                    return f"createSlide: insertionIndex {body['insertionIndex']} is outside the deck"
                deck.insert(body["insertionIndex"], {"id": made, "elements": []})
            elif kind == "createShape":
                page = next((slide for slide in deck if slide["id"] == body["elementProperties"]["pageObjectId"]), None)
                if page is None:
                    return f"createShape: no page {body['elementProperties']['pageObjectId']}"
                page["elements"].append(made)
            elif kind == "deleteObject":
                if body["objectId"] not in ids():
                    return f"deleteObject: no object {body['objectId']}"
                deck = [slide for slide in deck if slide["id"] != body["objectId"]]
                for slide in deck:
                    slide["elements"] = [e for e in slide["elements"] if e != body["objectId"]]
            elif body["objectId"] not in {e for slide in deck for e in slide["elements"]}:
                return f"{kind}: no shape {body['objectId']}"
        self.deck = deck
        return None


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
            if isinstance(files, FakeSlides):
                return files.ask(argv, cwd, timeout, out, keep)
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
        self.assertIn("An earlier apply made the deck p1; out.json names it.", self.prompt(third["run"]))
        # That run stopped before it read the new deck, so no blank slide was recorded: the resume deletes nothing.
        slides = FakeSlides([{"id": "p", "elements": ["i0"]}])
        record = self.model(third["run"], slides)
        self.assertEqual(record["deck"], {"presentation": "p1", "url": "https://example.test/deck", "slides": 7}, record)
        self.assertEqual([slide["id"] for slide in slides.deck], [*IDS, "p"])
        self.assertNotIn("deleteObject", json.dumps(slides.sent))


class TheDeckBatches(Case):
    """The deck apply's Slides requests, every one built by the runner and checked against a fake API (the lane
    review's class pass: the model executing a prose Slides contract). The model copies batches and reports; the ids,
    the references between requests, the one deletion and the resume are the runner's."""

    def start(self, deck=DECK):
        self.link()
        run, fingerprint = self.draft("deck", deck)
        self.run_, self.fingerprint = run, fingerprint
        status, started = self.apply(run, fingerprint)
        self.assertEqual(status, 202, started)
        return started["run"]

    def again(self):
        status, started = self.apply(self.run_, self.fingerprint)
        self.assertEqual(status, 202, started)
        return started["run"]

    def test_every_request_names_a_valid_id_made_before_it_or_already_there(self):
        full = [{"shape": "TEXT_BOX", "x": 0, "y": 0, "w": 720, "h": 405, "text": "t", "fill": "#000000",
                 "color": "#FFFFFF", "size": 96, "font": "F" * 50, "bold": False}] * rx.ELEMENTS
        for count in (6, 8):
            deck = {"title": "t", "slides": [{"id": f"sdexp_{n}", "message": "m", "elements": full}
                                             for n in range(1, count + 1)]}
            rx._shape("deck", deck)
            for there in ([], [{"id": "p", "elements": ["i0"]}]):
                known = {slide["id"] for slide in there}
                for batch in rx.plan(deck, there, None):
                    for request in batch:
                        (kind, body), = request.items()
                        with self.subTest(count=count, there=there, request=request):
                            if kind in ("createSlide", "createShape"):
                                self.assertRegex(body["objectId"], rx.OBJECT_ID)
                                self.assertNotIn(body["objectId"], known)
                                known.add(body["objectId"])
                            self.assertIn(body.get("elementProperties", {}).get("pageObjectId", body["objectId"]), known)
                            self.assertIn(body["objectId"], known)
        with self.assertRaisesRegex(ValueError, "id sdexp_1"):
            rx._shape("deck", {**DECK, "slides": [{**slide, "id": f"s{n}"} for n, slide in enumerate(DECK["slides"], 1)]})

    def test_a_new_deck_is_the_runners_batches_sent_as_written_and_loses_only_its_blank_slide(self):
        run = self.start()
        slides = FakeSlides()
        record = self.model(run, slides)
        self.assertEqual(record["state"], "done", record)
        self.assertEqual(record["deck"], {"presentation": "p1", "url": "https://example.test/deck", "slides": 7})
        self.assertEqual(slides.deck, [{"id": sid, "elements": SHAPES[sid]} for sid in IDS])
        built = [batch for batch in rx.plan(DECK, [{"id": "p", "elements": ["i0", "i1"]}], "p")]
        self.assertEqual(slides.sent, [*built, [{"deleteObject": {"objectId": "p"}}]])
        self.assertEqual(json.loads((self.state / "runs" / run / "out.json").read_text())["blank"], "p")
        text = rx.deck_prompt(json.loads((self.state / "runs" / run / "request.json").read_text()),
                              self.state / "runs" / run, False)
        self.assertIn("exactly as written: copy the JSON; change, add and drop\n   nothing", text)
        self.assertNotIn("objectId", text)

    def test_the_blank_slide_goes_only_when_recorded_at_creation_and_still_there(self):
        def added(deck, round_):
            if round_ == 1:
                deck.append({"id": "op_slide", "elements": []})

        def gone(deck, round_):
            if round_ == 1:
                deck[:] = [slide for slide in deck if slide["id"] != "p"]

        def template(deck, round_):
            if round_ == 0:
                deck.append({"id": "q", "elements": []})

        for edits, kept in ((added, ["op_slide"]), (gone, []), (template, ["p", "q"])):
            with self.subTest(edits=edits.__name__):
                slides = FakeSlides(edits=edits)
                record = self.model(self.start(), slides)
                self.assertEqual(record["state"], "done", record)
                self.assertEqual(sorted(slide["id"] for slide in slides.deck), sorted([*IDS, *kept]))
                self.assertEqual("deleteObject" in json.dumps(slides.sent), edits is added)
        # The model stopped after the create, before any read: no blank is recorded, and the resume deletes nothing.
        run = self.start()
        record = self.model(run, FakeSlides(stop=True))
        self.assertEqual(record["state"], "failed")
        self.assertIn("apply again to resume it", record["reason"])
        self.assertIsNone(json.loads((self.state / "runs" / run / "out.json").read_text())["blank"])
        slides = FakeSlides([{"id": "p", "elements": ["i0"]}])
        self.assertEqual(self.model(self.again(), slides)["state"], "done")
        self.assertEqual([slide["id"] for slide in slides.deck], [*IDS, "p"])
        self.assertEqual(rx.plan(DECK, [{"id": sid, "elements": SHAPES[sid]} for sid in IDS], "p"), [])
        self.assertEqual(rx.plan(DECK, [{"id": sid, "elements": SHAPES[sid]} for sid in IDS] + [{"id": "p", "elements": []}],
                                 None), [])

    def test_a_slide_missing_shapes_gets_only_their_requests_by_id(self):
        whole = [{"id": sid, "elements": list(SHAPES[sid])} for sid in IDS]
        whole[2]["elements"].remove("sdexp_3_2")
        self.assertEqual(rx.plan(DECK, whole, None), [rx.element_requests("sdexp_3", 2, DECK["slides"][2]["elements"][1])])
        run = self.start()
        made = json.loads((self.state / "runs" / run / "request.json").read_text())
        self.assertEqual(made["resume"], "")
        slides = FakeSlides(whole)
        (self.state / "runs" / run / "out.json").write_text(json.dumps({"outcome": "created", "presentation": "p1",
                                                                       "url": "https://example.test/deck", "blank": None}))
        realm_sweep.write_record(self.state / "runs" / run / "record.json", {"state": "failed", "pid": dead_pid()})
        record = self.model(self.again(), slides)
        self.assertEqual(record["state"], "done", record)
        self.assertEqual(slides.sent, [rx.element_requests("sdexp_3", 2, DECK["slides"][2]["elements"][1])])

    def test_a_resume_after_operator_edits_restores_only_what_is_missing_and_deletes_nothing_else(self):
        # The first apply stops with the last slide refused: six slides built, the blank slide still there.
        record = self.model(self.start(), slides := FakeSlides(refuse=6))
        self.assertEqual(record["state"], "failed", record)
        # The operator, between runs: deletes a shape and a slide, adds a slide and a shape, and moves a slide.
        deck = slides.deck
        self.assertEqual([slide["id"] for slide in deck], [*IDS[:6], "p"])
        deck[1]["elements"].remove("sdexp_2_1")
        deck[3]["elements"].append("op_shape")
        deck[:] = [slide for slide in deck if slide["id"] != "sdexp_5"]
        deck.insert(2, {"id": "op_slide", "elements": ["op_text"]})
        deck.insert(0, deck.pop(5))
        before = [slide["id"] for slide in deck]
        resumed = self.again()
        self.assertEqual(json.loads((self.state / "runs" / resumed / "request.json").read_text())["resume"]["blank"], "p")
        slides.sent, slides.refuse = [], None
        record = self.model(resumed, slides)
        self.assertEqual(record["state"], "done", record)
        self.assertEqual(slides.sent[0], rx.element_requests("sdexp_2", 1, DECK["slides"][1]["elements"][0]))
        self.assertEqual([batch[0]["createSlide"]["objectId"] for batch in slides.sent[1:3]], ["sdexp_5", "sdexp_7"])
        self.assertEqual(slides.sent[3:], [[{"deleteObject": {"objectId": "p"}}]])
        ids = [slide["id"] for slide in slides.deck]
        self.assertEqual(ids.index("sdexp_5"), ids.index("sdexp_4") + 1)
        self.assertEqual(ids.index("sdexp_7"), ids.index("sdexp_6") + 1)
        self.assertEqual([i for i in ids if i not in ("sdexp_5", "sdexp_7")], [i for i in before if i != "p"])
        self.assertIn("op_shape", next(slide for slide in slides.deck if slide["id"] == "sdexp_4")["elements"])
        self.assertIn("op_slide", ids)

    def test_a_refused_batch_fails_naming_the_api_error_and_the_next_apply_finishes(self):
        run = self.start()
        record = self.model(run, slides := FakeSlides(refuse=1))
        self.assertEqual(record["state"], "failed")
        self.assertIn("Slides took 1 of 7 batches: refused; apply again to resume it", record["reason"])
        slides.refuse = None
        self.assertEqual(self.model(self.again(), slides)["state"], "done")
        self.assertEqual(slides.deck, [{"id": sid, "elements": SHAPES[sid]} for sid in IDS])

    def test_a_draft_the_runner_cannot_build_is_refused_naming_the_field(self):
        element = DECK["slides"][0]["elements"][1]
        for bad, says in (({"visual": "a band", "notes": "n"}, "elements"),
                          ({"elements": [{**element, "x": 700}]}, "does not fit"),
                          ({"elements": [{**element, "fill": "red"}]}, "fill is not #RRGGBB"),
                          ({"elements": [{**element, "notes": "n"}]}, "does not build"),
                          ({"elements": [{**element, "shape": "IMAGE"}]}, "shape of"),
                          ({"elements": [element] * (rx.ELEMENTS + 1)}, "elements"),
                          ({"elements": [{**element, "size": float("inf")}]}, "size"),
                          ({"elements": [{**element, "w": float("nan")}]}, "does not fit"),
                          ({"elements": [{**element, "bold": "yes"}]}, "bold")):
            slide = {k: v for k, v in DECK["slides"][0].items() if k != "elements"} | bad
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, says):
                rx._shape("deck", {**DECK, "slides": [slide, *DECK["slides"][1:]]})
        # A draft from before the runner built the requests: its apply fails naming it, and sends nothing.
        folder = self.state / "runs" / "old"
        folder.mkdir(parents=True)
        (folder / "draft.json").write_text(json.dumps({"title": "t", "style": {}, "slides": [
            {"id": f"sdexp_{n}", "message": "m", "visual": "v", "notes": "n"} for n in range(1, 8)]}))
        with self.assertRaisesRegex(ValueError, "elements"):
            rx._deck({"resume": ""}, folder, lambda text: self.fail("asked the model"))

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
