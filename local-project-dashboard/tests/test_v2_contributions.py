"""The v2 Contributions page (sd:2113).

What this slice promises: `/contributions` answers under the shared policy and loads its script before `shell.js`, and the
old screen moves to `/classic/contributions`, which the palette lists. `/api/contributions/page` is the projection v1
renders: open rows in lane order with only the fields the page shows, settled rows counted per repository, open rows
capped at `OPEN_LIMIT` with the cut said, each row's scope from sd's repo table, and the GitHub tracker's freshness. The
page registers the design's Contributions commands; Acknowledge and Make task ask first and post to the v1 routes, and
Draft nudge, Open on GitHub and Re-run collector are copy only and post nothing. Nothing claims a GitHub reading.

`contributions.js` runs under JavaScriptCore (osascript) against the stand-in page and shell `test_v2_tasks` uses. The
browser half -- the look at 1440 and 375 px, focus, the confirm dialog -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sd_db import upsert_repo

from sd_dashboard import contribution_screen, server, v2

from support import NOW, ScreenCase
from test_contribution_screen import contribution, seed_registered
from test_v2_today import OSASCRIPT, Refused
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
PAGE_JS = (V2 / "static" / "contributions.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

#: The design's Contributions commands (design source products/system/designs/v2/contributions.js): id, object type, label,
#: key, risk. Acknowledge and Make task are `confirm` here, not the design's `undo`: no verb reverses either.
COMMANDS = [
    ["contribution.ack", "contribution", "Acknowledge", "a", "confirm"],
    ["contribution.nudge", "contribution", "Draft nudge", "d", "safe"],
    ["contribution.task", "contribution", "Make task", "k", "confirm"],
    ["contribution.open", "contribution", "Open on GitHub", "o", "safe"],
    ["collector.sync", "collector", "Re-run collector", "r", "safe"],
]

ISSUE = "https://github.com/example/project/issues/2"
EXTERNAL = "https://github.com/other/lib/pull/3"


def rows():
    """One row per case the page draws: each open lane, an issue with a local item, an unfiled row, and settled rows."""
    return [
        contribution(1, "newly_unblocked", title="Fix it's $HOME", observed_at="2026-09-05T10:00:00Z"),
        contribution(2, "awaiting_you", key="issue:" + ISSUE, url=ISSUE, item_id=7, event_ids=[], attention_sources=[],
                     observed_at="2026-09-04T10:00:00Z"),
        contribution(3, "awaiting_them", key="github:" + EXTERNAL, url=EXTERNAL, repo="other/lib", event_ids=[],
                     observed_at="2026-09-03T10:00:00Z", freshness={"status": "unknown", "reason": "Incomplete GitHub observation"}),
        contribution(4, "awaiting_them", key="item:4", url=None, repo=None, event_ids=[], observed_at=None,
                     freshness={"status": "unknown", "reason": "Not yet observed"}),
        contribution(5, "merged"), contribution(6, "merged"), contribution(7, "closed", repo="other/lib"),
    ]


class TheDocument(ScreenCase):
    """`contribution_screen.document` shapes the projection; it reads no GitHub and writes nothing."""

    def setUp(self):
        super().setUp()
        upsert_repo(self.connection, "/home/example/project", remote="git@github.com:Example/Project.git", managed=1)

    def doc(self, given=None):
        with mock.patch.object(contribution_screen.contributions, "projection", return_value=given if given is not None else rows()):
            return contribution_screen.document(self.connection, now=NOW)

    def test_open_rows_come_in_lane_order_newest_first_and_settled_rows_are_counted_only(self):
        doc = self.doc()
        self.assertEqual([r["key"] for r in doc["rows"]], [
            "github:https://github.com/example/project/pull/1", "issue:" + ISSUE, "github:" + EXTERNAL, "item:4"])
        self.assertEqual(doc["lanes"], {"newly_unblocked": 1, "awaiting_you": 1, "awaiting_them": 2, "merged": 2, "closed": 1})
        self.assertEqual((doc["total"], doc["open_total"], doc["truncated"], doc["read"]), (7, 4, False, NOW))
        self.assertEqual(doc["fresh"], {"current": 5, "unknown": 2})
        self.assertEqual(doc["settled"], [{"repo": "example/project", "internal": True, "merged": 2, "closed": 0},
                                          {"repo": "other/lib", "internal": False, "merged": 0, "closed": 1}])

    def test_a_row_carries_only_what_the_page_shows(self):
        row = self.doc(given=[contribution(1, evidence=[{"argv": ["secret"], "cwd": "/home/example/x"}],
                                           local_clone="/home/example/x")])["rows"][0]
        self.assertEqual(set(row), set(contribution_screen.ROW_FIELDS) | {"freshness", "internal", "why_internal"})
        self.assertNotIn("secret", json.dumps(row))

    def test_scope_comes_from_the_repo_table(self):
        upsert_repo(self.connection, "/home/example/tools", remote="https://github.com/example/tools")
        doc = self.doc(given=[contribution(1), contribution(2, repo="example/tools"), contribution(3, repo="other/lib"),
                              contribution(4, repo="/home/example/tools"), contribution(5, repo=None)])
        self.assertEqual([(r["internal"], r["why_internal"]) for r in doc["rows"]],
                         [(True, "managed"), (True, "registered"), (False, None), (True, "registered"), (False, None)])

    def test_open_rows_stop_at_the_limit_and_say_so(self):
        many = [contribution(n, "awaiting_them", observed_at=f"2026-09-0{n}T00:00:00Z") for n in range(1, 4)]
        with mock.patch.object(contribution_screen, "OPEN_LIMIT", 3):
            whole = self.doc(given=many)
        with mock.patch.object(contribution_screen, "OPEN_LIMIT", 2):
            cut = self.doc(given=many)
        self.assertEqual((len(whole["rows"]), whole["truncated"]), (3, False))
        self.assertEqual((len(cut["rows"]), cut["open_total"], cut["truncated"]), (2, 3, True))
        self.assertEqual([r["key"][-1] for r in cut["rows"]], ["3", "2"], "the cut dropped a newer row")

    def test_settled_repositories_past_the_limit_fold_into_other_per_scope(self):
        settled = [contribution(n, "merged", repo=f"other/r{n}") for n in range(1, 4)] + [contribution(9, "closed")]
        with mock.patch.object(contribution_screen, "SETTLED_REPOS", 2):
            doc = self.doc(given=settled)
        self.assertEqual([e["repo"] for e in doc["settled"]], ["example/project", "other/r1"])
        self.assertEqual(doc["settled_other"], {"internal": {"merged": 0, "closed": 0}, "external": {"merged": 2, "closed": 0}})

    def test_the_collector_is_the_github_tracker_freshness(self):
        self.assertEqual(self.doc()["collector"], {"state": "never", "last_success_at": None, "reason": ""})

    def test_the_document_writes_nothing(self):
        seed_registered(self.connection)
        before = tuple(self.connection.iterdump())
        doc = contribution_screen.document(self.connection, now=NOW)
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.assertEqual(doc["rows"][0]["title"], "Ready patch <safe>")


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/contributions")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Contributions · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"]+)"', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "contributions.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_and_the_palette_keeps_the_old_one(self):
        self.assertEqual(v2.SECTIONS.get("Contributions"), "/contributions")
        self.assertNotIn("Contributions", v2.CLASSIC)
        self.assertEqual(v2.SCREENS.get("Contributions (classic)"), "/classic/contributions")
        status, _, body = self.request("/classic/contributions")
        self.assertEqual(status, 200)
        self.assertIn("<title>Contributions — sd</title>", body)

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        before = self.snapshot()
        self.assertEqual(self.request("/api/contributions/page")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/contributions/page?scope=internal", headers=cookie)[0], 400)
        status, _, body = self.request("/api/contributions/page", headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["rows"], [])
        self.assertEqual(self.snapshot(), before)

    def test_a_row_the_page_reads_is_one_the_acknowledge_route_accepts(self):
        seed_registered(self.connection)
        status, _, body = self.request("/api/contributions/page", headers={"Cookie": self.cookie})
        row = json.loads(body)["rows"][0]
        payload = {"key": row["key"], "revision": row["revision"], "event_ids": row["event_ids"]}
        self.assertEqual(self.post("/api/contributions/acknowledge", payload)[0], 200)
        self.assertEqual(self.post("/api/contributions/acknowledge", payload)[0], 409)


SHELL_MORE = r"""
window.shell.row = () => null; window.shell.closePane = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
"""


class TheScript(ScreenCase):
    """contributions.js against the document `contribution_screen` builds."""

    def setUp(self):
        super().setUp()
        upsert_repo(self.connection, "/home/example/project", remote="https://github.com/example/project", managed=1)
        with mock.patch.object(contribution_screen.contributions, "projection", return_value=rows()):
            self.doc = contribution_screen.document(self.connection, now=NOW)

    def run_page(self, body, answer=None, search=""):
        answer = answer or ("(path, body) => path === '/api/contributions/page' ? [200, DOC] : path === '/api/items'"
                            " ? [200, { item: { id: 42, title: body.title } }] : [200, { ok: true }]")
        script = (STAND_IN + f"location.search = {json.dumps(search)};\nvar DOC = {json.dumps(self.doc)};\n" + MARKUP_JS
                  + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_MORE + f"\nANSWER = {answer};\n" + PAGE_JS
                  + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.attention = window.PAGE_ATTENTION; OUT.html = Object.fromEntries(Object.entries(ELS).map(([k, e]) => [k, e.html]));"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]); R.bulk = REG.filter(c => c.bulk).map(c => c.id);")
        self.assertEqual(out["R"]["reg"], COMMANDS)
        self.assertEqual(out["R"]["bulk"], ["contribution.ack"])

    def test_acknowledge_asks_first_and_posts_the_rows_exact_checkpoint(self):
        out = self.run_page("shellRun(cmd('contribution.ack'), C.get('github:https://github.com/example/project/pull/1')); await flush();")
        self.assertEqual(out["confirms"], ["contribution.ack"])
        self.assertEqual(out["posts"], [["/api/contributions/acknowledge", {
            "key": "github:https://github.com/example/project/pull/1", "revision": "a" * 64, "event_ids": ["event-1"]}, 64]])
        self.assertEqual(out["gets"], ["/api/contributions/page", "/api/contributions/page"], "the page did not reread after the write")
        self.assertEqual(out["toasts"][-1], ["Acknowledged · Fix it's $HOME", False])

    def test_a_refused_acknowledge_says_why_and_rereads(self):
        answer = "(path) => path === '/api/contributions/page' ? [200, DOC] : [409, { error: 'Events changed', reload: true }]"
        out = self.run_page("shellRun(cmd('contribution.ack'), C.get('github:https://github.com/example/project/pull/1')); await flush();", answer=answer)
        self.assertEqual(out["toasts"][-1], ["Fix it's $HOME not changed: Events changed", False])
        self.assertEqual(len(out["gets"]), 2)

    def test_make_task_asks_first_files_a_followup_and_is_off_for_a_tracked_row(self):
        out = self.run_page("""R.when = cmd('contribution.task').when(C.get('issue:""" + ISSUE + """'));
R.cli = cmd('contribution.task').cli(C.get('github:https://github.com/example/project/pull/1'));
shellRun(cmd('contribution.task'), C.get('github:""" + EXTERNAL + """')); await flush();""")
        self.assertEqual(out["R"]["when"], "already tracked as item #7")
        self.assertEqual(out["R"]["cli"], "sd task add 'Follow up: Fix it'\\''s $HOME' --kind followup --no-repo"
                                          " --body 'https://github.com/example/project/pull/1'")
        self.assertEqual(out["confirms"], ["contribution.task"])
        self.assertEqual(out["posts"], [["/api/items", {"title": "Follow up: Contribution 3", "body": EXTERNAL, "kind": "followup"}, 64]])
        self.assertEqual(out["toasts"][-1][0], "Followup #42 filed · Contribution 3")

    def test_the_copy_only_commands_post_nothing(self):
        out = self.run_page("""const ext = C.get('github:""" + EXTERNAL + """');
R.exec = ['contribution.nudge', 'contribution.open', 'collector.sync'].map(id => cmd(id).executes);
R.cli = [cmd('contribution.nudge').cli(ext), cmd('contribution.open').cli(ext), cmd('collector.sync').cli(C.get('collector'))];
shellRun(cmd('contribution.nudge'), ext); shellRun(cmd('contribution.open'), ext); shellRun(cmd('collector.sync'), C.get('collector')); await flush();""")
        self.assertEqual(out["R"]["exec"], [False, False, False])
        self.assertEqual(out["R"]["cli"], [f"gh pr comment {EXTERNAL} --body-file nudge.md", f"gh pr view --web {EXTERNAL}", "sd shadow sync"])
        self.assertEqual((out["posts"], out["confirms"]), ([], []))

    def test_off_commands_name_their_reason(self):
        out = self.run_page("""const w = (id, key) => cmd(id).when(C.get(key));
R.off = [w('contribution.ack', 'issue:""" + ISSUE + """'), w('contribution.nudge', 'item:4'), w('contribution.nudge', 'issue:""" + ISSUE + """'),
  w('contribution.open', 'item:4'), w('contribution.nudge', 'github:""" + EXTERNAL + """')];""")
        self.assertEqual(out["R"]["off"], ["no attention event on this row", "not filed on GitHub", "the next step is yours, not theirs",
                                           "not filed on GitHub", True])

    def test_the_page_draws_lanes_lamps_and_settled_counts_and_claims_no_github_reading(self):
        out = self.run_page("R.note = ELS['tp-note'].textContent; R.tally = ELS['settled-tally'].textContent;")
        rows, lamps, settled = out["html"]["rows"], out["html"]["annunciator"], out["html"]["bars"]
        self.assertEqual(re.findall(r'<tr class="lane" data-lane="(\w+)"', rows), ["newly_unblocked", "awaiting_you", "awaiting_them"])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', rows), [r["key"] for r in self.doc["rows"]])
        self.assertEqual(re.findall(r'data-cell="(\w+)" data-state="(\w+)"', lamps),
                         [("newly_unblocked", "caution"), ("awaiting_you", "caution"), ("awaiting_them", "ok"), ("github", "unknown")])
        self.assertIn("not checked", lamps)
        self.assertNotIn("GitHub now", rows)
        self.assertIn('title="example/project">example/project</span><span class="v">2</span>', settled)
        self.assertIn("No merge or close dates recorded", out["R"]["note"])
        self.assertEqual(out["R"]["tally"], "3 rows · 2 merged · 1 closed")
        self.assertEqual(out["attention"], {"state": "caution", "n": 2, "what": "contributions want you"})
        self.assertEqual(out["states"][0]["kind"], "loading")
        self.assertEqual((out["states"][-1]["kind"], out["states"][-1]["title"]), ("partial", "GitHub sync not fresh"))

    def test_the_scope_and_lane_filter_the_ledger(self):
        out = self.run_page("""R.all = ELS.rows.html; document.dispatchEvent(new CustomEvent('contributions:scope', { detail: 'external' }));
R.ext = ELS.rows.html; R.bars = ELS.bars.html;""")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["ext"]), ["github:" + EXTERNAL, "item:4"])
        self.assertNotIn("example/project", out["R"]["bars"])
        self.assertEqual(out["urls"][-1], {"scope": "external"})
        out = self.run_page("R.rows = ELS.rows.html;", search="?lane=awaiting_them&q=lib")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["github:" + EXTERNAL])
        self.assertEqual(out["urls"][-1], {"lane": "awaiting_them", "q": "lib"})

    def test_a_cut_document_says_how_many_rows_it_left_out(self):
        self.doc["truncated"], self.doc["open_total"] = True, 900
        out = self.run_page("R.f = [ELS.filtered.hidden, ELS.filtered.textContent];")
        self.assertEqual(out["R"]["f"], [False, "The document lists 4 of 900 open rows; v1 /classic/contributions lists every one."])

    def test_a_failed_read_and_an_empty_projection_each_say_so(self):
        out = self.run_page("", answer="() => [500, { error: 'boom' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("boom", out["states"][-1]["text"])
        self.assertEqual(out["attention"], {"state": "unknown", "n": 0, "what": "contributions not read"})
        empty = dict(self.doc, rows=[], total=0, open_total=0, settled=[])
        out = self.run_page("", answer=f"() => [200, {json.dumps(empty)}]")
        self.assertEqual((out["states"][-1]["kind"], out["states"][-1]["title"]), ("empty", "No contributions"))

    def test_details_name_the_scope_and_link_only_github(self):
        out = self.run_page("open('github:https://github.com/example/project/pull/1'); R.a = ELS.details.html; open('item:4'); R.b = ELS.details.html;")
        self.assertIn("example/project · internal (sd manages it)", out["R"]["a"])
        self.assertIn('href="https://github.com/example/project/pull/1"', out["R"]["a"])
        self.assertIn("local · external", out["R"]["b"])
        self.assertNotIn("href=", out["R"]["b"])

    def test_the_script_adds_no_sink_no_inline_style_and_no_own_list_keys(self):
        self.assertNotIn("innerHTML", PAGE_JS)
        self.assertNotIn("setAttribute('style'", PAGE_JS)
        self.assertNotRegex(PAGE_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", PAGE_JS)
        self.assertNotRegex(PAGE_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", PAGE_JS)
        self.assertNotIn("CONTRIBUTIONS_DATA", PAGE_JS)


if __name__ == "__main__":
    unittest.main()
