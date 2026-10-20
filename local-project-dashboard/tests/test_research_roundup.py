"""The Experiments view's reads and its Build roundup run (pack sd:5525).

`research_screen.experiment` reads a checkout's status brief, `30-brief/BRIEF-status.md`: the glance rows, the Asks table and
whether `30-brief/deck.json` sits beside it; `stale` marks a brief whose Updated day is over 7 days old. `research_roundup`
queues one `sd-research-kit roundup` over every listed deck.json in `publish_drain`'s run shape; a stand-in kit plays it here.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import research_roundup, research_screen

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
| **Deck** | [Roundup, 2026-10-20](https://example.test/deck) |

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

#: A stand-in for `sd-research-kit`: writes the deck it is asked for, fails, or hangs, as FAKE_MODE says.
FAKE_KIT = """import os, sys, time
mode = os.environ.get("FAKE_MODE", "write")
print("args:", *sys.argv[1:], flush=True)
if mode == "old":
    # A pack from before the roundup verb: its usage text, which does not name the verb.
    print("usage: sd-research-kit render|publish|checklinks|review|pins|fleet-pins|", file=sys.stderr, flush=True)
    print("Every verb acts on the current working directory.", file=sys.stderr)
    print("machine's queue, which is not a repository.", file=sys.stderr)
    sys.exit(1)
if mode == "fail":
    print("sd-research-kit roundup: /r/a/30-brief/deck.json: slides[0].cards: at most 6 cards", file=sys.stderr)
    sys.exit(1)
if mode == "sleep":
    time.sleep(30)
if mode != "silent":
    os.makedirs("docs/dashboard")
    open("docs/dashboard/roundup.pptx", "wb").write(b"pptx")
    print("wrote docs/dashboard/roundup.pptx: 4 slides from 2 decks")
    print("  /r/beta/30-brief/deck.json")
    print("  /r/alpha/30-brief/deck.json")
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
            "deck_url": "https://example.test/deck", "deck": True,
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
                                                           "deck": False})

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


class Roundup(unittest.TestCase):
    """Build roundup: the decks, the lock, the run state, the runner and the outcome."""

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        bin_dir = self.base / "pack" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "sd-research-kit").write_text(FAKE_KIT, encoding="utf-8")
        self.enterContext(mock.patch("sd_dashboard.pack_bin", return_value=bin_dir))
        self.enterContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state"), "FAKE_MODE": "write"}))
        self.root = self.base / "repos"
        self.spawned = []

    def spawn(self, argv, **options):
        self.spawned.append((argv, options))
        return mock.Mock(wait=lambda: 0)

    def board(self, *rows):
        return {"projects": [{"key": key, "experiment": {"deck": True, "updated": day}} for key, day in rows]}

    def started(self, inputs=None, now=None):
        status, run = research_roundup.start(inputs or [self.base / "a.json"], now=now, spawn=self.spawn)
        self.assertEqual(status, 202, run)
        return research_roundup.state_dir() / "runs" / f"{run['id']}.json"

    def ran(self, mode="write", timeout=None):
        record = self.started()
        if timeout:
            record.write_text(json.dumps({**json.loads(record.read_text(encoding="utf-8")), "timeout": timeout}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FAKE_MODE": mode}):
            code = research_roundup.run(record)
        return code, json.loads(record.read_text(encoding="utf-8"))

    def test_the_decks_are_the_served_checkouts_newest_updated_first(self):
        for key in ("research/old", "research/new", "research/tie", "research/nodeck"):
            brief(self.root, key, deck=key != "research/nodeck")
        found = research_roundup.decks(self.board(("research/old", "2026-10-01"), ("research/tie", "2026-10-20"),
                                                  ("research/new", "2026-10-20"), ("research/nodeck", "2026-10-21"),
                                                  ("research/gone", "2026-10-22"), ("../escape", "2026-10-23")), self.root)
        self.assertEqual([p.parent.parent.name for p in found], ["new", "tie", "old"])

    def test_no_deck_is_refused_and_nothing_starts(self):
        with self.assertRaisesRegex(ValueError, "No research checkout keeps a 30-brief/deck.json"):
            research_roundup.start([], spawn=self.spawn)
        self.assertEqual(self.spawned, [])

    def test_a_request_takes_no_fields(self):
        with self.assertRaisesRegex(ValueError, "Send no fields"):
            research_roundup.request({"decks": ["/etc/passwd"]})

    def test_a_second_start_while_one_builds_is_refused(self):
        state = research_roundup.state_dir()
        state.mkdir(parents=True)
        with open(state / "lock", "a+", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.write("roundup-20261020T000000Z")
            held.flush()
            status, answer = research_roundup.start([self.base / "a.json"], spawn=self.spawn)
        self.assertEqual(status, 409)
        self.assertIn("roundup-20261020T000000Z", answer["error"])
        self.assertEqual(self.spawned, [])

    def test_a_runner_that_does_not_start_reads_failed(self):
        def broken(argv, **options):
            raise OSError("no python")

        with self.assertRaises(OSError):
            research_roundup.start([self.base / "a.json"], spawn=broken)
        [record] = (research_roundup.state_dir() / "runs").glob("*.json")
        document = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual((document["state"], document["reason"]), ("failed", "the runner did not start: no python"))

    def test_the_runner_holds_the_lock_and_runs_the_kit_with_no_path(self):
        """R10-D6: the kit takes no path; it walks the checkouts itself, and the run keeps the decks it lists."""
        record = self.started([self.base / "a.json", self.base / "b.json"])
        argv, options = self.spawned[0]
        self.assertEqual((argv[2:4], options["start_new_session"], options["pass_fds"]), (["run", str(record)], True, (int(argv[4]),)))
        code = research_roundup.run(record)
        document = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual((code, document["state"], document["reason"]), (0, "done", None))
        out = record.with_suffix("") / "docs" / "dashboard" / "roundup.pptx"
        self.assertEqual((document["out"], out.read_bytes()), (str(out), b"pptx"))
        self.assertEqual(document["decks"], ["/r/beta/30-brief/deck.json", "/r/alpha/30-brief/deck.json"])
        self.assertEqual(record.with_suffix(".log").read_text(encoding="utf-8").splitlines()[0], "args: roundup")
        self.assertNotIn("cwd", research_roundup.status(document["id"])[1])

    def test_the_runner_runs_as_a_script_on_its_own(self):
        """The real spawn: this file run by path, with no package, as `start` launches it."""
        status, run = research_roundup.start([self.base / "a.json"])
        record = research_roundup.state_dir() / "runs" / f"{run['id']}.json"
        for _ in range(100):
            if json.loads(record.read_text(encoding="utf-8"))["state"] in ("done", "failed"):
                break
            time.sleep(0.1)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["state"], "done")

    def test_a_kit_that_fails_or_writes_nothing_reads_failed_with_its_last_line(self):
        code, document = self.ran("fail")
        self.assertEqual((code, document["state"]), (1, "failed"))
        self.assertEqual(document["reason"], "sd-research-kit roundup exited 1: sd-research-kit roundup: /r/a/30-brief/deck.json: "
                                             "slides[0].cards: at most 6 cards")
        research_roundup.state_dir().joinpath("runs").joinpath(f"{document['id']}.json").unlink()
        shutil.rmtree(research_roundup.state_dir() / "runs" / document["id"])
        code, document = self.ran("silent")
        self.assertEqual((code, document["state"]), (1, "failed"))
        self.assertIn("sd-research-kit roundup exited 0: args: roundup", document["reason"])

    def test_a_pack_from_before_the_roundup_verb_reads_failed_with_its_usage_line(self):
        """Older state: the server's pinned pack may predate the verb; the run names its verbs, not the usage text's last line."""
        code, document = self.ran("old")
        self.assertEqual((code, document["state"]), (1, "failed"))
        self.assertEqual(document["reason"],
                         "sd-research-kit roundup exited 1: usage: sd-research-kit render|publish|checklinks|review|pins|fleet-pins|")

    def test_a_build_past_its_bound_is_killed(self):
        code, document = self.ran("sleep", timeout=1)
        self.assertEqual((code, document["state"]), (1, "failed"))
        self.assertIn("did not end within 1 s; its process group was killed", document["reason"])

    def test_status_reads_a_gone_runner_and_a_stuck_queue_as_failed(self):
        record = self.started()
        document = json.loads(record.read_text(encoding="utf-8"))
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=research_roundup.STARTUP + 1)
        self.assertEqual(research_roundup.status(document["id"], now=later)[1]["reason"],
                         f"the runner never started within {research_roundup.STARTUP} s")
        dead = subprocess.Popen(["true"])
        dead.wait()
        record.write_text(json.dumps({**document, "state": "running", "pid": dead.pid}), encoding="utf-8")
        status, read = research_roundup.status(document["id"])
        self.assertEqual((status, read["state"]), (200, "failed"))
        self.assertIn("stopped without recording an outcome", read["reason"])
        self.assertNotIn("argv", read)
        self.assertEqual(research_roundup.status("../etc")[0], 404)

    def test_latest_is_none_before_the_first_run_then_the_last_run(self):
        self.assertEqual(research_roundup.latest(), (200, {"run": None}))
        record = self.started()
        research_roundup.run(record)
        status, found = research_roundup.latest()
        self.assertEqual((status, found["run"]["state"], found["run"]["id"]), (200, "done", record.stem))


if __name__ == "__main__":
    unittest.main()
