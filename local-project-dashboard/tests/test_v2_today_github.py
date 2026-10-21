"""Today's GitHub view (sd:5608): the operator's open pull requests and issues, and those merged or closed in 7 days.

What this slice promises: `/api/today/github` lists only what the dashboard already stores -- `shadow`, the Contributions
projection, the repositories with GitHub issues on, sd-ship's delivery notes and items filed under a GitHub ref -- and
calls no collector. A pull request is listed when it waits on the operator, is assigned to them or is theirs (upstream in
a repository sd does not register); an issue when it is theirs or a third party's in a repository with GitHub issues on;
watched rows and other people's pull requests are not. Rows sort newest GitHub activity first; a row with no stamp says
so and sorts last, and a settled row with no date is counted, never placed in the window. Each row links to its item when
a contribution, a delivery note or a ref names one. A source that fails is named, never an empty list.
`today-github.js` reads once the view shows, draws each part's first 5 rows with Show all, and says each state. It runs
under JavaScriptCore against the stand-in page, as `test_v2_today_status` runs `today-status.js`. The look at 375 px is a
manual check on the pull request.

sd:5633: a pull request row carries what its Details show (author, branch, dates, what waits on the operator) and the
registered checkout of its repository; a press opens those Details with Open on GitHub, Open item, Adversarial review
(a change review of that pull request) and Copilot review, which asks GitHub once per press through
`/api/today/github/copilot-review`. No test here runs gh or codex: the request takes a stand-in `run`.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sd_db import progress, reads, repos, upsert_repo
from sd_db.writes import set_item_fields

from sd_dashboard import today_github, v2
from sd_dashboard.v2 import pages

from support import NOW, ScreenCase, upsert_shadow
from test_v2_tasks import MARKUP_JS, SHELL, STAND_IN
from test_v2_today import OSASCRIPT
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
SCRIPT = V2 / "static" / "today-github.js"
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")
PAGE_JS = SCRIPT.read_text(encoding="utf-8") if SCRIPT.exists() else ""
HUB = "https://github.com"
SHA = "a" * 40


def seed(case):
    """Our repository with GitHub issues on, and one GitHub row for each case the view decides."""
    path = "/repos/anvil"
    upsert_repo(case.connection, path, remote="git@github.com:example/anvil.git")
    repos.set_github_issues(case.connection, path, "on")

    def row(kind, number, repo="example/anvil", **columns):
        upsert_shadow(case.connection, tracker="github", url=f"{HUB}/{repo}/{kind}/{number}", repo=repo, number=number,
                      kind="pull" if kind == "pull" else "issue", title=f"{repo} {kind} {number}", **columns)

    row("pull", 1, state="open", reason="filed", filed=True, author="me", updated_at="2026-09-06T10:00:00Z")
    row("pull", 2, repo="upstream/tool", state="open", reason="filed", author="me", updated_at="2026-09-06T11:00:00Z")
    row("pull", 3, state="open", reason="assigned", author="bo", created_at="2026-09-01T09:00:00Z")
    row("pull", 4, state="open", reason="watched", author="bo", updated_at="2026-09-06T11:30:00Z")
    row("pull", 5, state="open", author="bo", updated_at="2026-09-06T11:40:00Z")
    row("pull", 6, state="open", author="bo", updated_at="2026-09-05T08:00:00Z")
    row("issues", 7, state="open", author="alice", updated_at="2026-09-05T12:00:00Z")
    row("issues", 8, state="open", reason="filed", author="me")
    row("issues", 9, repo="upstream/tool", state="open", author="carol", updated_at="2026-09-06T09:00:00Z")
    row("pull", 10, state="merged", reason="filed", author="me", merged_at="2026-09-04T10:00:00Z",
        closed_at="2026-09-04T10:00:00Z")
    row("pull", 11, state="merged", reason="filed", author="me", merged_at="2026-08-20T10:00:00Z",
        closed_at="2026-08-20T10:00:00Z")
    row("pull", 12, state="closed", reason="filed", author="me")
    row("issues", 13, state="closed", reason="filed", author="me", closed_at="2026-09-05T10:00:00Z")
    triage = case.item("Triage alice's report", kind="task")
    set_item_fields(case.connection, triage, source="task-ref", external_id="github:example/anvil#7")
    shipped = case.item("Ship the anvil fix")
    case.note(shipped, f"Code delivery {HUB}/example/anvil/pull/10 at {SHA}", kind="comment")
    case.connection.commit()
    return {"triage": triage, "shipped": shipped}


def waiting(url):
    """`tracker_items` with `url` waiting on the operator, as the Contributions projection says of a review request."""
    read = progress.tracker_items
    return mock.patch.object(progress, "tracker_items", lambda *a, **k: [
        {**row, "needs_you": row["url"] == url} for row in read(*a, **k)])


class TheDocument(ScreenCase):
    def test_open_rows_are_the_operators_newest_activity_first_and_settled_rows_the_last_seven_days(self):
        ids = seed(self)
        with waiting(f"{HUB}/example/anvil/pull/6"):
            found = today_github.document(self.connection, now=NOW)
        self.assertEqual(found["error"], "")
        self.assertEqual(found["unread"], {})
        pulls = [(row["ref"], row["why"], row["at_what"]) for row in found["pulls"]]
        self.assertEqual(pulls, [("upstream/tool#2", "upstream", "updated"), ("example/anvil#1", "yours", "updated"),
                                 ("example/anvil#6", "waiting on you", "updated"),
                                 ("example/anvil#3", "assigned to you", "opened")])
        issues = [(row["ref"], row["why"], row["at"], row["item"]) for row in found["issues"]]
        self.assertEqual(issues, [("example/anvil#7", "opened by alice", "2026-09-05T12:00:00Z", ids["triage"]),
                                  ("example/anvil#8", "yours", None, None)])
        settled = [(row["ref"], row["state"], row["at_what"], row["item"]) for row in found["settled"]]
        self.assertEqual(settled, [("example/anvil#13", "closed", "closed", None),
                                   ("example/anvil#10", "merged", "merged", ids["shipped"])])
        self.assertEqual(found["undated"], 1)
        self.assertEqual(found["days"], 7)
        self.assertEqual(found["pulls"][0]["url"], f"{HUB}/upstream/tool/pull/2")
        json.dumps(found)  # the page reads it as JSON

    def test_a_read_that_failed_is_its_reason_never_an_empty_list(self):
        seed(self)
        with mock.patch.object(progress, "tracker_items", side_effect=sqlite3.OperationalError("database is locked")):
            found = today_github.document(self.connection, now=NOW)
        self.assertEqual(found["error"], "database is locked")
        self.assertEqual((found["pulls"], found["issues"], found["settled"]), ([], [], []))

    def test_a_source_that_failed_is_named_and_the_rest_still_lists(self):
        seed(self)
        with mock.patch.object(reads, "delivery_notes", side_effect=sqlite3.OperationalError("disk I/O error")):
            found = today_github.document(self.connection, now=NOW)
        self.assertEqual(found["unread"], {"delivery": "disk I/O error"})
        self.assertEqual([row["item"] for row in found["settled"]], [None, None])
        self.assertEqual(len(found["pulls"]), 3)

    def test_the_today_page_serves_the_read_in_its_own_view(self):
        page = next(page for page in pages.PAGES if page.section == "Today")
        self.assertIn("/api/today/github", [api.path for api in page.api])
        html = (V2 / "today.html").read_text(encoding="utf-8")
        self.assertIn('<div id="view-github" aria-label="GitHub" hidden>', html)
        self.assertLess(html.index('src="/ui/today-github.js"'), html.index('src="/ui/shell.js"'))
        self.assertIn("{ id: 'github', label: 'GitHub' }", (V2 / "static" / "today.js").read_text(encoding="utf-8"))


class TheDetails(ScreenCase):
    """sd:5633: what a pull request row's Details show, read from the same stores."""

    def test_a_pull_request_row_carries_its_facts_and_its_registered_checkout(self):
        seed(self)
        upsert_shadow(self.connection, tracker="github", url=f"{HUB}/example/anvil/pull/1", repo="example/anvil", number=1,
                      kind="pull", title="example/anvil pull 1", state="open", reason="filed", filed=True, author="me",
                      branch="fix-anvil", created_at="2026-09-01T08:00:00Z", updated_at="2026-09-06T10:00:00Z")
        self.connection.commit()
        found = today_github.document(self.connection, now=NOW)
        mine = next(row for row in found["pulls"] if row["ref"] == "example/anvil#1")
        stored = repos.row_for(self.connection, "/repos/anvil")["path"]
        self.assertEqual((mine["author"], mine["branch"], mine["created_at"], mine["updated_at"], mine["checkout"]),
                         ("me", "fix-anvil", "2026-09-01T08:00:00Z", "2026-09-06T10:00:00Z", stored))
        self.assertEqual((mine["merged_at"], mine["attention"]), (None, []))
        upstream = next(row for row in found["pulls"] if row["ref"] == "upstream/tool#2")
        self.assertIsNone(upstream["checkout"])
        merged = next(row for row in found["settled"] if row["ref"] == "example/anvil#10")
        self.assertEqual(merged["merged_at"], "2026-09-04T10:00:00Z")


def answered(stdout="", returncode=0, stderr=""):
    """A stand-in for subprocess.run: records each call and answers as gh would."""
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if isinstance(returncode, BaseException):
            raise returncode
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
    return run, calls


PULL_ONE = json.dumps({"number": 1, "base": {"repo": {"full_name": "example/anvil"}}})


class TheCopilotReview(ScreenCase):
    """sd:5633: one Copilot review request per press, for a pull request the shadow sync saw open."""

    def test_an_open_pull_request_asks_github_once_through_the_reviewer_endpoint(self):
        seed(self)
        run, calls = answered(PULL_ONE)
        status, body = today_github.copilot_review(self.connection, "example/anvil", 1, run=run)
        self.assertEqual((status, body), (200, {"repo": "example/anvil", "number": 1, "requested": today_github.COPILOT}))
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv[4:], ["api", "repos/example/anvil/pulls/1/requested_reviewers", "--method", "POST",
                                    "--input", "-"])
        self.assertEqual(json.loads(kwargs["input"]), {"reviewers": ["copilot-pull-request-reviewer[bot]"]})
        self.assertEqual(kwargs["timeout"], today_github.COPILOT_TIMEOUT)

    def test_a_pull_request_the_shadow_does_not_hold_open_is_refused_before_gh_runs(self):
        seed(self)
        run, calls = answered(PULL_ONE)
        for repo, number in (("example/anvil", 10), ("example/anvil", 99), ("other/anvil", 1)):
            with self.subTest(repo=repo, number=number):
                status, body = today_github.copilot_review(self.connection, repo, number, run=run)
                self.assertEqual(status, 409)
                self.assertIn("Copilot was not asked", body["error"])
        self.assertEqual(calls, [])

    def test_a_refusal_is_not_asked_and_any_other_failure_is_not_known(self):
        seed(self)
        cases = ((answered(returncode=1, stderr="gh: Validation Failed (HTTP 422)"), 409, "Copilot was not asked"),
                 (answered(returncode=127, stderr="sh: gh: not found"), 409, "Copilot was not asked"),
                 (answered(returncode=1, stderr="gh: HTTP 502"), 502, "not known"),
                 (answered(returncode=subprocess.TimeoutExpired("gh", 60)), 502, "not known"),
                 (answered(json.dumps({"number": 2, "base": {"repo": {"full_name": "example/anvil"}}})), 502, "not known"),
                 (answered("not json"), 502, "not known"))
        for (run, _), want, says in cases:
            with self.subTest(want=want, says=says):
                status, body = today_github.copilot_review(self.connection, "example/anvil", 1, run=run)
                self.assertEqual(status, want)
                self.assertIn(says, body["error"])

    def test_the_request_takes_a_repo_and_a_number_and_nothing_else(self):
        for values in ({}, {"repo": "example/anvil"}, {"repo": "example/anvil", "number": "1"},
                       {"repo": "example/anvil", "number": 0}, {"repo": "example anvil", "number": 1},
                       {"repo": "../x/y", "number": 1}, {"repo": "example/anvil", "number": 1, "reviewers": ["me"]},
                       {"repo": "example/anvil", "number": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                today_github.copilot_request(values)


class TheRoute(BrowserSession):
    def test_the_session_the_page_opens_reads_the_rows(self):
        seed(self)
        status, _, body = self.request("/api/today/github", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        found = json.loads(body)
        self.assertEqual([row["ref"] for row in found["issues"]], ["example/anvil#7", "example/anvil#8"])
        refused, _, _ = self.request("/api/today/github")
        self.assertNotEqual(refused, 200)

    def test_the_copilot_request_is_a_post_that_checks_its_body_and_the_shadow_first(self):
        seed(self)
        self.assertEqual(self.post("/api/today/github/copilot-review", {"repo": "example/anvil"})[0], 400)
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("gh must not run")):
            status, _, body = self.post("/api/today/github/copilot-review", {"repo": "example/anvil", "number": 10})
        self.assertEqual(status, 409, body)


def entry(n, **changes):
    return {"key": f"{HUB}/example/anvil/pull/{n}", "kind": "pull", "repo": "example/anvil", "number": n,
            "ref": f"example/anvil#{n}", "title": f"Pull {n}", "url": f"{HUB}/example/anvil/pull/{n}", "why": "yours",
            "state": "open", "at": "2026-10-19T15:00:00Z", "at_what": "updated", "item": None, **changes}


def doc(**changes):
    return {"now": "2026-10-19T16:00:00Z", "days": 7, "pulls": [], "issues": [], "settled": [], "undated": 0,
            "unread": {}, "error": "",
            "collector": {"state": "fresh", "last_success_at": "2026-10-19T15:30:00Z", "reason": ""}, **changes}


class TheScript(unittest.TestCase):
    """today-github.js against the stand-in page: what it reads and what it draws."""

    def run_page(self, body, answer):
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + COMMANDS_JS
                  + "\nANSWER = " + answer + ";\n" + PAGE_JS
                  + "\nvar R = {};\nconst show = () => document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'github', first: true } }));\n"
                  + "const press = key => ELS['github-sections'].listeners.click[0]({ target: { closest: s => s === '[data-gh-open]'"
                    " ? { dataset: { ghOpen: key } } : null } });\n"
                  + "const more = key => ELS['github-sections'].listeners.click[0]({ target: { closest: s => s === 'button[data-gh-more]'"
                    " ? { dataset: { ghMore: key } } : null } });\n"
                  + "(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def draw(self, found):
        return self.run_page("show(); await flush(); R.sections = ELS['github-sections'].html; R.tally = ELS['github-tally'].html;"
                             " more('pulls'); R.all = ELS['github-sections'].html;",
                             f"(path, body) => [200, {json.dumps(found)}]")

    def test_nothing_is_read_until_the_view_shows(self):
        out = self.run_page("R.sections = ELS['github-sections'].html;", f"(path, body) => [200, {json.dumps(doc())}]")
        self.assertEqual(out["gets"], [])
        self.assertIn("Reading GitHub", out["R"]["sections"])

    def test_a_part_shows_five_rows_and_show_all_opens_the_rest_in_place(self):
        found = doc(pulls=[entry(n) for n in range(1, 8)], issues=[entry(20, kind="issue", title="<b>odd</b>", item=42)])
        out = self.draw(found)
        self.assertEqual(out["gets"], ["/api/today/github"])
        first, every = out["R"]["sections"], out["R"]["all"]
        pulls = first[first.index('id="gh-pulls"'):first.index('id="gh-issues"')]
        self.assertEqual(pulls.count("<tr data-key="), 5)
        self.assertIn('data-gh-more="pulls" aria-expanded="false">Show all 7</button>', first)
        self.assertEqual(every[every.index('id="gh-pulls"'):every.index('id="gh-issues"')].count("<tr data-key="), 7)
        self.assertIn("Show the first 5", every)
        self.assertIn(f'<button type="button" data-gh-open="{HUB}/example/anvil/pull/1">Pull 1</button>', first)
        self.assertIn("&lt;b&gt;odd&lt;/b&gt;", first)
        self.assertIn('<a href="/tasks?row=42">sd:42</a>', first)
        self.assertIn("Issue · example/anvil#20", first)
        self.assertIn("Nothing in Merged or closed.", first)

    def test_waiting_rows_are_caution_settled_rows_say_how_and_a_missing_stamp_is_not_recorded(self):
        found = doc(pulls=[entry(1, why="waiting on you"), entry(2, at=None, at_what=None)],
                    settled=[entry(3, state="merged", at_what="merged")], undated=2)
        out = self.draw(found)
        sections, tally = out["R"]["sections"], out["R"]["tally"]
        self.assertIn('class="g g-caution" title="open">▲', sections)
        self.assertIn('class="g g-queued" title="open">◌', sections)
        self.assertIn('class="g g-ok" title="merged">●', sections)
        self.assertIn("merged · yours", sections)
        self.assertIn('activity not recorded', sections)
        self.assertIn("2 merged or closed rows with no date recorded, not placed", sections)
        self.assertIn('<a class="g-caution" href="#gh-pulls">▲ 1 waiting on you</a>', tally)
        self.assertIn("2 open pull requests", tally)
        self.assertIn("Nothing in Open issues.", sections)

    def test_a_stale_sync_and_an_unread_source_are_said(self):
        found = doc(pulls=[entry(1)], unread={"delivery": "disk I/O error"},
                    collector={"state": "stale", "last_success_at": "2026-10-17T15:30:00Z", "reason": ""})
        out = self.draw(found)
        self.assertIn("Items of merged pull requests (delivery notes) not read: disk I/O error", out["R"]["sections"])
        self.assertIn('class="g-caution">▲ sync stale · synced <time', out["R"]["tally"])
        never = self.draw(doc(collector={"state": "never", "last_success_at": None, "reason": ""}))
        self.assertIn("▨ sync never", never["R"]["tally"])

    def test_a_failed_read_says_why_and_draws_no_count(self):
        out = self.run_page("show(); await flush(); R.sections = ELS['github-sections'].html; R.tally = ELS['github-tally'].html;",
                            "(path, body) => [500, { error: 'database is locked' }]")
        self.assertIn("GitHub could not be read: database is locked. Refresh tries again.", out["R"]["sections"])
        self.assertEqual(out["R"]["tally"], "")
        refused = self.draw(doc(error="no such column: filed"))
        self.assertIn("GitHub could not be read: no such column: filed. Refresh tries again.", refused["R"]["sections"])



PR = f"{HUB}/example/anvil/pull/1"


class ThePullRequestDetails(unittest.TestCase):
    """sd:5633: a press on a pull request row opens its Details and the commands the shell runs on it."""

    run_page = TheScript.run_page

    # The stand-in's setTimeout runs at once, so a review run's poll answers settled: a queued answer would poll forever.
    def page(self, body, posts="path => path === '/api/adversarial' ? [202, { id: 'r1', state: 'queued' }]"
                               " : [200, { id: 'r1', state: 'failed', reason: 'stopped in the test' }]", found=None):
        found = found or doc(pulls=[entry(1, item=42, author="me", branch="fix-anvil", created_at="2026-10-18T09:00:00Z",
                                          updated_at="2026-10-19T15:00:00Z", attention=["Review requested"],
                                          checkout="~/repos/anvil", title="Fix <b>it</b>")],
                             settled=[entry(3, state="merged", at_what="merged", merged_at="2026-10-17T10:00:00Z",
                                            checkout="~/repos/anvil")],
                             issues=[entry(20, kind="issue", url=f"{HUB}/example/anvil/issues/20")])
        return self.run_page("show(); await flush();\n" + body,
                             f"(path, body) => path === '/api/today/github' ? [200, {json.dumps(found)}] : ({posts})(path, body)")

    def test_a_press_shows_the_pull_requests_facts_a_new_tab_link_and_its_commands(self):
        # The stand-in has no rows to mark in place, so a redraw (Show all) shows the selection the next draw keeps.
        out = self.page(f"press({json.dumps(PR)}); R.details = ELS.details.html; more('pulls'); R.sections = ELS['github-sections'].html;"
                        f" R.obj = C.get('gh:{PR}'); R.ids = REG.map(c => c.id);"
                        f" R.issue = C.get('gh:{HUB}/example/anvil/issues/20') || null;")
        details = out["R"]["details"]
        self.assertIn("Pull request · open", details)
        self.assertIn("<h2>Fix &lt;b&gt;it&lt;/b&gt;</h2>", details)
        for fact in ("<dt>Repo</dt><dd>example/anvil</dd>", "<dt>Number</dt><dd>#1</dd>", "<dt>Author</dt><dd>me</dd>",
                     "<dt>Branch</dt><dd>fix-anvil</dd>", '<time class="rel" data-long datetime="2026-10-18T09:00:00Z">',
                     "<dt>Waiting on</dt><dd>Review requested</dd>", '<a href="/tasks?row=42">sd:42</a>'):
            self.assertIn(fact, details)
        self.assertIn(f'<a href="{PR}" target="_blank" rel="noopener noreferrer">Open example/anvil#1 on GitHub</a>', details)
        self.assertIn('<div class="bar"></div>', details)
        self.assertIn(f'<tr data-key="{PR}" data-pr aria-selected="true">', out["R"]["sections"])
        self.assertEqual(out["R"]["obj"]["type"], "github pull request")
        self.assertEqual((out["R"]["obj"]["checkout"], out["R"]["obj"]["number"]), ("~/repos/anvil", 1))
        self.assertIsNone(out["R"]["issue"])
        self.assertEqual([i for i in out["R"]["ids"] if i.startswith("github pull request.")],
                         ["github pull request.open", "github pull request.item", "github pull request.adversarial-review",
                          "github pull request.copilot-review"])

    def test_adversarial_review_starts_a_change_review_of_that_pull_request_and_only_an_open_one(self):
        out = self.page(f"C.run(cmd('github pull request.adversarial-review'), C.get('gh:{PR}'));"
                        f" C.run(cmd('github pull request.adversarial-review'), C.get('gh:{HUB}/example/anvil/pull/3'));")
        self.assertEqual(out["posts"], [["/api/adversarial", {"kind": "change", "target": "~/repos/anvil", "pr": 1, "item": 42}, 64]])
        said = [t["msg"] for t in out["toasts"]]
        self.assertIn("Review started · run r1", said)
        self.assertIn("off: the pull request is merged; a review reads an open one", said)
        unregistered = self.page(f"C.run(cmd('github pull request.adversarial-review'), C.get('gh:{PR}'));",
                                 found=doc(pulls=[entry(1, checkout=None)]))
        self.assertEqual(unregistered["posts"], [])
        self.assertIn("example/anvil is not a registered repository", unregistered["toasts"][-1]["msg"])

    def test_copilot_review_asks_first_then_posts_once_and_a_settled_pull_request_is_off(self):
        out = self.page(f"C.run(cmd('github pull request.copilot-review'), C.get('gh:{PR}')); await flush();"
                        f" C.run(cmd('github pull request.copilot-review'), C.get('gh:{HUB}/example/anvil/pull/3'));",
                        posts="() => [200, { repo: 'example/anvil', number: 1 }]")
        self.assertEqual(out["confirms"], ["github pull request.copilot-review"])
        self.assertEqual(out["posts"], [["/api/today/github/copilot-review", {"repo": "example/anvil", "number": 1}, 64]])
        self.assertEqual(out["toasts"][0]["msg"], "Copilot review requested · example/anvil#1")
        self.assertEqual(out["toasts"][-1]["msg"], "off: the pull request is merged; Copilot reviews an open one")

    def test_a_refused_or_unknown_copilot_request_says_so(self):
        refused = self.page(f"C.run(cmd('github pull request.copilot-review'), C.get('gh:{PR}')); await flush();",
                            posts="() => [409, { error: 'gh exited 1: HTTP 422; Copilot was not asked' }]")
        self.assertIn("not changed: gh exited 1: HTTP 422; Copilot was not asked", refused["toasts"][-1]["msg"])
        lost = self.page(f"C.run(cmd('github pull request.copilot-review'), C.get('gh:{PR}')); await flush();",
                         posts="() => { throw new Error('Failed to fetch'); }")
        self.assertIn("no answer (Failed to fetch), so whether Copilot was asked is not known", lost["toasts"][-1]["msg"])

    def test_a_row_a_later_read_drops_is_retired_so_no_command_runs_on_it(self):
        out = self.run_page("show(); await flush(); R.before = C.get('gh:" + PR + "').type;"
                            " ANSWER = (path, body) => [200, " + json.dumps(doc()) + "];"
                            " ELS.refresh.listeners.click[0](); await flush(); R.after = C.get('gh:" + PR + "');",
                            f"(path, body) => [200, {json.dumps(doc(pulls=[entry(1)]))}]")
        self.assertEqual(out["R"]["before"], "github pull request")
        self.assertEqual(out["R"]["after"]["type"], "not listed")


if __name__ == "__main__":
    unittest.main()
