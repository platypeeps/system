"""The v2 Research page (sd:2122).

What this slice promises: `/research` answers under the shared policy and loads
its script before `shell.js`; `/api/research` is the board `collect_research`
reads (every checkout with a `research.conf.py`, one group deep), with each
checkout's stage and render freshness, read in a child under a budget;
`/api/research/<checkout>` is that checkout's source ledger and claim map, and
any other path is a 404. Nothing reads review rounds, and the document says so. A
research page built into `docs/dashboard/`, where `sd-research-kit` renders,
counts as built.

The page shows the board, the ledger and the claim map in three views, and the reasons; registers the design's
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
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COLLECTORS = Path(research_screen.__file__).resolve().parents[1] / "collectors.py"

#: The design's Research commands (design source products/system/designs/pages/research.js): id, object type, label, key, risk.
#: Start was risk undo in the design; with no run to take back it asks first (build:).
COMMANDS = [
    ["research.render", "research project", "Render", "r", "safe"],
    ["research.review", "research project", "Review", "v", "safe"],
    ["research.start", "research request", "Run", "r", "confirm"],
    ["experiment.create", "research page", "Draft experiment", None, "safe"],
    ["experiment.refresh", "research project", "Refresh experiments", None, "safe"],
    ["experiment.attach", "research project", "Attach experiment", None, "safe"],
    ["experiment.status", "research project", "Draft status update", None, "safe"],
    ["experiment.deck", "research project", "Draft deck", None, "safe"],
    ["experiment.apply", "research project", "Apply draft", None, "safe"],
    ["research.deck", "research experiment", "Build deck", None, "safe"],
    ["research.slides", "research experiment", "Import to Slides", None, "safe"],
    ["source.adversarial-review", "research source", "Adversarial review", None, "safe"],
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

#: A graded ledger and the documents that cite it (sd:5064 ruling): claims are sentences in 20-map, 30-brief and 40-docs
#: that link a ledger row's URL, matched without a fragment or a trailing slash.
GRADED = """# Sources

| Grade | Source | Used for |
|---|---|---|
| A | [Spec release](https://example.test/spec/) | the release date |
| B | [Vendor note](https://example.test/note) | the vendor view |
| C | [A summary](https://example.test/summary#top) | summary only |
| U | [Rumour](https://example.test/rumour) | not checked |
| S9 | [Ungraded](https://example.test/ungraded) | no grade |
| A | Unlinked primary | nothing to match |
"""
BRIEF = """# Brief

The release shipped in March ([spec](https://example.test/spec#s2)). A summary says it slipped
([summary](https://example.test/summary/)). A blog disagreed ([blog](https://example.test/blog)).
The vendor confirmed it ([Note. Vendor](https://example.test/note)) in May.

- Both readings stand ([note](https://example.test/note), [rumour](https://example.test/rumour)).
- Only a rumour backs this ([rumour](https://example.test/rumour)).

```text
A fenced link is not a claim ([spec](https://example.test/spec)).
```

| Row | [Spec](https://example.test/spec) | **used** |
"""
MAP = "An ungraded row backs nothing ([u](https://example.test/ungraded)). Also see <https://example.test/note>.\n"

#: A ledger with IDs and no grades, as several real ledgers keep it: documents cite its rows by ID or by a deeper link.
IDS = """| # | Source | Read |
|---|---|---|
| S1 | [Repo](https://example.test/org/repo) | a pin |
| S2 | [Org](https://example.test/org/) | the org |
| S12 | Notes kept by hand | a date |
| S3-old | Old pin | a date |
| LOG-21 | [Ticket](https://example.test/browse/LOG-21) | open |
"""
CITES = """The repo pins it ([file](https://example.test/org/repo/blob/main/x.rs#L3)). S12's notes agree. Section S1.2 is
not a citation. S120 is not an ID. The old pin S3-old stands. A sibling path falls to the org
([r](https://example.test/org/repository)). [LOG-21](https://example.test/browse/LOG-21) is open.
"""


#: A ledger's own grade legend, as the research checkouts write it above their tables (sd:5297): one line per grade, or
#: several on one line, a note on the line after, and a table cell in bold that is not a definition.
LEGEND = """# Sources

Grades: **A** = primary source, read directly (spec, source code).
**B** = secondary, read directly ([blog](https://example.test/b)). **C** = search-result summary only, full page not read.
**U** = unverified: fetch failed; lead only.
**M** = measured in this repo, reproducible from a named tool.
Graded 2026-10-18: a note on the next line, not part of M.

| Grade | Source |
|---|---|
| **A** = not a definition | [Spec](https://example.test/other) |
"""


def claimed(root: Path, key: str = "research/claims") -> Path:
    repo = checkout(root, key)
    for name, text in (("10-sources/registry.md", GRADED), ("30-brief/BRIEF.md", BRIEF), ("20-map/MAP.md", MAP)):
        (repo / name).parent.mkdir(exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    return repo


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

    def test_a_claim_is_a_sentence_that_links_a_ledger_row_and_its_grades_decide_it(self):
        # sd:5064: the claim map read nothing; the board said "no claim reader" for every project.
        found = research_screen.registry(claimed(self.root))
        got = [(c["doc"], c["line"], c["s"], c["text"], c["keys"]) for c in found["claims"]]
        spec, note, summary, rumour, ungraded = (f"https://example.test/{n}" for n in ("spec", "note", "summary", "rumour", "ungraded"))
        self.assertEqual(got, [
            ("20-map/MAP.md", 1, "unverified", "An ungraded row backs nothing (u).", [ungraded]),
            ("20-map/MAP.md", 1, "supported", "Also see https://example.test/note.", [note]),
            ("30-brief/BRIEF.md", 3, "supported", "The release shipped in March (spec).", [spec]),
            ("30-brief/BRIEF.md", 3, "unverified", "A summary says it slipped (summary).", [summary]),
            ("30-brief/BRIEF.md", 5, "supported", "The vendor confirmed it (Note. Vendor) in May.", [note]),
            ("30-brief/BRIEF.md", 7, "supported", "Both readings stand (note, rumour).", [note, rumour]),
            ("30-brief/BRIEF.md", 8, "unverified", "Only a rumour backs this (rumour).", [rumour]),
            ("30-brief/BRIEF.md", 14, "supported", "Row · Spec · used", [spec]),
        ])
        self.assertEqual((found["tally"], found["claims_total"], found["docs_read"], found["unread"], found["graded"]),
                         ({"supported": 5, "unverified": 3, "ungraded": 1}, 8, 2, [], 5))
        self.assertEqual([c["grades"] for c in found["claims"]], ["", "B", "A", "C", "B", "BU", "U", "A"])
        cited = {r["title"]: (r["key"], r["cited"]) for r in found["rows"]}
        self.assertEqual(cited, {"Spec release": (spec, 2), "Vendor note": (note, 3), "A summary": (summary, 1),
                                 "Rumour": (rumour, 2), "Ungraded": (ungraded, 1), "Unlinked primary": ("", 0)})

    def test_a_ledger_names_its_own_grades_and_the_claim_types_say_how_each_is_graded(self):
        # sd:5297: the claim map gave no reason for a type; its rule sat only in the legend's copy, and the grades in none.
        repo = claimed(self.root)
        (repo / "SOURCES.md").write_text(LEGEND, encoding="utf-8")
        found = research_screen.registry(repo)
        self.assertEqual(found["grades"], {
            "A": "primary source, read directly (spec, source code).", "B": "secondary, read directly (blog).",
            "C": "search-result summary only, full page not read.", "U": "unverified: fetch failed; lead only.",
            "M": "measured in this repo, reproducible from a named tool."})
        claims = research_screen.sources("research/claims", now=NOW, backend=lambda *_: found)["claims"]
        self.assertEqual(claims["grades"], found["grades"])
        self.assertEqual(claims["types"], {"supported": {"rule": "cites an A, B or M source", "grades": "ABM"},
                                           "unverified": {"rule": "cites only C, U or ungraded sources", "grades": "CU"}})
        # A ledger that defines no grades sends none, and a table cell in bold is never read as a definition.
        self.assertEqual(research_screen.registry(claimed(self.root, "research/bare"))["grades"], {})

    def test_a_claim_cites_a_row_by_its_id_or_a_deeper_link_and_an_ungraded_ledger_leaves_it_unverified(self):
        # sd:5064 follow-up: real ledgers cite S12 or LOG-21 by ID, and link deeper paths than their rows; none matched.
        repo = checkout(self.root, "research/ids")
        for name, text in (("10-sources/registry.md", IDS), ("30-brief/BRIEF.md", CITES)):
            (repo / name).parent.mkdir(exist_ok=True)
            (repo / name).write_text(text, encoding="utf-8")
        found = research_screen.registry(repo)
        got = [(c["line"], c["text"][:22], c["keys"], c["s"], c["grades"]) for c in found["claims"]]
        self.assertEqual(got, [
            (1, "The repo pins it (file", ["https://example.test/org/repo"], "unverified", ""),
            (1, "S12's notes agree.", ["id:S12"], "unverified", ""),
            (2, "The old pin S3-old sta", ["id:S3-old"], "unverified", ""),
            (2, "A sibling path falls t", ["https://example.test/org"], "unverified", ""),
            (3, "LOG-21 is open.", ["https://example.test/browse/LOG-21"], "unverified", ""),
        ])
        self.assertEqual((found["tally"], found["graded"]), ({"supported": 0, "unverified": 5, "ungraded": 5}, 0))
        self.assertEqual({r["id"]: (r["key"], r["cited"]) for r in found["rows"]}, {
            "S1": ("https://example.test/org/repo", 1), "S2": ("https://example.test/org", 1), "S12": ("id:S12", 1),
            "S3-old": ("id:S3-old", 1), "LOG-21": ("https://example.test/browse/LOG-21", 1)})

    def test_an_id_ledger_carries_its_grade_in_a_grade_column(self):
        # sd:5064 grades: an ID ledger keeps its ID first, so the grade sits in a column headed Grade.
        repo = checkout(self.root, "research/idgrades")
        ledger = ("# Ledger\n\n## Sources\n\n| ID | Grade | Source | Scope |\n|---|---|---|---|\n"
                  "| S1 | A | [Repo](https://example.test/org/repo) | code |\n"
                  "| S2 | C | [Post](https://example.test/post) | summary |\n")
        for name, text in (("10-sources/registry.md", ledger), ("30-brief/BRIEF.md", "S1 holds it. S2 says so.\n")):
            (repo / name).parent.mkdir(exist_ok=True)
            (repo / name).write_text(text, encoding="utf-8")
        found = research_screen.registry(repo)
        self.assertEqual([(c["text"], c["s"], c["grades"]) for c in found["claims"]],
                         [("S1 holds it.", "supported", "A"), ("S2 says so.", "unverified", "C")])
        self.assertEqual(found["graded"], 2)
        self.assertEqual([r.get("grade") for r in found["rows"]], ["A", "C"])  # the page tallies and colours by it

    def test_a_document_the_claim_reader_skips_is_named_never_an_empty_success(self):
        repo = claimed(self.root)
        outside = self.root / "outside.md"
        outside.write_text(BRIEF, encoding="utf-8")
        (repo / "30-brief" / "linked.md").symlink_to(outside)
        (repo / "40-docs").mkdir()
        (repo / "40-docs" / "big.md").write_text("x" * 4000, encoding="utf-8")
        with mock.patch.object(research_screen, "CLAIM_BYTES", 2000):
            found = research_screen.registry(repo)
        self.assertEqual(found["unread"], [
            {"doc": "30-brief/linked.md", "error": "a link, which the reader does not follow"},
            {"doc": "40-docs/big.md", "error": "larger than the 2 MB the reader reads"}])
        self.assertEqual((found["docs_read"], found["claims_total"]), (2, 8))
        with mock.patch.object(research_screen, "CLAIM_FILES", 1):
            found = research_screen.registry(repo)
        self.assertEqual(found["unread"], [{"doc": "3 more documents", "error": "past the 1-document bound"}])
        self.assertEqual(found["claims_total"], 2)
        with mock.patch.object(research_screen, "UNREAD", 2), mock.patch.object(research_screen, "CLAIM_BYTES", 10):
            found = research_screen.registry(repo)
        self.assertEqual(found["unread"], [{"doc": "20-map/MAP.md", "error": "larger than the 2 MB the reader reads"},
                                           {"doc": "3 more documents", "error": "skipped as well; the list stops here"}])
        found = research_screen.registry(repo, time_left=lambda: 0.0)
        self.assertEqual((found["unread"], found["claims"]),
                         ([{"doc": "4 more documents", "error": "the reading ran out of time"}], []))

    def test_the_claim_map_and_ledger_together_stay_under_the_print_bound_and_keep_cited_rows(self):
        repo = claimed(self.root)
        ledger = GRADED + "".join(f"| B | [Filler {n}](https://example.test/f{n}) | {'é' * 150} |\n" for n in range(80))
        (repo / "10-sources" / "registry.md").write_text(ledger, encoding="utf-8")
        brief = "".join(f"Claim {n} {'é' * 200} ([n](https://example.test/note)).\n\n" for n in range(300))
        (repo / "40-docs").mkdir()
        (repo / "40-docs" / "long.md").write_text(brief, encoding="utf-8")
        found = research_screen.registry(repo)
        self.assertLessEqual(len(json.dumps(found, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
                             research_screen.PRINT_BYTES)
        self.assertEqual((found["total"], found["claims_total"]), (86, 308))
        self.assertLess(len(found["claims"]), research_screen.CLAIMS)
        self.assertTrue(found["claims"] and found["rows"])
        # Every cited row is kept ahead of the filler, in ledger order.
        self.assertEqual([r["title"] for r in found["rows"][:5]], ["Spec release", "Vendor note", "A summary", "Rumour", "Ungraded"])

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
        self.assertNotIn("claims", doc)  # the claim map is per project, in the ledger document (sd:5064)
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

    def test_the_ledger_document_carries_the_claim_map_through_the_child(self):
        claimed(self.root, "research/alpha")
        found = research_screen.sources("research/alpha", now=NOW)
        self.assertEqual(found["error"], "")
        claims = found["claims"]
        self.assertEqual((claims["total"], claims["tally"], claims["read"], claims["unread"], claims["dirs"]),
                         (8, {"supported": 5, "unverified": 3, "ungraded": 1}, 2, [], ["20-map", "30-brief", "40-docs"]))
        self.assertEqual(claims["graded"], 5)
        self.assertEqual(claims["rows"][2]["text"], "The release shipped in March (spec).")
        self.assertEqual({r["title"]: r["cited"] for r in found["rows"]}["Vendor note"], 3)
        # A child that sends no claim map gives None, which the page reads as unknown, never as "no claims".
        bare = research_screen.sources("research/alpha", now=NOW, backend=lambda *_: {"found": True, "rows": [], "total": 0})
        self.assertIsNone(bare["claims"])

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
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "commands.js", "research.js",
                                   "shell.js"])
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


# The stand-in additions research.js needs beyond test_v2_tasks: the shell's row, url, setContext, its view switch (the views
# shown and the counts put on them) and a record of fetched paths.
SHELL_MORE = r"""
window.shell.row = () => ROW; window.shell.setContext = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
OUT.views = []; OUT.counts = {}; function clearTimeout() {}
window.shell.view = { show: id => OUT.views.push(id), count: (id, n) => { OUT.counts[id] = n; }, current: () => null };
var ROW = null;
"""


def ledger_reads(out):
    """The page's reads less each selected project's experiment link (sd:5296) and the last deck run (pack sd:5525),
    which `TheScript` tests on their own."""
    return [path for path in out["gets"] if not path.startswith("/api/research/experiments?") and path not in ("/api/research/deck/status", "/api/research/slides/status")]


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
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        repo = checkout(root, "research/beta", registry=REGISTRY)
        (repo / "30-brief").mkdir()
        (repo / "30-brief" / "BRIEF.md").write_text("The spec shipped ([spec](https://example.test/spec)).\n\n"
                                                    "The note agrees ([note](https://example.test/note/)).\n", encoding="utf-8")
        self.ledger = research_screen.sources("research/beta", now=NOW, backend=lambda *_: research_screen.registry(repo))
        self.ledger["files"][0].update(mtime="2026-09-01T10:00Z", prov="Compiled.")

    def run_page(self, body, answer=None, search="", row="null"):
        answer = answer or (f"(path) => path === '/api/research' ? [200, {json.dumps(self.doc)}]"
                            f" : path === '/api/research/research/beta' ? [200, {json.dumps(self.ledger)}] : [404, {{ error: 'No research checkout at this address.' }}]")
        script = (STAND_IN + f"location.search = {json.dumps(search)};\n" + MARKUP_JS + "\nconst mk = window.markup.html;\n"
                  + SHELL + SHELL_MORE + "\n" + READ_SHELL + f"\nROW = {row};\nANSWER = {answer};\n" + COMMANDS_JS + RESEARCH_JS
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

    def test_a_header_sorts_the_board_and_the_url_keeps_it(self):
        # sd:3455: the shell's sort header. State is the default; its bands draw only in that order.
        head = "ELS['rows-head'].listeners.click[0]({ target: { closest: s => s === 'button[data-sort]' ? { dataset: { sort: 'what' } } : null } });"
        ids = r'<tr data-id="([^"]+)"'
        out = self.run_page(f"R.head = ELS['rows-head'].html; R.rows = ELS.rows.html; {head} R.up = ELS.rows.html; R.head2 = ELS['rows-head'].html;"
                            f" {head} R.down = ELS.rows.html; R.url = String(OUT.urls[OUT.urls.length - 1]);")
        self.assertEqual(re.findall(r'data-sort="(\w+)"', out["R"]["head"]), ["s", "what", "stage", "render", "last"])
        self.assertEqual(re.findall(r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"', out["R"]["head"]), [("ascending", "s")])
        self.assertEqual(re.findall(ids, out["R"]["rows"]), ["research/beta", "group/gamma", "research/alpha"])
        self.assertIn("band-start", out["R"]["rows"])
        self.assertEqual(re.findall(r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"', out["R"]["head2"]), [("ascending", "what")])
        self.assertEqual(re.findall(ids, out["R"]["up"]), ["research/alpha", "research/beta", "group/gamma"])
        self.assertNotIn("band-start", out["R"]["up"])
        self.assertEqual(re.findall(ids, out["R"]["down"]), ["group/gamma", "research/beta", "research/alpha"])
        self.assertIn("sort=what", out["R"]["url"])
        self.assertIn("dir=desc", out["R"]["url"])
        out = self.run_page("R.rows = ELS.rows.html;", search="?sort=what&dir=desc")
        self.assertEqual(re.findall(ids, out["R"]["rows"]), ["group/gamma", "research/beta", "research/alpha"])

    def test_render_runs_in_the_checkout_and_review_is_copy_only(self):
        # sd:5632: Render posts the render run; Review stays a line to copy.
        out = self.run_page("""const beta = C.get('research/beta');
R.lines = ['research.render', 'research.review'].map(id => [cmd(id).executes, cmd(id).cli(beta), cmd(id).primary ? cmd(id).primary(beta) : null]);
['research.render', 'research.review'].forEach(id => shellRun(cmd(id), beta)); await flush();""")
        self.assertEqual(out["R"]["lines"], [[True, "cd ~/'repos/research/beta' && sd-research-kit render", True],
                                             [False, "cd ~/'repos/research/beta' && sd-research-kit review", None]])
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/research/render", {"repo": "research/beta"}]])
        self.assertIn("Copy it into a terminal: the dashboard does not run sd-research-kit review", [t[0] for t in out["toasts"]])

    def test_start_research_is_off_with_its_reason_shows_the_line_and_sends_nothing(self):
        out = self.run_page("""const input = ELS.shift;
input.value = 'how do vendors detect spans? repo:beta deep'; (input.listeners.input || []).forEach(f => f());
(input.listeners.input || []).forEach(f => f());
R.draft = Object.assign({}, C.get('draft:research')); R.when = cmd('research.start').when(C.get('draft:research'));
R.executes = cmd('research.start').executes; R.req = ELS.req.html;
(input.listeners.keydown || []).forEach(f => f({ key: 'Enter', preventDefault() {}, stopPropagation() {} })); await flush();""")
        draft = out["R"]["draft"]
        self.assertEqual((draft["q"], draft["repo"], draft["depth"]), ("how do vendors detect spans?", "research/beta", "deep"))
        self.assertEqual(draft["cmd"], "sd task add 'Research: how do vendors detect spans?' --body 'repo=beta depth=deep skill=sd-research-repo stage=draft'")
        # The copied line runs as it is: no bare <item>, which the shell reads as a redirect from a file named item.
        self.assertNotRegex(draft["cmd"], r"(?<![\"'])<\w")
        self.assertIn("does not create items", out["R"]["when"])
        self.assertFalse(out["R"]["executes"])
        self.assertIn('data-copy="sd task add', out["R"]["req"])
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
        self.assertIn("Creates an item in beta", out["R"]["req"])

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
        self.assertEqual(last["kind"], "partial", "the page did not say rounds are not read")
        self.assertIn(research_screen.ROUNDS_REASON, last["text"])
        self.assertNotIn("claim", last["text"])

    def test_the_first_row_is_selected_and_its_ledger_is_read_and_drawn(self):
        out = self.run_page("R.reader = ELS.reader.html; R.claims = ELS.claims.html; R.det = ELS.details.html; R.tally = ELS['reader-tally'].html;")
        self.assertEqual(ledger_reads(out), ["/api/research", "/api/research/research/beta"])
        self.assertIn("/api/research/experiments?project=research%2Fbeta", out["gets"])
        reader = out["R"]["reader"]
        self.assertIn("SOURCES.md", reader)
        self.assertIn("Spec release — bold code", reader)
        self.assertIn('href="https://example.test/spec"', reader)
        self.assertEqual(re.findall(r'<li class="grp label">([^<]+)</li>', reader), ["Primary", "Later"])
        self.assertIn("A 1", out["R"]["tally"])
        self.assertEqual(re.findall(r'<li class="claim" data-c="(\d+)"', out["R"]["claims"]), ["0", "1"])
        self.assertIn("The spec shipped (spec).", out["R"]["claims"])
        self.assertIn('data-cites="0"', out["R"]["reader"])
        self.assertIn("Beta", out["R"]["det"])
        self.assertIn("Brief · stale · 30-brief/BRIEF.md", out["R"]["det"])

    def test_the_row_in_the_address_wins_and_a_missing_ledger_says_why(self):
        out = self.run_page("R.reader = ELS.reader.html; R.det = ELS.details.html;", row="'group/gamma'")
        self.assertEqual(ledger_reads(out), ["/api/research", "/api/research/group/gamma"])
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
        self.assertEqual(ledger_reads(out), ["/api/research", "/api/research/constructor"])
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
        self.assertEqual(ledger_reads(out), ["/api/research", "/api/research/research/beta", "/api/research/group/gamma"])
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

    def test_a_source_opens_its_details_with_the_claims_that_cite_it(self):
        out = self.run_page("""const tap = i => (ELS.reader.listeners.click || []).forEach(f => f({ target: { closest: s => s === '.src' ? { dataset: { i } } : null } }));
tap('1'); R.det = ELS.details.html; tap('2'); R.unlinked = ELS.details.html;""")
        det = out["R"]["det"]
        self.assertIn("Vendor note", det)
        self.assertIn("Secondary | vendor", det)
        self.assertIn("The note agrees (note). · 30-brief/BRIEF.md:3", det)
        self.assertIn("No sentence in 20-map, 30-brief, 40-docs cites this source.", out["R"]["unlinked"])

    def reviewed(self):
        """Beta's ledger with two reviews of its Vendor note, newest first, as `adversarial.source_reviews` reads them."""
        self.ledger["reviews"] = {"https://example.test/note": [
            {"item": 91, "stem": "2026-10-18 - Adversarial review · source beta-note · 120000Z", "filed": "2026-10-18T12:00:00Z",
             "verdict": "Do the claims hold? NO. The vendor view is read as the spec."},
            {"item": 90, "stem": "2026-10-11 - Adversarial review · source beta-note · 090000Z", "filed": "2026-10-11T09:00:00Z",
             "verdict": "Do the claims hold? YES."}]}
        self.ledger["reviews_error"] = ""

    def test_a_reviewed_source_shows_its_latest_brief_and_details_list_the_older_ones(self):
        """sd:5101: the row shows the latest review's date, verdict and link; Details lists every review."""
        self.reviewed()
        out = self.run_page("""PAGE_LIST.select('research/beta'); await flush(); R.reader = ELS.reader.html;
const tap = i => (ELS.reader.listeners.click || []).forEach(f => f({ target: { closest: s => s === '.src' ? { dataset: { i } } : null } }));
tap('1'); R.det = ELS.details.html; tap('0'); R.none = ELS.details.html;""")
        self.assertIn('<p class="rev">Reviewed 2026-10-18 · Do the claims hold? NO. The vendor view is read as the spec. · '
                      '<a href="/item/91">brief sd:91</a> · 1 older in Details</p>', out["R"]["reader"])
        self.assertEqual(out["R"]["reader"].count('class="rev"'), 1)
        self.assertIn("<h3>Adversarial reviews</h3>", out["R"]["det"])
        self.assertIn('2026-10-11 · Do the claims hold? YES. · <a href="/item/90">brief sd:90</a>', out["R"]["det"])
        self.assertIn("None yet. Adversarial review files one; a re-run adds a brief and keeps the last.", out["R"]["none"])

    def test_adversarial_review_on_a_source_posts_its_checkout_and_key(self):
        self.reviewed()
        answer = (f"(path, body) => path === '/api/research' ? [200, {json.dumps(self.doc)}]"
                  f" : path === '/api/research/research/beta' ? [200, {json.dumps(self.ledger)}]"
                  " : path === '/api/adversarial' ? [202, { id: 's1', state: 'queued', item: null, filed: null }]"
                  " : path === '/api/adversarial/s1' ? [200, { id: 's1', state: 'done', item: null, filed: 'written', brief_item: 92 }]"
                  " : [404, { error: 'no' }]")
        # The stand-in's setTimeout runs at once, so the first poll answers settled; the queued draw is caught as it lands.
        out = self.run_page("""PAGE_LIST.select('research/beta'); await flush();
const tap = i => (ELS.reader.listeners.click || []).forEach(f => f({ target: { closest: s => s === '.src' ? { dataset: { i } } : null } }));
tap('1'); const o = C.get('source:research/beta https://example.test/note'); R.on = cmd('source.adversarial-review').when(o);
document.addEventListener('adversarial:run', e => { if (e.detail.run.state === 'queued') R.det = ELS.details.html; });
shellRun(cmd('source.adversarial-review'), o); await flush(); await flush(); R.end = ELS.details.html;""", answer=answer)
        self.assertEqual(out["R"]["on"], True)
        self.assertEqual(out["posts"], [["/api/adversarial", {"kind": "source", "target": "research/beta",
                                                              "source": "https://example.test/note"}, 64]])
        self.assertIn("Run s1 is queued", out["R"]["det"])
        # A filed review reads the ledger again, so the new brief lists beside the older ones.
        self.assertEqual(out["gets"].count("/api/research/research/beta"), 2)
        self.assertIn('<a href="/item/92">brief sd:92</a>', out["R"]["end"])

    def test_add_source_lists_what_add_writes_and_posts_pasted_urls_to_reading_by_default(self):
        # sd:5112: no project picked, so the target is research/reading; a bad line keeps Add off and says why.
        answer = (f"(path, body) => path === '/api/research' ? [200, {json.dumps(self.doc)}]"
                  " : path === '/api/research/intake' ? [200, { item: 77, branch: 'source-intake/20261018T210000Z',"
                  " added: [{ kind: 'url', title: 'A', key: 'https://example.test/a' }], enqueued: true }] : [404, { error: 'no' }]")
        out = self.run_page("""const urls = ELS['intake-urls'], typed = v => { urls.value = v; urls.listeners.input.forEach(f => f()); };
typed('https://example.test/a\\nnot-a-url'); R.bad = ELS['intake-list'].html; R.off = ELS['intake-add'].disabled;
R.to = ELS['intake-to'].value; R.targets = ELS['intake-to'].html;
typed('https://example.test/a'); R.on = ELS['intake-add'].disabled; R.label = ELS['intake-add'].textContent;
ELS.intake.listeners.submit.forEach(f => f({ preventDefault() {} })); await flush(); await flush();
R.said = ELS['intake-said'].html; R.left = urls.value;""", answer=answer)
        self.assertIn("not an http or https address", out["R"]["bad"])
        self.assertIn("a U row; its title is read on Add", out["R"]["bad"])
        self.assertTrue(out["R"]["off"])
        self.assertEqual(out["R"]["to"], "research/reading")
        self.assertIn('<option value="research/reading" selected>reading · no topic repo</option>', out["R"]["targets"])
        self.assertIn('<option value="research/beta">', out["R"]["targets"])
        self.assertEqual((out["R"]["on"], out["R"]["label"]), (False, "Add 1 source"))
        self.assertEqual(out["posts"], [["/api/research/intake", {"target": "research/reading",
                                                                  "urls": ["https://example.test/a"], "files": []}, 64]])
        self.assertIn('Queued: 1 row on source-intake/20261018T210000Z as <a href="/item/77">sd:77</a>.', out["R"]["said"])
        self.assertIn("once the lane merges it", out["R"]["said"])
        self.assertEqual(out["R"]["left"], "")

    def test_a_file_dropped_on_the_sources_panel_is_listed_with_where_it_goes(self):
        # sd:5112: the board's own first-row select leaves the target on reading; the viewer picks beta in the box.
        out = self.run_page("""PAGE_LIST.select('research/beta'); await flush(); R.auto = ELS['intake-to'].value;
ELS['intake-to'].value = 'research/beta'; ELS['intake-to'].listeners.change.forEach(f => f());
const files = [{ name: 'note.pdf', size: 2048 }, { name: 'scan.pdf', size: 6 * 1024 * 1024 }, { name: 'tool.exe', size: 10 }];
ELS['view-sources'].listeners.drop.forEach(f => f({ preventDefault() {}, dataTransfer: { files, getData: () => '' } }));
R.list = ELS['intake-list'].html; R.off = ELS['intake-add'].disabled; R.to = ELS['intake-to'].value;
ELS['intake-list'].listeners.click.forEach(f => f({ target: { closest: s => s === '[data-unfile]' ? { dataset: { unfile: '2' } } : null } }));
R.on = ELS['intake-add'].disabled; R.label = ELS['intake-add'].textContent;""")
        self.assertIn("note.pdf · 2 KB</span><span class=\"w\">committed in 10-sources/files", out["R"]["list"])
        self.assertIn("scan.pdf · 6.0 MB</span><span class=\"w\">over 5 MB: kept under the bulk storage root, linked", out["R"]["list"])
        self.assertIn("not added: PDF, Markdown, HTML or text only", out["R"]["list"])
        self.assertTrue(out["R"]["off"])
        self.assertEqual((out["R"]["auto"], out["R"]["to"]), ("research/reading", "research/beta"))
        self.assertEqual((out["R"]["on"], out["R"]["label"]), (False, "Add 2 sources"))

    def test_adversarial_review_on_a_source_is_off_when_its_briefs_cannot_be_read(self):
        self.ledger["reviews"], self.ledger["reviews_error"] = None, "~/repos/research/beta is not a registered repository"
        out = self.run_page("""PAGE_LIST.select('research/beta'); await flush();
const tap = i => (ELS.reader.listeners.click || []).forEach(f => f({ target: { closest: s => s === '.src' ? { dataset: { i } } : null } }));
tap('1'); R.why = cmd('source.adversarial-review').when(C.get('source:research/beta https://example.test/note')); R.det = ELS.details.html;""")
        self.assertEqual(out["R"]["why"], "briefs not read: ~/repos/research/beta is not a registered repository")
        self.assertIn("Not known: the reviews were not read (~/repos/research/beta is not a registered repository).", out["R"]["det"])

    def test_a_claim_lights_its_sources_and_a_source_count_lights_its_claims(self):
        # sd:5064: the claim map showed "no claim reader" for every project; the ledger carried no cited mark.
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        repo = claimed(root, "research/beta")
        (repo / "40-docs").mkdir()
        (repo / "40-docs" / "linked.md").symlink_to(repo / "20-map" / "MAP.md")
        self.ledger = research_screen.sources("research/beta", now=NOW, backend=lambda *_: research_screen.registry(repo))
        out = self.run_page("""R.claims = ELS.claims.html; R.for = ELS['claims-for'].textContent;
const claimTap = c => (ELS.claims.listeners.click || []).forEach(f => f({ target: { closest: s => s === '.claim[data-c]' ? { dataset: { c } } : s === 'button' ? {} : null } }));
claimTap('5'); R.lit = ELS.reader.html; R.det = ELS.details.html;
(ELS.reader.listeners.click || []).forEach(f => f({ target: { closest: s => s === '.src' ? { dataset: { i: '5' } } : null } })); R.unkeyed = ELS.details.html;
(ELS.reader.listeners.click || []).forEach(f => f({ target: { closest: s => s === '[data-cites]' ? { dataset: { cites: '1' } } : null } }));
R.litClaims = ELS.claims.html;
(ELS.claims.listeners.click || []).forEach(f => f({ target: { closest: s => s === '[data-verdict]' ? { dataset: { verdict: 'unverified' } } : null } }));
R.filtered = ELS.claims.html;""")
        claims = out["R"]["claims"]
        self.assertIn("● 5 supported", claims)
        self.assertIn("▨ 3 unverified", claims)
        self.assertNotRegex(claims, r"inferred|corrected")
        self.assertIn("30-brief/BRIEF.md:7", claims)
        self.assertIn("40-docs/linked.md not read: a link, which the reader does not follow.", claims)
        self.assertIn("8 claims", out["R"]["for"])
        # Claim 5 cites the B note and the U rumour: both rows are lit, nothing else.
        lit = re.findall(r'<li class="src" data-i="(\d+)"[^>]*data-lit', out["R"]["lit"])
        self.assertEqual(lit, ["1", "3"])
        self.assertIn("Claim · supported", out["R"]["det"])
        self.assertIn("cites an A, B or M source (grades BU)", out["R"]["det"])
        self.assertIn("1 of the unverified cite only ungraded sources", claims)
        self.assertIn("No link or ID in this ledger row, so no claim can cite it.", out["R"]["unkeyed"])
        # The note's count lights the three claims that cite it.
        self.assertEqual(re.findall(r'<li class="claim" data-c="(\d+)"[^>]*data-lit', out["R"]["litClaims"]), ["1", "4", "5"])
        self.assertEqual(re.findall(r'<li class="claim" data-c="(\d+)"[^>]*hidden', out["R"]["filtered"]), ["1", "2", "4", "5", "7"])

    def test_the_page_has_four_views_and_the_selected_project_carries_across_them(self):
        # sd:5064 operator request: Projects | Sources | Claim map tabs, through the shell's view switch.
        page = (V2 / "research.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'<section id="view-([a-z]+)"', page), ["projects", "sources", "claims", "experiments"])
        out = self.run_page("""R.declared = window.PAGE_VIEWS.map(v => [v.id, v.label]);
R.start = OUT.views.slice(); PAGE_LIST.select('research/alpha'); PAGE_LIST.select('research/beta'); await flush();
R.selecting = OUT.views.slice(); R.counts = Object.assign({}, OUT.counts); R.for = [ELS['reader-for'].textContent, ELS['claims-for'].textContent];
(ELS.claims.listeners.click || []).forEach(f => f({ target: { closest: s => s === '.claim[data-c]' ? { dataset: { c: '1' } } : s === 'button' ? {} : null } }));
R.afterClaim = OUT.views.slice(); R.lit = ELS.reader.html;
(ELS.reader.listeners.click || []).forEach(f => f({ target: { closest: s => s === '[data-cites]' ? { dataset: { cites: '0' } } : null } }));
R.afterSource = OUT.views.slice(); R.litClaims = ELS.claims.html;""")
        r = out["R"]
        self.assertEqual(r["declared"], [["projects", "Projects"], ["sources", "Sources"], ["claims", "Claim map"], ["experiments", "Experiments"]])
        # Selecting a project stays on Projects; the panels name it.
        self.assertEqual((r["start"], r["selecting"]), ([], []))
        self.assertEqual(r["for"], ["· Beta", "· Beta · 2 claims"])
        self.assertEqual(r["counts"], {"experiments": 0, "sources": 3, "claims": 2})
        self.assertEqual(r["afterClaim"], ["sources"])
        self.assertEqual(re.findall(r'<li class="src" data-i="(\d+)"[^>]*data-lit', r["lit"]), ["1"])
        self.assertEqual(r["afterSource"], ["sources", "claims"])
        self.assertEqual(re.findall(r'<li class="claim" data-c="(\d+)"[^>]*data-lit', r["litClaims"]), ["0"])

    def experiment_board(self):
        """Alpha keeps a stale brief with three asks and a deck.json, beta an unread one; gamma keeps none."""
        rows = self.doc["projects"]
        rows[0]["experiment"] = {"state": "Waiting on review", "tone": "caution", "updated": "2026-08-20", "stale": True,
                                 "line": "11 pull requests are open.", "error": "", "deck": True, "deck_url": "https://example.test/deck",
                                 "links": [{"text": "Alpha: execution", "url": "https://example.test/exp/2"}], "built": "",
                                 "asks": [{"who": "Owners", "what": "Review #1"}, {"who": "Docs", "what": "Review #2"}, {"who": "QA", "what": "Run it"}],
                                 "asks_total": 3}
        rows[1]["experiment"] = {"error": "30-brief/BRIEF-status.md exceeds the bounded read size.", "deck": False, "built": ""}
        return json.dumps(self.doc)

    def test_experiments_lists_each_status_brief_with_its_state_staleness_asks_and_links(self):
        """pack sd:5525: one row per brief; a stale Updated day and an unread brief each say so; each row has its action."""
        doc = self.experiment_board()
        answer = (f"(path, body) => path === '/api/research' ? [200, {doc}] : path === '/api/research/deck/status' ? [200, {{ run: null }}]"
                  " : [404, { error: 'No research checkout at this address.' }]")
        out = self.run_page("""window.shell.commands.rowActions = id => mk`<span data-for="${id}"></span>`; renderExperiments();
R.rows = ELS['xp-rows'].html; R.tally = ELS['xp-tally'].html;""", answer=answer)
        rows, r = re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), out["R"]
        self.assertEqual(rows, ["research/alpha", "research/beta"])
        self.assertIn('<td class="g g-caution" title="waiting">', r["rows"])
        self.assertIn("Waiting on review", r["rows"])
        self.assertIn("stale: over 7 days", r["rows"])
        self.assertIn("<b>Owners</b>: Review #1", r["rows"])
        self.assertIn("1 more in the brief", r["rows"])
        self.assertNotIn("Run it", r["rows"])
        self.assertIn('href="https://example.test/exp/2"', r["rows"])
        self.assertIn('href="https://example.test/deck"', r["rows"])
        self.assertIn("deck.json ●, no deck built yet", r["rows"])
        self.assertIn("The status brief was not read: 30-brief/BRIEF-status.md exceeds the bounded read size.</span>", r["rows"])
        self.assertEqual(re.findall(r'<td class="acts"><span data-for="([^"]+)">', r["rows"]), ["xp:research/alpha", "xp:research/beta"])
        # The tally counts briefs, stale ones and asks; there is no deck count and no run line above the ledger.
        self.assertEqual(re.findall(r"<span>([^<]+)</span>", r["tally"]), ["2 briefs", "1 stale", "3 asks"])
        self.assertNotIn("xp-run", (V2 / "research.html").read_text(encoding="utf-8"))
        self.assertEqual(out["counts"]["experiments"], 2)
        # The page's filter field reaches this ledger by the board's words.
        out = self.run_page("R.rows = ELS['xp-rows'].html;", answer=answer, search="?q=beta")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["research/beta"])

    def test_build_deck_posts_the_rows_checkout_follows_the_run_and_links_the_deck_it_wrote(self):
        """pack sd:5525: a row's Build deck queues `sd-research-kit deck` in its checkout, as Publish now queues a drain; once
        the run is done the board is read again and the row links the built deck."""
        doc = self.experiment_board()
        built = json.loads(doc)
        built["projects"][0]["experiment"]["built"] = NOW
        run = {"id": "deck-20260906T120000Z", "state": "queued", "reason": None, "repo": "research/alpha",
               "out": "/r/alpha/docs/dashboard/deck.pptx", "started": NOW, "finished": None}
        done = {**run, "state": "done", "finished": NOW}
        answer = (f"(path, body) => path === '/api/research' ? [200, (BOARDS = (typeof BOARDS === 'number' ? BOARDS : 0) + 1) > 1 ? {json.dumps(built)} : {doc}]"
                  f" : path === '/api/research/deck' ? [202, {json.dumps(run)}] : path === '/api/research/deck/status' ? [200, {{ run: null }}]"
                  f" : path === '/api/research/deck/{run['id']}' ? [200, {json.dumps(done)}] : [404, {{ error: 'No research checkout at this address.' }}]")
        out = self.run_page("""const o = C.get('xp:research/alpha'), c = cmd('research.deck');
R.when = c.when(o); R.primary = c.primary(o); R.cli = c.cli(o); R.icon = c.icon; shellRun(c, o); await flush(); await flush(); await flush();
R.rows = ELS['xp-rows'].html; R.after = c.when(C.get('xp:research/alpha'));""", answer=answer)
        r = out["R"]
        self.assertEqual((r["when"], r["primary"], r["icon"]), (True, True, "file-text"))
        self.assertEqual(r["cli"], "cd ~/'repos/research/alpha' && sd-research-kit deck")
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/research/deck", {"repo": "research/alpha"}]], out["toasts"])
        self.assertIn(f"/api/research/deck/{run['id']}", out["gets"])
        self.assertEqual(out["gets"].count("/api/research"), 2)
        self.assertIn('<a href="/research/deck/research/alpha" download>deck.pptx</a>', r["rows"])
        self.assertIn(["Deck built · research/alpha", False], out["toasts"])
        self.assertIs(r["after"], True)

    def test_build_deck_is_off_without_a_deck_json_and_while_a_run_builds(self):
        # The poll's first read fails, which is not known, not settled; the stand-in's timers fire at once, so the second answers.
        doc = self.experiment_board()
        running = {"id": "deck-20260906T120000Z", "state": "running", "repo": "research/alpha", "out": "/x.pptx", "started": NOW}
        answer = (f"(path, body) => path === '/api/research' ? [200, {doc}] : path === '/api/research/deck/status' ? [200, {{ run: {json.dumps(running)} }}]"
                  f" : path !== '/api/research/deck/{running['id']}' ? [404, {{ error: 'none' }}]"
                  f" : (POLLS = (typeof POLLS === 'number' ? POLLS : 0) + 1) < 2 ? [503, {{ error: 'unread' }}] : [200, {json.dumps({**running, 'state': 'failed', 'reason': 'the kit exited 1'})}]")
        out = self.run_page("""R.rows = ELS['xp-rows'].html; R.beta = cmd('research.deck').when(C.get('xp:research/beta'));
R.alpha = cmd('research.deck').when(C.get('xp:research/alpha'));""", answer=answer)
        self.assertIn("Last build failed: the kit exited 1", out["R"]["rows"])
        self.assertIn("no deck.json: Build deck is off", out["R"]["rows"])
        self.assertEqual(out["gets"].count("/api/research/deck/deck-20260906T120000Z"), 2)
        self.assertEqual(out["R"]["beta"], "this checkout keeps no 30-brief/deck.json")
        self.assertIs(out["R"]["alpha"], True)
        # While a run builds, every row's action is off and names it.
        answer = f"(path, body) => path === '/api/research' ? [200, {doc}] : [200, {{ run: null }}]"
        out = self.run_page(f"""setTimeout = () => 0; followBuild({{ ...{json.dumps(running)}, unread: 'unread' }}, false);
R.when = cmd('research.deck').when(C.get('xp:research/alpha')); R.rows = ELS['xp-rows'].html;""", answer=answer)
        self.assertEqual(out["R"]["when"], "run deck-20260906T120000Z is building research/alpha's deck")
        self.assertIn("Building: run deck-20260906T120000Z is running; its state was not read: unread", out["R"]["rows"])

    def test_import_to_slides_posts_the_rows_checkout_and_links_the_slides_file(self):
        """#25693 option A: a built deck goes to Slides through workspace-mcp; the row links the file and says when the deck
        changed since; the action is off with no built deck or while workspace-mcp does not answer."""
        board = json.loads(self.experiment_board())
        board["projects"][0]["experiment"]["built"] = NOW
        ready = {"ready": True, "why": "", "imports": {}}
        slides = {"id": "p-1", "link": "https://docs.google.com/presentation/d/p-1/edit", "built": NOW, "imported": NOW}
        answer = (f"(path, body) => path === '/api/research' ? [200, {json.dumps(board)}] : path === '/api/research/slides/status' ? [200, {json.dumps(ready)}]"
                  f" : path === '/api/research/slides' ? [200, {json.dumps({'slides': slides, 'reused': False, 'replaces': None})}] : [200, {{ run: null }}]")
        out = self.run_page("""const c = cmd('research.slides'), o = C.get('xp:research/alpha');
R.when = c.when(o); R.beta = c.when(C.get('xp:research/beta')); R.ask = c.askFirst; R.before = ELS['xp-rows'].html;
R.said = await c.run(o); R.rows = ELS['xp-rows'].html;
SLIDES.imports['research/alpha'] = { ...SLIDES.imports['research/alpha'], built: '2026-09-01T00:00:00Z' }; renderExperiments(); R.older = ELS['xp-rows'].html;
SLIDES = { ready: false, why: 'workspace-mcp at http://127.0.0.1:8083/mcp did not answer: refused', imports: {} }; R.down = c.when(o);""", answer=answer)
        r = out["R"]
        self.assertEqual((r["when"], r["ask"]), (True, True))
        self.assertEqual(r["beta"], "no deck is built: press Build deck first")
        self.assertNotIn("docs.google.com", r["before"])
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/research/slides", {"repo": "research/alpha"}]])
        self.assertEqual(r["said"], "Imported to Slides · Alpha")
        self.assertIn('href="https://docs.google.com/presentation/d/p-1/edit"', r["rows"])
        self.assertIn("current deck", r["rows"])
        self.assertIn("an older deck: import again", r["older"])
        self.assertEqual(r["down"], "workspace-mcp at http://127.0.0.1:8083/mcp did not answer: refused")

    def test_sources_and_claim_map_send_the_viewer_to_projects_when_none_is_selected(self):
        out = self.run_page("""ANSWER = () => [500, { error: 'boom' }]; await load(); await flush();
R.reader = ELS.reader.html; R.claims = ELS.claims.html; R.counts = Object.assign({}, OUT.counts);
(ELS.claims.listeners.click || []).forEach(f => f({ target: { closest: s => s === '[data-view]' ? { dataset: { view: 'projects' } } : null } }));
R.views = OUT.views.slice();""")
        for panel in ("reader", "claims"):
            self.assertIn('No project is selected. <button class="linkbtn" type="button" data-view="projects">Pick one in Projects</button>.', out["R"][panel])
        self.assertEqual(out["R"]["counts"], {"experiments": None, "sources": None, "claims": None})
        self.assertEqual(out["R"]["views"], ["projects"])

    def experiments(self, doc):
        """The answer with `/api/research/experiments` for beta as `doc` (research_experiments.document's shape)."""
        base = {"read": NOW, "running": "", "source": "u", "account": "operator@example.test", "kit": "/kit/design.md",
                "problems": {"source": "", "account": "", "kit": ""}, "cache": None, "cache_error": "", "runs": [],
                "runs_error": "", "project": "research/beta", "link": None, "link_error": ""}
        return (f"(path, body) => path === '/api/research' ? [200, {json.dumps(self.doc)}]"
                f" : path === '/api/research/research/beta' ? [200, {json.dumps(self.ledger)}]"
                f" : path === '/api/research/experiments?project=research%2Fbeta' ? [200, {json.dumps({**base, **doc})}]"
                " : path === '/api/research/experiments/act' ? [202, { run: '20261019-170000-000001', started: 'x' }]"
                " : [404, { error: 'no' }]")

    def test_apply_shows_the_draft_it_writes_asks_first_and_posts_the_drafts_hash(self):
        # sd:5296: a finished status draft waits for Apply; the confirm shows the old and new section before anything is sent.
        draft = {"before": {"section": "2026-10-01 started", "status": "Todo"},
                 "after": {"section": "2026-10-19 ran the probe. 2026-10-01 started", "status": "In Progress"}, "entry": "ran"}
        run = {"run": "20261019-160000-000001", "verb": "draft", "action": "status", "project": "research/beta", "state": "done",
               "reason": "RESULT: ok", "log": "/s/run.log", "fingerprint": "f" * 64, "draft": draft}
        answer = self.experiments({"link": {"page": "1" * 32, "data_source": "u", "url": "https://example.test/x", "title": "Span sampling"},
                                   "cache": {"read": NOW, "source": "u", "current": True, "rows": [{"page": "1" * 32, "title": "Span sampling", "stage": "Running", "status": "Todo"}]},
                                   "runs": [run]})
        out = self.run_page("""PAGE_LIST.select('research/beta'); await flush(); const o = C.get('research/beta');
R.section = ELS.details.html; R.on = cmd('experiment.apply').when(o); R.shows = cmd('experiment.apply').consequence(o);
shellRun(cmd('experiment.apply'), o); await flush();""", answer=answer)
        self.assertIn('href="https://example.test/x"', out["R"]["section"])
        self.assertIn("Stage</dt><dd>Running · Status Todo", out["R"]["section"])
        self.assertIn("Waiting for Apply: the status update drafted 2026-10-19 16:00 UTC", out["R"]["section"])
        self.assertTrue(out["R"]["on"])
        self.assertIn("Now: “2026-10-01 started”. After: “2026-10-19 ran the probe. 2026-10-01 started”. Status Todo becomes In Progress.",
                      out["R"]["shows"])
        self.assertEqual(out["confirms"], ["experiment.apply"])
        self.assertEqual(out["posts"], [["/api/research/experiments/act", {"verb": "apply", "run": run["run"], "fingerprint": "f" * 64,
                                                                       "recreate": False}, 64]])

    def test_the_apply_confirm_shows_the_whole_draft_and_apply_posts_only_the_draft_it_showed(self):
        # sd:5296: the confirm shows every field Apply writes, and Apply posts the draft that confirm showed, never a newer one.
        deck = {"title": "Span sampling", "slides": [{"id": f"sdexp_{n}", "message": f"m{n}", "elements": [
            {"shape": "RECTANGLE", "x": 0, "y": 300, "w": 720, "h": 40, "fill": "#000000"},
            {"shape": "TEXT_BOX", "x": 40, "y": 40, "w": 640, "h": 60, "text": f"say {n}", "color": "#FFFFFF", "size": 28,
             "font": "Inter", "bold": True}]} for n in range(1, 7)]}
        create = {"title": "Trace budgets", "problem": "Budgets drift.", "body": "## Hypothesis\nTail."}
        done = {"verb": "draft", "project": "research/beta", "state": "done", "reason": "RESULT: ok", "log": "/s/run.log"}
        linked = {"page": "1" * 32, "data_source": "u", "title": "Span sampling"}
        out = self.run_page("""PAGE_LIST.select('research/beta'); await flush(); const o = C.get('research/beta');
R.deck = cmd('experiment.apply').consequence(o); shellRun(cmd('experiment.apply'), o); await flush();
R.again = cmd('experiment.apply').run(o);""", answer=self.experiments({"link": linked, "runs": [
            {**done, "run": "20261019-160000-000002", "action": "deck", "fingerprint": "d" * 64, "draft": deck}]}))
        self.assertIn("6. m6 Shapes: RECTANGLE at 0,300 720x40 fill #000000; TEXT_BOX at 40,40 640x60 “say 6” text #FFFFFF 28pt Inter bold.",
                      out["R"]["deck"])
        self.assertEqual([p[1]["fingerprint"] for p in out["posts"]], ["d" * 64])
        self.assertEqual(out["R"]["again"], "Apply writes only a draft its confirm showed: choose Apply draft again")
        out = self.run_page("""PAGE_LIST.select('research/beta'); await flush(); const o = C.get('research/beta');
R.create = cmd('experiment.apply').consequence(o);""", answer=self.experiments({"runs": [
            {**done, "run": "20261019-160000-000003", "action": "create", "fingerprint": "c" * 64, "draft": create}]}))
        self.assertIn("its body “## Hypothesis\nTail.”", out["R"]["create"])

    def test_refresh_and_drafts_post_without_asking_and_attach_asks_naming_what_it_writes(self):
        answer = self.experiments({"cache": {"read": NOW, "source": "u", "current": True,
                                             "rows": [{"page": "2" * 32, "title": "Trace budgets", "stage": "Idea", "status": "Todo"}]}})
        out = self.run_page("""PAGE_LIST.select('research/beta'); await flush(); const o = C.get('research/beta');
R.section = ELS.details.html; R.status = cmd('experiment.status').when(o); R.apply = cmd('experiment.apply').when(o);
R.options = cmd('experiment.attach').fields(o)[0].options; R.says = cmd('experiment.attach').consequence(o);
shellRun(cmd('experiment.refresh'), o); await flush();
FIELD_VALUES = { page: '2'.repeat(32), project: 'research/beta', description: 'Tail sampling keeps the slow spans' };
shellRun(cmd('experiment.attach'), o); await flush(); ELS['draft-exp'].listeners.click[0]({}); await flush();
R.projects = cmd('experiment.create').fields(C.get('research:page'))[0].options.map(x => x.value);""", answer=answer)
        self.assertIn("Not linked. Attach one, or use Draft experiment in the toolbar.", out["R"]["section"])
        # Draft experiment is a page action (toolbar), not a project command; its first project is the one shown.
        self.assertEqual(out["R"]["projects"][0], "research/beta")
        self.assertEqual(out["R"]["status"], "not linked: attach or create an experiment first")
        self.assertEqual(out["R"]["apply"], "no finished draft waits for Apply")
        self.assertEqual(out["R"]["options"], [{"value": "2" * 32, "label": "Trace budgets · Idea"}])
        self.assertEqual(out["R"]["says"], "Links Beta to the chosen experiment in the workflow database. Notion is not written.")
        self.assertEqual(out["confirms"], ["experiment.attach", "experiment.create"])
        self.assertEqual([p[1] for p in out["posts"]], [
            {"verb": "list"}, {"verb": "attach", "page": "2" * 32, "replaces": None, "project": "research/beta"},
            {"verb": "draft", "action": "create", "description": "Tail sampling keeps the slow spans", "project": "research/beta"}])

    def test_the_deck_is_off_while_the_brand_kit_is_unset_and_the_section_says_why(self):
        answer = self.experiments({"link": {"page": "1" * 32, "data_source": "u", "title": "Span sampling"},
                                   "problems": {"source": "", "account": "", "kit": "RESEARCH_BRAND_KIT is not set. Export it, or copy x to y."}})
        out = self.run_page("""PAGE_LIST.select('research/beta'); await flush(); const o = C.get('research/beta');
R.deck = cmd('experiment.deck').when(o); R.section = ELS.details.html;""", answer=answer)
        self.assertEqual(out["R"]["deck"], "RESEARCH_BRAND_KIT is not set. Export it, or copy x to y.")
        self.assertIn("RESEARCH_BRAND_KIT is not set. Export it, or copy x to y. The deck is off until then.", out["R"]["section"])

    def test_each_claim_type_and_claim_carries_hover_text_from_the_document_and_the_ledgers_grades(self):
        # sd:5297: the legend and the claims named a type with no hover text saying what it means or how it is graded.
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        repo = claimed(root, "research/beta")
        (repo / "SOURCES.md").write_text(LEGEND, encoding="utf-8")
        self.ledger = research_screen.sources("research/beta", now=NOW, backend=lambda *_: research_screen.registry(repo))
        # The page reads the rule from the document, so a rule worded there differently shows that wording.
        self.ledger["claims"]["types"]["supported"]["rule"] = "cites a strong source"
        out = self.run_page("R.claims = ELS.claims.html;")
        claims = out["R"]["claims"]
        supported = ("Supported: cites a strong source. A = primary source, read directly (spec, source code). "
                     "B = secondary, read directly (blog). M = measured in this repo, reproducible from a named tool.")
        unverified = ("Unverified: cites only C, U or ungraded sources. C = search-result summary only, full page not read. "
                      "U = unverified: fetch failed; lead only.")
        self.assertIn(f'<span class="sr" id="claim-type-supported">{supported}</span>', claims)
        self.assertIn(f'<span class="sr" id="claim-type-unverified">{unverified}</span>', claims)
        self.assertIn(f'<span title="{supported}" aria-describedby="claim-type-supported">● supported: cites a strong source</span>', claims)
        self.assertRegex(claims, r'<button type="button" class="v-unverified" data-verdict="unverified" aria-pressed="false"'
                                 r' title="Unverified: [^"]+" aria-describedby="claim-type-unverified">')
        # Each claim's mark says its type and the grades it cites; its button, which a keyboard reaches, points at the
        # type's full text.
        self.assertIn('<span class="g g-ok" title="Supported: cites a strong source. This claim cites B and U.">', claims)
        self.assertIn('<span class="g g-unknown" title="Unverified: cites only C, U or ungraded sources. This claim cites only'
                      ' ungraded sources.">', claims)
        self.assertIn('<button type="button" aria-describedby="claim-type-supported">Both readings stand (note, rumour).</button>',
                      claims)

    def test_a_ledger_without_a_grade_legend_says_so_in_the_hover_text(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ledger = research_screen.sources("research/beta", now=NOW,
                                              backend=lambda *_: research_screen.registry(claimed(root, "research/beta")))
        claims = self.run_page("R.claims = ELS.claims.html;")["R"]["claims"]
        self.assertIn('<span class="sr" id="claim-type-supported">Supported: cites an A, B or M source. This ledger does not'
                      ' define its grades.</span>', claims)

    def test_an_ungraded_ledger_says_why_every_claim_is_unverified(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        repo = checkout(root, "research/beta")
        for name, text in (("10-sources/registry.md", IDS), ("30-brief/BRIEF.md", CITES)):
            (repo / name).parent.mkdir(exist_ok=True)
            (repo / name).write_text(text, encoding="utf-8")
        self.ledger = research_screen.sources("research/beta", now=NOW, backend=lambda *_: research_screen.registry(repo))
        out = self.run_page("""R.claims = ELS.claims.html;
(ELS.claims.listeners.click || []).forEach(f => f({ target: { closest: s => s === '.claim[data-c]' ? { dataset: { c: '1' } } : s === 'button' ? {} : null } }));
R.det = ELS.details.html; R.lit = ELS.reader.html;""")
        self.assertIn("▨ 5 unverified", out["R"]["claims"])
        self.assertIn("The ledger has no grades, so all 5 claims are unverified", out["R"]["claims"])
        self.assertIn("cites only ungraded sources", out["R"]["det"])
        self.assertEqual(re.findall(r'<li class="src" data-i="(\d+)"[^>]*data-lit', out["R"]["lit"]), ["2"])

    def test_a_ledger_without_a_claim_map_says_claims_were_not_read(self):
        self.ledger["claims"] = None
        out = self.run_page("R.claims = ELS.claims.html;")
        self.assertIn("Claims not read for Beta: the reader sent no claim map.", out["R"]["claims"])

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
        self.assertEqual(ledger_reads(out), ["/api/research"])

    def test_the_page_reads_its_board_through_the_shared_reader(self):
        # read.js holds the generation, retirement and failure rules (sd:2491); the ledger keeps its own per-project cache.
        self.assertIn("shell.read(", RESEARCH_JS)
        self.assertNotIn("getJSON('/api/research')", RESEARCH_JS)
        out = self.run_page("")
        self.assertEqual(out["states"][0]["text"], "Reading the research checkouts. Rows appear when /api/research answers.")

    def test_the_badge_rule_counts_render_stale_projects_and_an_unread_board_is_unknown(self):
        # sd:3446: the read's rule, which every page's rail shows; Gamma's unread config is unknown and counts nothing.
        self.assertEqual(self.doc["attention"], {"state": "caution", "n": 1, "what": "render-stale projects"})
        def broken(*_):
            raise OSError("the collector timed out")
        self.assertEqual(research_screen.document(now=NOW, backend=broken)["attention"],
                         {"state": "unknown", "n": 0, "what": "research not read"})

    def test_a_project_the_next_read_drops_runs_no_command_and_loses_the_selection(self):
        out = self.run_page("""const first = ANSWER('/api/research')[1], A = ANSWER;
ANSWER = path => path === '/api/research' ? [200, { ...first, projects: first.projects.filter(p => p.key !== 'research/beta'),
  attention: { state: 'ok', n: 0, what: 'render-stale projects' } }] : A(path);
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
    page, section, route, api = "research", "Research", "/research", ("/api/research", "/api/research/experiments", "/api/research/experiments/act",
                                                                "/api/research/deck", "/api/research/render", "/api/research/deck/status", "/api/research/slides", "/api/research/slides/status", "/api/research/deck/deck-20261020T120000Z",
                                                                "/api/research/example")


if __name__ == "__main__":
    unittest.main()
