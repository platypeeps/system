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


#: The class from review rounds 1 to 3: an absolute or home path token in
#: text sent to the hosted call. One row per form, as (id, text, whether it
#: must halt). PR body, failure table.
BOUNDARIES = (
    ("B01 start of string", "/opt/private/file.py", True),
    ("B02 whitespace", "never skip\t/opt/private/file.py", True),
    ("B03 double quote", 'matched "/opt/private/file.py"', True),
    ("B04 single quote", "matched '/opt/private/file.py'", True),
    ("B05 backtick", "matched `/opt/private/file.py`", True),
    ("B06 emphasis *", "never skip **/opt/private/file.py**", True),
    ("B07 emphasis _", "never skip _/opt/private/file.py_", True),
    ("B08 parenthesis", "see [x](/opt/private/file.py)", True),
    ("B09 bracket and brace", "paths [/opt/a/b] {/srv/c/d}", True),
    ("B10 angle bracket", "link </opt/private/file.py>", True),
    ("B11 KEY=/path", "ROOT=/opt/private/file.py", True),
    ("B12 x:/path", "x:/opt/private/file.py", True),
    ("B13 path:line", "/opt/private/file.py:12 is risky", True),
    ("B14 comma and semicolon", "a.py,/opt/x/y;/srv/z/w", True),
    ("B15 file:// URL", "file:///opt/private/file.py", True),
    ("B16 ~/ home", "see ~/notes/a.md", True),
    ("B17 ~user/ home", "see ~someone/notes.md", True),
    ("B18 $HOME/", "see $HOME/notes.md", True),
    ("B19 ${HOME}/", "see ${HOME}/notes.md", True),
    ("B20 other punctuation", "!/opt/a/b #/opt/a/b -/opt/a/b @/opt/a/b", True),
    ("B21 typographic quote", "matched \u201c/opt/private/file.py\u201d", True),
    ("B22 glued after a word", "foo/opt/private/file.py", False),
    ("B23 relative paths", "src/a.py docs/**", False),
    ("B24 fractions and and/or", "1/2 of the paths, and/or, 25% off", False),
    ("B25 glued after a closing bracket", "app/(site)/styles/a.css x[0]/y/z {a}/b/c", False),
    # Round 3: deny by default. A path sign is a hit whatever follows it;
    # one form per row, so no row passes on another form's hit.
    ("R01 one segment", "/secret", True),
    ("R02 one segment in a sentence", "never skip /private.txt", True),
    ("R03 one segment after a quote", 'matched "/secret"', True),
    ("R04 one segment in parentheses", "matched (/x)", True),
    ("R05 file: URL, one segment", "file:/secret", True),
    ("R06 ~user with no slash", "see ~someone", True),
    ("R07 a tilde after a word", "x~/notes.md", True),
    ("R08 Windows drive, slash", "C:/work", True),
    ("R09 Windows drive, backslash", "D:\\work\\a.txt", True),
    ("R10 UNC path", "\\\\server\\share", True),
    ("R11 shell variable", "$TMPDIR/x", True),
    ("R12 braced shell variable", "${XDG_CONFIG_HOME}/y", True),
    ("R13 Windows variable", "%USERPROFILE%", True),
    ("R14 glued after a word and a dot", "rule./etc/passwd", True),
    ("R15 dot relative, false halt", "./a/b.py", True),
    ("R16 dot-dot relative, false halt", "../c/d.py", True),
    ("R17 glob after a star, false halt", "docs/**/*.md", True),
    ("R18 backup file tilde, false halt", "a.py~", True),
    # Found by the adversarial review of round 3.
    ("R19 space after the root slash", "/ secret/file.py", True),
    ("R20 tab after the root slash", "/\tprivate/file.py", True),
    ("R21 Windows variable with brackets", "%ProgramFiles(x86)%", True),
    ("R22 percent-encoded slash", "%2Fetc%2Fpasswd", True),
    ("R23 command substitution", "$(pwd)", True),
    ("R24 spaced slash, false halt", "a / b", True),
    ("R25 ANSI colour before a path", "\x1b[31m/etc/passwd\x1b[0m", True),
    ("R26 C1 control before a path", "\x9b31m/etc/passwd", True),
    ("R27 file: URL without a slash", "FILE:private.txt", True),
    ("R28 profile: is not a scheme", "profile: standard", False),
)


class TheBoundaryClass(unittest.TestCase):
    """Each boundary form halts in a changed path and in a routing reason."""

    def test_each_boundary_form(self):
        for row, text, halts in BOUNDARIES:
            for state in (experiment.state_of(["src/a.py", text], 1, "r"),
                          experiment.state_of(["src/a.py"], 1, text)):
                with self.subTest(row):
                    found = experiment.private_path(state, None)
                    self.assertEqual(found is not None, halts, f"{row}: {text!r}")


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

    def test_another_spelling_of_the_ledger_is_the_same_ledger(self):
        # The meter strips `JEV_METER_DB`, so the identity check must judge
        # the path the meter will open, not the one typed (review round 3).
        live = self.config / ".local/share/sd/sd.db"
        live.parent.mkdir(parents=True)
        initialise(live)
        with contextlib.closing(sqlite3.connect(self.db)) as source:
            rows = source.execute("SELECT kind, key, timestamp, body FROM state").fetchall()
        with contextlib.closing(sqlite3.connect(self.ledger)) as target, target:
            target.executemany("INSERT INTO state (kind, key, timestamp, body) VALUES (?, ?, ?, ?)",
                               rows)
        source = self.ledger
        for ledger, db in ((f"{live} ", self.db), (f" {live}\n", self.db),
                           (f"{source} ", source), (f"{source}\t", source)):
            with self.subTest(ledger=ledger):
                Stub.seen = []
                self.ledger, self.db = ledger, db
                self.out = Path(tempfile.mkdtemp()) / "faithful"
                code, _out = self.faithful()
                self.assertEqual(code, 2)
                self.assertEqual(Stub.seen, [])
        for path in (live, source):
            with contextlib.closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM judgment").fetchone(),
                                 (0,), path)

    def test_a_resolved_name_ending_in_space_is_judged_as_resolved(self):
        # Adversarial review of round 3: the live path resolves to a name
        # ending in space, so a second strip judged a file nobody opens; and
        # a ledger resolving to such a name is opened stripped by the meter.
        live = self.config / ".local/share/sd/sd.db"
        live.parent.mkdir(parents=True)
        spaced = self.config / ".local/share/sd/live.db "
        initialise(spaced)
        live.symlink_to(spaced)
        (self.config / "hard.db").hardlink_to(spaced)
        plain = self.config / "plain.db"
        initialise(plain)
        initialise(self.config / "plain.db ")
        (self.config / "to-spaced.db").symlink_to(self.config / "plain.db ")
        for ledger in (self.config / "hard.db", self.config / "to-spaced.db"):
            with self.subTest(ledger=ledger.name):
                Stub.seen = []
                self.ledger = ledger
                self.out = Path(tempfile.mkdtemp()) / "faithful"
                code, _out = self.faithful()
                self.assertEqual(code, 2)
                self.assertEqual(Stub.seen, [])
        for path in (spaced, plain):
            with contextlib.closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM judgment").fetchone(),
                                 (0,), path)

    def test_a_source_resolving_to_a_name_ending_in_space_is_read_as_resolved(self):
        # The source read must open the file the identity check judged, not
        # that name stripped, which here is the ledger itself.
        with contextlib.closing(sqlite3.connect(self.db)) as source:
            rows = source.execute("SELECT kind, key, timestamp, body FROM state").fetchall()
        with contextlib.closing(sqlite3.connect(self.ledger)) as target, target:
            target.executemany("INSERT INTO state (kind, key, timestamp, body) VALUES (?, ?, ?, ?)",
                               rows)
        spaced = Path(f"{self.ledger} ")
        checkpoint_db([]).replace(spaced)
        self.db = self.config / "source-link.db"
        self.db.symlink_to(spaced)
        code, _out = self.faithful()
        self.assertEqual(code, 2)               # too few states to draw in the spaced file
        self.assertEqual(Stub.seen, [])

    def test_the_source_is_opened_as_named_and_read_only(self):
        # `?` in --db once ended the path inside the read-only URI, so the
        # file the identity check judged was not the file opened, and the
        # `mode=ro` that followed was read as part of another parameter.
        Stub.choice_value = "standard"
        with contextlib.closing(sqlite3.connect(self.db)) as source:
            rows = source.execute("SELECT kind, key, timestamp, body FROM state").fetchall()
        with contextlib.closing(sqlite3.connect(self.ledger)) as target, target:
            target.executemany("INSERT INTO state (kind, key, timestamp, body) VALUES (?, ?, ?, ?)",
                               rows)
        self.db = f"{self.ledger}?x=1"
        code, _out = self.faithful()
        self.assertEqual(code, 2)
        self.assertEqual(Stub.seen, [])
        with contextlib.closing(sqlite3.connect(self.ledger)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM judgment").fetchone(), (0,))

    def test_the_default_source_is_the_live_database_of_the_given_home(self):
        # The live check reads `env`'s HOME; the default --db must read the
        # same HOME, not the process's.
        live = self.config / ".local/share/sd/sd.db"
        live.parent.mkdir(parents=True)
        live.write_bytes(self.db.read_bytes())
        out = io.StringIO()
        code = experiment.main(["reconcile", "--since", "2026-09-22", "--until", "2026-09-25"],
                               out=out, env={"HOME": str(self.config)})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["extractions"]["A"]["heads"], 60)

    def test_one_name_is_one_file_before_the_file_exists(self):
        # On a satellite `sd_db` sends the default path to the hub, so a
        # ledger named as the live database needs no local file to be it.
        missing = experiment.db_path(self.config / "nowhere/sd.db")
        self.assertTrue(experiment.same_file(missing, experiment.db_path(f"{missing} ")))
        self.assertFalse(experiment.same_file(missing, self.config / "elsewhere/sd.db"))

    def test_a_ledger_that_refuses_stops_the_run(self):
        self.ledger = Path(tempfile.mkdtemp()) / "missing.db"
        code, _out = self.faithful()
        self.assertEqual(code, 2)
        self.assertEqual(Stub.seen, [])
