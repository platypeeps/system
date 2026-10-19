"""Land (sd:5104): a checkout's uncommitted and unpushed work, one commit per item, then the lane's enqueue.

What this slice promises: a preview moves nothing; the write refuses a revision that differs from the preview's, so
evidence from another checkout or a checkout that changed lands nothing. Each item gets one commit whose message ends
with only `Authored-with: claude/anthropic`. A default-branch checkout is never committed on: Land makes
`land/sd-<n>` and its own worktree, restores the checkout, and moves an ahead default branch back to origin's. A run
stopped after the branch was made is resumed by the next Land, and an enqueue the lane refused is retried by it.

Every test runs real git against fixture repositories under a temporary folder, with a local bare repository as
origin, and a stub in place of `sd-ship`: no test reaches GitHub, the lane or a real checkout.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

from sd_db import workflow

from sd_dashboard import land

from support import ScreenCase
from test_health_cleanup import FIXTURE_GIT, Isolated, git
from test_workflow_actions import BrowserSession

STUB = """#!/bin/sh
printf '%s\\n' "$@" > "{log}"
cat "{answer}"
exit $(cat "{code}")
"""


class Fixture(Isolated):
    def setUp(self):
        super().setUp()
        os.environ.update(FIXTURE_GIT)
        stub = self.root / "sd-ship"
        self.argv_log, self.answer, self.code = self.root / "argv", self.root / "answer.json", self.root / "code"
        stub.write_text(STUB.format(log=self.argv_log, answer=self.answer, code=self.code))
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.lane({"ok": True, "status": "pending", "item": 0})
        patcher = patch("sd_dashboard.queue_screen.command", lambda name: str(stub))
        patcher.start()
        self.addCleanup(patcher.stop)

    def lane(self, answer: dict, code: int = 0) -> None:
        self.answer.write_text(json.dumps(answer))
        self.code.write_text(str(code))

    def sent(self) -> list[str]:
        return self.argv_log.read_text().splitlines()

    def checkout(self, name: str = "alpha") -> Path:
        """A clone of a bare origin with one commit on main, registered in sd-db by its absolute path."""
        origin = self.root / f"{name}.git"
        git("init", "-q", "--bare", "-b", "main", str(origin))
        seed = self.root / f"seed-{name}"
        git("clone", "-q", str(origin), str(seed))
        (seed / "lib").mkdir()
        (seed / "lib" / "a.txt").write_text("a\n")
        (seed / "README").write_text("r\n")
        git("-C", str(seed), "add", "-A")
        git("-C", str(seed), "commit", "-q", "-m", "first")
        git("-C", str(seed), "push", "-q", "origin", "HEAD:main")
        path = self.root / "repos" / name
        git("clone", "-q", str(origin), str(path))
        self.repo(str(path))
        return path

    def preview(self, path: Path, items=None):
        parameters = {"path": [str(path)]} | ({"items": [items]} if items is not None else {})
        return land.preview(self.connection, parameters)

    def land(self, path: Path, items: str, revision: str, claim: str = "deliver"):
        return land.request({"path": str(path), "items": items, "revision": revision, "claim": claim})(self.connection)

    def log(self, path: Path, rev: str) -> list[str]:
        return git("-C", str(path), "log", "--format=%B%x00", rev).split("\0")


class Spec(Fixture, ScreenCase):
    def test_one_item_takes_every_path_and_prefixes_split_the_rest(self):
        self.assertEqual(land.parse_spec("5104"), [(5104, [])])
        self.assertEqual(land.parse_spec("sd:5104: lib/ docs/; 5105"), [(5104, ["lib/", "docs/"]), (5105, [])])

    def test_two_items_without_a_prefix_or_a_path_outside_the_checkout_are_refused(self):
        with self.assertRaisesRegex(ValueError, "Only one item may take the rest"):
            land.parse_spec("5104; 5105")
        with self.assertRaisesRegex(ValueError, "stays inside it"):
            land.parse_spec("5104: ../x")
        with self.assertRaisesRegex(ValueError, "names no item"):
            land.parse_spec("lib/")


class DefaultBranch(Fixture, ScreenCase):
    def test_the_preview_moves_nothing_and_names_the_branch_and_worktree_it_would_make(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        (path / "lib" / "a.txt").write_text("changed\n")
        found = self.preview(path, str(item))
        self.assertEqual((found["mode"], found["land_branch"], found["ready"]), ("branch", f"land/sd-{item}", True))
        self.assertEqual(found["worktree"], str(self.root / "home" / "worktrees" / f"alpha-land-sd-{item}"))
        self.assertEqual(found["groups"], [{"item": item, "title": "Land button", "paths": ["lib/a.txt"]}])
        self.assertIn("lane enqueue --item", found["cli"])
        self.assertEqual(git("-C", str(path), "branch", "--list", "land/*"), "")
        self.assertEqual(git("-C", str(path), "status", "--porcelain"), "M lib/a.txt")

    def test_land_commits_once_per_item_on_a_new_branch_and_leaves_main_clean(self):
        path = self.checkout()
        one, two = self.item("Land button", repo=str(path)), self.item("Cleanup", repo=str(path))
        main = git("-C", str(path), "rev-parse", "HEAD")
        (path / "lib" / "a.txt").write_text("changed\n")
        (path / "lib" / "new.txt").write_text("new\n")
        (path / "README").unlink()
        spec = f"{one}; {two}: README"
        found = self.preview(path, spec)
        answer = self.land(path, spec, found["revision"])
        self.assertTrue(answer["ok"], answer)
        branch, tree = f"land/sd-{one}", Path(found["worktree"])
        messages = self.log(path, f"{main}..{branch}")
        self.assertEqual([m.strip() for m in messages if m.strip()],
                         [f"Cleanup (sd:{two})\n\nAuthored-with: claude/anthropic",
                          f"Land button (sd:{one})\n\nAuthored-with: claude/anthropic"])
        self.assertEqual(git("-C", str(path), "rev-parse", "HEAD"), main)
        self.assertEqual(git("-C", str(path), "status", "--porcelain"), "")
        self.assertEqual((tree / "lib" / "new.txt").read_text(), "new\n")
        self.assertFalse((tree / "README").exists())
        sent = self.sent()
        tip = git("-C", str(path), "rev-parse", branch)
        self.assertEqual(sent[:5], ["-C", str(tree), "lane", "enqueue", "--item"])
        self.assertEqual(sent[sent.index("--expected-head") + 1], tip)
        self.assertEqual([sent[i + 1] for i, a in enumerate(sent) if a == "--item"], [str(one), str(two)])
        self.assertIn("--deliver", sent)
        self.assertEqual(answer["lane"]["status"], "pending")

    def test_unpushed_commits_on_main_move_to_the_branch_and_main_goes_back_to_origin(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        origin = git("-C", str(path), "rev-parse", "origin/main")
        (path / "README").write_text("ahead\n")
        git("-C", str(path), "commit", "-qam", f"local work (sd:{item})")
        ahead = git("-C", str(path), "rev-parse", "HEAD")
        found = self.preview(path)
        self.assertEqual((found["suggested"], found["spec"]), (str(item), str(item)))
        answer = self.land(path, found["spec"], found["revision"])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(git("-C", str(path), "rev-parse", "HEAD"), origin)
        self.assertEqual(git("-C", str(path), "rev-parse", f"land/sd-{item}"), ahead)
        self.assertIn("moved back", answer["reset"])

    def test_a_checkout_changed_after_the_preview_lands_nothing(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        (path / "lib" / "a.txt").write_text("changed\n")
        found = self.preview(path, str(item))
        (path / "lib" / "b.txt").write_text("later\n")
        with self.assertRaises(workflow.StaleItem):
            self.land(path, str(item), found["revision"])
        self.assertEqual(git("-C", str(path), "branch", "--list", "land/*"), "")
        self.assertFalse(self.argv_log.exists())

    def test_a_revision_from_another_checkout_is_refused(self):
        alpha, beta = self.checkout("alpha"), self.checkout("beta")
        item = self.item("Land button", repo=str(alpha))
        for path in (alpha, beta):
            (path / "lib" / "a.txt").write_text("changed\n")
        with self.assertRaises(workflow.StaleItem):
            self.land(alpha, str(item), self.preview(beta, str(item))["revision"])
        self.assertEqual(git("-C", str(alpha), "branch", "--list", "land/*"), "")

    def test_a_path_no_item_takes_is_named_and_refused(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        (path / "lib" / "a.txt").write_text("changed\n")
        (path / "README").write_text("changed\n")
        found = self.preview(path, f"{item}: lib/")
        self.assertEqual((found["ready"], found["left"]), (False, ["README"]))
        with self.assertRaisesRegex(ValueError, "Every change needs an item"):
            self.land(path, f"{item}: lib/", found["revision"])

    def test_a_done_item_and_an_unregistered_checkout_are_refused(self):
        path = self.checkout()
        item = self.item("Old", repo=str(path), status="done")
        (path / "README").write_text("changed\n")
        self.assertEqual(self.preview(path, str(item))[0], 409)
        self.assertIn("is done", self.preview(path, str(item))[1]["error"])
        self.assertIn("not a registered repository", self.preview(self.root / "nowhere", "1")[1]["error"])

    def test_nothing_to_land_is_a_refusal(self):
        path = self.checkout()
        status, body = self.preview(path)
        self.assertEqual(status, 409)
        self.assertIn("nothing to land", body["error"])


class Resume(Fixture, ScreenCase):
    def test_a_run_stopped_after_the_branch_is_finished_by_the_next_land(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        (path / "lib" / "a.txt").write_text("changed\n")
        found = self.preview(path, str(item))
        with patch.object(land, "_restore", side_effect=land.Unknown("stopped")):
            status, body = self.land(path, str(item), found["revision"])
        self.assertEqual((status, body["outcome"]), (502, "unknown"))
        self.assertEqual(git("-C", str(path), "status", "--porcelain"), "M lib/a.txt")
        again = self.preview(path, str(item))
        self.assertEqual((again["mode"], again["ready"]), ("resume", True))
        answer = self.land(path, str(item), again["revision"])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(git("-C", str(path), "status", "--porcelain"), "")
        self.assertEqual(len([m for m in self.log(path, f"origin/main..land/sd-{item}") if m.strip()]), 1)
        self.assertIn("enqueue", self.sent())

    def test_a_run_stopped_before_the_worktree_adds_it_on_the_next_land(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        (path / "lib" / "a.txt").write_text("changed\n")
        found = self.preview(path, str(item))
        real = land._git

        def stop(where, *args, **options):
            if args[:2] == ("worktree", "add"):
                raise land.Unknown("stopped")
            return real(where, *args, **options)
        with patch.object(land, "_git", side_effect=stop):
            self.assertEqual(self.land(path, str(item), found["revision"])[0], 502)
        again = self.preview(path, str(item))
        self.assertTrue(self.land(path, str(item), again["revision"])["ok"])
        self.assertEqual((Path(again["worktree"]) / "lib" / "a.txt").read_text(), "changed\n")
        self.assertEqual(git("-C", str(path), "status", "--porcelain"), "")

    def test_a_run_stopped_before_main_moved_back_moves_it_on_the_next_land(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        origin = git("-C", str(path), "rev-parse", "origin/main")
        (path / "README").write_text("ahead\n")
        git("-C", str(path), "commit", "-qam", f"local work (sd:{item})")
        found = self.preview(path)
        real = land._git

        def stop(where, *args, **options):
            if args[:2] == ("reset", "-q") and "--keep" in args:
                raise land.Unknown("stopped")
            return real(where, *args, **options)
        with patch.object(land, "_git", side_effect=stop):
            self.assertEqual(self.land(path, found["spec"], found["revision"])[0], 502)
        again = self.preview(path)
        self.assertEqual(again["mode"], "resume")
        self.assertTrue(self.land(path, again["spec"], again["revision"])["ok"])
        self.assertEqual(git("-C", str(path), "rev-parse", "HEAD"), origin)

    def test_an_enqueue_the_lane_refused_keeps_the_commits_and_the_next_land_enqueues_again(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        (path / "lib" / "a.txt").write_text("changed\n")
        found = self.preview(path, str(item))
        self.lane({"ok": False, "error": "hub down", "code": "stale_head"}, code=3)
        answer = self.land(path, str(item), found["revision"])
        self.assertEqual(answer["lane"], {"status": "not queued", "error": "hub down"})
        self.lane({"ok": True, "status": "pending", "item": item})
        again = self.preview(path, str(item))
        self.assertEqual(again["mode"], "resume")
        self.assertEqual(self.land(path, str(item), again["revision"])["lane"]["status"], "pending")

    def test_a_body_file_that_could_not_be_written_before_the_enqueue_is_unknown_and_names_the_commits(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        (path / "lib" / "a.txt").write_text("changed\n")
        found = self.preview(path, str(item))
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            status, body = self.land(path, str(item), found["revision"])
        self.assertEqual((status, body["outcome"], len(body["commits"])), (502, "unknown", 1))
        self.assertIn("disk full", body["error"])
        self.assertFalse(self.argv_log.exists(), "the lane was asked without a body file")
        self.assertTrue(self.land(path, str(item), self.preview(path, str(item))["revision"])["ok"])

    def test_a_branch_that_does_not_hold_this_head_is_another_landing_and_is_refused(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        git("-C", str(path), "branch", f"land/sd-{item}", "HEAD")
        (path / "README").write_text("ahead\n")
        git("-C", str(path), "commit", "-qam", "more")
        status, body = self.preview(path, str(item))
        self.assertEqual(status, 409, body)
        self.assertIn("another landing", body["error"])


class TreeState(Fixture, ScreenCase):
    """Land ships the working files, so every other view of the tree must agree with them or Land refuses first:
    staged content the working file does not hold, an unmerged path, a merge in progress, an item with no work."""

    def checkout_on(self, mode: str, item: int, name: str = "alpha") -> Path:
        path = self.checkout(name)
        if mode == "in place":
            git("-C", str(path), "switch", "-q", "-c", f"sd-{item}-land")
        return path

    def test_a_staged_change_the_working_file_reverted_is_refused_and_kept_in_both_modes(self):
        item = self.item("Land button")
        for mode, name in (("branch", "alpha"), ("in place", "beta")):
            with self.subTest(mode=mode):
                path = self.checkout_on(mode, item, name)
                (path / "README").write_text("staged\n")
                git("-C", str(path), "add", "README")
                (path / "README").write_text("r\n")
                status, body = self.preview(path, str(item))
                self.assertEqual(status, 409, body)
                self.assertIn("README", body["error"])
                self.assertEqual(git("-C", str(path), "show", ":README"), "staged")
                git("-C", str(path), "add", "README")
                self.assertEqual(self.preview(path, str(item))[0], 409, "the index now matches HEAD: nothing to land")

    def test_a_staged_delete_with_the_file_still_on_disk_is_refused(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        git("-C", str(path), "rm", "-q", "--cached", "README")
        status, body = self.preview(path, str(item))
        self.assertEqual(status, 409, body)
        self.assertIn("README", body["error"])

    def test_a_partly_staged_file_lands_once_the_index_and_the_file_agree(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        (path / "README").write_text("staged\n")
        git("-C", str(path), "add", "README")
        (path / "README").write_text("staged\nand more\n")
        self.assertEqual(self.preview(path, str(item))[0], 409)
        git("-C", str(path), "add", "README")
        found = self.preview(path, str(item))
        self.assertTrue(self.land(path, str(item), found["revision"])["ok"])
        self.assertEqual(git("-C", str(path), "show", f"land/sd-{item}:README"), "staged\nand more")

    def test_an_unmerged_path_or_a_merge_in_progress_is_refused(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        git("-C", str(path), "switch", "-q", "-c", "other")
        (path / "README").write_text("other\n")
        git("-C", str(path), "commit", "-qam", "other")
        git("-C", str(path), "switch", "-q", "main")
        (path / "README").write_text("main\n")
        git("-C", str(path), "commit", "-qam", f"main (sd:{item})")
        subprocess.run(["git", "-C", str(path), "merge", "-q", "other"], capture_output=True, check=False)
        status, body = self.preview(path, str(item))
        self.assertEqual(status, 409, body)
        self.assertIn("merge", body["error"])
        (path / "README").write_text("both\n")
        git("-C", str(path), "add", "README")
        status, body = self.preview(path, str(item))
        self.assertEqual(status, 409, body)
        self.assertIn("MERGE_HEAD", body["error"])

    def test_a_staged_rename_lands_as_one_delete_and_one_add(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        git("-C", str(path), "mv", "lib/a.txt", "lib/b.txt")
        found = self.preview(path, str(item))
        self.assertEqual(found["groups"][0]["paths"], ["lib/a.txt", "lib/b.txt"])
        self.assertTrue(self.land(path, str(item), found["revision"])["ok"])
        tree = Path(found["worktree"])
        self.assertEqual(((tree / "lib" / "a.txt").exists(), (tree / "lib" / "b.txt").read_text()), (False, "a\n"))
        self.assertEqual(git("-C", str(path), "status", "--porcelain"), "")

    def test_an_item_that_takes_no_path_and_no_commit_names_is_refused(self):
        path = self.checkout()
        one, two = self.item("Land button", repo=str(path)), self.item("Docs", repo=str(path))
        (path / "lib" / "a.txt").write_text("changed\n")
        status, body = self.preview(path, f"{one}; {two}: docs/")
        self.assertEqual(status, 409, body)
        self.assertIn(f"sd:{two} takes no changed path", body["error"])

    def test_a_file_staged_apart_before_the_restore_is_kept(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        (path / "lib" / "a.txt").write_text("changed\n")
        found = self.preview(path, str(item))
        real = land._restore

        def staged(where, head, tip, names):
            (path / "lib" / "a.txt").write_text("staged\n")
            git("-C", str(path), "add", "lib/a.txt")
            (path / "lib" / "a.txt").write_text("changed\n")
            return real(where, head, tip, names)
        with patch.object(land, "_restore", side_effect=staged):
            answer = self.land(path, str(item), found["revision"])
        self.assertEqual(answer["kept"], ["lib/a.txt"])
        self.assertEqual(git("-C", str(path), "show", ":lib/a.txt"), "staged")

    def test_a_file_staged_apart_before_the_in_place_ref_moves_lands_nothing(self):
        item = self.item("Land button")
        path = self.checkout_on("in place", item)
        head = git("-C", str(path), "rev-parse", "HEAD")
        (path / "lib" / "a.txt").write_text("changed\n")
        found = self.preview(path)
        real = land._commit_chain

        def staged(*args):
            made = real(*args)
            (path / "README").write_text("staged\n")
            git("-C", str(path), "add", "README")
            (path / "README").write_text("r\n")
            return made
        with patch.object(land, "_commit_chain", side_effect=staged):
            with self.assertRaisesRegex(ValueError, "README"):
                self.land(path, found["spec"], found["revision"])
        self.assertEqual(git("-C", str(path), "rev-parse", "HEAD"), head)
        self.assertEqual(git("-C", str(path), "show", ":README"), "staged")


class ResumeEvidence(Fixture, ScreenCase):
    """A land branch is resumed only when it holds every current change and names every item, and the lane has not
    merged it already: the branch existing is not evidence of the same landing."""

    def refused_once(self, path: Path, spec: str) -> dict:
        found = self.preview(path, spec)
        self.lane({"ok": False, "error": "hub down", "code": "stale_head"}, code=3)
        self.assertEqual(self.land(path, spec, found["revision"])["lane"]["status"], "not queued")
        self.lane({"ok": True, "status": "pending", "item": 0})
        self.argv_log.unlink()
        return found

    def test_new_work_after_a_refused_enqueue_is_named_and_not_enqueued(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        (path / "lib" / "a.txt").write_text("changed\n")
        self.refused_once(path, str(item))
        (path / "README").write_text("later\n")
        status, body = self.preview(path, str(item))
        self.assertEqual(status, 409, body)
        self.assertIn("README", body["error"])
        self.assertFalse(self.argv_log.exists())

    def test_a_resume_naming_an_item_no_landing_commit_names_is_refused(self):
        path = self.checkout()
        one, two = self.item("Land button", repo=str(path)), self.item("Docs", repo=str(path))
        (path / "lib" / "a.txt").write_text("changed\n")
        self.refused_once(path, str(one))
        status, body = self.preview(path, f"{one}; {two}: docs/")
        self.assertEqual(status, 409, body)
        self.assertIn(f"sd:{two} takes no changed path", body["error"])

    def test_a_landing_the_lane_already_merged_is_not_enqueued_again(self):
        item = self.item("Land button")
        for name, feature in (("alpha", False), ("beta", True)):
            with self.subTest(feature=feature):
                path = self.checkout(name)
                if feature:
                    git("-C", str(path), "switch", "-q", "-c", f"sd-{item}-land")
                (path / "lib" / "a.txt").write_text("changed\n")
                found = self.preview(path, str(item))
                answer = self.land(path, str(item), found["revision"], claim="associate-only")
                body = {"id": name, "item": item, "worktree": found["worktree"], "branch": answer["branch"],
                        "status": "merged", "head": answer["head"], "repository": f"example/{name}"}
                self.connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                                        (f"lane:v1:example/{name}:{name}", "2026-10-18T20:00:00Z", json.dumps(body)))
                self.connection.commit()
                status, body = self.preview(path, str(item))
                self.assertEqual(status, 409, body)
                self.assertIn("already merged", body["error"])


class FeatureBranch(Fixture, ScreenCase):
    def test_a_feature_branch_is_committed_in_place_and_enqueued_from_the_checkout(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        git("-C", str(path), "switch", "-q", "-c", f"sd-{item}-land")
        (path / "lib" / "a.txt").write_text("changed\n")
        found = self.preview(path)
        self.assertEqual((found["mode"], found["spec"]), ("in place", str(item)))
        answer = self.land(path, found["spec"], found["revision"], claim="associate-only")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(git("-C", str(path), "status", "--porcelain"), "")
        self.assertEqual(git("-C", str(path), "log", "-1", "--format=%s"), f"Land button (sd:{item})")
        self.assertEqual(self.sent()[:2], ["-C", str(path)])
        self.assertIn("--associate-only", self.sent())

    def test_a_branch_moved_after_the_read_is_refused_by_the_ref_update(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        git("-C", str(path), "switch", "-q", "-c", f"sd-{item}-land")
        (path / "lib" / "a.txt").write_text("changed\n")
        found = self.preview(path)
        real = land._read

        def moved(*args):
            facts = real(*args)
            git("-C", str(path), "commit", "-q", "--allow-empty", "-m", "moved")
            return facts
        with patch.object(land, "_read", side_effect=moved):
            status, body = self.land(path, found["spec"], found["revision"])
        self.assertEqual((status, body["outcome"]), (502, "unknown"))
        self.assertEqual(git("-C", str(path), "log", "-1", "--format=%s"), "moved")


class InPlaceSteps(Fixture, ScreenCase):
    def test_a_run_stopped_after_the_ref_moved_refreshes_the_index_without_an_empty_commit(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        git("-C", str(path), "switch", "-q", "-c", f"sd-{item}-land")
        (path / "lib" / "a.txt").write_text("changed\n")
        found = self.preview(path)
        real = land._git

        def stop(where, *args, **options):
            if args == ("reset", "-q"):
                raise land.Unknown("stopped")
            return real(where, *args, **options)
        with patch.object(land, "_git", side_effect=stop):
            self.assertEqual(self.land(path, found["spec"], found["revision"])[0], 502)
        landed = git("-C", str(path), "rev-parse", "HEAD")
        again = self.preview(path)
        answer = self.land(path, again["spec"], again["revision"])
        self.assertEqual((answer["commits"], answer["head"]), ([], landed))
        self.assertEqual(git("-C", str(path), "status", "--porcelain"), "")


class Route(Fixture, BrowserSession):
    def test_the_page_previews_with_a_get_and_lands_with_a_post(self):
        path = self.checkout()
        item = self.item("Land button", repo=str(path))
        (path / "lib" / "a.txt").write_text("changed\n")
        from urllib.parse import quote
        status, _, body = self.request(f"/api/repos/land/preview?path={quote(str(path))}&items={item}",
                                       headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        found = json.loads(body)
        status, _, body = self.post("/api/repos/land", {"path": str(path), "items": str(item),
                                                        "revision": found["revision"], "claim": "deliver"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["branch"], f"land/sd-{item}")
