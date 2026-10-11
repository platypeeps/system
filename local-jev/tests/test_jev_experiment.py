"""Jev experiment entry 2 (sd:2764): the steps `jev_experiment.py` runs.

Every checkpoint here is a fixture in a database of its own, and every Jev
call goes to the loopback stub `test_jev` serves, so no case reads the
operator's workflow database or reaches TypeSafe.
"""

import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import jev_experiment as experiment

from .test_jev import Stub, StubServer
from .test_jev_metering import initialise
from .test_jev_compare import SD_DB_PATH

#: The pack's `bin/sd_jev.py` `_jev_state` and `_jev_criteria` output at
#: b6b58bb14 for the inputs in `TheRebuiltState`, as SHA-256.
PACK_STATE_SHA256 = "df9cf37a23d86345b98fdd7b06340f45d325460541a07967bfd17bd734fd207b"
PACK_CRITERIA_SHA256 = "4de730c7968e4e31558dfc3f3e8cccb454d2e517b35e555f672c03240df59b42"


def checkpoint_db(rows) -> Path:
    """A database with the `state` columns the steps read, holding `rows`."""
    path = Path(tempfile.mkdtemp()) / "sd.db"
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, kind TEXT,"
                           " key TEXT, timestamp TEXT, body TEXT, resolved_at TEXT)")
        for key, stamp, body in rows:
            connection.execute(
                "INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                (key, stamp, body if isinstance(body, str) else json.dumps(body)))
    return path


def a_pass(head, routed="standard", tier="standard", paths=("a.py",), lines=3,
           reason="no category matched; default tier standard", below=None):
    jev = {"routed_tier": routed, "tier": tier, "moved": tier != routed, "source": "judged"}
    if below:
        jev["below_routed"] = below
    return {"head": head, "report": {
        "jev": jev, "subject": {"head": head, "paths": list(paths), "lines": lines},
        "route": {"tier": routed, "reason": f"{reason}; Jev read the diff and chose tier {tier}"}}}


def run(argv, env=None):
    out = io.StringIO()
    code = experiment.main(argv, out=out, env=env or {})
    return code, out.getvalue()


class TheReconciliation(unittest.TestCase):
    """Step 0: each extraction counts a head once, as it defines a head."""

    def test_a_later_pass_at_a_later_head_counts_once_under_each_extraction(self):
        first = {"repository": "owner/repo", "head": "h2", "reviewed_head": None,
                 "passes": [a_pass("h1", tier="skip"), a_pass("h2")]}
        again = {"repository": "owner/repo", "head": "h2", "reviewed_head": None,
                 "passes": [a_pass("h2", tier="deep")]}
        db = checkpoint_db([("ship:x", "2026-09-22T00:00:00+00:00", first),
                            ("ship-review-no-item:y", "2026-09-22T01:00:00+00:00", again),
                            ("ship:z", "2026-09-22T02:00:00+00:00", "{not json")])
        code, out = run(["reconcile", "--db", str(db)])
        self.assertEqual(code, 0)
        found = json.loads(out)
        entry1, a, b = (found["extractions"][n] for n in ("entry1", "A", "B"))
        # Pass heads: h1 read once (moved to skip), h2 first read as standard.
        self.assertEqual((entry1["heads"], entry1["moved"], entry1["to_skip"]), (2, 1, 1))
        self.assertEqual((a["heads"], a["moved"], a["to_skip"]), (2, 1, 1))
        # The checkpoint's head, first pass only: h2 with h1's skip reading.
        self.assertEqual((b["heads"], b["moved"], b["to_skip"]), (1, 1, 1))
        self.assertEqual(found["invalid_bodies"], 1)

    def test_the_window_bounds_are_entry_ones(self):
        db = checkpoint_db([
            ("ship:x", "2026-09-21T06:55:06+00:00", {"repository": "o/r", "head": "h0",
                                                     "passes": [a_pass("h0")]}),
            ("ship:x", "2026-09-21T06:55:07+00:00", {"repository": "o/r", "head": "h1",
                                                     "passes": [a_pass("h1")]}),
            ("ship:x", "2026-09-29T20:10:00+00:00", {"repository": "o/r", "head": "h2",
                                                     "passes": [a_pass("h2")]})])
        found = json.loads(run(["reconcile", "--db", str(db)])[1])
        self.assertEqual(found["extractions"]["entry1"]["heads"], 1)


class TheHeuristic(unittest.TestCase):
    """Step 3: the frozen file, and the shapes it was written for."""

    def setUp(self):
        self.heuristic = experiment.frozen_heuristic()

    def state(self, paths, lines):
        return json.loads(experiment.state_of(paths, lines, "r"))

    def test_the_file_is_the_registered_one(self):
        self.assertEqual(hashlib.sha256(experiment.HEURISTIC.read_bytes()).hexdigest(),
                         experiment.HEURISTIC_SHA256)
        self.assertEqual(run(["heuristic"])[0], 0)

    def test_a_changed_file_is_refused(self):
        changed = Path(tempfile.mkdtemp()) / "h.py"
        changed.write_bytes(experiment.HEURISTIC.read_bytes() + b"\n")
        with self.assertRaises(experiment.Refused):
            experiment.frozen_heuristic(changed)

    def test_the_shapes(self):
        self.assertEqual(self.heuristic(self.state([], 0), "standard"), "skip")
        self.assertEqual(self.heuristic(self.state(["img/a.PNG", "b.svg"], 0), "standard"), "skip")
        self.assertEqual(self.heuristic(self.state(["lib/auth.py"], 5), "standard"), "deep")
        self.assertEqual(self.heuristic(self.state(["docs/a.md", "tests/test_b.py"], 20),
                                        "standard"), "cheap")
        self.assertEqual(self.heuristic(self.state(["docs/a.md", "src/b.py"], 20),
                                        "standard"), "standard")


class TheRebuiltState(unittest.TestCase):
    """Step 1: the state is the pack's, less the suffix `sd-review` adds."""

    def test_the_state_is_the_packs_byte_for_byte(self):
        paths = ["b/two.py", "a/one.md"] + [f"p{i}.txt" for i in range(41)]
        state = experiment.state_of(paths, 7, "no category matched; default tier standard")
        self.assertEqual(hashlib.sha256(state.encode()).hexdigest(), PACK_STATE_SHA256)
        self.assertEqual(hashlib.sha256(experiment.criteria().encode()).hexdigest(),
                         PACK_CRITERIA_SHA256)

    def test_the_jev_suffix_is_stripped(self):
        for tail in ("standard", "cheap, below the routed tier standard"):
            self.assertEqual(
                experiment.routing_said(f"no category matched; Jev read the diff and chose tier {tail}"),
                "no category matched")

    def test_an_absolute_or_home_path_is_found(self):
        for path in ("/Users/someone/x.py", "/etc/hosts", "~/notes.md", "src/home/x/Users/y"):
            state = experiment.state_of(["a.py", path], 1, "r")
            self.assertIsNotNone(experiment.private_path(state, None), path)
        self.assertIsNone(experiment.private_path(experiment.state_of(["src/a.py"], 1, "r"), None))

    def test_an_absolute_path_inside_a_string_is_found(self):
        for reason in ("never skip /opt/private/file.py", "matched (/srv/x)",
                       "path=/var/lib/y", "see ~/notes.md first", "a, /etc/hosts"):
            state = experiment.state_of(["src/a.py"], 1, reason)
            self.assertIsNotNone(experiment.private_path(state, None), reason)
        for reason in ("no category matched; default tier standard",
                       "docs/a.md and tests/b.py; 1/2 of the paths"):
            state = experiment.state_of(["src/a.py", "a/b/c.md"], 1, reason)
            self.assertIsNone(experiment.private_path(state, None), reason)


class TheFaithfulnessCheck(StubServer):
    """Step 1 end to end against the stub: the experiment ledger only."""

    def setUp(self):
        super().setUp()
        self.ledger = Path(tempfile.mkdtemp()) / "experiment.db"
        initialise(self.ledger)
        self.out = Path(tempfile.mkdtemp()) / "faithful"
        rows = []
        for n in range(30):
            rows.append((f"ship:{n}", f"2026-09-23T00:00:{n:02d}+00:00",
                         {"repository": "owner/repo", "head": f"m{n:039d}",
                          "passes": [a_pass(f"m{n:039d}", tier="skip")]}))
            rows.append((f"ship:k{n}", f"2026-09-24T00:00:{n:02d}+00:00",
                         {"repository": "owner/repo", "head": f"k{n:039d}",
                          "passes": [a_pass(f"k{n:039d}")]}))
        self.db = checkpoint_db(rows)

    def env(self, **extra):
        # A live switch in the parent's environment must not reach the child.
        return super().env(PYTHONPATH=SD_DB_PATH, HOME=str(self.config),
                           JEV_COMPARE_STAGES="JEV_SD_REVIEW", JEV_COMPARE_HAIKU_VIA="anthropic",
                           JEV_SHADOW="on", **extra)

    def faithful(self):
        return run(["faithful", "--db", str(self.db), "--ledger", str(self.ledger),
                    "--out", str(self.out)], self.env())

    def test_fifty_states_are_asked_once_and_land_in_the_experiment_ledger(self):
        Stub.choice_value = "standard"
        code, out = self.faithful()
        summary = json.loads(out)
        self.assertEqual((summary["asked"], summary["moved_drawn"]), (50, 25))
        self.assertEqual(summary["matched"], 25)   # the stub always says standard
        self.assertEqual(code, 1)                  # 25 of 50 is below 46
        self.assertEqual(len(Stub.seen), 50)
        self.assertNotIn("--unsure-below", json.dumps(Stub.seen))
        with contextlib.closing(sqlite3.connect(self.ledger)) as connection:
            stages = connection.execute(
                "SELECT stage, arm, count(*) FROM judgment GROUP BY stage, arm").fetchall()
        self.assertEqual(stages, [("JEV_SD_REVIEW", "jev", 50)])
        answers = [json.loads(line) for line in (self.out / "answers.jsonl").read_text().splitlines()]
        self.assertEqual(len(answers), 50)
        sent = Stub.seen[0]["payload"]
        self.assertEqual(sent["state"], json.loads(answers[0]["state"]))

    def test_a_second_run_refuses(self):
        Stub.choice_value = "standard"
        self.faithful()
        Stub.seen = []
        code, _out = self.faithful()
        self.assertEqual(code, 2)
        self.assertEqual(Stub.seen, [])

    def test_a_state_with_a_home_path_halts_before_any_call(self):
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("DELETE FROM state")
            for n in range(30):
                for prefix, tier in (("m", "skip"), ("k", "standard")):
                    head = f"{prefix}{n:039d}"
                    connection.execute(
                        "INSERT INTO state (kind, key, timestamp, body) VALUES"
                        " ('checkpoint', 'ship:x', '2026-09-25T00:00:00+00:00', ?)",
                        (json.dumps({"repository": "o/r", "head": head, "passes": [
                            a_pass(head, tier=tier, paths=("/Users/someone/a.py",))]}),))
        code, _out = self.faithful()
        self.assertEqual(code, 2)
        self.assertEqual(Stub.seen, [])
        self.assertFalse((self.out / "answers.jsonl").exists())

    def test_the_live_ledger_is_refused_as_the_experiment_ledger(self):
        live = self.config / ".local/share/sd/sd.db"
        live.parent.mkdir(parents=True)
        initialise(live)
        for ledger in (live, self.config / "link.db"):
            if ledger.name == "link.db":
                ledger.symlink_to(live)
            self.ledger = ledger
            code, _out = self.faithful()
            self.assertEqual(code, 2, ledger)
        self.assertEqual(Stub.seen, [])
        with contextlib.closing(sqlite3.connect(live)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM judgment").fetchone(), (0,))

    def test_the_source_database_is_refused_as_the_experiment_ledger(self):
        # The checkpoints sit in the ledger file, so only the identity check
        # can refuse: the draw would succeed and the calls would be made.
        with contextlib.closing(sqlite3.connect(self.db)) as source:
            rows = source.execute("SELECT kind, key, timestamp, body FROM state").fetchall()
        with contextlib.closing(sqlite3.connect(self.ledger)) as target, target:
            target.executemany("INSERT INTO state (kind, key, timestamp, body) VALUES (?, ?, ?, ?)",
                               rows)
        self.db = self.out.parent / "same.db"
        self.db.symlink_to(self.ledger)
        code, _out = self.faithful()
        self.assertEqual(code, 2)
        self.assertEqual(Stub.seen, [])

    def test_a_ledger_that_refuses_stops_the_run(self):
        self.ledger = Path(tempfile.mkdtemp()) / "missing.db"
        code, _out = self.faithful()
        self.assertEqual(code, 2)
        self.assertEqual(Stub.seen, [])
