"""The Experiments view's reads and its Build deck run (pack sd:5525).

`research_screen.experiment` reads a checkout's status brief, `30-brief/BRIEF-status.md`: the glance rows, the Asks table and
whether `30-brief/deck.json` sits beside it and when its deck was built; `stale` marks a brief whose Updated day is over 7
days old. `research_deck` queues one `sd-research-kit deck` in a checkout in `publish_drain`'s run shape; a stand-in kit plays
it here.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import research_deck, research_screen
from test_workflow_actions import BrowserSession

BRIEF = """# START HERE — Example Revamp: Status

Compiled 2026-10-20.

---

## 1. Status at a glance

| | |
|---|---|
| **State** | 🟡 Waiting on review |
| **Updated** | 2026-10-20 |
| **In one line** | Research is done. **11** pull requests are open; see [the list](https://example.test/pulls). |
| **Experiment** | [Example: execution](https://example.test/exp/2), iteration 2. Iteration 1, [Example: discovery](https://example.test/exp/1) |
| **Hypothesis** | Small changes are enough. |
| **Deck** | [Status deck, 2026-10-20](https://example.test/deck) |

## 2. What we learned

| Who | What |
|---|---|
| Not an ask | This table is in another section |

## 5. Asks

| Who | What |
|---|---|
| Service owners | Review [#1](https://example.test/pull/1) to #11 |
| Docs owner | Review docs#2 |

## 6. Next

1. Land them.
"""

#: A stand-in for `sd-research-kit`: writes the deck in its working directory, fails, or hangs, as FAKE_MODE says.
FAKE_KIT = """import os, sys, time
mode = os.environ.get("FAKE_MODE", "write")
print("args:", *sys.argv[1:], flush=True)
print("cwd:", os.getcwd(), flush=True)
print("trigger:", os.environ.get("SD_RESEARCH_TRIGGER"), flush=True)
if mode == "old":
    # A pack from before the deck verb: its usage text, which does not name the verb.
    print("usage: sd-research-kit render|publish|checklinks|review|pins|fleet-pins|", file=sys.stderr, flush=True)
    print("Every verb acts on the current working directory.", file=sys.stderr)
    print("machine's queue, which is not a repository.", file=sys.stderr)
    sys.exit(1)
if mode == "fail":
    print("sd-research-kit deck: 30-brief/deck.json: slides[0].cards: at most 6 cards", file=sys.stderr)
    sys.exit(1)
if mode == "sleep":
    time.sleep(30)
if mode != "silent":
    os.makedirs("docs/dashboard", exist_ok=True)
    open("docs/dashboard/deck.pptx", "wb").write(b"pptx")
    print("wrote docs/dashboard/deck.pptx: 3 slides")
"""


def brief(root: Path, key: str, text: str = BRIEF, *, deck: bool = True) -> Path:
    repo = root / key
    (repo / "30-brief").mkdir(parents=True)
    (repo / "research.conf.py").write_text("DOCS = []\n", encoding="utf-8")
    (repo / research_screen.BRIEF).write_text(text, encoding="utf-8")
    if deck:
        (repo / research_screen.DECK).write_text("{}", encoding="utf-8")
    return repo


class TheBrief(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def test_the_glance_rows_asks_and_deck_are_read_by_section_name(self):
        found = research_screen.experiment(brief(self.root, "research/alpha"))
        self.assertEqual(found, {
            "state": "Waiting on review", "tone": "caution", "updated": "2026-10-20",
            "line": "Research is done. 11 pull requests are open; see the list.",
            "links": [{"text": "Example: execution", "url": "https://example.test/exp/2"},
                      {"text": "Example: discovery", "url": "https://example.test/exp/1"}],
            "deck_url": "https://example.test/deck", "deck": True, "built": "",
            "asks": [{"who": "Service owners", "what": "Review #1 to #11"}, {"who": "Docs owner", "what": "Review docs#2"}],
            "asks_total": 2, "error": "",
        })

    def test_the_state_word_decides_the_tone_and_any_other_word_is_unknown(self):
        for cell, tone in (("Running", "ok"), ("Blocked on a reviewer", "warning"), ("Done; shipped", "ok"),
                           ("🟢 Shipped; two items wait", "unknown"), ("", "unknown")):
            with self.subTest(cell=cell):
                text = BRIEF.replace("🟡 Waiting on review", cell)
                found = research_screen.experiment(brief(Path(tempfile.mkdtemp(dir=self.root)), "r", text))
                self.assertEqual(found["tone"], tone)

    def test_no_brief_a_linked_brief_and_an_oversized_brief(self):
        repo = self.root / "plain"
        repo.mkdir()
        self.assertIsNone(research_screen.experiment(repo))
        linked = brief(self.root, "linked", deck=False)
        target = self.root / "elsewhere.md"
        target.write_text(BRIEF, encoding="utf-8")
        (linked / research_screen.BRIEF).unlink()
        (linked / research_screen.BRIEF).symlink_to(target)
        self.assertIsNone(research_screen.experiment(linked))
        big = brief(self.root, "big", "x" * (research_screen.REGISTRY_BYTES + 1), deck=False)
        self.assertEqual(research_screen.experiment(big), {"error": f"{research_screen.BRIEF} exceeds the bounded read size",
                                                           "deck": False, "built": ""})

    def test_the_built_deck_is_the_kits_file_not_a_link(self):
        repo = brief(self.root, "research/alpha")
        built = repo / research_screen.BUILT
        built.parent.mkdir(parents=True)
        built.write_bytes(b"pptx")
        os.utime(built, (1792454400, 1792454400))
        self.assertEqual(research_screen.experiment(repo)["built"], "2026-10-20T00:00:00Z")
        self.assertEqual(research_deck.built("research/alpha", self.root), built)
        (self.root / "outside.pptx").write_bytes(b"other")
        built.unlink()
        built.symlink_to(self.root / "outside.pptx")
        self.assertEqual(research_screen.experiment(repo)["built"], "")
        self.assertIsNone(research_deck.built("research/alpha", self.root))
        self.assertIsNone(research_deck.built("../research/alpha", self.root))

    def test_a_brief_with_no_glance_or_asks_reads_empty_not_wrong(self):
        found = research_screen.experiment(brief(self.root, "bare", "# A brief\n\nNo table here.\n", deck=False))
        self.assertEqual((found["state"], found["tone"], found["updated"], found["asks"], found["links"], found["deck"]),
                         ("", "unknown", "", [], [], False))

    def test_a_brief_is_stale_over_seven_days_and_unknown_without_a_day(self):
        rows = [{"experiment": {"updated": day}} for day in ("2026-10-13", "2026-10-12", "", "2026-13-01")] + [
            {"experiment": None}, {}]
        research_screen.stale(rows, "2026-10-20T12:00:00Z")
        self.assertEqual([r["experiment"]["stale"] for r in rows[:4]], [False, True, None, None])
        research_screen.stale(rows, "not a time")
        self.assertIsNone(rows[1]["experiment"]["stale"])

    def test_the_board_carries_the_brief_through_the_child(self):
        brief(self.root, "research/alpha")
        with mock.patch.dict(os.environ, {"REPO_ROOT": str(self.root)}):
            doc = research_screen.document(now="2026-10-30T00:00:00Z")
        self.assertEqual(doc["error"], "")
        [row] = doc["projects"]
        self.assertEqual((row["experiment"]["tone"], row["experiment"]["stale"]), ("caution", True))


class BuildDeck(unittest.TestCase):
    """Build deck: the checkout, the lock, the run state, the runner and the outcome."""

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        bin_dir = self.base / "pack" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "sd-research-kit").write_text(FAKE_KIT, encoding="utf-8")
        self.enterContext(mock.patch("sd_dashboard.pack_bin", return_value=bin_dir))
        self.enterContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state"), "FAKE_MODE": "write"}))
        self.root = self.base / "repos"
        self.repo = brief(self.root, "research/alpha")
        self.spawned = []

    def spawn(self, argv, **options):
        self.spawned.append((argv, options))
        return mock.Mock(wait=lambda: 0)

    def started(self, key="research/alpha", now=None):
        status, run = research_deck.start(key, self.root, now=now, spawn=self.spawn)
        self.assertEqual(status, 202, run)
        return research_deck.state_dir() / "runs" / f"{run['id']}.json"

    def ran(self, mode="write", timeout=None):
        record = self.started()
        if timeout:
            record.write_text(json.dumps({**json.loads(record.read_text(encoding="utf-8")), "timeout": timeout}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FAKE_MODE": mode}):
            code = research_deck.run(record)
        return code, json.loads(record.read_text(encoding="utf-8"))

    def test_the_runner_runs_the_kits_deck_in_the_rows_checkout_with_no_path(self):
        """R10-D6: `sd-research-kit deck` takes no path; it runs in the checkout and writes docs/dashboard/deck.pptx there."""
        record = self.started()
        argv, options = self.spawned[0]
        self.assertEqual((argv[2:4], options["start_new_session"], options["pass_fds"]), (["run", str(record)], True, (int(argv[4]),)))
        code = research_deck.run(record)
        document = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual((code, document["state"], document["reason"], document["repo"]), (0, "done", None, "research/alpha"))
        out = self.repo / "docs" / "dashboard" / "deck.pptx"
        self.assertEqual((document["out"], out.read_bytes()), (str(out), b"pptx"))
        self.assertEqual(record.with_suffix(".log").read_text(encoding="utf-8").splitlines()[:2], ["args: deck", f"cwd: {self.repo}"])
        self.assertNotIn("cwd", research_deck.status(document["id"])[1])

    def test_render_runs_the_kits_render_as_a_dashboard_run_and_needs_no_deck_json(self):
        """sd:5632: Render runs `sd-research-kit render` in the checkout; SD_RESEARCH_TRIGGER makes the kit refuse an unread conf."""
        repo = brief(self.root, "research/nodeck", deck=False)
        status, run = research_deck.start("research/nodeck", self.root, verb="render", spawn=self.spawn)
        self.assertEqual(status, 202, run)
        self.assertTrue(run["id"].startswith("render-"))
        record = research_deck.state_dir() / "runs" / f"{run['id']}.json"
        with mock.patch.dict(os.environ, {"FAKE_MODE": "silent"}):
            code = research_deck.run(record)
        document = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual((code, document["state"], document["reason"]), (0, "done", None))
        self.assertEqual(record.with_suffix(".log").read_text(encoding="utf-8").splitlines()[:3],
                         ["args: render", f"cwd: {repo}", "trigger: dashboard"])
        self.assertEqual(research_deck.status(run["id"])[0], 200)
        with mock.patch.dict(os.environ, {"FAKE_MODE": "fail"}):
            record.unlink()
            status, run = research_deck.start("research/nodeck", self.root, verb="render", spawn=self.spawn)
            record = research_deck.state_dir() / "runs" / f"{run['id']}.json"
            self.assertEqual(research_deck.run(record), 1)
        self.assertTrue(json.loads(record.read_text(encoding="utf-8"))["reason"].startswith("sd-research-kit render exited 1: "))
        self.assertTrue(callable(research_deck.request({"repo": "research/nodeck"}, verb="render")))

    def test_review_runs_the_kits_review_and_keeps_its_report_for_the_page(self):
        """sd:5655: Review runs `sd-research-kit review` in the checkout; the run keeps what it printed, pass or fail."""
        repo = brief(self.root, "research/nodeck", deck=False)
        status, run = research_deck.start("research/nodeck", self.root, verb="review", spawn=self.spawn)
        self.assertEqual(status, 202, run)
        self.assertTrue(run["id"].startswith("review-"))
        record = research_deck.state_dir() / "runs" / f"{run['id']}.json"
        with mock.patch.dict(os.environ, {"FAKE_MODE": "silent"}):
            self.assertEqual(research_deck.run(record), 0)
        code, document = research_deck.status(run["id"])
        self.assertEqual((code, document["state"]), (200, "done"))
        self.assertTrue(document["output"].startswith(f"args: review\ncwd: {repo}\ntrigger: None"))
        with mock.patch.dict(os.environ, {"FAKE_MODE": "fail"}):
            record.unlink()
            status, run = research_deck.start("research/nodeck", self.root, verb="review", spawn=self.spawn)
            record = research_deck.state_dir() / "runs" / f"{run['id']}.json"
            self.assertEqual(research_deck.run(record), 1)
        failed = json.loads(record.read_text(encoding="utf-8"))
        self.assertTrue(failed["reason"].startswith("sd-research-kit review exited 1"))
        self.assertIn("args: review", failed["output"])

    def test_a_checkout_the_board_does_not_serve_or_one_without_a_deck_json_is_refused(self):
        brief(self.root, "research/nodeck", deck=False)
        for key, said in (("research/nodeck", "research/nodeck keeps no 30-brief/deck.json"), ("../escape", "No research checkout"),
                          ("research/gone", "No research checkout")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, said):
                research_deck.start(key, self.root, spawn=self.spawn)
        linked = self.repo / research_screen.DECK
        (self.root / "outside.json").write_text("{}", encoding="utf-8")
        linked.unlink()
        linked.symlink_to(self.root / "outside.json")
        with self.assertRaisesRegex(ValueError, "keeps no 30-brief/deck.json"):
            research_deck.start("research/alpha", self.root, spawn=self.spawn)
        self.assertEqual(self.spawned, [])

    def test_a_request_names_one_checkout(self):
        for values in ({}, {"repo": 1}, {"repo": "research/alpha", "out": "/etc/passwd"}):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "Send"):
                research_deck.request(values)
        self.assertTrue(callable(research_deck.request({"repo": "research/alpha"})))

    def test_a_second_start_while_one_builds_is_refused(self):
        state = research_deck.state_dir()
        state.mkdir(parents=True)
        with open(state / "lock", "a+", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.write("deck-20261020T000000Z")
            held.flush()
            status, answer = research_deck.start("research/alpha", self.root, spawn=self.spawn)
        self.assertEqual(status, 409)
        self.assertIn("deck-20261020T000000Z", answer["error"])
        self.assertEqual(self.spawned, [])

    def test_a_runner_that_does_not_start_reads_failed(self):
        def broken(argv, **options):
            raise OSError("no python")

        with self.assertRaises(OSError):
            research_deck.start("research/alpha", self.root, spawn=broken)
        [record] = (research_deck.state_dir() / "runs").glob("*.json")
        document = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual((document["state"], document["reason"]), ("failed", "the runner did not start: no python"))

    def test_the_runner_runs_as_a_script_on_its_own(self):
        """The real spawn: this file run by path, with no package, as `start` launches it."""
        status, run = research_deck.start("research/alpha", self.root)
        record = research_deck.state_dir() / "runs" / f"{run['id']}.json"
        for _ in range(100):
            if json.loads(record.read_text(encoding="utf-8"))["state"] in ("done", "failed"):
                break
            time.sleep(0.1)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["state"], "done")

    def test_a_kit_that_fails_or_writes_nothing_new_reads_failed(self):
        code, document = self.ran("fail")
        self.assertEqual((code, document["state"]), (1, "failed"))
        self.assertEqual(document["reason"], "sd-research-kit deck exited 1: sd-research-kit deck: 30-brief/deck.json: "
                                             "slides[0].cards: at most 6 cards")
        # An old deck.pptx from an earlier build is not this run's output.
        old = self.repo / research_screen.BUILT
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        os.utime(old, (1_000_000_000, 1_000_000_000))
        research_deck.state_dir().joinpath("runs").joinpath(f"{document['id']}.json").unlink()
        code, document = self.ran("silent")
        self.assertEqual((code, document["state"]), (1, "failed"))
        self.assertEqual("sd-research-kit deck exited 0: trigger: None", document["reason"])

    def test_a_pack_from_before_the_deck_verb_reads_failed_with_its_usage_line(self):
        """Older state: the server's pinned pack may predate the verb; the run names its verbs, not the usage text's last line."""
        code, document = self.ran("old")
        self.assertEqual((code, document["state"]), (1, "failed"))
        self.assertEqual(document["reason"],
                         "sd-research-kit deck exited 1: usage: sd-research-kit render|publish|checklinks|review|pins|fleet-pins|")

    def test_a_build_past_its_bound_is_killed(self):
        code, document = self.ran("sleep", timeout=1)
        self.assertEqual((code, document["state"]), (1, "failed"))
        self.assertIn("did not end within 1 s; its process group was killed", document["reason"])

    def test_status_reads_a_gone_runner_and_a_stuck_queue_as_failed(self):
        record = self.started()
        document = json.loads(record.read_text(encoding="utf-8"))
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=research_deck.STARTUP + 1)
        self.assertEqual(research_deck.status(document["id"], now=later)[1]["reason"],
                         f"the runner never started within {research_deck.STARTUP} s")
        dead = subprocess.Popen(["true"])
        dead.wait()
        record.write_text(json.dumps({**document, "state": "running", "pid": dead.pid}), encoding="utf-8")
        status, read = research_deck.status(document["id"])
        self.assertEqual((status, read["state"]), (200, "failed"))
        self.assertIn("stopped without recording an outcome", read["reason"])
        self.assertNotIn("argv", read)
        self.assertEqual(research_deck.status("../etc")[0], 404)

    def test_latest_is_none_before_the_first_run_then_the_last_run(self):
        self.assertEqual(research_deck.latest(), (200, {"run": None}))
        record = self.started()
        research_deck.run(record)
        status, found = research_deck.latest()
        self.assertEqual((status, found["run"]["state"], found["run"]["id"]), (200, "done", record.stem))


class TheDownload(BrowserSession):
    """`/research/deck/<key>`: the built deck as an attachment, for a page's session only, from a served checkout only."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {"REPO_ROOT": str(self.root)}))
        super().setUp()
        repo = brief(self.root, "research/alpha")
        (repo / "docs" / "dashboard").mkdir(parents=True)
        (repo / research_screen.BUILT).write_bytes(b"pptx")

    def test_the_built_deck_downloads_with_a_session(self):
        status, headers, body = self.request("/research/deck/research/alpha", headers={"Cookie": self.cookie})
        self.assertEqual((status, body), (200, "pptx"))
        self.assertEqual(headers["Content-Type"], "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="research-alpha-deck.pptx"')

    def test_no_session_no_deck_and_no_checkout_are_refused(self):
        self.assertEqual(self.request("/research/deck/research/alpha")[0], 403)
        brief(self.root, "research/beta")
        for path in ("/research/deck/research/beta", "/research/deck/research/gone", "/research/deck/..%2Fetc"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, headers={"Cookie": self.cookie})[0], 404)


if __name__ == "__main__":
    unittest.main()
