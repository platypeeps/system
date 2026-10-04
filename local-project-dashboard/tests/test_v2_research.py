"""The v2 Research page (sd:2122).

What this slice promises: `/research` answers under the shared policy and loads
its script before `shell.js`; `/api/research` is the board `collect_research`
reads (every checkout with a `research.conf.py`, one group deep), with each
checkout's stage and render freshness, read in a child under a budget;
`/api/research/<checkout>` is that checkout's source ledger, and any other path
is a 404. Nothing reads review rounds or claims, and the document says so. A
research page built into `docs/dashboard/`, where `sd-research-kit` renders,
counts as built.

The page shows the board, the ledger and the reasons; registers the design's
Research commands with the design's ids, labels and keys; keeps Render and
Review copy only and Start research off with a reason; and posts nothing.

`research.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` uses. The browser half -- the look at 375 px, focus,
the Shapeshift field -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import itertools
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import research_screen, server, v2

from support import NOW, ScreenCase
from test_v2_read import READ_SHELL
from test_v2_today import OSASCRIPT, Refused
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers

V2 = Path(v2.__file__).resolve().parent
RESEARCH_JS = (V2 / "static" / "research.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COLLECTORS = Path(research_screen.__file__).resolve().parents[1] / "collectors.py"

#: The design's Research commands (design source products/system/designs/pages/research.js): id, object type, label, key, risk.
#: Start was risk undo in the design; with no run to take back it asks first (build:).
COMMANDS = [
    ["research.render", "research project", "Render", "r", "safe"],
    ["research.review", "research project", "Review", "v", "safe"],
    ["research.start", "research request", "Run", "r", "confirm"],
]

REGISTRY = """# Sources

Compiled 2026-08-27 from the primary releases.
Each row is a dated read.

---

## Primary

| # | Source | Read | Weight |
|---|---|---|---|
| A | [Spec release](https://example.test/spec) — **bold** `code` | read 2026-08-26 | Primary |
| B | Vendor note <https://example.test/note> | 2026-08-27 | Secondary \\| vendor |

## Later

| ID | Title | Used for |
|---|---|---|
| S3 | ~~Old~~ claim | measurement |

Not a row | after the table
"""


def shq_path(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def collectors_module():
    spec = importlib.util.spec_from_file_location("test_research_collectors", COLLECTORS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def checkout(root: Path, key: str, *, conf: str = "DOCS=[dict(src='index.md', out='index', title='Index')]\n",
             dirs=(), registry=None) -> Path:
    repo = root / key
    repo.mkdir(parents=True)
    (repo / "research.conf.py").write_text(conf, encoding="utf-8")
    (repo / "index.md").write_text("A source page.\n", encoding="utf-8")
    for name, markdown in dirs:
        (repo / name).mkdir()
        if markdown:
            (repo / name / "note.md").write_text("# note\n", encoding="utf-8")
    if registry is not None:
        (repo / "SOURCES.md").write_text(registry, encoding="utf-8")
    return repo


class TheReader(unittest.TestCase):
    """The filesystem readers the page side runs in its child: ledger, stage and render."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_a_ledger_reads_its_provenance_sections_headers_and_rows(self):
        head, rows = research_screen.parse_registry(REGISTRY, "SOURCES.md")
        self.assertEqual(head["prov"], "Compiled 2026-08-27 from the primary releases. Each row is a dated read.")
        self.assertEqual([(r["sec"], r["id"]) for r in rows], [("Primary", "A"), ("Primary", "B"), ("Later", "S3")])
        a, b, s3 = rows
        self.assertEqual((a["title"], a["url"], a["c3"], a["c4"]), ("Spec release — bold code", "https://example.test/spec", "read 2026-08-26", "Primary"))
        self.assertEqual(a["h"], ["#", "Source", "Read", "Weight"])
        self.assertEqual((b["url"], b["c4"]), ("https://example.test/note", "Secondary | vendor"))
        self.assertEqual((s3["title"], s3["c4"], s3["h"]), ("Old claim", "", ["ID", "Title", "Used for", ""]))

    def test_a_table_without_outer_pipes_is_read(self):
        # sd:2414: 'ID | Source' over '--- | ---' read as empty. A pipe in a prose line over a rule is still provenance.
        text = "Kept by hand | weekly.\n---\n\n## Plain\n\nID | Source | Used for\n--- | --- | ---\nS1 | A plain row | why\nS2 | [Linked](https://example.test/l) | how\n"
        head, rows = research_screen.parse_registry(text, "SOURCES.md")
        self.assertEqual(head["prov"], "Kept by hand | weekly.")
        self.assertEqual([(r["sec"], r["id"], r["title"], r["c3"]) for r in rows],
                         [("Plain", "S1", "A plain row", "why"), ("Plain", "S2", "Linked", "how")])
        self.assertEqual((rows[1]["url"], rows[0]["h"]), ("https://example.test/l", ["ID", "Source", "Used for", ""]))

    def test_the_ledger_files_are_read_in_order_and_cut_at_the_row_bound_with_their_total(self):
        repo = checkout(self.root, "group/one", registry=REGISTRY)
        (repo / "10-sources").mkdir()
        long = "| # | Source |\n|---|---|\n" + "".join(f"| S{n} | Source {n} |\n" for n in range(research_screen.ROWS + 5))
        (repo / "10-sources" / "registry.md").write_text(long, encoding="utf-8")
        found = research_screen.registry(repo)
        self.assertEqual([f["file"] for f in found["files"]], ["SOURCES.md", "10-sources/registry.md"])
        self.assertEqual(found["total"], 3 + research_screen.ROWS + 5)
        self.assertEqual(len(found["rows"]), research_screen.ROWS)
        self.assertEqual(found["rows"][3]["f"], "10-sources/registry.md")

    def test_the_ledger_document_stays_under_its_print_bound(self):
        repo = checkout(self.root, "group/wide")
        # Two-byte characters at the cell bound: 60 such rows print past the bound unless the reader cuts rows.
        cell = "é" * 500
        table = "| # | Source | Read | Weight |\n|---|---|---|---|\n" + f"| S | {cell} | {cell} | {cell} |\n" * 70
        (repo / "SOURCES.md").write_text(table, encoding="utf-8")
        found = research_screen.registry(repo)
        self.assertLessEqual(len(json.dumps(found, ensure_ascii=False).encode("utf-8")), research_screen.PRINT_BYTES)
        self.assertEqual(found["total"], 70)
        self.assertLess(len(found["rows"]), research_screen.ROWS)
        self.assertTrue(all(len(r["title"]) <= research_screen.CELL for r in found["rows"]))

    def test_an_oversized_or_linked_ledger_is_not_read(self):
        repo = checkout(self.root, "group/big")
        (repo / "SOURCES.md").write_bytes(b"x" * (research_screen.REGISTRY_BYTES + 1))
        outside = self.root / "outside.md"
        outside.write_text(REGISTRY, encoding="utf-8")
        (repo / "10-sources").mkdir()
        (repo / "10-sources" / "registry.md").symlink_to(outside)
        found = research_screen.registry(repo)
        self.assertEqual([(f["file"], bool(f["error"])) for f in found["files"]], [("SOURCES.md", True)])
        self.assertEqual(found["rows"], [])

    def test_the_stage_is_the_numbered_directories_that_hold_markdown(self):
        bare = checkout(self.root, "group/bare")
        self.assertIsNone(research_screen.stage(bare))
        laid = checkout(self.root, "group/laid", dirs=[("00-overview", True), ("10-sources", False), ("30-brief", True)])
        self.assertEqual(research_screen.stage(laid), ["00-overview", "30-brief"])

    def test_render_is_unknown_without_a_read_config_or_documents_and_counts_what_is_not_fresh(self):
        self.assertEqual(research_screen.render({"error": "computed"})["s"], "unknown")
        self.assertEqual(research_screen.render({"error": "", "docs": []})["txt"], "no documents declared")
        self.assertEqual(research_screen.render({"error": "", "docs": [{"state": "stale"}]})["s"], "caution")
        stale = research_screen.render({"error": "", "docs": [{"state": "stale"}, {"state": "not built"}, {"state": "fresh"}]})
        self.assertEqual(stale, {"s": "caution", "txt": "2 of 3 documents not fresh", "sub": "1 stale · 1 not built"})
        fresh = research_screen.render({"error": "", "docs": [{"state": "fresh", "updated": "2026-09-01"}]})
        self.assertEqual(fresh, {"s": "ok", "txt": "1 of 1 fresh", "sub": "newest source 2026-09-01"})

    def test_a_key_names_only_a_research_checkout_under_the_root(self):
        checkout(self.root, "group/one")
        (self.root / "group" / "plain").mkdir()
        (self.root / "group" / "link").symlink_to(self.root / "group" / "one")
        self.assertEqual(research_screen.checkout(self.root, "group/one"), self.root / "group" / "one")
        for key in ("group/plain", "group/link", "../group/one", "group/../group/one", ".hidden", "group/one/extra"):
            self.assertIsNone(research_screen.checkout(self.root, key), key)

    def test_the_board_cuts_documents_then_projects_to_stay_under_its_print_bound(self):
        docs = "".join(f"dict(src='{'s' * 300}.md', out='o{i}', title='{'t' * 700}'),\n" for i in range(100))
        for n in range(3):
            checkout(self.root, f"deep/w{n}", conf=f"DOCS=[\n{docs}]\n")
        for n in range(220):
            checkout(self.root, f"many/{'m' * 90}{n:03}", conf="DOCS=[]\n")
        collectors = collectors_module()
        with mock.patch.object(collectors, "REPO_ROOT", self.root), mock.patch.object(collectors, "git_facts", return_value=None):
            found = research_screen.board(collectors)
        self.assertLessEqual(len(json.dumps(found, ensure_ascii=False, separators=(",", ":")).encode("utf-8")), research_screen.PRINT_BYTES)
        listed = found["projects"]
        self.assertEqual(found["cut"], 223 - len(listed))
        self.assertGreater(found["cut"], 0)
        self.assertTrue(all(p["docs_total"] == 100 for p in listed if p["key"].startswith("deep/")))
        # Documents go before projects: the three wide checkouts sort first and stay listed with their whole count.
        self.assertEqual([p["key"] for p in listed[:3]], ["deep/w0", "deep/w1", "deep/w2"])
        self.assertTrue(all(len(p["docs"]) < 100 for p in listed[:3]))

    def test_a_linked_checkout_or_group_is_never_read(self):
        # The walk followed linked groups and checkouts, so it parsed a config and ran git outside the root before the board dropped the row.
        checkout(self.root, "group/one")
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        checkout(outside, "group/away")
        (self.root / "group" / "linked").symlink_to(outside / "group" / "away")
        (self.root / "elsewhere").symlink_to(outside / "group")
        (self.root / "group" / "inner").symlink_to(self.root / "group" / "one")
        (self.root / "group" / "conf").mkdir()
        (self.root / "group" / "conf" / "research.conf.py").symlink_to(outside / "group" / "away" / "research.conf.py")
        collectors = collectors_module()
        with mock.patch.object(collectors, "REPO_ROOT", self.root), \
                mock.patch.object(collectors, "git_facts", return_value=None) as git, \
                mock.patch.object(collectors, "read_research_config", wraps=collectors.read_research_config) as conf:
            names = [item["name"] for item in collectors.collect_research()]
        self.assertEqual(names, ["one"])
        self.assertEqual([call.args[0] for call in conf.call_args_list], [self.root / "group" / "one" / "research.conf.py"])
        self.assertEqual([call.args[0] for call in git.call_args_list], [self.root / "group" / "one"])

    def test_a_page_built_into_docs_dashboard_counts_as_built(self):
        # sd-research-kit renders into docs/dashboard/; the collector looked only in build/, so every document was "not built".
        repo = checkout(self.root, "group/one")
        page = repo / "docs" / "dashboard" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_text("<p>built</p>", encoding="utf-8")
        os.utime(repo / "index.md", (1_000_000_000, 1_000_000_000))
        old = checkout(self.root, "group/old")
        (old / "build").mkdir()
        (old / "build" / "index.html").write_text("<p>built</p>", encoding="utf-8")
        os.utime(old / "index.md", (1_000_000_000, 1_000_000_000))
        collectors = collectors_module()
        with mock.patch.object(collectors, "REPO_ROOT", self.root), mock.patch.object(collectors, "git_facts", return_value=None):
            states = {item["name"]: [d["state"] for d in item["docs"]] for item in collectors.collect_research()}
        self.assertEqual(states, {"one": ["fresh"], "old": ["fresh"]})

    def test_a_built_document_names_no_address_nothing_serves(self):
        # sd:2399: /research/<repo>/<out>.html had no route, and no view reads it; the served copy is on Documents.
        repo = checkout(self.root, "group/one")
        (repo / "docs" / "dashboard").mkdir(parents=True)
        (repo / "docs" / "dashboard" / "index.html").write_text("<p>built</p>", encoding="utf-8")
        collectors = collectors_module()
        with mock.patch.object(collectors, "REPO_ROOT", self.root), mock.patch.object(collectors, "git_facts", return_value=None):
            [item] = collectors.collect_research()
        self.assertEqual([d["state"] for d in item["docs"]], ["fresh"])
        self.assertNotIn("href", item["docs"][0])

    def test_the_child_counts_its_deadline_from_the_page_start(self):
        # sd:2501: the child counted from its own main(), so a slow interpreter start spent the margin and the page's kill won.
        argv = []

        class Budget:
            seconds = research_screen.RESEARCH_SECONDS

            def __init__(self, *_, **__):
                pass

            def run(self, command, label):
                argv.append(command)
                return mock.Mock(returncode=0, stdout="{}", stderr="")

        stub = mock.Mock(Budget=Budget, OverBudget=RuntimeError)
        before = time.clock_gettime(time.CLOCK_MONOTONIC)
        with mock.patch.object(research_screen, "_collectors", return_value=stub):
            research_screen.collect("board")
            research_screen.collect("sources", "research/alpha")
        after = time.clock_gettime(time.CLOCK_MONOTONIC)
        self.assertEqual([a[3:-1] for a in argv], [["board", "4"], ["sources", "research/alpha", "4"]])
        self.assertTrue(all(before <= float(a[-1]) <= after for a in argv), argv)
        deadlines = []

        class Collectors:
            REPO_ROOT = str(self.root)

            @staticmethod
            @contextlib.contextmanager
            def set_deadline(seconds, *, started):
                deadlines.append(started)
                yield

        since = time.clock_gettime(time.CLOCK_MONOTONIC) - 2.0
        with mock.patch.object(research_screen, "_collectors", return_value=Collectors), \
                mock.patch.object(research_screen, "board", return_value={}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(research_screen.main(["board", "4", f"{since:.6f}"]), 0)
        # Two seconds spent starting come off the deadline, never on top of it.
        self.assertLessEqual(deadlines[0], time.monotonic() - 2.0)


class TheDocument(ScreenCase):
    """`research_screen.document` and `sources`, through the real child, against a temporary checkout tree."""

    def setUp(self):
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict("os.environ", {"REPO_ROOT": str(self.root)}))

    def test_the_board_lists_each_checkout_with_its_key_stage_render_and_config_refusal(self):
        checkout(self.root, "research/alpha", dirs=[("00-overview", True)], registry=REGISTRY)
        checkout(self.root, "research/beta", conf="A=dict(src='x.md')\nA['out']='x'\nDOCS=[A]\n")
        doc = research_screen.document(now=NOW)
        self.assertEqual(doc["error"], "")
        self.assertEqual((doc["read"], doc["cap"], doc["dirs"]), (NOW, 2, list(research_screen.DIRS)))
        self.assertEqual(doc["rounds"], {"available": False, "reason": research_screen.ROUNDS_REASON})
        self.assertEqual(doc["claims"], {"available": False, "reason": research_screen.CLAIMS_REASON})
        rows = {p["key"]: p for p in doc["projects"]}
        self.assertEqual(set(rows), {"research/alpha", "research/beta"})
        alpha, beta = rows["research/alpha"], rows["research/beta"]
        self.assertEqual((alpha["label"], alpha["dirs"], alpha["render"]["s"], alpha["conf"], alpha["git"]),
                         ("alpha", ["00-overview"], "caution", None, None))
        self.assertEqual(alpha["docs"], [{"title": "Index", "src": "index.md", "state": "not built", "updated": alpha["docs"][0]["updated"]}])
        self.assertEqual((beta["render"]["s"], beta["dirs"]), ("unknown", None))
        self.assertIn("computed configuration is not executed", beta["conf"])

    def test_a_ledger_is_served_only_for_a_research_checkout(self):
        checkout(self.root, "research/alpha", registry=REGISTRY)
        (self.root / "research" / "plain").mkdir()
        found = research_screen.sources("research/alpha", now=NOW)
        self.assertEqual((found["error"], found["total"], [r["id"] for r in found["rows"]]), ("", 3, ["A", "B", "S3"]))
        for key in ("research/plain", "research/missing", "../etc", "research/alpha/x"):
            self.assertIsNone(research_screen.sources(key, now=NOW), key)

    def test_the_board_lists_only_the_checkouts_a_ledger_is_served_for(self):
        # A linked checkout, or a group linked out of the root, was on the board while its ledger was a 404.
        checkout(self.root, "research/alpha", registry=REGISTRY)
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        checkout(outside, "group/away", registry=REGISTRY)
        (self.root / "research" / "linked").symlink_to(outside / "group" / "away")
        (self.root / "elsewhere").symlink_to(outside / "group")
        keys = [p["key"] for p in research_screen.document(now=NOW)["projects"]]
        self.assertEqual(keys, ["research/alpha"])
        for key in ("research/linked", "elsewhere/away"):
            self.assertIsNone(research_screen.sources(key, now=NOW), key)

    def test_a_ledger_is_never_served_through_a_linked_group_or_config(self):
        # The key path took a checkout through a linked group, or one whose research.conf.py is a link; the walk skips both.
        checkout(self.root, "research/alpha", registry=REGISTRY)
        (self.root / "alias").symlink_to(self.root / "research")
        linked = self.root / "research" / "conflink"
        linked.mkdir()
        (linked / "SOURCES.md").write_text(REGISTRY, encoding="utf-8")
        (linked / "research.conf.py").symlink_to(self.root / "research" / "alpha" / "research.conf.py")
        self.assertEqual([p["key"] for p in research_screen.document(now=NOW)["projects"]], ["research/alpha"])
        self.assertEqual(research_screen.sources("research/alpha", now=NOW)["total"], 3)
        for key in ("alias/alpha", "research/conflink"):
            self.assertIsNone(research_screen.sources(key, now=NOW), key)
        # REPO_ROOT itself may be a link; a checkout directly under it still serves its ledger.
        checkout(self.root, "solo", registry=REGISTRY)
        link = Path(self.enterContext(tempfile.TemporaryDirectory())) / "repos"
        link.symlink_to(self.root)
        with mock.patch.dict("os.environ", {"REPO_ROOT": str(link)}):
            self.assertEqual(research_screen.sources("solo", now=NOW)["total"], 3)

    def test_an_accepted_config_at_its_limits_still_fits_the_board(self):
        # 100 documents with 700-character titles printed 75,763 bytes; the 64 KiB read killed the child and the board was empty.
        docs = "".join(f"dict(src='index.md', out='o{i}', title='{'t' * 700}'),\n" for i in range(100))
        checkout(self.root, "research/wide", conf=f"DOCS=[\n{docs}]\n")
        checkout(self.root, "research/alpha")
        doc = research_screen.document(now=NOW)
        self.assertEqual(doc["error"], "")
        rows = {p["key"]: p for p in doc["projects"]}
        self.assertEqual(set(rows), {"research/alpha", "research/wide"})
        wide = rows["research/wide"]
        self.assertEqual((wide["docs_total"], wide["render"]["txt"]), (100, "100 of 100 documents not fresh"))
        self.assertTrue(all(len(d["title"]) <= research_screen.CELL for d in wide["docs"]))

    def test_a_reader_that_fails_is_named_and_lists_nothing(self):
        def broken(*_):
            raise ValueError("research reading was stopped at its budget: python ran past its budget of 4 seconds")
        doc = research_screen.document(now=NOW, backend=broken)
        self.assertEqual((doc["projects"], doc["error"]), ([], "research reading was stopped at its budget: python ran past its budget of 4 seconds"))
        self.assertIn("stopped at its budget", research_screen.sources("research/alpha", now=NOW, backend=broken)["error"])


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/research")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Research · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "research.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_and_the_old_views_stay_in_the_palette(self):
        self.assertEqual(v2.SECTIONS.get("Research"), "/research")
        self.assertNotIn("Research", v2.CLASSIC)
        self.assertIn("/operations?area=resources", v2.SCREENS.values())

    def test_the_data_routes_need_a_session_take_no_query_and_name_an_unknown_checkout(self):
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/research")[0], 403)
        self.assertEqual(self.request("/api/research/research/alpha")[0], 403)
        self.assertEqual(self.request("/api/research?state=caution", headers=cookie)[0], 400)
        board = {"root": "~/repos", "projects": [{"key": "research/alpha"}]}
        ledger = {"found": True, "files": [], "rows": [], "total": 0}
        answers = {("board",): board, ("sources", "research/alpha"): ledger}
        with mock.patch.object(research_screen, "collect", lambda *a: answers.get(a, {"found": False})):
            status, _, body = self.request("/api/research", headers=cookie)
            self.assertEqual((status, json.loads(body)["projects"]), (200, board["projects"]))
            status, _, body = self.request("/api/research/research/alpha", headers=cookie)
            self.assertEqual((status, json.loads(body)["key"]), (200, "research/alpha"))
            self.assertEqual(self.request("/api/research/research/missing", headers=cookie)[0], 404)
            self.assertEqual(self.request("/api/research/..%2Fetc", headers=cookie)[0], 404)


# The stand-in additions research.js needs beyond test_v2_tasks: the shell's row, url, setContext and a record of fetched paths.
SHELL_MORE = r"""
window.shell.row = () => ROW; window.shell.setContext = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
var ROW = null;
"""


def project(key, label, render, *, conf=None, docs=None, dirs=None):
    return {"key": key, "label": label, "path": f"~/repos/{key}", "dirs": dirs, "render": render, "conf": conf,
            "docs": docs or [], "git": {"branch": "main", "dirty": 1, "behind": 0, "last_iso": "2026-09-05T10:00:00Z", "subject": "A commit"}}


class TheScript(ScreenCase):
    """research.js against documents shaped as `research_screen` builds them."""

    def setUp(self):
        super().setUp()
        self.doc = research_screen.document(now=NOW, backend=lambda *_: {"root": "~/repos", "projects": [
            project("research/alpha", "Alpha", {"s": "ok", "txt": "1 of 1 fresh", "sub": ""}, dirs=["00-overview", "10-sources"]),
            project("research/beta", "Beta", {"s": "caution", "txt": "1 of 1 documents not fresh", "sub": "1 stale"},
                    docs=[{"title": "Brief", "src": "30-brief/BRIEF.md", "state": "stale", "updated": "2026-09-01"}]),
            project("group/gamma", "Gamma", {"s": "unknown", "txt": "config unread", "sub": ""}, conf="computed data"),
        ]})
        _, rows = research_screen.parse_registry(REGISTRY, "SOURCES.md")
        self.ledger = {"read": NOW, "key": "research/beta", "error": "", "files": [{"file": "SOURCES.md", "mtime": "2026-09-01T10:00Z", "prov": "Compiled.", "error": ""}],
                       "rows": rows, "total": 3}

    def run_page(self, body, answer=None, search="", row="null"):
        answer = answer or (f"(path) => path === '/api/research' ? [200, {json.dumps(self.doc)}]"
                            f" : path === '/api/research/research/beta' ? [200, {json.dumps(self.ledger)}] : [404, {{ error: 'No research checkout at this address.' }}]")
        script = (STAND_IN + f"location.search = {json.dumps(search)};\n" + MARKUP_JS + "\nconst mk = window.markup.html;\n"
                  + SHELL + SHELL_MORE + "\n" + READ_SHELL + f"\nROW = {row};\nANSWER = {answer};\n" + RESEARCH_JS
                  + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.attention = OUT.attention; OUT.page = window.PAGE_ATTENTION;"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]);")
        self.assertEqual(out["R"]["reg"], COMMANDS)

    def test_render_and_review_are_copy_only_and_name_the_checkout(self):
        out = self.run_page("""const beta = C.get('research/beta');
R.lines = ['research.render', 'research.review'].map(id => [cmd(id).executes, cmd(id).cli(beta), cmd(id).primary ? cmd(id).primary(beta) : null]);
['research.render', 'research.review'].forEach(id => shellRun(cmd(id), beta)); await flush();""")
        self.assertEqual(out["R"]["lines"], [[False, "cd ~/'repos/research/beta' && sd-research-kit render", True],
                                             [False, "cd ~/'repos/research/beta' && sd-research-kit review", None]])
        self.assertEqual(out["posts"], [])
        self.assertEqual([t[0] for t in out["toasts"]], ["Copy it into a terminal: the dashboard does not run sd-research-kit"] * 2)

    def test_start_research_is_off_with_its_reason_shows_the_lines_and_sends_nothing(self):
        out = self.run_page("""const input = ELS.shift;
input.value = 'how do vendors detect spans? repo:beta deep'; (input.listeners.input || []).forEach(f => f());
(input.listeners.input || []).forEach(f => f());
R.draft = Object.assign({}, C.get('draft:research')); R.when = cmd('research.start').when(C.get('draft:research'));
R.executes = cmd('research.start').executes; R.req = ELS.req.html;
(input.listeners.keydown || []).forEach(f => f({ key: 'Enter', preventDefault() {}, stopPropagation() {} })); await flush();""")
        draft = out["R"]["draft"]
        self.assertEqual((draft["q"], draft["repo"], draft["depth"]), ("how do vendors detect spans?", "research/beta", "deep"))
        self.assertEqual(draft["cmd"], "ITEM=$(sd task add 'Research: how do vendors detect spans?' --body 'repo=beta depth=deep skill=sd-research-repo stage=draft' --json"
                                       " | python3 -c 'import json, sys; print(json.load(sys.stdin)[\"item\"][\"id\"])') &&\n"
                                       "sd run --sequential --role author --scope 'beta' --budget-minutes 60 \"$ITEM\"")
        # The copied lines run as they are: no bare <item>, which the shell reads as a redirect from a file named item.
        self.assertNotRegex(draft["cmd"], r"(?<![\"'])<\w")
        self.assertIn("does not create items or queue runs", out["R"]["when"])
        self.assertFalse(out["R"]["executes"])
        self.assertIn('data-copy="ITEM=$(sd task add', out["R"]["req"])
        self.assertIn("Not started here:", out["R"]["req"])
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["confirms"], [])
        self.assertEqual([t[0] for t in out["toasts"]], [f"Not started: {out['R']['when']}"])

    def test_start_mode_after_an_empty_filter_selects_a_shown_row_again(self):
        # Start mode cleared the filter and redrew the board but kept the selection the empty filter had cleared.
        out = self.run_page("""window.shell.reconcile = o => { const keys = visible().map(p => p.key);
  if (!keys.includes(o.current)) { if (keys.length) o.select(keys[0]); else o.clear(); } };
const input = ELS.shift; const fire = (t, e) => (input.listeners[t] || []).forEach(f => f(e));
input.value = 'zzz'; fire('input'); R.cleared = selected;
input.selectionStart = input.value.length; fire('keydown', { key: 'ArrowRight', preventDefault() {}, stopPropagation() {} }); await flush();
R.selected = selected; R.req = ELS.req.html;""")
        self.assertIsNone(out["R"]["cleared"])
        self.assertEqual(out["R"]["selected"], "research/beta")
        self.assertIn("queues one author assignment in beta", out["R"]["req"])

    def test_the_field_guesses_a_question_as_the_reference_did_and_in_linear_time(self):
        # The reference's guess was one regex with a nested quantifier; a run of tags after a ? backtracked exponentially.
        reference = re.compile(r"\?\s*(\S+:\S+\s*)*$|^(start|research|investigate|how|why|what|which|does|is|can)\b", re.I)
        texts = sorted({"".join(t).strip() for n in range(1, 6) for t in itertools.product("a?: x", repeat=n)}
                       | {"how do vendors detect spans? repo:beta deep", "spans? repo:beta depth:deep", "Why", "a? b:c d", "is", "isle"})
        out = self.run_page(f"""R.got = {json.dumps(texts)}.map(guess);
const t = Date.now(); R.long = guess('?' + '!:!'.repeat(18) + ' x'); R.ms = Date.now() - t;""")
        self.assertEqual(out["R"]["got"], ["start" if reference.search(t) else "filter" for t in texts])
        self.assertEqual(out["R"]["long"], "filter")
        self.assertLess(out["R"]["ms"], 500)

    def test_the_field_hint_says_what_enter_does_in_each_reading(self):
        # The hint said "Enter commits" while Start is off and Enter starts nothing.
        page = (V2 / "research.html").read_text(encoding="utf-8")
        self.assertNotIn("Enter commits", page)
        out = self.run_page("""const input = ELS.shift; const fire = (t, e) => (input.listeners[t] || []).forEach(f => f(e));
input.value = 'zzz'; fire('input'); R.filter = ELS['shift-hint'].html;
input.selectionStart = input.value.length; fire('keydown', { key: 'ArrowRight', preventDefault() {}, stopPropagation() {} }); await flush();
R.start = ELS['shift-hint'].html;""")
        self.assertIn("Enter keeps the filter", out["R"]["filter"])
        self.assertIn("Enter starts nothing: Start is off", out["R"]["start"])

    def test_the_copied_start_lines_queue_the_item_they_create(self):
        # The copied sd run line read ITEM, which nothing set: unset it stopped after the task was made, and a stale ITEM queued
        # another task. Run the copied text in sh against a stand-in sd: the run must get the id the add printed.
        out = self.run_page("""const input = ELS.shift; input.value = 'how do vendors detect spans? repo:beta';
(input.listeners.input || []).forEach(f => f()); (input.listeners.input || []).forEach(f => f());
R.cmd = C.get('draft:research').cmd;""")
        with tempfile.TemporaryDirectory() as bin_dir:
            log = Path(bin_dir) / "runs"
            stand_in = Path(bin_dir) / "sd"
            stand_in.write_text(f"""#!/bin/sh
if [ "$1 $2" = "task add" ]; then [ -n "$SD_ADD_FAILS" ] && exit 1; echo '{{"item": {{"id": 4242, "title": "x"}}}}'; exit 0; fi
echo "$*" >> {shq_path(log)}
""", encoding="utf-8")
            stand_in.chmod(0o755)
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", ITEM="7")
            ran = subprocess.run(["sh", "-c", out["R"]["cmd"]], env=env, capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").split()[-1], "4242")
            log.unlink()
            failed = subprocess.run(["sh", "-c", out["R"]["cmd"]], env=dict(env, SD_ADD_FAILS="1"), capture_output=True, text=True,
                                    timeout=30, check=False)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(log.exists(), "sd run ran after sd task add failed")

    def test_the_board_ranks_the_rows_and_shows_rounds_as_not_recorded_with_the_reason(self):
        out = self.run_page("R.rows = ELS.rows.html; R.tally = ELS.tally.html; R.sum = ELS.sum.html;")
        rows = out["R"]["rows"]
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', rows), ["research/beta", "group/gamma", "research/alpha"])
        self.assertEqual(rows.count("not recorded"), 3)
        self.assertIn(f'title="{research_screen.ROUNDS_REASON}"', rows)
        self.assertIn("~/repos/group/gamma", rows)
        self.assertIn("1 render-stale", out["R"]["tally"])
        self.assertIn("3 projects", out["R"]["sum"])
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 1, "what": "render-stale projects"})
        self.assertEqual(out["states"][0]["kind"], "loading")
        last = out["states"][-1]
        self.assertEqual(last["kind"], "partial", "the page did not say rounds and claims are not read")
        self.assertIn(research_screen.CLAIMS_REASON, last["text"])

    def test_the_first_row_is_selected_and_its_ledger_is_read_and_drawn(self):
        out = self.run_page("R.reader = ELS.reader.html; R.claims = ELS.claims.html; R.det = ELS.details.html; R.tally = ELS['reader-tally'].html;")
        self.assertEqual(out["gets"], ["/api/research", "/api/research/research/beta"])
        reader = out["R"]["reader"]
        self.assertIn("SOURCES.md", reader)
        self.assertIn("Spec release — bold code", reader)
        self.assertIn('href="https://example.test/spec"', reader)
        self.assertEqual(re.findall(r'<li class="grp label">([^<]+)</li>', reader), ["Primary", "Later"])
        self.assertIn("A 1", out["R"]["tally"])
        self.assertIn(f"Claims not extracted for Beta: {research_screen.CLAIMS_REASON}.", out["R"]["claims"])
        self.assertIn("Beta", out["R"]["det"])
        self.assertIn("Brief · stale · 30-brief/BRIEF.md", out["R"]["det"])

    def test_the_row_in_the_address_wins_and_a_missing_ledger_says_why(self):
        out = self.run_page("R.reader = ELS.reader.html; R.det = ELS.details.html;", row="'group/gamma'")
        self.assertEqual(out["gets"], ["/api/research", "/api/research/group/gamma"])
        self.assertIn("The ledger was not read: No research checkout at this address.", out["R"]["reader"])
        self.assertIn("could not read research.conf.py: “computed data”", out["R"]["det"])

    def test_a_checkout_named_like_an_object_property_reads_its_own_ledger(self):
        # The ledger cache was a plain object: "constructor" looked cached, so its ledger was never read.
        doc = research_screen.document(now=NOW, backend=lambda *_: {"root": "~/repos", "projects": [
            project("constructor", "Constructor", {"s": "ok", "txt": "1 of 1 fresh", "sub": ""})]})
        ledger = dict(self.ledger, key="constructor")
        answer = (f"(path) => path === '/api/research' ? [200, {json.dumps(doc)}]"
                  f" : path === '/api/research/constructor' ? [200, {json.dumps(ledger)}] : [404, {{ error: 'none' }}]")
        out = self.run_page("R.reader = ELS.reader.html; R.tally = ELS['reader-tally'].html;", answer=answer)
        self.assertEqual(out["gets"], ["/api/research", "/api/research/constructor"])
        self.assertIn("Spec release — bold code", out["R"]["reader"])
        self.assertIn("A 1", out["R"]["tally"])

    def test_a_state_named_like_an_object_property_filters_nothing(self):
        # ?state=constructor passed the plain-object check, so the board showed no row.
        out = self.run_page("R.rows = ELS.rows.html;", search="?state=constructor")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["research/beta", "group/gamma", "research/alpha"])

    def test_a_late_ledger_answer_never_draws_over_the_newer_selection(self):
        # Alpha's read is still out when beta is selected again; alpha's answer must not take the reader or the details.
        out = self.run_page("""PAGE_LIST.select('group/gamma'); PAGE_LIST.select('research/beta'); await flush();
R.reader = ELS.reader.html; R.tally = ELS['reader-tally'].html; R.det = ELS.details.html; R.selected = selected;""")
        self.assertEqual(out["gets"], ["/api/research", "/api/research/research/beta", "/api/research/group/gamma"])
        self.assertEqual(out["R"]["selected"], "research/beta")
        self.assertIn("Spec release — bold code", out["R"]["reader"])
        self.assertNotIn("not read", out["R"]["reader"] + out["R"]["tally"])
        self.assertIn("Beta", out["R"]["det"])

    def test_a_cut_board_says_what_it_did_not_list(self):
        # Past its print bound the board lists fewer documents and projects; the page says so instead of showing a short list as whole.
        board = {"root": self.doc["root"], "projects": self.doc["projects"], "cut": 2}
        self.doc = research_screen.document(now=NOW, backend=lambda *_: board)
        self.assertEqual(self.doc["cut"], 2)
        beta = next(p for p in self.doc["projects"] if p["key"] == "research/beta")
        beta["docs_total"] = 40
        out = self.run_page("R.det = ELS.details.html; R.sum = ELS.sum.html;")
        self.assertIn("40 documents in research.conf.py · 1 listed: the board passed its print bound", out["R"]["det"])
        self.assertIn("2 projects not listed", out["R"]["sum"])
        self.assertIn("2 projects not listed: the board passed its print bound", out["states"][-1]["text"])

    def test_a_source_opens_its_details_and_no_claim_is_claimed(self):
        out = self.run_page("""(ELS.reader.listeners.click || []).forEach(f => f({ target: { closest: s => s === '.src' ? { dataset: { i: '1' } } : null } }));
R.det = ELS.details.html;""")
        det = out["R"]["det"]
        self.assertIn("Vendor note", det)
        self.assertIn("Secondary | vendor", det)
        self.assertIn(f"Not known: {research_screen.CLAIMS_REASON}.", det)

    def test_a_failed_read_and_an_empty_board_each_say_so(self):
        out = self.run_page("", answer="() => [500, { error: 'boom' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("boom", out["states"][-1]["text"])
        failed = research_screen.document(now=NOW, backend=lambda *_: (_ for _ in ()).throw(ValueError("over budget")))
        out = self.run_page("R.rows = ELS.rows.html;", answer=f"() => [200, {json.dumps(failed)}]")
        self.assertEqual((out["states"][-1]["kind"], out["R"]["rows"]), ("error", ""))
        self.assertIn("over budget", out["states"][-1]["text"])
        empty = research_screen.document(now=NOW, backend=lambda *_: {"root": "~/repos", "projects": []})
        out = self.run_page("", answer=f"() => [200, {json.dumps(empty)}]")
        self.assertEqual((out["states"][-1]["kind"], out["states"][-1]["title"]), ("empty", "No research checkouts"))
        self.assertEqual(out["gets"], ["/api/research"])

    def test_the_page_reads_its_board_through_the_shared_reader(self):
        # read.js holds the generation, retirement and failure rules (sd:2491); the ledger keeps its own per-project cache.
        self.assertIn("shell.read(", RESEARCH_JS)
        self.assertNotIn("getJSON('/api/research')", RESEARCH_JS)
        out = self.run_page("")
        self.assertEqual(out["states"][0]["text"], "Reading the research checkouts. Rows appear when /api/research answers.")

    def test_a_project_the_next_read_drops_runs_no_command_and_loses_the_selection(self):
        out = self.run_page("""const first = ANSWER('/api/research')[1], A = ANSWER;
ANSWER = path => path === '/api/research' ? [200, { ...first, projects: first.projects.filter(p => p.key !== 'research/beta') }] : A(path);
await load(); await flush();
R.type = C.get('research/beta').type; R.rows = ELS.rows.html; R.selected = selected; R.det = ELS.details.html;""")
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["group/gamma", "research/alpha"])
        self.assertEqual(out["R"]["selected"], "group/gamma")
        self.assertIn("Gamma", out["R"]["det"])
        self.assertEqual(out["attention"][-1], {"state": "ok", "n": 0, "what": "render-stale projects"})

    def test_an_older_read_that_answers_last_changes_nothing(self):
        out = self.run_page("""const first = ANSWER('/api/research')[1], A = ANSWER, held = [];
ANSWER = path => path === '/api/research' ? new Promise(ok => held.push(ok)) : A(path);
const older = load(), newer = load();
held[1]([200, { ...first, projects: first.projects.filter(p => p.key === 'research/alpha') }]); await newer;
held[0]([200, first]); await older; await flush();
R.rows = ELS.rows.html; R.type = C.get('research/beta').type;""")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["research/alpha"])
        self.assertEqual(out["R"]["type"], "not listed")

    def test_a_failed_read_after_a_good_one_clears_the_board_the_ledger_and_the_rail(self):
        out = self.run_page("""ANSWER = () => [500, { error: 'boom' }]; await load(); await flush();
R.rows = ELS.rows.html; R.type = C.get('research/beta').type; R.reader = ELS.reader.html; R.det = ELS.details.html;""")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), [])
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertIn("No project is selected", out["R"]["reader"])
        self.assertIn("were not read", out["R"]["det"])
        self.assertEqual(out["attention"][-1]["state"], "unknown")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("boom", out["states"][-1]["text"])

    def test_a_ledger_error_that_ends_its_sentence_gets_no_second_period(self):
        # sd:2429: 'No research checkout at this address..'
        out = self.run_page("R.reader = ELS.reader.html;", row="'group/gamma'")
        self.assertIn("No research checkout at this address. Reselect", out["R"]["reader"])
        self.assertNotIn("..", out["R"]["reader"])

    def test_the_script_adds_no_sink_no_inline_style_no_sample_data_and_no_own_list_keys(self):
        self.assertNotIn("innerHTML", RESEARCH_JS)
        self.assertNotIn("setAttribute('style'", RESEARCH_JS)
        self.assertNotRegex(RESEARCH_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", RESEARCH_JS)
        self.assertNotRegex(RESEARCH_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", RESEARCH_JS)
        # The design's sample checkouts and ledgers name the operator's repositories; none ships, and no path is written in.
        self.assertNotRegex(RESEARCH_JS, r"RESEARCH_SOURCES|PROJECTS = \[\s*\{|~/repos/")


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "research", "Research", "/research", ("/api/research", "/api/research/example")


if __name__ == "__main__":
    unittest.main()
