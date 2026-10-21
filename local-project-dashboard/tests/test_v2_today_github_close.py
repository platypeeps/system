"""Close original (sd:5669): once the lane merged Merge's copy, Today > GitHub closes the original with a link.

What this slice promises: the copy counts as merged only by a `Code delivery` note, on the item filed under the
original's ref, that names another pull request of the same repository. The preview shows the exact comment and moves
nothing; the write reads the merge again and GitHub's own state, posts one comment carrying a marker, then closes (not
merges) the pull request. A pull request GitHub shows closed or merged, or a copy not merged, is a 409 and moves nothing.
A comment posted before a failure is found by its marker on the next press, so no second comment is posted.

No test reaches GitHub: `GitHub` stands in for `subprocess.run` and answers each `gh api` call as GitHub would.
"""

from __future__ import annotations

import json
import re
import subprocess
from unittest import mock

from sd_db import ship, upsert_repo, upsert_shadow, workflow, writes

from sd_dashboard import command_journal, pr_close, today_github
from sd_dashboard.issue_filing import GH

from support import ScreenCase
from test_workflow_actions import BrowserSession

REPO = "example/anvil"
LANE = f"https://github.com/{REPO}/pull/12"
COMMIT = "c" * 40


class GitHub:
    """A stand-in for subprocess.run that answers `gh api` for pull request 5: its state, its comments and its close.

    `fail[step]` answers that step instead: a CompletedProcess, or an exception to raise. `lost` steps act on GitHub,
    then fail on the way back, as a call whose answer was lost does."""

    def __init__(self, state: str = "open", merged: bool = False, number: int = 5, full_name: str = REPO):
        self.state, self.merged, self.number, self.full_name = state, merged, number, full_name
        self.comments: list[dict] = []
        self.calls: list[tuple[str, list, dict]] = []
        self.fail: dict = {}
        self.lost: set = set()

    def __call__(self, argv, **kwargs):
        if argv[:3] != ["sh", "-c", GH]:
            return REAL_RUN(argv, **kwargs)
        args = argv[4:]
        step = ("close" if "PATCH" in args else "comment" if "POST" in args else "find" if "--jq" in args else "read")
        self.calls.append((step, args, kwargs))
        sent = json.loads(kwargs["input"]) if kwargs.get("input") else None
        if step in self.fail:
            answer = self.fail[step]
            if isinstance(answer, BaseException):
                raise answer
            return answer
        if step == "comment":
            self.comments.append({"body": sent["body"],
                                  "html_url": f"https://github.com/{REPO}/pull/5#issuecomment-{len(self.comments) + 1}"})
            out = json.dumps(self.comments[-1])
        elif step == "close":
            self.state = sent["state"]
            out = json.dumps({"number": self.number, "state": self.state})
        elif step == "find":
            marker = json.loads(re.search(r"contains\((\".*\")\)", args[-1])[1])
            out = "".join(c["html_url"] + "\n" for c in self.comments if marker in c["body"])
        else:
            out = json.dumps({"number": self.number, "state": self.state, "merged": self.merged,
                              "base": {"repo": {"full_name": self.full_name}}})
        if step in self.lost:
            raise subprocess.TimeoutExpired("gh", pr_close.GH_SECONDS)
        return subprocess.CompletedProcess(argv, 0, out, "")

    def steps(self) -> list[str]:
        return [step for step, _, _ in self.calls]


REAL_RUN = subprocess.run


def refused(stderr: str = "gh: Forbidden (HTTP 403)", code: int = 1):
    return subprocess.CompletedProcess([], code, "", stderr)


class Copy(ScreenCase):
    def setUp(self):
        super().setUp()
        upsert_repo(self.connection, "/repos/anvil", remote=f"git@github.com:{REPO}.git")
        upsert_shadow(self.connection, tracker="github", url=f"https://github.com/{REPO}/pull/5", repo=REPO, number=5,
                      kind="pull", title="Bump anvil", state="open", author="dependabot[bot]")
        self.copy = self.item(f"Merge {REPO}#5: Bump anvil")
        writes.set_item_fields(self.connection, self.copy, source="task-ref", external_id=f"github:{REPO}#5")
        self.connection.commit()

    def deliver(self, url: str = LANE, commit: str = COMMIT, item: int | None = None) -> None:
        """The note `sd-ship merge` writes once the lane's pull request merged."""
        ship.note_merge(self.connection, item or self.copy, {"pull_request": {"url": url}, "merge_commit": commit})
        self.connection.commit()

    def preview(self, repo: str = REPO, number: int = 5):
        return pr_close.preview(self.connection, {"repo": [repo], "number": [str(number)]})

    def close(self, revision: str, github: GitHub, number: int = 5):
        return pr_close.close(self.connection, REPO, number, revision, run=github)


class Close(Copy):
    def test_the_preview_shows_the_comment_linking_the_lanes_pull_request_and_moves_nothing(self):
        self.deliver()
        found = self.preview()
        self.assertEqual((found["merged"], found["commit"], found["item"]), (LANE, COMMIT, self.copy))
        self.assertIn(f"landed through {LANE}, merge commit {COMMIT}", found["text"])
        self.assertTrue(found["text"].endswith(f"<!-- close-original {LANE} {COMMIT} -->"))
        self.assertRegex(found["revision"], r"^[0-9a-f]{64}$")
        self.assertIn(f"gh api repos/{REPO}/pulls/5 --method PATCH -f state=closed", found["cli"])

    def test_close_reads_github_comments_once_then_closes_and_never_merges(self):
        self.deliver()
        found, github = self.preview(), GitHub()
        answer = self.close(found["revision"], github)
        self.assertEqual(github.steps(), ["read", "find", "comment", "close"])
        self.assertEqual(github.calls[0][1], ["api", f"repos/{REPO}/pulls/5"])
        self.assertEqual(github.calls[2][1][:4], ["api", f"repos/{REPO}/issues/5/comments", "--method", "POST"])
        self.assertEqual(json.loads(github.calls[2][2]["input"]), {"body": found["text"]})
        self.assertEqual(github.calls[3][1][:4], ["api", f"repos/{REPO}/pulls/5", "--method", "PATCH"])
        self.assertEqual(json.loads(github.calls[3][2]["input"]), {"state": "closed"})
        self.assertEqual((answer["closed"], answer["commented"], answer["merged"]), (True, True, LANE))
        self.assertEqual(answer["comment"], f"https://github.com/{REPO}/pull/5#issuecomment-1")
        self.assertEqual((github.state, github.merged, len(github.comments)), ("closed", False, 1))

    def test_an_open_row_carries_its_copys_merge_and_none_before_it(self):
        before = today_github.document(self.connection, now="2026-10-21T20:00:00Z")
        self.assertIsNone(before["pulls"][0]["landed"])
        self.deliver()
        after = today_github.document(self.connection, now="2026-10-21T20:00:00Z")
        self.assertEqual(after["pulls"][0]["landed"], {"item": self.copy, "url": LANE, "commit": COMMIT})


class Refusals(Copy):
    def test_a_copy_not_merged_is_refused_before_gh_runs(self):
        github = GitHub()
        status, body = self.preview()
        self.assertEqual(status, 409)
        self.assertIn(f"No merge of {REPO}#5's copy is recorded", body["error"])
        status, body = self.close("a" * 64, github)
        self.assertEqual(status, 409, body)
        self.assertEqual(github.calls, [])

    def test_a_delivery_of_another_pull_request_is_no_merge_of_the_copy(self):
        # The original itself, another repository's pull request, and an item filed under another ref.
        self.deliver(url=f"https://github.com/{REPO}/pull/5")
        self.deliver(url="https://github.com/other/anvil/pull/12", commit="d" * 40)
        other = self.item("Another copy")
        writes.set_item_fields(self.connection, other, source="task-ref", external_id=f"github:{REPO}#6")
        self.deliver(item=other, commit="e" * 40)
        self.assertIsNone(pr_close.landed(self.connection, REPO, 5))
        self.assertEqual(self.preview()[0], 409)

    def test_a_revision_that_differs_moves_nothing(self):
        self.deliver()
        found, github = self.preview(), GitHub()
        self.deliver(url=f"https://github.com/{REPO}/pull/13", commit="f" * 40)
        with self.assertRaises(workflow.StaleItem):
            self.close(found["revision"], github)
        self.assertEqual(github.calls, [])

    def test_a_pull_request_github_shows_closed_or_merged_is_refused(self):
        self.deliver()
        revision = self.preview()["revision"]
        for github, says in ((GitHub(state="closed"), "closed"), (GitHub(state="closed", merged=True), "merged")):
            with self.subTest(says=says):
                status, body = self.close(revision, github)
                self.assertEqual(status, 409, body)
                self.assertIn(f"GitHub shows {REPO}#5 {says}; nothing to close", body["error"])
                self.assertEqual(github.steps(), ["read"])

    def test_a_read_that_fails_is_not_known_and_posts_nothing(self):
        self.deliver()
        revision = self.preview()["revision"]
        lost, timeout, find = GitHub(), GitHub(), GitHub()
        other, elsewhere = GitHub(number=6), GitHub(full_name="other/anvil")
        lost.fail["read"] = refused("gh: HTTP 502")
        timeout.fail["read"] = subprocess.TimeoutExpired("gh", 60)
        find.fail["find"] = refused("gh: HTTP 500")
        for github in (lost, timeout, other, elsewhere, find):
            with self.subTest(steps=github.fail or github.number):
                status, body = self.close(revision, github)
                self.assertEqual(status, 502, body)
                self.assertIn("was not read", body["error"])
                self.assertNotIn("comment", github.steps())
                self.assertNotIn("close", github.steps())

    def test_a_comment_github_refused_is_not_posted(self):
        self.deliver()
        github = GitHub()
        github.fail["comment"] = refused("gh: Unable to create comment because issue is locked (HTTP 403)")
        status, body = self.close(self.preview()["revision"], github)
        self.assertEqual(status, 409, body)
        self.assertIn("the comment was not posted", body["error"])
        self.assertEqual((github.steps(), github.state), (["read", "find", "comment"], "open"))

    def test_the_request_takes_a_repo_a_number_and_a_revision_and_nothing_else(self):
        good = {"repo": REPO, "number": 5, "revision": "a" * 64}
        pr_close.request(good, "test")
        for values in ({}, {**good, "number": "5"}, {**good, "number": 0}, {**good, "number": True},
                       {**good, "repo": "../x"}, {**good, "revision": "x"}, {**good, "text": "hi"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                pr_close.request(values, "test")


class Steps(Copy):
    def test_a_comment_with_no_answer_is_found_by_the_next_close_and_not_posted_again(self):
        self.deliver()
        revision, github = self.preview()["revision"], GitHub()
        github.lost.add("comment")
        status, body = self.close(revision, github)
        self.assertEqual((status, body["outcome"], github.state), (502, "unknown", "open"))
        self.assertIn("Whether the comment was posted is not known", body["error"])
        github.lost.clear()
        answer = self.close(revision, github)
        self.assertEqual((answer["closed"], answer["commented"]), (True, False))
        self.assertEqual(answer["comment"], f"https://github.com/{REPO}/pull/5#issuecomment-1")
        self.assertEqual(github.steps(), ["read", "find", "comment", "read", "find", "close"])
        self.assertEqual(len(github.comments), 1)

    def test_a_close_that_failed_after_the_comment_closes_on_the_next_press_with_one_comment(self):
        self.deliver()
        revision, github = self.preview()["revision"], GitHub()
        github.fail["close"] = refused("gh: Validation Failed (HTTP 422)")
        status, body = self.close(revision, github)
        self.assertEqual(status, 502, body)
        self.assertEqual((body["commented"], body["closed"], github.state), (True, False, "open"))
        self.assertIn("Close original again closes without a second comment", body["error"])
        self.assertNotIn("outcome", body)
        del github.fail["close"]
        answer = self.close(revision, github)
        self.assertEqual((answer["closed"], answer["commented"], github.state), (True, False, "closed"))
        self.assertEqual(len(github.comments), 1)

    def test_a_close_with_no_answer_reads_closed_on_the_next_press(self):
        self.deliver()
        revision, github = self.preview()["revision"], GitHub()
        github.lost.add("close")
        status, body = self.close(revision, github)
        self.assertEqual((status, body["outcome"], github.state), (502, "unknown", "closed"))
        status, body = self.close(revision, github)
        self.assertEqual(status, 409, body)
        self.assertEqual(len(github.comments), 1)


class Route(Copy, BrowserSession):
    def test_the_page_previews_with_a_get_and_closes_with_a_post_which_the_journal_records(self):
        self.deliver()
        status, _, body = self.request(f"/api/today/github/close/preview?repo={REPO}&number=5",
                                       headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        found = json.loads(body)
        self.assertEqual(self.post("/api/today/github/close", {"repo": REPO, "number": 5})[0], 400)
        github = GitHub()
        with mock.patch.object(subprocess, "run", github):
            status, _, body = self.post("/api/today/github/close", {"repo": REPO, "number": 5,
                                                                     "revision": found["revision"]})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["closed"], github.steps()), (True, ["read", "find", "comment", "close"]))
        self.assertEqual(command_journal.command("/api/today/github/close", {"repo": REPO, "number": 5})[0].label,
                         "Close original")
