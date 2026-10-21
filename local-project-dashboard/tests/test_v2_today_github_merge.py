"""Merge (sd:5668): Today > GitHub copies an open pull request into a worktree and enqueues it in the lane.

What this slice promises: the preview reads the pull request's head with `ls-remote` and moves nothing; the write reads
every fact again and moves nothing on a revision that differs, such as a head that moved. Merge fetches
`pull/<n>/head`, makes `pr/<n>` at that head and a worktree under `~/worktrees/<checkout>-pr-<n>`, reuses the item
filed under `github:<owner>/<name>#<n>` or files one, and enqueues with `--deliver`. Each step a run stopped after is
found by the next Merge (`resume`); an enqueue the lane refused is enqueued again with the same item. Only an open pull
request in a registered repository whose origin names it is taken.

Every test runs real git against fixture repositories under a temporary folder: a local bare repository stands in for
GitHub, with the pull request's head under `refs/pull/<n>/head`, and the checkout's origin names GitHub and reaches the
bare one through `url.<path>.insteadOf`. A stub stands in for `sd-ship`: no test reaches GitHub, the lane or a real
checkout, and none writes the live database.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from sd_db import upsert_repo, upsert_shadow, workflow, writes

from sd_dashboard import command_journal, pr_merge

from support import ScreenCase
from test_health_cleanup import FIXTURE_GIT, git
from test_land import Fixture
from test_workflow_actions import BrowserSession

REMOTE = "git@github.com:example/anvil.git"
REPO = "example/anvil"


class Copy(Fixture):
    def pull(self, number: int = 5, author: str = "dependabot[bot]", state: str = "open") -> tuple[Path, str]:
        """A checkout of example/anvil whose origin holds pull request `number`, one commit by `author`."""
        origin = self.root / "anvil.git"
        git("init", "-q", "--bare", "-b", "main", str(origin))
        self.seed = self.root / "seed-anvil"
        git("clone", "-q", str(origin), str(self.seed))
        (self.seed / "README").write_text("r\n")
        git("-C", str(self.seed), "add", "-A")
        git("-C", str(self.seed), "commit", "-q", "-m", "first")
        git("-C", str(self.seed), "push", "-q", "origin", "HEAD:main")
        head = self.push(number, "bump\n", author)
        path = self.root / "repos" / "anvil"
        git("clone", "-q", str(origin), str(path))
        git("-C", str(path), "config", "remote.origin.url", REMOTE)
        git("-C", str(path), "config", f"url.{origin}.insteadOf", REMOTE)
        upsert_repo(self.connection, str(path), remote=REMOTE)
        upsert_shadow(self.connection, tracker="github", url=f"https://github.com/{REPO}/pull/{number}", repo=REPO,
                      number=number, kind="pull", title="Bump anvil", state=state, author=author)
        self.connection.commit()
        return path, head

    def push(self, number: int, text: str, author: str = "dependabot[bot]") -> str:
        """A commit by `author` on the pull request's head, as GitHub keeps it under refs/pull/<n>/head."""
        (self.seed / "deps.txt").write_text(text)
        git("-C", str(self.seed), "add", "-A")
        subprocess.run(["git", "-C", str(self.seed), "commit", "-q", "-m", "Bump deps"], check=True, capture_output=True,
                       env={**os.environ, **FIXTURE_GIT, "GIT_AUTHOR_NAME": author,
                            "GIT_AUTHOR_EMAIL": "bot@example.test"})
        git("-C", str(self.seed), "push", "-q", "-f", "origin", f"HEAD:refs/pull/{number}/head")
        return git("-C", str(self.seed), "rev-parse", "HEAD")

    def preview(self, number: int = 5, repo: str = REPO):
        return pr_merge.preview(self.connection, {"repo": [repo], "number": [str(number)]})

    def merge(self, revision: str, number: int = 5):
        return pr_merge.request({"repo": REPO, "number": number, "revision": revision}, "test")(self.connection)

    def filed(self, number: int = 5):
        return writes.item_by_external(self.connection, "task-ref", f"github:{REPO}#{number}")

    def tree(self, number: int = 5) -> Path:
        return self.root / "home" / "worktrees" / f"anvil-pr-{number}"


class Merge(Copy, ScreenCase):
    def test_the_preview_names_the_pull_request_its_head_author_worktree_and_item_and_moves_nothing(self):
        path, head = self.pull()
        found = self.preview()
        self.assertEqual((found["head"], found["author"], found["branch"], found["mode"]),
                         (head, "dependabot[bot]", "pr/5", "copy"))
        self.assertEqual(found["worktree"], str(self.tree()))
        self.assertEqual((found["item"], found["ref"], found["item_title"]),
                         (None, f"github:{REPO}#5", f"Merge {REPO}#5: Bump anvil"))
        self.assertIn(f"fetch origin pull/5/head && sd-ship -C {self.tree()} lane enqueue --item '<new item>'",
                      found["cli"])
        self.assertRegex(found["revision"], r"^[0-9a-f]{64}$")
        self.assertEqual(git("-C", str(path), "branch", "--list", "pr/*"), "")
        self.assertFalse((path / ".git" / "FETCH_HEAD").exists())
        self.assertFalse(self.tree().exists())
        self.assertIsNone(self.filed())

    def test_merge_copies_the_head_into_its_worktree_files_the_item_and_enqueues_it(self):
        path, head = self.pull()
        main = git("-C", str(path), "rev-parse", "HEAD")
        answer = self.merge(self.preview()["revision"])
        self.assertTrue(answer["ok"], answer)
        item = self.filed()
        self.assertEqual((answer["item"], answer["filed"], answer["lane"]["status"]), (item["id"], True, "pending"))
        self.assertEqual((item["title"], item["status"]), (f"Merge {REPO}#5: Bump anvil", "planning"))
        self.assertEqual(git("-C", str(path), "rev-parse", "pr/5"), head)
        self.assertEqual(git("-C", str(self.tree()), "rev-parse", "HEAD"), head)
        self.assertEqual(git("-C", str(self.tree()), "log", "-1", "--format=%an"), "dependabot[bot]")
        self.assertEqual((git("-C", str(path), "rev-parse", "HEAD"), git("-C", str(path), "branch", "--show-current")),
                         (main, "main"))
        sent = self.sent()
        self.assertEqual(sent[:6], ["-C", str(self.tree()), "lane", "enqueue", "--item", str(item["id"])])
        self.assertEqual(sent[sent.index("--expected-head") + 1], head)
        self.assertEqual(sent[sent.index("--title") + 1], f"Bump anvil ({REPO}#5)")
        self.assertIn("--deliver", sent)
        body = Path(sent[sent.index("--body-file") + 1]).read_text()
        self.assertIn(f"- sd:{item['id']}: Merge {REPO}#5: Bump anvil.", body)
        self.assertIn(f"Copies https://github.com/{REPO}/pull/5 by dependabot[bot] at {head[:12]}", body)
        self.assertIn("## Test plan", body)

    def test_an_item_filed_under_the_ref_is_reused(self):
        self.pull()
        # GitHub reads an owner and a name in any case: the lower-cased ref is the same pull request.
        item = self.item("Land the deps bump", repo=str(self.root / "repos" / "anvil"))
        writes.set_item_fields(self.connection, item, source="task-ref", external_id=f"github:{REPO}#5".lower())
        self.connection.commit()
        found = self.preview()
        self.assertEqual((found["item"], found["item_title"]), (item, "Land the deps bump"))
        self.assertIn(f"--item {item}", found["cli"])
        answer = self.merge(found["revision"])
        self.assertEqual((answer["item"], answer["filed"]), (item, False))
        self.assertEqual(self.sent()[5], str(item))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM item WHERE source = 'task-ref'").fetchone()[0], 1)


class Steps(Copy, ScreenCase):
    def test_a_head_that_moved_since_the_preview_moves_nothing(self):
        path, _ = self.pull()
        found = self.preview()
        self.push(5, "later\n")
        with self.assertRaises(workflow.StaleItem):
            self.merge(found["revision"])
        self.assertEqual(git("-C", str(path), "branch", "--list", "pr/*"), "")
        self.assertFalse(self.tree().exists())
        self.assertIsNone(self.filed())
        self.assertFalse(self.argv_log.exists())

    def test_a_revision_from_another_pull_request_is_refused(self):
        path, _ = self.pull()
        self.push(6, "other\n", author="bo")
        upsert_shadow(self.connection, tracker="github", url=f"https://github.com/{REPO}/pull/6", repo=REPO, number=6,
                      kind="pull", title="Bump anvil", state="open", author="dependabot[bot]")
        self.connection.commit()
        with self.assertRaises(workflow.StaleItem):
            self.merge(self.preview(6)["revision"])
        self.assertEqual(git("-C", str(path), "branch", "--list", "pr/*"), "")
        self.assertFalse(self.argv_log.exists())

    def test_a_head_that_moved_during_the_fetch_makes_no_branch(self):
        path, _ = self.pull()
        found = self.preview()
        real = pr_merge._read

        def moved(*args):
            facts = real(*args)
            self.push(5, "during\n")
            return facts
        with patch.object(pr_merge, "_read", side_effect=moved):
            with self.assertRaisesRegex(workflow.StaleItem, "moved to"):
                self.merge(found["revision"])
        self.assertEqual(git("-C", str(path), "branch", "--list", "pr/*"), "")
        self.assertIsNone(self.filed())
        self.assertFalse(self.argv_log.exists())

    def test_a_fetch_that_failed_moves_nothing_and_the_next_merge_copies(self):
        path, head = self.pull()
        found = self.preview()
        real = pr_merge._git

        def offline(where, *args, **options):
            if args[:1] == ("fetch",):
                raise pr_merge.Unknown("git fetch failed: could not resolve host")
            return real(where, *args, **options)
        with patch.object(pr_merge, "_git", side_effect=offline):
            status, body = self.merge(found["revision"])
        self.assertEqual((status, body["outcome"]), (502, "unknown"))
        self.assertIn("could not resolve host", body["error"])
        self.assertEqual(git("-C", str(path), "branch", "--list", "pr/*"), "")
        self.assertIsNone(self.filed())
        again = self.preview()
        self.assertEqual((again["mode"], again["revision"]), ("copy", found["revision"]))
        self.assertTrue(self.merge(again["revision"])["ok"])
        self.assertEqual(git("-C", str(self.tree()), "rev-parse", "HEAD"), head)

    def test_a_run_stopped_before_the_worktree_adds_it_on_the_next_merge(self):
        path, head = self.pull()
        found = self.preview()
        real = pr_merge._git

        def stop(where, *args, **options):
            if args[:2] == ("worktree", "add"):
                raise pr_merge.Unknown("stopped")
            return real(where, *args, **options)
        with patch.object(pr_merge, "_git", side_effect=stop):
            self.assertEqual(self.merge(found["revision"])[0], 502)
        self.assertEqual(git("-C", str(path), "rev-parse", "pr/5"), head)
        again = self.preview()
        self.assertEqual((again["mode"], again["worktree_listed"]), ("resume", False))
        self.assertNotIn("fetch", again["cli"])
        answer = self.merge(again["revision"])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(git("-C", str(self.tree()), "rev-parse", "HEAD"), head)

    def test_an_enqueue_the_lane_refused_is_enqueued_again_with_the_same_item(self):
        self.pull()
        self.lane({"ok": False, "error": "hub down", "code": "stale_head"}, code=3)
        answer = self.merge(self.preview()["revision"])
        self.assertEqual(answer["lane"], {"status": "not queued", "error": "hub down"})
        item = self.filed()["id"]
        self.lane({"ok": True, "status": "pending", "item": item})
        again = self.preview()
        self.assertEqual((again["mode"], again["worktree_listed"], again["item"]), ("resume", True, item))
        answer = self.merge(again["revision"])
        self.assertEqual((answer["lane"]["status"], answer["item"], answer["filed"]), ("pending", item, False))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM item WHERE source = 'task-ref'").fetchone()[0], 1)

    def test_an_enqueue_that_gave_no_answer_is_unknown(self):
        self.pull()
        self.lane({"unexpected": True}, code=1)
        self.answer.write_text("not json")
        status, body = self.merge(self.preview()["revision"])
        self.assertEqual((status, body["outcome"]), (502, "unknown"))
        self.assertIn("Copied, but whether the lane queued it is not known", body["error"])
        self.assertEqual(body["item"], self.filed()["id"])

    def test_a_branch_at_another_head_is_refused_naming_both(self):
        self.pull()
        old = self.merge(self.preview()["revision"])["head"]
        new = self.push(5, "pushed again\n")
        status, body = self.preview()
        self.assertEqual(status, 409, body)
        self.assertIn(f"pr/5 holds {old[:12]}, but {REPO}#5's head is {new[:12]}", body["error"])

    def test_a_folder_in_the_worktrees_place_is_refused(self):
        self.pull()
        self.tree().mkdir(parents=True)
        status, body = self.preview()
        self.assertEqual(status, 409, body)
        self.assertIn("is not pr/5's worktree", body["error"])

    def test_a_copy_the_lane_merged_is_not_enqueued_again(self):
        self.pull()
        answer = self.merge(self.preview()["revision"])
        entry = {"id": "anvil", "item": answer["item"], "worktree": answer["worktree"], "branch": "pr/5",
                 "status": "merged", "head": answer["head"], "repository": REPO}
        self.connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                                (f"lane:v1:{REPO}:anvil", "2026-10-21T20:00:00Z", json.dumps(entry)))
        self.connection.commit()
        status, body = self.preview()
        self.assertEqual(status, 409, body)
        self.assertIn("already merged", body["error"])


class Refusals(Copy, ScreenCase):
    def test_a_head_that_does_not_read_is_unknown_and_moves_nothing(self):
        path, _ = self.pull()
        git("-C", str(path), "config", "--unset", f"url.{self.root / 'anvil.git'}.insteadOf")
        git("-C", str(path), "config", f"url.{self.root / 'gone.git'}.insteadOf", REMOTE)
        status, body = self.preview()
        self.assertEqual(status, 502, body)
        self.assertIn("not read", body["error"])

    def test_a_pull_request_not_open_or_not_in_a_registered_repository_or_another_origin_is_refused(self):
        path, _ = self.pull()
        upsert_shadow(self.connection, tracker="github", url=f"https://github.com/{REPO}/pull/6", repo=REPO, number=6,
                      kind="pull", title="Old", state="merged", author="bo")
        upsert_shadow(self.connection, tracker="github", url="https://github.com/other/tool/pull/1", repo="other/tool",
                      number=1, kind="pull", title="Upstream", state="open", author="bo")
        self.connection.commit()
        self.assertIn("not an open pull request", self.preview(6)[1]["error"])
        self.assertIn("not an open pull request", self.preview(99)[1]["error"])
        self.assertIn("not a registered repository", self.preview(1, "other/tool")[1]["error"])
        git("-C", str(path), "config", "remote.origin.url", "git@github.com:example/forge.git")
        status, body = self.preview()
        self.assertEqual(status, 409)
        self.assertIn("not example/anvil", body["error"])

    def test_a_done_item_under_the_ref_is_refused(self):
        self.pull()
        item = self.item("Old copy", status="done")
        writes.set_item_fields(self.connection, item, source="task-ref", external_id=f"github:{REPO}#5")
        self.connection.commit()
        status, body = self.preview()
        self.assertEqual(status, 409)
        self.assertIn(f"sd:{item}, filed under github:{REPO}#5, is done", body["error"])

    def test_the_request_takes_a_repo_a_number_and_a_revision_and_nothing_else(self):
        good = {"repo": REPO, "number": 5, "revision": "a" * 64}
        pr_merge.request(good, "test")
        for values in ({}, {**good, "number": "5"}, {**good, "number": 0}, {**good, "number": True},
                       {**good, "repo": "../x"}, {**good, "revision": "x"}, {**good, "claim": "deliver"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                pr_merge.request(values, "test")


class Route(Copy, BrowserSession):
    def test_the_page_previews_with_a_get_and_merges_with_a_post_which_the_journal_records(self):
        _, head = self.pull()
        status, _, body = self.request(f"/api/today/github/merge/preview?repo={REPO}&number=5",
                                       headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        found = json.loads(body)
        self.assertEqual(found["head"], head)
        self.assertEqual(self.post("/api/today/github/merge", {"repo": REPO, "number": 5})[0], 400)
        status, _, body = self.post("/api/today/github/merge", {"repo": REPO, "number": 5, "revision": found["revision"]})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["branch"], body["lane"]["status"]), ("pr/5", "pending"))
        self.assertEqual(command_journal.command("/api/today/github/merge", {"repo": REPO, "number": 5})[0].label, "Merge")
