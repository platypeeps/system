"""The v2 Contributions page (sd:2113).

What this slice promises: `/contributions` answers under the shared policy and loads its script before `shell.js`, and the
old screen moves to `/classic/contributions`, which the palette lists. `/api/contributions/page` is the projection v1
renders: open rows in lane order with only the fields the page shows, settled rows counted per repository, open rows
capped at `OPEN_LIMIT` with the cut said, each row's scope from sd's repo table, and the GitHub tracker's freshness. The
page registers the design's Contributions commands; Acknowledge and Make task ask first and post, Make task a linked task, and
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

from sd_db import contributions, upsert_repo

from sd_dashboard import contribution_screen, server, v2

from support import NOW, ScreenCase
from test_contribution_screen import contribution, seed_registered
from test_v2_today import OSASCRIPT, Refused
from test_v2_tasks import SHELL, STAND_IN
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers

V2 = Path(v2.__file__).resolve().parent
PAGE_JS = (V2 / "static" / "contributions.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

#: The design's Contributions commands (design source products/system/designs/pages/contributions.js): id, object type, label,
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
                                           local_clone="/home/example/x", blocked_on="waiting on a private note")])["rows"][0]
        self.assertEqual(set(row), set(contribution_screen.ROW_FIELDS) | {"repo", "has_draft", "freshness", "internal", "why_internal"})
        self.assertNotIn("secret", json.dumps(row))
        # Review 4 of PR #72 (34daa6618266): blocked_on is free-form metadata the page never reads.
        self.assertNotIn("private note", json.dumps(row))
        self.assertNotIn("blocked_on", PAGE_JS)

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

    def test_no_field_holds_a_local_path_or_a_draft_digest(self):
        draft = {"path": "/home/example/drafts/issue.md", "sha256": "f" * 64}
        doc = self.doc(given=[contribution(1, url=None, repo="/home/example/clone", draft_path=draft),
                              contribution(2, url=None, repo="~/clone-two"), contribution(3, "merged", repo="/home/example/old")])

        def strings(value):
            if isinstance(value, dict):
                for key, inner in value.items():
                    yield key
                    yield from strings(inner)
            elif isinstance(value, list):
                for inner in value:
                    yield from strings(inner)
            elif isinstance(value, str):
                yield value
        leaked = [text for text in strings(doc) if text.startswith(("/", "~")) or "/home/example" in text or "f" * 64 in text]
        self.assertEqual(leaked, [])
        self.assertEqual([(r["repo"], r["has_draft"]) for r in doc["rows"]], [("local: clone", True), ("local: clone-two", False)])
        self.assertEqual(doc["settled"][0]["repo"], "local: old")

    def test_the_counts_cover_every_open_row_even_past_the_limit(self):
        many = [contribution(1, "newly_unblocked"), contribution(2, "awaiting_you", repo="other/lib", url=None),
                contribution(3, "awaiting_them", repo="other/lib", item_id=9, freshness={"status": "unknown", "reason": "x"})]
        with mock.patch.object(contribution_screen, "OPEN_LIMIT", 1):
            doc = self.doc(given=many)
        self.assertEqual(len(doc["rows"]), 1)
        self.assertEqual(doc["counts"], {
            "internal": {"newly_unblocked": 1, "awaiting_you": 0, "awaiting_them": 0, "unknown": 0, "linked": 0, "unfiled": 0},
            "external": {"newly_unblocked": 0, "awaiting_you": 1, "awaiting_them": 1, "unknown": 1, "linked": 1, "unfiled": 1}})

    def test_settled_checkouts_named_alike_stay_two_entries(self):
        # Review 2 of PR #72 (aa5a4f3222ed): the label `local: project` keyed the fold, so two checkouts became one entry.
        doc = self.doc(given=[contribution(1, "merged", repo="/home/example/a/project"),
                              contribution(2, "merged", repo="/home/example/a/project"),
                              contribution(3, "closed", repo="/home/example/b/project")])
        self.assertEqual(doc["settled"], [{"repo": "local: project", "internal": False, "merged": 2, "closed": 0},
                                          {"repo": "local: project", "internal": False, "merged": 0, "closed": 1}])

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


    def test_make_task_files_a_task_the_projection_links_to_the_row(self):
        # Review 2 of PR #72 (42fa0e59c1bf): a standalone followup never linked, so the row still offered Make task.
        url = "https://github.com/example/project/pull/21"
        contributions.observe_pull(self.connection, url, {"complete": True, "observed_at": "2026-09-09T12:00:00Z",
            "operator": {"id": "1", "login": "author"}, "author": {"id": "1", "login": "author"}, "repo": "example/project",
            "title": "Upstream patch", "state": "open", "head": "a" * 40, "base": "b" * 40, "draft": False,
            "mergeable": "mergeable", "ci": "success", "ci_head": "a" * 40, "ci_ids": ["check:1"], "why": ["author"],
            "blocking_labels": [], "labels": [], "reviews": [], "events": []},
            expected_revision=contributions.snapshot(self.connection, "github:" + url)["revision"])
        self.connection.commit()
        read = lambda: next(r for r in json.loads(self.request("/api/contributions/page", headers={"Cookie": self.cookie})[2])["rows"]
                            if r["url"] == url)
        row = read()
        self.assertIsNone(row["item_id"])
        status, _, out = self.post("/api/contributions/task", {"key": row["key"], "title": row["title"]})
        self.assertEqual(status, 200, out)
        self.assertEqual(read()["item_id"], out["item"]["id"])
        status, _, again = self.post("/api/contributions/task", {"key": row["key"], "title": row["title"]})
        self.assertEqual(status, 400)
        self.assertIn("already belongs", again["error"])
        self.assertEqual(self.post("/api/contributions/task", {"key": "item:4", "title": "x"})[0], 400)


SHELL_MORE = r"""
window.shell.row = () => null; window.shell.closePane = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
OUT.picks = []; C.pick = id => OUT.picks.push(id); OUT.opened = []; window.open = u => OUT.opened.push(u);
// An answer that settles after k more turns, so a test can make an older read land after a newer one.
const later = (v, k) => { let p = Promise.resolve(v); for (let i = 0; i < k; i++) p = p.then(x => x); return p; };
"""


class TheScript(ScreenCase):
    """contributions.js against the document `contribution_screen` builds."""

    def setUp(self):
        super().setUp()
        upsert_repo(self.connection, "/home/example/project", remote="https://github.com/example/project", managed=1)
        with mock.patch.object(contribution_screen.contributions, "projection", return_value=rows()):
            self.doc = contribution_screen.document(self.connection, now=NOW)

    def run_page(self, body, answer=None, search=""):
        answer = answer or ("(path, body) => path === '/api/contributions/page' ? [200, DOC] : path === '/api/contributions/task'"
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

    def test_make_task_asks_first_files_a_linked_task_and_is_off_for_a_tracked_row(self):
        out = self.run_page("""R.when = [cmd('contribution.task').when(C.get('issue:""" + ISSUE + """')), cmd('contribution.task').when(C.get('item:4'))];
R.cli = cmd('contribution.task').cli(C.get('github:https://github.com/example/project/pull/1'));
shellRun(cmd('contribution.task'), C.get('github:""" + EXTERNAL + """')); await flush();""")
        self.assertEqual(out["R"]["when"], ["already tracked as item #7", "not filed on GitHub"])
        self.assertEqual(out["R"]["cli"], """printf '%s\\n' '{"pull_url":"https://github.com/example/project/pull/1"}' > contribution.json"""
                                          " && sd task contribution add 'Fix it'\\''s $HOME' --file contribution.json")
        self.assertEqual(out["confirms"], ["contribution.task"])
        self.assertEqual(out["posts"], [["/api/contributions/task", {"key": "github:" + EXTERNAL, "title": "Contribution 3"}, 64]])
        self.assertEqual(out["toasts"][-1][0], "Task #42 filed · Contribution 3")

    def test_a_landed_write_whose_reread_fails_still_says_it_landed_and_turns_its_command_off(self):
        # Review 2 of PR #72 (84cde549031a): the reread sat inside the write's promise, so a failed GET reported a committed
        # POST as "not changed", and Make task stayed on for a retry that files a second task.
        answer = """(() => { let n = 0; return path => path === '/api/contributions/page' ? (n++ ? [500, { error: 'gone' }] : [200, DOC])
          : path === '/api/contributions/task' ? [200, { item: { id: 42 } }] : [200, { ok: true }]; })()"""
        out = self.run_page("""const ext = C.get('github:""" + EXTERNAL + """'), one = C.get('github:https://github.com/example/project/pull/1');
shellRun(cmd('contribution.task'), ext); await flush(); shellRun(cmd('contribution.ack'), one); await flush();
R.off = [cmd('contribution.task').when(ext), cmd('contribution.ack').when(one)];""", answer=answer)
        self.assertEqual([t[0] for t in out["toasts"]], ["Task #42 filed · Contribution 3", "Acknowledged · Fix it's $HOME"])
        self.assertEqual(out["R"]["off"], ["already tracked as item #42", "no attention event on this row"])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("The write landed, but the contributions were not read again: gone", out["states"][-1]["text"])

    def test_a_refused_write_whose_reread_fails_does_not_say_it_landed(self):
        # Review 4 of PR #72 (005e99951263): both rejection handlers reread, and a failed reread said the write landed.
        answer = """(() => { let n = 0; return path => path === '/api/contributions/page' ? (n++ ? [500, { error: 'gone' }] : [200, DOC])
          : [409, { error: 'Events changed', reload: true }]; })()"""
        out = self.run_page("""shellRun(cmd('contribution.ack'), C.get('github:https://github.com/example/project/pull/1')); await flush();""", answer=answer)
        self.assertEqual(out["toasts"][-1], ["Fix it's $HOME not changed: Events changed", False])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertNotIn("landed", out["states"][-1]["text"])
        self.assertIn("The contributions were not read again: gone", out["states"][-1]["text"])

    def test_after_a_bulk_acknowledge_only_the_newest_reread_draws(self):
        # Review 2 of PR #72 (903bc49868b2): one reread per row, and an older answer that lands last replaced the newer one.
        ext = next(r for r in self.doc["rows"] if r["key"] == "github:" + EXTERNAL)
        ext["event_ids"] = ["event-3"]
        newer = dict(self.doc, rows=[r for r in self.doc["rows"] if r["key"] != ext["key"]])
        answer = """(() => { let n = 0; return path => path !== '/api/contributions/page' ? [200, { ok: true }]
          : (n++, n === 2 ? later([200, DOC], 60) : n === 3 ? [200, """ + json.dumps(newer) + """] : [200, DOC]); })()"""
        out = self.run_page("""shellBulk(cmd('contribution.ack'), [C.get('github:https://github.com/example/project/pull/1'), C.get('github:""" + EXTERNAL + """')]);
await flush(); R.rows = ELS.rows.html;""", answer=answer)
        self.assertEqual(len(out["gets"]), 3)
        self.assertNotIn(EXTERNAL, out["R"]["rows"])

    def test_a_row_a_reread_no_longer_lists_is_retired_and_unpicked(self):
        # Review 2 of PR #72 (c714acabd3ed): the object map kept the vanished row, so a stale Acknowledge could still act on it.
        newer = dict(self.doc, rows=[r for r in self.doc["rows"] if r["key"] != "github:" + EXTERNAL])
        answer = """(() => { let n = 0; return path => path !== '/api/contributions/page' ? [200, { ok: true }]
          : n++ ? [200, """ + json.dumps(newer) + """] : [200, DOC]; })()"""
        out = self.run_page("""document.dispatchEvent(new CustomEvent('shell:picked', { detail: ['github:""" + EXTERNAL + """'] }));
shellRun(cmd('contribution.ack'), C.get('github:https://github.com/example/project/pull/1')); await flush();
const gone = C.get('github:""" + EXTERNAL + """'); R.gone = [gone.type, gone.label, REG.filter(c => c.on === gone.type).length];""", answer=answer)
        self.assertEqual(out["R"]["gone"], ["not listed", "Contribution 3 (no longer listed)", 0])
        self.assertEqual(out["picks"], ["github:" + EXTERNAL])

    def test_the_copy_only_commands_post_nothing(self):
        out = self.run_page("""const ext = C.get('github:""" + EXTERNAL + """');
R.exec = ['contribution.nudge', 'contribution.open', 'collector.sync'].map(id => cmd(id).executes);
R.cli = [cmd('contribution.nudge').cli(ext), cmd('contribution.open').cli(ext), cmd('collector.sync').cli(C.get('collector'))];
shellRun(cmd('contribution.nudge'), ext); shellRun(cmd('contribution.open'), ext); shellRun(cmd('collector.sync'), C.get('collector')); await flush();""")
        self.assertEqual(out["R"]["exec"], [False, False, False])
        self.assertEqual(out["R"]["cli"], [f"gh pr comment {EXTERNAL} --body-file nudge.md", f"gh pr view --web {EXTERNAL}", "sd shadow sync"])
        self.assertEqual((out["posts"], out["confirms"]), ([], []))
        # Review 2 of PR #72 (8af737dffde4): the shell calls run for a copy-only command too, and run opened a tab.
        self.assertEqual(out["opened"], [])

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

    def test_a_cut_document_counts_from_the_totals_not_the_rows_it_carries(self):
        self.doc["truncated"], self.doc["open_total"] = True, 904
        # Internal: 300 + 100 open, 50 unknown, 7 linked, 2 unfiled. External, from rows(): 2 awaiting them, both unknown, 1 unfiled.
        self.doc["counts"]["internal"].update(newly_unblocked=300, awaiting_you=100, unknown=50, linked=7, unfiled=2)
        out = self.run_page("""R.sub = ELS.sub.html; R.lamps = ELS.annunciator.html; R.tally = ELS.tally.html; R.source = ELS.source.html;
document.dispatchEvent(new CustomEvent('contributions:scope', { detail: 'external' })); R.ext = ELS.annunciator.html;""")
        self.assertEqual(out["attention"], {"state": "caution", "n": 400, "what": "contributions want you"})
        self.assertIn('<span id="open-n">402</span>', out["R"]["sub"])
        self.assertEqual(re.findall(r"<b>(\d+)</b>", out["R"]["lamps"]), ["300", "100", "2"])
        self.assertIn("▲ 400 yours", out["R"]["tally"])
        self.assertIn("<b>52</b> of 402 open rows unknown freshness", out["R"]["source"])
        self.assertIn("7 linked items · 3 unfiled", out["R"]["source"])
        self.assertEqual(re.findall(r"<b>(\d+)</b>", out["R"]["ext"]), ["0", "0", "2"])

    def test_a_cut_document_with_no_carried_row_in_the_scope_says_the_limit_left_them_out(self):
        # Review 4 of PR #72 (9370ec932593): the ledger said "Nothing open" while the counts showed open rows.
        self.doc["truncated"], self.doc["open_total"] = True, 900
        self.doc["rows"] = [r for r in self.doc["rows"] if r["internal"]]
        out = self.run_page("""document.dispatchEvent(new CustomEvent('contributions:scope', { detail: 'external' })); R.rows = ELS.rows.html;""")
        self.assertNotIn("Nothing open", out["R"]["rows"])
        self.assertIn("2 open rows in external repositories were left out by the limit", out["R"]["rows"])

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

    def test_an_unfiled_draft_reads_as_a_draft_from_the_flag_alone(self):
        row = next(r for r in self.doc["rows"] if r["key"] == "item:4")
        row["has_draft"] = True
        out = self.run_page("R.rows = ELS.rows.html;")
        self.assertIn("local  · Unfiled issue draft", out["R"]["rows"])

    def test_the_page_text_names_the_route_make_task_posts(self):
        # Review 4 of PR #72 (0435ca1e4fbb): the help and the page note still said a followup through /api/items.
        page = (V2 / "contributions.html").read_text(encoding="utf-8")
        routes = re.findall(r"post\('(/api/[^']+)'", PAGE_JS)
        self.assertEqual(routes, ["/api/contributions/acknowledge", "/api/contributions/task"])
        for route in routes:
            self.assertIn(f"POST {route}", page)
        self.assertNotIn("/api/items", page)
        self.assertNotIn("followup", page)
        self.assertIn("Make task files a task linked to the contribution", page)

    def test_the_script_adds_no_sink_no_inline_style_and_no_own_list_keys(self):
        self.assertNotIn("innerHTML", PAGE_JS)
        self.assertNotIn("setAttribute('style'", PAGE_JS)
        self.assertNotRegex(PAGE_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", PAGE_JS)
        self.assertNotRegex(PAGE_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", PAGE_JS)
        self.assertNotIn("CONTRIBUTIONS_DATA", PAGE_JS)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "contributions", "Contributions", "/contributions", ("/api/contributions/page", "/api/contributions/task")


if __name__ == "__main__":
    unittest.main()
