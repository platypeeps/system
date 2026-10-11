"""Jev experiment entry 2 (sd:2764): the steps `jev_experiment.py` runs.

Every checkpoint here is a fixture in a database of its own, and every Jev
call goes to the loopback stub `test_jev` serves, so no case reads the
operator's workflow database or reaches TypeSafe.
"""

import contextlib
import hashlib
import io
import json
import socket
import subprocess
import tempfile
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jev_experiment as experiment

from .test_jev import Stub, StubServer
from .test_jev_metering import connect, initialise
from .test_jev_compare import SD_DB_PATH

#: The pack's `bin/sd_jev.py` `_jev_state` and `_jev_criteria` output at
#: b6b58bb14 for the inputs in `TheRebuiltState`, as SHA-256.
PACK_STATE_SHA256 = "df9cf37a23d86345b98fdd7b06340f45d325460541a07967bfd17bd734fd207b"
PACK_CRITERIA_SHA256 = "4de730c7968e4e31558dfc3f3e8cccb454d2e517b35e555f672c03240df59b42"


def checkpoint_db(rows) -> Path:
    """A database the migrations built, holding `rows` as checkpoints. Opened
    through `sd_db` alone: nothing outside it opens the database
    (`local-sd-db/tests/test_one_store.py`)."""
    path = Path(tempfile.mkdtemp()) / "sd.db"
    initialise(path)
    with contextlib.closing(connect(path)) as connection:
        for key, stamp, body in rows:
            connection.execute(
                "INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                (key, stamp, body if isinstance(body, str) else json.dumps(body)))
    return path


def checkout(tracked, untracked=(), remote=True):
    """A git checkout whose index lists `tracked`; `untracked` files sit
    beside them on disk. Its `origin` is the remote `register` records,
    unless `remote` is false."""
    root = Path(tempfile.mkdtemp()) / "checkout"
    for name in (*tracked, *untracked):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", remote_of(root)],
                       check=True)
    if tracked:
        subprocess.run(["git", "-C", str(root), "add", "--", *tracked], check=True)
    return root


def remote_of(root):
    return f"https://github.com/owner/{root.parent.name}.git"


def register(db, root, visibility, managed=1, remote=None):
    """`root` as a repository row with its last observed visibility, the way
    `sd shadow sync` stores it (`sd_db.protection.alerts`). A `visibility` of
    None leaves the row unobserved, so `protection.rows` lends it the row of a
    checkout with the same remote."""
    stamp = "2026-10-10T00:00:00+00:00"
    body = {"alerts": {"secret_scanning": {"visibility": visibility}}}
    with contextlib.closing(connect(db)) as connection:
        connection.execute(
            "INSERT INTO repo (path, remote, created_at, updated_at, managed) VALUES (?, ?, ?, ?, ?)",
            (str(root), remote or remote_of(root), stamp, stamp, managed))
        if visibility is None:
            return
        connection.execute(
            "INSERT INTO repo_protection (repo, observed_at, status, body) VALUES (?, ?, 'unknown', ?)",
            (str(root), stamp, json.dumps(body)))


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
    ("B22 glued after a word", "foo/opt/private/file.py", True),
    ("B23 relative paths", "src/a.py docs/**", True),
    ("B24 fractions and and/or", "1/2 of the paths, and/or, 25% off", True),
    ("B25 glued after a closing bracket", "app/(site)/styles/a.css x[0]/y/z {a}/b/c", True),
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
    ("R28 profile:, false halt", "profile: standard", True),
    # Review round 4: a closing bracket before a root slash.
    ("R29 closing bracket before a root", "never skip ]/opt/private/file.py", True),
    ("R30 closing brace before a root", "never skip }/private.txt", True),
    ("R31 closing parenthesis before a root", "see x)/etc/passwd", True),
    # Second codex pass on round 4: Unicode format characters.
    ("R32 bidi override", "review \u202eprivate.txt", True),
    ("R33 zero-width space", "review a\u200bb", True),
    # What still passes: no slash, backslash, tilde, `file:`, variable sign
    # or control character anywhere.
    ("N01 plain words", "no category matched; default tier standard", False),
    ("N02 a percent then a space", "25% off", False),
    ("N03 a file at the top level", "README.md", False),
)


#: Each probe's path with its root, prefix or wrapper taken off, as if a public
#: repository tracked it: a probe must halt on what comes before the path,
#: not on the path being unknown (review round 4, operator decision).
NEAR_MISSES = frozenset((
    "opt/private/file.py", "opt/a/b", "srv/c/d", "opt/x/y", "srv/z/w", "srv/x",
    "notes/a.md", "etc/passwd", "secret/file.py", "private/file.py", "work/a.txt",
    "a/b.py", "c/d.py", "var/lib/y", "src/a.py", "styles/a.css", "y/z", "b/c", "x/y",
    "a.py", "README.md"))


class TheBoundaryClass(unittest.TestCase):
    """Each boundary form halts in a changed path and in a routing reason,
    even when the path after its root is a tracked public one."""

    def test_each_boundary_form(self):
        # A changed path passes only as a whole tracked path, so a prose
        # row that passes as a reason halts as a changed path.
        for row, text, halts in BOUNDARIES:
            for state, expected in (
                    (experiment.state_of(["a.py", text], 1, "r"), halts or text not in NEAR_MISSES),
                    (experiment.state_of(["a.py"], 1, text), halts)):
                with self.subTest(row):
                    found = experiment.private_path(state, None, NEAR_MISSES)
                    self.assertEqual(found is not None, expected, f"{row}: {text!r}")


class ThePublicAllowlist(unittest.TestCase):
    """Review round 4, operator decision: a path passes only when it is, once
    wrapping punctuation is stripped, exactly one `git ls-files` lists in a
    public managed repository."""

    def setUp(self):
        self.db = checkpoint_db([])
        self.public = checkout(["pkg/mod.py", ".github/ci.yml"], untracked=["pkg/other.py"])
        self.private = checkout(["secret/plan.md"])
        register(self.db, self.public, "public")
        register(self.db, self.private, "private")
        self.allowed = experiment.public_paths(self.db)

    def halts(self, text):
        return [experiment.private_path(state, None, self.allowed) is not None
                for state in (experiment.state_of(["pkg/mod.py", text], 1, "r"),
                              experiment.state_of(["pkg/mod.py"], 1, text))]

    def test_a_tracked_public_path_passes(self):
        self.assertEqual(self.allowed, frozenset({"pkg/mod.py", ".github/ci.yml"}))
        for text in ("pkg/mod.py", ".github/ci.yml"):
            self.assertEqual(self.halts(text), [False, False], text)
        for text in ("touches `pkg/mod.py`.", '"**pkg/mod.py**", then (pkg/mod.py);'):
            self.assertEqual(self.halts(text), [True, False], text)

    def test_an_untracked_top_level_changed_path_halts(self):
        # Codex pass on round 4: a changed path with no `/` skipped the
        # allowlist. A changed path passes only as a whole tracked path.
        for path in ("private.txt", "a.py", "pkg"):
            state = experiment.state_of(["pkg/mod.py", path], 1, "r")
            self.assertIsNotNone(experiment.private_path(state, None, self.allowed), path)

    def test_a_checkout_whose_origin_is_not_the_row_s_adds_nothing(self):
        # Codex pass on round 4: `protection.rows` lends a public sibling's
        # visibility by the stored remote, so the checkout must be that remote.
        db = checkpoint_db([])
        public = checkout(["a/listed.py"])
        register(db, public, "public")
        twin = checkout(["d/twin.py"])
        subprocess.run(["git", "-C", str(twin), "remote", "set-url", "origin", remote_of(public)],
                       check=True)
        register(db, twin, None, remote=remote_of(public))
        no_origin = checkout(["b/no-origin.py"], remote=False)
        register(db, no_origin, None, remote=remote_of(public))
        other = checkout(["c/other.py"])
        register(db, other, None, remote=remote_of(public))
        # The twin is a checkout of the public remote, so it borrows rightly.
        self.assertEqual(experiment.public_paths(db), frozenset({"a/listed.py", "d/twin.py"}))

    def test_an_inherited_git_dir_does_not_choose_the_checkout(self):
        # Second codex pass on round 4: `git -C` obeys `GIT_DIR`, so an
        # inherited one listed another repository's index under a public row.
        db = checkpoint_db([])
        public = checkout(["a/listed.py"])
        register(db, public, "public")
        rogue = checkout(["r/rogue.py"])
        subprocess.run(["git", "-C", str(rogue), "remote", "set-url", "origin", remote_of(public)],
                       check=True)
        with unittest.mock.patch.dict("os.environ", {"GIT_DIR": str(rogue / ".git")}):
            self.assertEqual(experiment.public_paths(db), frozenset({"a/listed.py"}))

    def test_a_root_or_prefix_before_a_tracked_path_halts(self):
        for text in ("/pkg/mod.py", "./pkg/mod.py", "../pkg/mod.py", "~/pkg/mod.py",
                     "file:pkg/mod.py", "$X/pkg/mod.py", "%X%/pkg/mod.py", "\\pkg/mod.py",
                     "x/pkg/mod.py", "pkg/mod.py/", "pkg//mod.py", "]/pkg/mod.py",
                     "pkg/mod.py:12", "pkg/mod.py\x1b[0m"):
            self.assertEqual(self.halts(text), [True, True], text)

    def test_a_tracked_path_in_a_private_repository_halts(self):
        self.assertNotIn("secret/plan.md", self.allowed)
        self.assertEqual(self.halts("secret/plan.md"), [True, True])

    def test_an_untracked_look_alike_halts(self):
        for text in ("pkg/other.py", "pkg/mod.py.orig", "PKG/mod.py", "pkg/mod.pyc"):
            self.assertEqual(self.halts(text), [True, True], text)

    def test_a_repository_not_read_as_public_adds_nothing(self):
        # Unmanaged, visibility never read, and a checkout git cannot list:
        # each answer is unknown, and unknown is not public.
        db = checkpoint_db([])
        register(db, checkout(["a/unmanaged.py"]), "public", managed=0)
        register(db, checkout(["a/unread.py"]), None)
        gone = checkout(["a/gone.py"])
        register(db, gone, "public")
        subprocess.run(["rm", "-rf", str(gone)], check=True)
        self.assertEqual(experiment.public_paths(db), frozenset())


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
        self.assertIsNone(experiment.private_path(experiment.state_of(["a.py"], 1, "r"), None,
                                                  frozenset({"a.py"})))

    def test_an_absolute_path_inside_a_string_is_found(self):
        for reason in ("never skip /opt/private/file.py", "matched (/srv/x)",
                       "path=/var/lib/y", "see ~/notes.md first", "a, /etc/hosts"):
            state = experiment.state_of(["a.py"], 1, reason)
            self.assertIsNotNone(experiment.private_path(state, None), reason)
        for reason in ("no category matched; default tier standard",
                       "docs and tests only; half of the paths"):
            state = experiment.state_of(["a.py", "README.md"], 1, reason)
            self.assertIsNone(experiment.private_path(state, None, frozenset({"a.py", "README.md"})),
                              reason)


class TheFaithfulnessCheck(StubServer):
    """Step 1 end to end against the stub: the experiment ledger only."""

    def setUp(self):
        super().setUp()
        self.ledger = Path(tempfile.mkdtemp()) / "experiment.db"
        initialise(self.ledger)
        self.out = Path(tempfile.mkdtemp()) / "faithful"
        # The checkpoints name the public checkout's repository: step 1 draws
        # only from public managed repositories (operator, 2026-10-11).
        public = checkout(["a.py", "pkg/mod.py"])
        self.slug = f"owner/{public.parent.name}"
        rows = []
        for n in range(30):
            rows.append((f"ship:{n}", f"2026-09-23T00:00:{n:02d}+00:00",
                         {"repository": self.slug, "head": f"m{n:039d}",
                          "passes": [a_pass(f"m{n:039d}", tier="skip")]}))
            rows.append((f"ship:k{n}", f"2026-09-24T00:00:{n:02d}+00:00",
                         {"repository": self.slug, "head": f"k{n:039d}",
                          "passes": [a_pass(f"k{n:039d}")]}))
        self.db = checkpoint_db(rows)
        register(self.db, public, "public")

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
        with contextlib.closing(connect(self.ledger, write=False)) as connection:
            stages = [tuple(row) for row in connection.execute(
                "SELECT stage, arm, count(*) FROM judgment GROUP BY stage, arm")]
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

    def restate(self, paths):
        """The 60 checkpoints again, each changing `paths`."""
        with contextlib.closing(connect(self.db)) as connection:
            connection.execute("DELETE FROM state")
        self.add(self.slug, "m", 30, tier="skip", paths=paths)
        self.add(self.slug, "k", 30, paths=paths)

    def add(self, repository, prefix, count, tier="standard", paths=("a.py",),
            reason="no category matched; default tier standard"):
        """`count` more checkpoints of `repository`, heads `prefix` and a number."""
        with contextlib.closing(connect(self.db)) as connection:
            for n in range(count):
                head = f"{prefix}{n:039d}"
                connection.execute(
                    "INSERT INTO state (kind, key, timestamp, body) VALUES"
                    " ('checkpoint', 'ship:x', '2026-09-25T00:00:00+00:00', ?)",
                    (json.dumps({"repository": repository, "head": head, "passes": [
                        a_pass(head, tier=tier, paths=paths, reason=reason)]}),))

    def answers(self):
        return [json.loads(line) for line in (self.out / "answers.jsonl").read_text().splitlines()]

    def test_only_public_repository_states_are_drawn(self):
        # Operator, 2026-10-11: the pool is the states of public managed
        # repositories. A private repository tracking the same path, and a
        # repository no row names, stay out of it.
        Stub.choice_value = "standard"
        private = checkout(["a.py"])
        register(self.db, private, "private")
        self.add(f"owner/{private.parent.name}", "p", 30, tier="skip")
        self.add(f"owner/{private.parent.name}", "q", 30)
        self.add("other/repo", "u", 30, tier="skip")
        code, out = self.faithful()
        summary = json.loads(out)
        self.assertEqual(code, 1, out)
        self.assertEqual((summary["pool"], summary["pool_moved"], summary["pool_halted"]),
                         (60, 30, 0))
        self.assertEqual((summary["asked"], summary["moved_drawn"]), (50, 25))
        self.assertEqual({one["repository"] for one in self.answers()}, {self.slug})

    def test_a_public_state_whose_path_is_no_longer_tracked_is_left_out(self):
        # "whose paths are still tracked": such a state leaves the pool
        # rather than halting the run.
        Stub.choice_value = "standard"
        self.add(self.slug, "g", 10, tier="skip", paths=("a.py", "gone.py"))
        code, out = self.faithful()
        summary = json.loads(out)
        self.assertEqual(code, 1, out)
        self.assertEqual((summary["pool"], summary["pool_halted"]), (60, 0))
        self.assertFalse(any(one["head"].startswith("g") for one in self.answers()))

    def test_fewer_moved_states_than_half_are_all_drawn(self):
        # Operator, 2026-10-11: keep 25 moved if the pool has 25, else all.
        Stub.choice_value = "standard"
        with contextlib.closing(connect(self.db)) as connection:
            connection.execute("DELETE FROM state")
        self.add(self.slug, "m", 10, tier="skip")
        self.add(self.slug, "k", 60)
        code, out = self.faithful()
        summary = json.loads(out)
        self.assertEqual(code, 1, out)          # 40 of 50 is below 46
        self.assertEqual((summary["asked"], summary["matched"], summary["moved_drawn"],
                          summary["pool_moved"]), (50, 40, 10, 10))
        self.assertEqual(sum(one["moved"] for one in self.answers()), 10)

    def test_too_few_states_in_the_pool_refuses_before_any_call(self):
        with contextlib.closing(connect(self.db)) as connection:
            connection.execute("DELETE FROM state")
        self.add(self.slug, "m", 10, tier="skip")
        self.add(self.slug, "k", 39)
        code, _out = self.faithful()
        self.assertEqual(code, 2)
        self.assertEqual(Stub.seen, [])

    def test_a_pool_state_the_privacy_check_halts_is_counted(self):
        repos = {self.slug.lower(): frozenset({"a.py"})}
        states = [{"repository": self.slug, "head": "h", "moved": False,
                   "state": experiment.state_of(["a.py"], 1, reason)}
                  for reason in ("no category matched", "never skip /Users/someone/x.py")]
        pool, halted = experiment.public_pool(states, repos, None, frozenset({"a.py"}))
        self.assertEqual((len(pool), halted), (2, 1))

    def test_a_halt_anywhere_in_the_pool_stops_the_run_before_any_call(self):
        # The whole pool is checked, not only the 50 drawn.
        self.add(self.slug, "h", 1, tier="skip", reason="never skip /Users/someone/x.py")
        code, _out = self.faithful()
        self.assertEqual(code, 2)
        self.assertEqual(Stub.seen, [])

    def test_a_tracked_public_path_reaches_the_call(self):
        Stub.choice_value = "standard"
        self.restate(("pkg/mod.py",))
        code, out = self.faithful()
        self.assertEqual(code, 1, out)          # 25 of 50 match the stub
        self.assertEqual(len(Stub.seen), 50)
        self.assertEqual(Stub.seen[0]["payload"]["state"]["changed_paths"], ["pkg/mod.py"])

    def test_a_tracked_private_path_halts_before_any_call(self):
        register(self.db, checkout(["secret/plan.md"]), "private")
        self.restate(("secret/plan.md",))
        code, _out = self.faithful()
        self.assertEqual(code, 2)
        self.assertEqual(Stub.seen, [])

    def test_a_state_with_a_home_path_halts_before_any_call(self):
        self.restate(("/Users/someone/a.py",))
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
        with contextlib.closing(connect(live, write=False)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM judgment").fetchone()[0], 0)

    def test_the_source_database_is_refused_as_the_experiment_ledger(self):
        # The checkpoints sit in the ledger file, so only the identity check
        # can refuse: the draw would succeed and the calls would be made.
        with contextlib.closing(connect(self.db, write=False)) as source:
            rows = source.execute("SELECT kind, key, timestamp, body FROM state").fetchall()
        with contextlib.closing(connect(self.ledger)) as target:
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
        with contextlib.closing(connect(self.db, write=False)) as source:
            rows = source.execute("SELECT kind, key, timestamp, body FROM state").fetchall()
        with contextlib.closing(connect(self.ledger)) as target:
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
            with contextlib.closing(connect(path, write=False)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM judgment").fetchone()[0],
                                 0, path)

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
            with contextlib.closing(connect(path, write=False)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM judgment").fetchone()[0],
                                 0, path)

    def test_a_source_resolving_to_a_name_ending_in_space_is_read_as_resolved(self):
        # The source read must open the file the identity check judged, not
        # that name stripped, which here is the ledger itself.
        with contextlib.closing(connect(self.db, write=False)) as source:
            rows = source.execute("SELECT kind, key, timestamp, body FROM state").fetchall()
        with contextlib.closing(connect(self.ledger)) as target:
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
        with contextlib.closing(connect(self.db, write=False)) as source:
            rows = source.execute("SELECT kind, key, timestamp, body FROM state").fetchall()
        with contextlib.closing(connect(self.ledger)) as target:
            target.executemany("INSERT INTO state (kind, key, timestamp, body) VALUES (?, ?, ?, ?)",
                               rows)
        self.db = f"{self.ledger}?x=1"
        code, _out = self.faithful()
        self.assertEqual(code, 2)
        self.assertEqual(Stub.seen, [])
        with contextlib.closing(connect(self.ledger, write=False)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM judgment").fetchone()[0], 0)

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


def committed(files_then, files_now):
    """A checkout with two commits and an `origin` as `register` records it:
    (root, base sha, head sha)."""
    root = checkout([])
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.test"]
    shas = []
    for files in (files_then, files_now):
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), *ident, "commit", "-q", "--allow-empty",
                        "-m", "x"], check=True)
        shas.append(subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                                   capture_output=True, text=True).stdout.strip())
    return root, shas[0], shas[1]


class Tripwire(dict):
    """A parsed JSON object that fails the case when an outcome is read."""

    OUTCOMES = ("findings", "completed_reviews", "status", "outcomes", "requested_reviews")

    def __getitem__(self, key):
        if key in self.OUTCOMES:
            raise AssertionError(f"read {key!r}")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in self.OUTCOMES:
            raise AssertionError(f"read {key!r}")
        return super().get(key, default)


class ThePopulation(unittest.TestCase):
    """Step 2: one row per reviewed head, its state or why not, and no outcome."""

    def setUp(self):
        self.root, self.base, self.head = committed({"a.py": "1\n"},
                                                    {"a.py": "1\n2\n3\n", "docs/b.md": "x\n"})
        self.repo = f"owner/{self.root.parent.name}"

    def body(self, head, *passes_, repo=None, **extra):
        return dict({"repository": repo or self.repo, "head": head, "reviewed_head": None,
                     "base": "main", "passes": list(passes_)}, **extra)

    def build(self, rows, route=None):
        db = checkpoint_db(rows)
        register(db, self.root, "public")
        found, summary = experiment.population(db, route=route)
        return {r["head"]: r for r in found}, summary["summary"]

    def test_a_duplicate_head_is_one_row_tagged_by_its_first_checkpoint(self):
        reviewed = a_pass("h1", reason="no category matched; default tier standard")
        rows, summary = self.build([
            ("ship:a", "2026-09-10T00:00:00+00:00", self.body("h1", reviewed)),
            ("ship:b", "2026-09-22T00:00:00+00:00", self.body("h1", a_pass("h1", tier="skip")))])
        self.assertEqual(list(rows), ["h1"])
        self.assertEqual((rows["h1"]["population"], summary["C1"]["heads"], summary["E"]["heads"]),
                         ("C1", 1, 0))
        # The state is the first pass's, without the suffix sd-review adds.
        state = json.loads(rows["h1"]["state"])
        self.assertEqual(state["deterministic_routing_said"],
                         "no category matched; default tier standard")

    def test_an_invalid_json_body_is_counted_and_skipped(self):
        rows, summary = self.build([
            ("ship:a", "2026-09-22T00:00:00+00:00", "{not json"),
            ("ship:b", "2026-09-22T00:00:01+00:00", self.body("h2", a_pass("h2")))])
        self.assertEqual(list(rows), ["h2"])
        self.assertEqual(summary["invalid_bodies"], 1)

    def test_a_declined_reading_is_e0_with_its_stored_state(self):
        declined = a_pass("h3")
        del declined["report"]["jev"]
        rows, summary = self.build([("ship:a", "2026-09-23T00:00:00+00:00",
                                     self.body("h3", declined))])
        self.assertEqual((rows["h3"]["recorded"], rows["h3"]["e0"], rows["h3"]["routed"]),
                         (None, True, "standard"))
        self.assertIsNotNone(rows["h3"]["state"])
        self.assertEqual(summary["E"]["e0"], 1)

    def test_a_head_with_no_stored_subject_is_rebuilt_from_git(self):
        bare = {"head": self.head, "base": self.base}
        calls = []

        def route(paths, lines, policy):
            calls.append((paths, lines, policy))
            return "cheap", "category docs starts at tier cheap"
        rows, summary = self.build([("ship:a", "2026-09-12T00:00:00+00:00",
                                     self.body(self.head, bare))], route=route)
        row = rows[self.head]
        self.assertEqual((row["source"], row["routed"], row["dropped"]),
                         ("rebuilt-from-git", "cheap", None))
        self.assertEqual(calls, [(["a.py", "docs/b.md"], 3, None)])
        self.assertEqual(json.loads(row["state"]), {
            "changed_paths": ["a.py", "docs/b.md"], "changed_paths_omitted": 0,
            "path_count": 2, "lines_moved": 3,
            "deterministic_routing_said": "category docs starts at tier cheap"})
        self.assertEqual(summary["C1"]["rebuilt_from_git"], 1)

    def test_a_head_whose_objects_are_gone_is_dropped_and_counted(self):
        gone = "0" * 40
        rows, summary = self.build([("ship:a", "2026-09-12T00:00:00+00:00",
                                     self.body(gone, {"head": gone, "base": self.base}))],
                                   route=lambda *a: ("standard", "r"))
        self.assertEqual((rows[gone]["state"], rows[gone]["dropped"]), (None, "objects-gone"))
        self.assertEqual(summary["C1"]["dropped"], {"objects-gone": 1})

    def test_git_checks_a_stored_state(self):
        good = a_pass(self.head, paths=("a.py", "docs/b.md"), lines=3)
        good["report"]["subject"]["base"] = self.base
        rows, summary = self.build([("ship:a", "2026-09-22T00:00:00+00:00",
                                     self.body(self.head, good))])
        self.assertEqual(rows[self.head]["git_check"], "match")
        good["report"]["subject"]["lines"] = 9
        rows, _summary = self.build([("ship:a", "2026-09-22T00:00:00+00:00",
                                      self.body(self.head, good))])
        self.assertEqual(rows[self.head]["git_check"], "mismatch")

    def test_the_paper_subset_is_marked(self):
        rows, summary = self.build([
            ("ship:a", "2026-09-22T00:00:00+00:00", self.body("h4", a_pass("h4", tier="skip"))),
            ("ship:b", "2026-10-02T00:00:00+00:00", self.body("h5", a_pass("h5")))])
        self.assertEqual((rows["h4"]["paper"], rows["h5"]["paper"]), (True, False))
        self.assertEqual(summary["paper_in_population"], 1)

    def test_no_finding_review_count_or_status_is_read(self):
        outcome = a_pass("h6")
        outcome["report"].update(findings=[{"disposition": "blocking"}], completed_reviews=1,
                                 status="blocked")
        db = checkpoint_db([("ship:a", "2026-09-10T00:00:00+00:00",
                             self.body("h6", outcome, status="clear"))])
        real = json.loads
        with unittest.mock.patch.object(experiment.json, "loads",
                                        lambda text, **kw: real(text, object_hook=Tripwire, **kw)):
            found, _summary = experiment.population(db)
        self.assertEqual(len(found), 1)
        self.assertNotIn("finding", json.dumps(found))

    def test_the_verb_writes_the_file_once(self):
        db = checkpoint_db([("ship:a", "2026-09-22T00:00:00+00:00", self.body("h7", a_pass("h7")))])
        out = Path(tempfile.mkdtemp())
        argv = ["population", "--db", str(db), "--out", str(out), "--pack", str(out / "none")]
        code, printed = run(argv)
        self.assertEqual(code, 0)
        text = (out / "population.jsonl").read_bytes()
        self.assertEqual(json.loads(printed)["sha256"], hashlib.sha256(text).hexdigest())
        self.assertEqual(json.loads(text)["alias"], "R01")
        self.assertEqual(run(argv)[0], 2)

    def test_a_checkpoint_with_no_review_pass_adds_no_head(self):
        rows, summary = self.build([
            ("sd-gate-receipt:a", "2026-09-10T00:00:00+00:00", {"repository": self.repo, "head": "h8"}),
            ("ship:b", "2026-09-22T00:00:00+00:00", self.body("h8", a_pass("h8")))])
        self.assertEqual((rows["h8"]["population"], summary["no_passes"]), ("E", 1))


def reviewed(head, *dispositions, completed=1, severity="high"):
    """A pass at `head` whose review completed `completed` times with these findings."""
    one = a_pass(head)
    one["report"].update(completed_reviews=completed, findings=[
        {"disposition": d, "severity": severity} for d in dispositions])
    return one


def population_file(folder, rows):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "population.jsonl").write_text("".join(json.dumps(dict(
        {"repository": "o/r", "paper": False, "state": None}, **r)) + "\n" for r in rows))


class TheObjectiveLabels(unittest.TestCase):
    """Step 4: blocking@head from the first completed review at that head."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp()) / "experiment"

    def labels(self, rows, heads, population="E"):
        db = checkpoint_db(rows)
        population_file(self.out, [{"head": h, "population": population} for h in heads])
        code, printed = run(["labels", "objective", "--population", population,
                             "--db", str(db), "--out", str(self.out)])
        found = {}
        target = self.out / "labels" / f"{population}.jsonl"
        if target.exists():
            found = {r["head"]: r for r in map(json.loads, target.read_text().splitlines())}
        return code, found, db

    def body(self, head, *passes_):
        return {"repository": "o/r", "head": head, "passes": list(passes_)}

    def test_a_head_with_only_advisory_findings_is_not_blocking(self):
        code, found, _db = self.labels([("ship:a", "2026-09-22T00:00:00+00:00",
                                         self.body("h1", reviewed("h1", "advisory", "advisory")))],
                                       ["h1"])
        self.assertEqual(code, 0)
        self.assertIs(found["h1"]["blocking_at_head"], False)

    def test_a_first_pass_that_did_not_complete_is_passed_over(self):
        code, found, _db = self.labels([("ship:a", "2026-09-22T00:00:00+00:00", self.body(
            "h2", reviewed("h2", "blocking", completed=0), reviewed("h2", "advisory")))], ["h2"])
        self.assertIs(found["h2"]["blocking_at_head"], False)

    def test_a_head_with_no_completed_review_is_unknown(self):
        _code, found, _db = self.labels([("ship:a", "2026-09-22T00:00:00+00:00", self.body(
            "h3", reviewed("h3", "blocking", completed=0)))], ["h3"])
        self.assertEqual((found["h3"]["blocking_at_head"], found["h3"]["unknown"]),
                         (None, "no-completed-review"))

    def test_a_later_head_s_finding_is_ignored(self):
        _code, found, _db = self.labels([("ship:a", "2026-09-22T00:00:00+00:00", self.body(
            "h5", reviewed("h4", "advisory"), reviewed("h5", "blocking")))], ["h4", "h5"])
        self.assertIs(found["h4"]["blocking_at_head"], False)
        self.assertEqual((found["h5"]["blocking_at_head"], found["h5"]["severities"]),
                         (True, ["high"]))

    def test_a_c1_request_before_sealing_refuses_and_reads_nothing(self):
        db = checkpoint_db([("ship:a", "2026-09-10T00:00:00+00:00",
                             self.body("h6", reviewed("h6", "blocking")))])
        population_file(self.out, [{"head": "h6", "population": "C1"}])
        with unittest.mock.patch.object(experiment, "checkpoints",
                                        side_effect=AssertionError("read a checkpoint")):
            code, _printed = run(["labels", "objective", "--population", "C1",
                                  "--db", str(db), "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertFalse((self.out / "labels").exists())

    def test_hashes_on_the_item_alone_do_not_seal_c1(self):
        """Files no finished replay wrote, their hashes noted: still sealed shut
        (codex round 1). `TheReplay` opens C1 after a real replay."""
        db = checkpoint_db([("ship:a", "2026-09-10T00:00:00+00:00",
                             self.body("h7", reviewed("h7", "blocking")))])
        population_file(self.out, [{"head": "h7", "population": "C1"}])
        (self.out / "replay").mkdir()
        hashes = []
        for arm in experiment.MODEL_ARMS:
            path = self.out / "replay" / f"{arm}.jsonl"
            path.write_text(json.dumps({"arm": arm}) + "\n")
            hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
        stamp = "2026-10-11T00:00:00+00:00"
        with contextlib.closing(connect(db)) as connection:
            connection.execute(
                "INSERT INTO item (id, kind, title, status, created_at, updated_at)"
                " VALUES (2764, 'work', 't', 'in_progress', ?, ?)", (stamp, stamp))
            connection.execute("INSERT INTO note (item, timestamp, kind, body) VALUES"
                               " (2764, ?, 'comment', ?)", (stamp, " ".join(hashes)))
        code, _printed = run(["labels", "objective", "--population", "C1", "--db", str(db),
                              "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertFalse((self.out / "labels").exists())

    def test_c2_refuses_until_step_15_seals_it(self):
        code, found, _db = self.labels([], ["h8"], population="C2")
        self.assertEqual((code, found), (2, {}))


from .test_jev_compare import Arm, CompareCase  # noqa: E402

#: What the Haiku stub answers for the tier question.
HAIKU_REPLY = json.dumps({"probabilities": {"skip": 0.1, "cheap": 0.1, "standard": 0.7,
                                            "deep": 0.1}})


def sha(n):
    """A head SHA no span may carry."""
    return (f"{n}feedface" + "ab" * 20)[:40]


#: Jev attempts as `ask_once` returns them: a billed invalid answer, an
#: invalid answer with no cost, and a priced answer of `cheap`.
BILLED_INVALID = {"provider": "jev", "model": "m", "outcome": "invalid", "cause": "invalid",
                  "usd": 0.42, "tokens_in": 10_000_000}
UNPRICED_INVALID = {"provider": "jev", "model": "m", "outcome": "invalid", "cause": "invalid"}
CHEAP = {"provider": "jev", "model": "m", "outcome": "ok", "cause": None, "answer": "2",
         "confidence": 0.5, "probabilities": "0.1,0.6,0.2,0.1", "usd": 0.01, "tokens_in": 238}


#: The remote of the replay's public checkout: `owner/repo`, the checkpoints' repository.
REPO_URL = "https://github.com/owner/repo.git"


class TheReplay(CompareCase):
    """Step 5 against loopback stubs: Jev, Kev, Anthropic and the collector."""

    HEADS = 5

    def setUp(self):
        super().setUp()
        Arm.reply = HAIKU_REPLY
        self.folder = Path(tempfile.mkdtemp()) / "experiment"
        self.folder.mkdir()
        self.ledger = self.folder / "experiment.db"
        initialise(self.ledger)
        self.live = self.config / ".local/share/sd/sd.db"
        self.live.parent.mkdir(parents=True)
        initialise(self.live)
        self.db = checkpoint_db([])
        # The checkpoints' repository is public: step 1 draws from it.
        public = checkout(["a.py", "pkg/mod.py"])
        subprocess.run(["git", "-C", str(public), "remote", "set-url", "origin", REPO_URL],
                       check=True)
        register(self.db, public, "public", remote=REPO_URL)
        self.population = self.folder / "population.jsonl"
        self.write_population(["a.py"])
        retry = unittest.mock.patch.object(experiment, "RETRY_WAIT", 0)
        retry.start()
        self.addCleanup(retry.stop)
        self.enterContext(unittest.mock.patch.object(experiment, "FAITHFUL_PASS", 1))
        self.enterContext(unittest.mock.patch.object(experiment, "FAITHFUL_COUNT", 2))
        self.faithful = self.folder.parent / "faithful-2"
        # The registered draw: half moved (head 90), half not (head 91).
        self.checkpoint(90)
        self.checkpoint(91, tier="standard")
        self.write_faithful([(90, "cheap"), (91, "cheap")])

    def checkpoint(self, *heads, tier="cheap", repository="owner/repo"):
        """E heads with a recorded reading of `tier` where the rule routed
        `standard`: the faithfulness draw's source, moved unless `tier` is
        `standard`."""
        with contextlib.closing(connect(self.db)) as connection:
            for n in heads:
                connection.execute(
                    "INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                    (f"ship:{n}", "2026-09-22T00:00:00+00:00", json.dumps({
                        "repository": repository, "head": sha(n), "reviewed_head": None,
                        "passes": [a_pass(sha(n), tier=tier)]})))

    def write_faithful(self, lines, match=True, state=None):
        """Step 1's answers as `ask_jev` writes them: (head, choice) per line,
        or (head, choice, fields) to change a record, each claiming `match`.
        A choice of None is no answer after the one retry."""
        source = {c["head"]: c for c in experiment.candidates(self.db)}
        records = []
        for n, choice, *fields in lines:
            answer = {"type": "choice", "choice": choice, "confidence": 0.9,
                      "probabilities": {**dict.fromkeys(experiment.TIERS, 0.0), choice: 0.9}}
            tries = [{"exit": 0, "wait_ms": 300, "error": []}]
            if choice is None:
                answer, tries = None, [{"exit": 1, "wait_ms": 300, "error": ["refused"]}] * 2
            records.append(dict({
                "repository": "owner/repo", "head": sha(n), "recorded": "cheap", "match": match,
                "moved": True, "state": state or source.get(sha(n), {}).get("state", "{}"),
                "answer": answer, "tries": tries}, **(fields[0] if fields else {})))
        self.faithful.mkdir(exist_ok=True)
        (self.faithful / "answers.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records))
        # Stored claims the gate must not trust: each line's flag and a summary.
        claimed = len(lines) if match else 0
        (self.faithful / "summary.json").write_text(json.dumps({
            "asked": len(lines), "matched": claimed, "threshold": experiment.FAITHFUL_PASS,
            "passed": claimed >= experiment.FAITHFUL_PASS}))

    def attest(self, more=(), registration=None):
        """A note on sd:2764 binding step 1's files to the registration and population."""
        more = list(more)
        document = Path(more[more.index("--preregistration") + 1]) if "--preregistration" in more \
            else experiment.REGISTRATION
        hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (
            self.faithful / "answers.jsonl", document, self.population,
            self.faithful / "summary.json")]
        if registration is not None:
            hashes[1] = registration
        stamp = "2026-10-11T00:00:00+00:00"
        with contextlib.closing(connect(self.db)) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO item (id, kind, title, status, created_at, updated_at)"
                " VALUES (2764, 'work', 't', 'in_progress', ?, ?)", (stamp, stamp))
            connection.execute("INSERT INTO note (item, timestamp, kind, body) VALUES"
                               " (2764, ?, 'comment', ?)", (stamp, " ".join(hashes)))

    def write_population(self, paths, private=()):
        """Heads 0 and 1 are C1; a head in `private` holds a path no public repository tracks."""
        reason = "no category matched; default tier standard"
        self.state = experiment.state_of(paths, 3, reason)
        hidden = experiment.state_of(["secret/plan.md"], 3, reason)
        self.population.write_text("".join(json.dumps({
            "repository": "owner/repo", "head": sha(n), "alias": "R01", "paper": False,
            "population": "C1" if n < 2 else "E",
            "state": hidden if n in private else self.state}) + "\n"
            for n in range(self.HEADS)))

    def env(self, **extra):
        return super().env(JEV_TRACES_TIMEOUT="5", JEV_COMPARE_ANTHROPIC_KEY="test-key", **extra)

    def replay(self, arm, *more, env=None, faithful=True, attest=True):
        if attest and self.faithful.exists():
            self.attest(more)
        gate = ["--faithful", str(self.faithful)] if faithful else []
        return run(["replay", "--arm", arm, "--population", str(self.population),
                    "--out", str(self.folder), "--ledger", str(self.ledger), "--db", str(self.db),
                    "--traces", self.base + "/v1/traces", *gate, *more], env or self.env())

    def answers(self, name):
        path = self.folder / "replay" / f"{name}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def rows(self, db=None):
        with contextlib.closing(connect(db or self.ledger, write=False)) as connection:
            return [tuple(r) for r in connection.execute(
                "SELECT arm, stage, run_id, outcome, count(*) FROM judgment"
                " GROUP BY arm, stage, run_id, outcome ORDER BY arm")]

    def spans(self):
        return [s for s in Arm.seen if s["path"] == "/v1/traces"]

    def test_five_heads_write_fifteen_answers_fifteen_spans_and_no_live_row(self):
        for arm in ("kev", "jev", "haiku"):
            code, out = self.replay(arm)
            self.assertEqual(code, 0, out)
        found = [line for arm in ("kev", "jev", "haiku") for line in self.answers(arm)]
        self.assertEqual(len(found), 15)
        self.assertEqual({(a["arm"], a["tier"]) for a in found},
                         {("kev", "deep"), ("jev", "skip"), ("haiku", "standard")})
        self.assertEqual(len(self.spans()), 15)
        resource = self.spans()[0]["body"]["resourceSpans"][0]["resource"]["attributes"]
        self.assertIn({"key": "openinference.project.name",
                       "value": {"stringValue": "jev-experiment-entry-2"}}, resource)
        self.assertNotIn("feedface", json.dumps([s["body"] for s in self.spans()]))
        self.assertEqual(self.rows(), [
            ("haiku", "JEV_SD_REVIEW", "jev-entry-2-replay", "ok", 5),
            ("jev", "JEV_SD_REVIEW", "jev-entry-2-replay", "ok", 5),
            ("kev", "JEV_SD_REVIEW", "jev-entry-2-replay", "ok", 5)])
        self.assertEqual(self.rows(self.live), [])
        manifest = json.loads((self.folder / "manifest.json").read_text())
        self.assertEqual(manifest["registration"]["commit"], experiment.REGISTRATION_COMMIT)
        self.assertEqual(manifest["population"]["sha256"],
                         hashlib.sha256(self.population.read_bytes()).hexdigest())
        self.assertEqual(manifest["heuristic_sha256"], experiment.HEURISTIC_SHA256)
        self.assertEqual(manifest["arms"]["kev"]["spans_sent"], 5)

    def test_the_payload_is_the_one_step_one_sends(self):
        experiment.ask_jev(self.state, self.env(JEV_METER="0"), sleep=lambda _s: None)
        self.assertEqual(self.replay("jev")[0], 0)
        first, replayed = Stub.seen[0]["payload"], Stub.seen[1]["payload"]
        self.assertEqual(first, replayed)

    def test_a_second_run_without_rerun_refuses(self):
        self.assertEqual(self.replay("kev")[0], 0)
        before = len(Arm.seen)
        self.assertEqual(self.replay("kev")[0], 2)
        self.assertEqual(len(Arm.seen), before)
        code, _out = self.replay("kev", "--rerun", "the stub moved")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.answers("kev.run2")), 5)
        manifest = json.loads((self.folder / "manifest.json").read_text())
        self.assertEqual(manifest["arms"]["kev.run2"]["reason"], "the stub moved")

    def test_the_cap_reserves_both_attempts_before_each_head(self):
        Stub.input_tokens = 10_000_000          # $0.42 a call at Jev's list price
        with unittest.mock.patch.object(experiment, "reserve", return_value=0.42, create=True):
            code, _out = self.replay("jev", "--cap", "1.0")
            self.assertEqual(code, 1)
            self.assertEqual((len(Stub.seen), len(self.answers("jev"))), (1, 1))
            self.assertLessEqual(sum(a["usd"] for a in self.answers("jev")), 1.0)
            code, _out = self.replay("jev", "--cap", "1.0")
        self.assertEqual((code, len(Stub.seen)), (2, 1))           # halted until resumed

    def test_an_answer_with_no_cost_is_charged_its_reservation_and_halts(self):
        Stub.drop_usage = True
        with unittest.mock.patch.object(experiment, "reserve", return_value=0.01, create=True):
            code, _out = self.replay("jev")
        self.assertEqual((code, len(Stub.seen)), (1, 1))
        self.assertEqual([a["usd"] for a in self.answers("jev")], [0.01])
        manifest = json.loads((self.folder / "manifest.json").read_text())
        self.assertIn("no cost", manifest["arms"]["jev"]["halted"])

    def test_a_call_stopped_before_its_answer_keeps_its_reserved_charge(self):
        real = experiment.ask_arm

        def stop(*args, **kwargs):
            real(*args, **kwargs)
            raise KeyboardInterrupt("stopped between the call and its answer")
        with unittest.mock.patch.object(experiment, "reserve", return_value=0.42, create=True):
            with unittest.mock.patch.object(experiment, "ask_arm", stop):
                with self.assertRaises(KeyboardInterrupt):
                    self.replay("jev", "--cap", "1.0")
            code, _out = self.replay("jev", "--cap", "1.0")
        self.assertEqual(code, 1)                   # 0.84 kept, so no second head fits
        self.assertEqual([(a["outcome"], a["usd"]) for a in self.answers("jev")],
                         [("interrupted", 0.84)])
        self.assertEqual(len(Stub.seen), 1)

    def test_a_cap_that_is_not_a_finite_amount_is_refused(self):
        for cap in ("nan", "inf", "-1"):
            with self.subTest(cap=cap), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.replay("jev", "--cap", cap)
        self.assertEqual(Stub.seen, [])

    def draft(self, text):
        path = Path(tempfile.mkdtemp()) / "pre.md"
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        return path

    def test_an_open_or_unregistered_prediction_refuses_before_any_call(self):
        text = experiment.REGISTRATION.read_bytes()
        h3 = b"PREDICTION (operator, 2026-10-05): H3 holds"
        for name, draft in (
                # The replay's hosted scope is the 2026-10-11 amendment's.
                ("without the amendment", text[:experiment.REGISTERED_BYTES]),
                ("an edited amendment", text.replace(b"30 C1 heads", b"59 C1 heads")),
                ("blank", text + b"\nPREDICTION (operator, 2026-10-11): ___\n"),
                ("whitespace", text + b"\nPREDICTION (operator, 2026-10-11):   \t\n"),
                ("unconfirmed", text + b"\nPREDICTION (operator, drafted, UNCONFIRMED): x\n"),
                ("empty", b""),
                ("edited", text.replace(h3, b"PREDICTION (operator, 2026-10-05): H3 fails"))):
            with self.subTest(name=name):
                self.folder = Path(tempfile.mkdtemp())       # no earlier run's manifest
                code, _out = self.replay("jev", "--preregistration", str(self.draft(draft)))
                self.assertEqual(code, 2)
        self.assertEqual(Stub.seen, [])
        self.assertFalse((self.folder / "replay").exists())

    def test_the_registration_is_frozen_across_arms_and_a_resume(self):
        document = self.draft(experiment.REGISTRATION.read_bytes())
        self.assertEqual(self.replay("kev", "--preregistration", str(document))[0], 0)
        with document.open("ab") as fh:
            fh.write(b"\n## Amendment, later\n\nA line added after the replay started.\n")
        self.assertEqual(self.replay("jev", "--preregistration", str(document))[0], 2)
        self.assertEqual(self.replay("kev", "--preregistration", str(document),
                                     "--rerun", "x")[0], 2)
        self.assertEqual(Stub.seen, [])
        manifest = json.loads((self.folder / "manifest.json").read_text())
        self.assertEqual(manifest["registration"]["document"],
                         hashlib.sha256(experiment.REGISTRATION.read_bytes()).hexdigest())

    def test_a_changed_heuristic_refuses(self):
        with unittest.mock.patch.object(experiment, "HEURISTIC_SHA256", "0" * 64):
            self.assertEqual(self.replay("kev")[0], 2)
        self.assertEqual(Arm.seen, [])

    def test_the_live_ledger_and_a_refusing_ledger_stop_the_replay(self):
        for ledger in (self.live, self.folder / "missing" / "x.db"):
            with self.subTest(ledger=ledger):
                self.ledger = ledger
                self.assertEqual(self.replay("kev")[0], 2)
        self.assertEqual((Arm.seen, self.rows(self.live)), ([], []))

    def test_hosted_arms_are_asked_about_the_public_pool_only_and_kev_about_all(self):
        self.write_population(["a.py"], private=(1, 3))
        for arm in ("jev", "haiku", "kev"):
            self.assertEqual(self.replay(arm)[0], 0)
        self.assertEqual([len(self.answers(arm)) for arm in ("jev", "haiku", "kev")], [3, 3, 5])
        self.assertEqual({a["head"] for a in self.answers("jev") + self.answers("haiku")},
                         {sha(0), sha(2), sha(4)})
        sent = json.dumps(Stub.seen + [s for s in Arm.seen if s["path"] == "/v1/messages"])
        self.assertNotIn("secret", sent)
        manifest = json.loads((self.folder / "manifest.json").read_text())
        self.assertEqual([manifest["arms"][arm]["scope"]["heads"] for arm in ("jev", "kev")],
                         [3, 5])

    def test_a_resume_whose_public_pool_moved_refuses(self):
        real = experiment.jev_meter.write
        calls = []

        def stop_on_second(row, env=None):
            calls.append(row)
            if len(calls) == 2:
                raise KeyboardInterrupt("stopped")
            return real(row, env)
        with unittest.mock.patch.object(experiment.jev_meter, "write", stop_on_second):
            with self.assertRaises(KeyboardInterrupt):
                self.replay("jev")
        before = len(Stub.seen)
        with unittest.mock.patch.object(experiment, "public_paths", return_value=frozenset()):
            self.assertEqual(self.replay("jev")[0], 2)
        self.assertEqual(len(Stub.seen), before)

    def test_a_failed_call_is_retried_once_then_recorded_as_no_answer(self):
        Stub.status = 500
        code, _out = self.replay("jev")
        self.assertEqual(code, 0)
        self.assertEqual(len(Stub.seen), 10)
        found = self.answers("jev")
        self.assertEqual({(a["tier"], a["outcome"], len(a["attempts"])) for a in found},
                         {(None, "unavailable", 2)})
        self.assertEqual(self.rows(), [("jev", "JEV_SD_REVIEW", "jev-entry-2-replay",
                                        "unavailable", 5)])

    def test_step_one_answers_are_reused_for_the_same_state(self):
        self.checkpoint(0)
        self.checkpoint(1, tier="standard")
        self.write_faithful([(0, "cheap"), (1, "cheap")])
        population = [json.loads(line) for line in self.population.read_text().splitlines()]
        population[1]["state"] = "{}"                 # head 1's population state differs
        self.population.write_text("".join(json.dumps(r) + "\n" for r in population))
        self.assertEqual(self.replay("jev")[0], 0)
        found = {a["head"]: a for a in self.answers("jev")}
        self.assertEqual((found[sha(0)]["reused"], found[sha(0)]["tier"]),
                         ("faithful-2", "cheap"))
        self.assertIsNone(found[sha(1)]["reused"])   # another state is asked again
        self.assertEqual(len(Stub.seen), 4)

    def test_a_run_stopped_after_a_call_does_not_ask_that_head_again(self):
        real = experiment.ask_arm
        calls = []

        def stop_on_second(*args, **kwargs):
            calls.append(real(*args, **kwargs))
            if len(calls) == 2:
                raise KeyboardInterrupt("stopped between the call and its answer")
            return calls[-1]
        with unittest.mock.patch.object(experiment, "ask_arm", stop_on_second):
            with self.assertRaises(KeyboardInterrupt):
                self.replay("kev")
        self.assertTrue((self.folder / "replay" / "kev.asking").exists())
        self.assertEqual(self.replay("kev")[0], 0)
        found = self.answers("kev")
        self.assertEqual(len(found), 5)
        self.assertEqual([a["outcome"] for a in found].count("interrupted"), 1)
        kev_calls = [s for s in Arm.seen if s["path"] == "/v1/systemone"]
        self.assertEqual(len(kev_calls), 5)
        self.assertFalse((self.folder / "replay" / "kev.asking").exists())

    def test_a_ledger_that_refuses_a_row_halts_and_keeps_the_answer(self):
        with unittest.mock.patch.object(experiment.jev_meter, "write",
                                        return_value=("refused", None)):
            self.assertEqual(self.replay("kev")[0], 1)
        kev_calls = lambda: [s for s in Arm.seen if s["path"] == "/v1/systemone"]  # noqa: E731
        self.assertEqual((len(kev_calls()), self.answers("kev")), (1, []))
        self.assertEqual(self.replay("kev", "--resume", "the ledger is back")[0], 0)
        found = self.answers("kev")
        self.assertEqual({(a["outcome"], a["ledger"]) for a in found}, {("ok", "written")})
        self.assertEqual((len(found), len(kev_calls())), (5, 5))
        self.assertEqual(self.rows(), [("kev", "JEV_SD_REVIEW", "jev-entry-2-replay", "ok", 5)])

    def test_a_row_written_before_a_stop_is_not_written_twice(self):
        real = experiment.jev_meter.write

        def write_then_stop(row, env=None):
            said = real(row, env)
            raise KeyboardInterrupt(f"stopped after the ledger said {said[0]}")
        with unittest.mock.patch.object(experiment.jev_meter, "write", write_then_stop):
            with self.assertRaises(KeyboardInterrupt):
                self.replay("kev")
        self.assertEqual(self.replay("kev")[0], 0)
        self.assertEqual(self.rows(), [("kev", "JEV_SD_REVIEW", "jev-entry-2-replay", "ok", 5)])
        self.assertEqual(len(self.answers("kev")), 5)

    def test_an_unreadable_ledger_on_resume_halts_and_asks_nothing(self):
        with unittest.mock.patch.object(experiment.jev_meter, "write",
                                        return_value=("refused", None)):
            self.assertEqual(self.replay("kev")[0], 1)
        before = len(Arm.seen)
        with unittest.mock.patch.object(experiment, "recorded", return_value="unreadable"):
            self.assertEqual(self.replay("kev", "--resume", "the ledger is back")[0], 1)
        self.assertEqual(len(Arm.seen), before)
        self.assertTrue((self.folder / "replay" / "kev.asking").exists())
        manifest = json.loads((self.folder / "manifest.json").read_text())
        self.assertIn("unreadable", manifest["arms"]["kev"]["halted"])

    def test_answers_no_manifest_names_refuse(self):
        self.assertEqual(self.replay("kev")[0], 0)
        (self.folder / "manifest.json").unlink()
        self.assertEqual(self.replay("kev")[0], 2)

    def test_another_arm_s_waiting_call_counts_against_the_cap(self):
        replay = self.folder / "replay"
        replay.mkdir()
        (replay / "jev.asking").write_text(json.dumps({"repository": "owner/repo",
                                                       "head": sha(0), "reserved": 0.84}))
        with unittest.mock.patch.object(experiment, "reserve", return_value=0.42, create=True):
            self.assertEqual(self.replay("haiku", "--cap", "1.0")[0], 1)
        self.assertEqual([s for s in Arm.seen if s["path"] == "/v1/messages"], [])

    def test_a_kev_outage_pauses_the_arm_once_and_a_second_ends_it(self):
        Arm.status = 500
        kev_calls = lambda: [s for s in Arm.seen if s["path"] == "/v1/systemone"]  # noqa: E731
        with unittest.mock.patch.object(experiment, "KEV_OUTAGE", 2, create=True):
            self.assertEqual(self.replay("kev")[0], 1)          # two failed heads pause it
            self.assertEqual(len(self.answers("kev")), 2)
            before = len(kev_calls())
            with unittest.mock.patch.object(experiment, "kev_up", return_value=False,
                                            create=True):
                self.assertEqual(self.replay("kev", "--resume", "back?")[0], 2)  # still down
            self.assertEqual(len(kev_calls()), before)
            with unittest.mock.patch.object(experiment, "kev_up", return_value=True,
                                            create=True):
                self.assertEqual(self.replay("kev", "--resume", "kev is back")[0], 0)
        found = self.answers("kev")
        self.assertEqual([a["outcome"] for a in found],
                         ["unavailable"] * 4 + ["ended"])
        self.assertEqual(len(kev_calls()), 8)
        entry = json.loads((self.folder / "manifest.json").read_text())["arms"]["kev"]
        self.assertEqual((entry["outages"], bool(entry["finished"])), (2, True))

    def test_a_resume_on_another_population_refuses(self):
        self.stop_after_first_call("kev")
        self.write_population(["pkg/mod.py"])
        self.assertEqual(self.replay("kev")[0], 2)
        self.assertEqual(len([s for s in Arm.seen if s["path"] == "/v1/systemone"]), 1)

    def test_a_head_that_costs_more_than_its_reservation_halts(self):
        Stub.input_tokens = 10_000_000          # $0.42, far above a few kilobytes' bound
        code, _out = self.replay("jev", "--cap", "1.0")
        self.assertEqual((code, len(Stub.seen)), (1, 1))
        manifest = json.loads((self.folder / "manifest.json").read_text())
        self.assertIn("above its reservation", manifest["arms"]["jev"]["halted"])

    def test_haiku_with_no_key_refuses(self):
        self.assertEqual(self.replay("haiku", env=self.env_without("JEV_COMPARE_ANTHROPIC_KEY"))[0],
                         2)
        self.assertEqual(Arm.seen, [])

    def env_without(self, name):
        env = self.env()
        env.pop(name)
        return env

    def test_kev_off_this_machine_is_refused(self):
        remote = [(2, 1, 6, "", ("198.51.100.7", 80)), (2, 1, 6, "", ("127.0.0.1", 80))]
        for url, resolved in (("http://192.0.2.1:9/v1/systemone", None),
                              ("file:///v1/systemone", None),
                              ("http://kev.example.test/v1/systemone", [remote]),
                              ("http://kev.example.test/v1/systemone", socket.gaierror(8, "no"))):
            with self.subTest(url=url, resolved=resolved), contextlib.ExitStack() as stack:
                if resolved is not None:     # no real lookup: it can wait 30 s here
                    stack.enter_context(unittest.mock.patch.object(
                        socket, "getaddrinfo", side_effect=resolved))
                self.assertEqual(self.replay("kev", env=self.env(JEV_COMPARE_KEV_URL=url))[0], 2)
        self.assertEqual(Arm.seen, [])

    def test_kev_s_redirect_is_not_followed(self):
        landed = []

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(302)
                self.send_header("Location", "/landed")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                landed.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/systemone"
        code, _out = self.replay("kev", env=self.env(JEV_COMPARE_KEV_URL=url, KEV_API_KEY="k"))
        self.assertEqual(code, 0)
        self.assertEqual(landed, [])
        self.assertEqual({a["outcome"] for a in self.answers("kev")}, {"unavailable"})

    def in_turn(self, *events):
        """`ask_once` answers `events` in turn, then the last one again; the
        list it returns counts the calls."""
        calls = []

        def fake(arm, payload, definition, conf, env):
            calls.append(arm)
            return dict(events[min(len(calls), len(events)) - 1])
        self.enterContext(unittest.mock.patch.object(experiment, "ask_once", fake))
        return calls

    def costing(self, bound):
        self.enterContext(unittest.mock.patch.object(experiment, "reserve", return_value=bound,
                                                     create=True))

    def test_a_first_attempt_above_its_reservation_is_not_retried(self):
        self.costing(0.1)
        calls = self.in_turn(BILLED_INVALID, CHEAP)
        self.assertEqual(self.replay("jev")[0], 1)
        self.assertEqual(len(calls), 1)
        manifest = json.loads((self.folder / "manifest.json").read_text())
        self.assertIn("above its reservation", manifest["arms"]["jev"]["halted"])

    def test_a_first_attempt_with_no_cost_is_not_retried(self):
        self.costing(0.5)
        calls = self.in_turn(UNPRICED_INVALID, CHEAP)
        self.assertEqual(self.replay("jev")[0], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual([a["usd"] for a in self.answers("jev")], [0.5])

    def test_the_ledger_row_carries_every_attempt_s_cost(self):
        self.costing(0.5)
        self.in_turn(BILLED_INVALID, CHEAP)
        self.assertEqual(self.replay("jev", "--cap", "1.0")[0], 1)   # the next head does not fit
        with contextlib.closing(connect(self.ledger, write=False)) as connection:
            rows = [tuple(r) for r in connection.execute("SELECT outcome, answer, usd FROM judgment")]
        self.assertEqual([(o, a, round(u, 2)) for o, a, u in rows], [("ok", "2", 0.43)])
        self.assertEqual([(a["tier"], len(a["attempts"])) for a in self.answers("jev")],
                         [("cheap", 2)])

    def test_a_recovered_answer_with_no_cost_halts_and_asks_nothing_more(self):
        Stub.drop_usage = True
        with unittest.mock.patch.object(experiment.jev_meter, "write",
                                        return_value=("refused", None)):
            self.assertEqual(self.replay("jev")[0], 1)
        self.assertEqual(len(Stub.seen), 1)
        self.assertEqual(self.replay("jev")[0], 2)                   # halted until resumed
        self.assertEqual(self.replay("jev", "--resume", "the ledger is back")[0], 1)
        self.assertEqual(len(Stub.seen), 1)
        self.assertEqual([a["tier"] for a in self.answers("jev")], ["skip"])
        manifest = json.loads((self.folder / "manifest.json").read_text())
        self.assertIn("no cost", manifest["arms"]["jev"]["halted"])

    def test_a_halt_holds_until_it_is_resumed_by_name(self):
        self.costing(0.42)
        self.assertEqual(self.replay("jev", "--cap", "0.5")[0], 1)
        self.assertEqual(self.replay("jev")[0], 2)
        self.assertEqual(Stub.seen, [])
        self.assertEqual(self.replay("jev", "--resume", "the cap was a typo")[0], 0)
        self.assertEqual(len(Stub.seen), 5)
        manifest = json.loads((self.folder / "manifest.json").read_text())
        self.assertEqual([r["reason"] for r in manifest["arms"]["jev"]["resumes"]],
                         ["the cap was a typo"])

    def test_a_resume_refuses_lines_that_are_not_final_answers(self):
        replay = self.stop_after_first_call("kev")
        other = {"repository": "owner/repo", "head": sha(3), "arm": "kev",
                 "run_id": "jev-entry-2-replay", "outcome": "ok", "tier": "deep", "usd": 0.0,
                 "at": "2026-10-11T00:00:00+00:00"}
        for name, line in (("identity only", {"repository": "owner/repo", "head": sha(3)}),
                           ("another arm", dict(other, arm="jev")),
                           ("another run", dict(other, run_id="jev-entry-2-replay-2")),
                           ("not final", dict(other, outcome="pending", tier=None)),
                           ("an answer with no tier", dict(other, tier=None)),
                           ("no cost", dict(other, usd=None)),
                           ("ended with no outage", dict(other, outcome="ended", tier=None))):
            with self.subTest(name=name):
                (replay / "kev.jsonl").write_text(json.dumps(line) + "\n")
                self.assertEqual(self.replay("kev")[0], 2)
        self.assertEqual(len([s for s in Arm.seen if s["path"] == "/v1/systemone"]), 1)

    def seal(self, *arms):
        """A note on sd:2764 carrying each arm's answer file hash."""
        stamp = "2026-10-11T00:00:00+00:00"
        with contextlib.closing(connect(self.db)) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO item (id, kind, title, status, created_at, updated_at)"
                " VALUES (2764, 'work', 't', 'in_progress', ?, ?)", (stamp, stamp))
            connection.execute("INSERT INTO note (item, timestamp, kind, body) VALUES"
                               " (2764, ?, 'comment', ?)", (stamp, " ".join(
                                   hashlib.sha256((self.folder / "replay" / f"{arm}.jsonl")
                                                  .read_bytes()).hexdigest() for arm in arms)))

    def labels_c1(self):
        return run(["labels", "objective", "--population", "C1", "--db", str(self.db),
                    "--out", str(self.folder)], self.env())

    def test_c1_labels_open_only_once_every_arm_answered_each_c1_head(self):
        self.write_population(["a.py"], private=(1,))
        for arm in ("kev", "jev"):
            self.assertEqual(self.replay(arm)[0], 0)
        real = experiment.jev_meter.write
        calls = []

        def stop_on_second(row, env=None):
            calls.append(row)
            if len(calls) == 2:
                raise KeyboardInterrupt("stopped")
            return real(row, env)
        with unittest.mock.patch.object(experiment.jev_meter, "write", stop_on_second):
            with self.assertRaises(KeyboardInterrupt):
                self.replay("haiku")
        self.seal("kev", "jev", "haiku")
        self.assertEqual(self.labels_c1()[0], 2)        # Haiku's file is partial
        self.assertEqual(self.replay("haiku")[0], 0)
        self.assertEqual(self.labels_c1()[0], 2)        # its finished file's hash is not noted
        self.seal("haiku")
        registered = self.population.read_bytes()
        self.population.write_bytes(registered + self.population.read_bytes().splitlines(True)[0])
        self.assertEqual(self.labels_c1()[0], 2)        # not the population the replay read
        self.population.write_bytes(registered)
        with unittest.mock.patch.object(experiment, "public_paths", return_value=frozenset()):
            self.assertEqual(self.labels_c1()[0], 2)    # the hosted scope moved
        code, printed = self.labels_c1()
        self.assertEqual((code, json.loads(printed)["all"]["heads"]), (0, 2))
        self.assertEqual(self.labels_c1()[0], 2)        # labels are written once

    def stop_after_first_call(self, arm):
        """A run stopped after its first call, before that call's answer."""
        real = experiment.ask_arm

        def stop(*args, **kwargs):
            real(*args, **kwargs)
            raise KeyboardInterrupt("stopped between the call and its answer")
        with unittest.mock.patch.object(experiment, "ask_arm", stop):
            with self.assertRaises(KeyboardInterrupt):
                self.replay(arm)
        return self.folder / "replay"

    def test_identity_only_lines_do_not_seal_c1(self):
        for arm in ("kev", "jev", "haiku"):
            self.assertEqual(self.replay(arm)[0], 0)
        path = self.folder / "replay" / "haiku.jsonl"
        path.write_text("".join(json.dumps({"repository": a["repository"], "head": a["head"]})
                                + "\n" for a in self.answers("haiku")))
        self.seal("kev", "jev", "haiku")
        self.assertEqual(self.labels_c1()[0], 2)
        self.assertFalse((self.folder / "labels").exists())

    def test_a_line_cut_off_by_a_stop_is_dropped_and_its_head_not_asked_again(self):
        replay = self.stop_after_first_call("kev")
        (replay / "kev.jsonl").write_text('{"repository": "owner/repo", "head": "' + sha(0)[:10])
        self.assertEqual(self.replay("kev")[0], 0)
        found = self.answers("kev")
        self.assertEqual(([a["outcome"] for a in found].count("interrupted"), len(found)), (1, 5))
        self.assertEqual(len([s for s in Arm.seen if s["path"] == "/v1/systemone"]), 5)

    def test_a_marker_without_a_reservation_refuses(self):
        marker = self.stop_after_first_call("jev") / "jev.asking"
        marker.write_text(json.dumps({"repository": "owner/repo", "head": sha(0)}))
        self.assertEqual(self.replay("jev")[0], 2)
        self.assertEqual(len(Stub.seen), 1)

    def one_head(self):
        self.HEADS = 1
        self.write_population(["a.py"])

    def test_a_recovered_answer_over_the_cap_halts_the_last_head(self):
        self.one_head()
        self.costing(0.4)
        self.in_turn(dict(CHEAP, usd=0.4))
        with unittest.mock.patch.object(experiment.jev_meter, "write",
                                        return_value=("refused", None)):
            self.assertEqual(self.replay("jev", "--cap", "2.0")[0], 1)
        (self.folder / "replay" / "haiku.jsonl").write_text(json.dumps({"usd": 0.8}) + "\n")
        self.assertEqual(self.replay("jev", "--resume", "the ledger is back", "--cap", "1.0")[0], 1)
        self.assertEqual([a["usd"] for a in self.answers("jev")], [0.4])
        manifest = json.loads((self.folder / "manifest.json").read_text())
        self.assertIn("spend cap", manifest["arms"]["jev"]["halted"])

    def test_an_interrupted_call_over_the_cap_halts_the_last_head(self):
        self.one_head()
        self.costing(0.4)
        self.stop_after_first_call("jev")
        (self.folder / "replay" / "haiku.jsonl").write_text(json.dumps({"usd": 0.5}) + "\n")
        self.assertEqual(self.replay("jev", "--cap", "1.0")[0], 1)
        self.assertEqual([(a["outcome"], a["usd"]) for a in self.answers("jev")],
                         [("interrupted", 0.8)])
        manifest = json.loads((self.folder / "manifest.json").read_text())
        self.assertIn("spend cap", manifest["arms"]["jev"]["halted"])

    def test_an_interrupted_call_gets_its_ledger_row_before_its_line(self):
        self.costing(0.42)
        replay = self.stop_after_first_call("jev")
        with unittest.mock.patch.object(experiment.jev_meter, "write",
                                        return_value=("refused", None)):
            self.assertEqual(self.replay("jev", "--cap", "1.0")[0], 1)
        self.assertEqual((self.answers("jev"), (replay / "jev.asking").exists()), ([], True))
        self.assertEqual(self.replay("jev", "--resume", "the ledger is back", "--cap", "1.0")[0], 1)
        with contextlib.closing(connect(self.ledger, write=False)) as connection:
            rows = [tuple(r) for r in connection.execute(
                "SELECT outcome, error_class, usd, run_id FROM judgment")]
        self.assertEqual(rows, [("unavailable", "Interrupted", 0.84, experiment.RUN_ID)])
        self.assertEqual([a["outcome"] for a in self.answers("jev")], ["interrupted"])
        self.assertFalse((replay / "jev.asking").exists())

    def test_another_row_under_this_pair_is_not_this_answer(self):
        self.costing(0.42)
        self.stop_after_first_call("jev")
        instance = json.loads((self.folder / "manifest.json").read_text())["instance"]
        pair = hashlib.sha256(f"{instance}:{experiment.RUN_ID}:owner/repo:{sha(0)}".encode())
        said, _id = experiment.jev_meter.write(
            {"caller": "jev-experiment", "stage": "JEV_SD_REVIEW", "arm": "jev",
             "pair": pair.hexdigest()[:16], "provider": "typesafe", "model": "m",
             "primitive": "choice", "outcome": "ok", "answer": "1", "usd": 0.01,
             "run_id": experiment.RUN_ID},
            {"JEV_METER": "1", "JEV_METER_DB": str(self.ledger)})
        self.assertEqual(said, "written")
        self.assertEqual(self.replay("jev")[0], 1)
        self.assertEqual(self.answers("jev"), [])
        self.assertTrue((self.folder / "replay" / "jev.asking").exists())
        manifest = json.loads((self.folder / "manifest.json").read_text())
        self.assertIn("another row", manifest["arms"]["jev"]["halted"])

    def test_a_stop_while_kev_s_ended_lines_are_written_asks_kev_nothing_more(self):
        Arm.status = 500
        kev_calls = lambda: [s for s in Arm.seen if s["path"] == "/v1/systemone"]  # noqa: E731
        real = json.dumps

        def stop_at_ended(value, *args, **kwargs):
            if isinstance(value, dict) and value.get("outcome") == "ended":
                raise KeyboardInterrupt("stopped while the ended lines were written")
            return real(value, *args, **kwargs)
        self.enterContext(unittest.mock.patch.object(experiment, "KEV_OUTAGE", 2, create=True))
        self.enterContext(unittest.mock.patch.object(experiment, "kev_up", return_value=True,
                                                     create=True))
        self.assertEqual(self.replay("kev")[0], 1)
        with unittest.mock.patch.object(experiment.json, "dumps", stop_at_ended):
            with self.assertRaises(KeyboardInterrupt):
                self.replay("kev", "--resume", "kev is back")
        before = len(kev_calls())
        self.assertEqual(self.replay("kev")[0], 0)
        self.assertEqual(len(kev_calls()), before)
        self.assertEqual([a["outcome"] for a in self.answers("kev")],
                         ["unavailable"] * 4 + ["ended"])

    def test_another_arm_s_held_answer_counts_at_its_cost(self):
        self.costing(0.1)
        calls = self.in_turn(dict(CHEAP, usd=1.2))
        with unittest.mock.patch.object(experiment.jev_meter, "write",
                                        return_value=("refused", None)):
            self.assertEqual(self.replay("jev", "--cap", "10")[0], 1)
        self.assertEqual(self.replay("haiku", "--cap", "1.0")[0], 1)
        self.assertEqual(len(calls), 1)

    def test_two_folders_sharing_a_ledger_keep_a_row_each(self):
        self.one_head()
        self.costing(0.42)
        for folder in ("first", "second"):
            self.folder = self.folder.parent / folder
            self.folder.mkdir()
            self.stop_after_first_call("jev")
            self.assertEqual(self.replay("jev")[0], 0)
        with contextlib.closing(connect(self.ledger, write=False)) as connection:
            rows = connection.execute("SELECT count(*), sum(usd) FROM judgment"
                                      " WHERE error_class = 'Interrupted'").fetchone()
        self.assertEqual((rows[0], round(rows[1], 2)), (2, 1.68))

    def test_kev_s_failure_streak_survives_a_stop(self):
        Arm.status = 500
        self.enterContext(unittest.mock.patch.object(experiment, "KEV_OUTAGE", 2, create=True))
        reserve = unittest.mock.patch.object(experiment, "reserve", create=True,
                                             side_effect=[0.0, KeyboardInterrupt("stopped")])
        with reserve, self.assertRaises(KeyboardInterrupt):
            self.replay("kev")
        self.assertEqual(self.replay("kev")[0], 1)
        self.assertEqual(len(self.answers("kev")), 2)

    def test_kev_s_outage_is_saved_before_the_line_that_starts_it(self):
        Arm.status = 500
        kev_calls = lambda: [s for s in Arm.seen if s["path"] == "/v1/systemone"]  # noqa: E731
        self.enterContext(unittest.mock.patch.object(experiment, "KEV_OUTAGE", 2, create=True))
        real, lines = json.dumps, []

        def stop_at_second_line(value, *args, **kwargs):
            if isinstance(value, dict) and value.get("outcome") == "unavailable" \
                    and "run_id" in value:
                lines.append(value)
                if len(lines) == 2:
                    raise KeyboardInterrupt("stopped before the line")
            return real(value, *args, **kwargs)
        with unittest.mock.patch.object(experiment.json, "dumps", stop_at_second_line):
            with self.assertRaises(KeyboardInterrupt):
                self.replay("kev")
        before = len(kev_calls())
        self.assertEqual(self.replay("kev")[0], 2)                  # paused until resumed
        self.assertEqual(len(kev_calls()), before)
        with unittest.mock.patch.object(experiment, "kev_up", return_value=True, create=True):
            self.assertEqual(self.replay("kev", "--resume", "kev is back")[0], 0)
        found = self.answers("kev")
        self.assertEqual([a["outcome"] for a in found], ["unavailable"] * 4 + ["ended"])
        entry = json.loads((self.folder / "manifest.json").read_text())["arms"]["kev"]
        self.assertEqual(entry["outages"], 2)

    def test_a_failed_tail_repair_keeps_every_complete_line(self):
        real, calls = experiment.ask_arm, []

        def stop_third(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 3:
                raise KeyboardInterrupt("stopped before the third call")
            return real(*args, **kwargs)
        with unittest.mock.patch.object(experiment, "ask_arm", stop_third):
            with self.assertRaises(KeyboardInterrupt):
                self.replay("kev")
        answers = self.folder / "replay" / "kev.jsonl"
        with answers.open("a") as fh:
            fh.write('{"repository": "owner/repo", "head": "' + sha(2)[:10])

        def emptied(path, data):
            path.open("wb").close()
            raise OSError("disk full")
        with unittest.mock.patch.object(Path, "write_bytes", emptied), \
                unittest.mock.patch.object(experiment.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.replay("kev")
        self.assertEqual(len(self.answers("kev")), 2)
        before = len([s for s in Arm.seen if s["path"] == "/v1/systemone"])
        self.assertEqual(self.replay("kev")[0], 0)
        self.assertEqual([a["outcome"] for a in self.answers("kev")],
                         ["ok", "ok", "interrupted", "ok", "ok"])
        self.assertEqual(len([s for s in Arm.seen if s["path"] == "/v1/systemone"]), before + 2)

    def test_no_faithfulness_result_refuses_before_any_call(self):
        for name, folder in (("no flag", None), ("empty folder", Path(tempfile.mkdtemp()))):
            with self.subTest(name=name):
                if folder is None:
                    with self.assertRaises(SystemExit):
                        self.replay("jev", faithful=False)
                else:
                    self.assertEqual(self.replay("jev", "--faithful", str(folder),
                                                 faithful=False, attest=False)[0], 2)
        self.assertEqual(Stub.seen, [])
        self.assertFalse((self.folder / "replay").exists())

    def test_a_failing_faithfulness_result_refuses(self):
        self.checkpoint(*range(92, 190))
        for name, lines, kwargs in (
                ("no answer matches, every flag says it does", [(90, "deep"), (91, "deep")], {}),
                ("100 records, half matching", [(n, "cheap" if n % 2 else "deep")
                                                for n in (90, 91, *range(92, 190))], {}),
                ("one head twice", [(90, "cheap"), (90, "cheap")], {}),
                ("one record", [(90, "cheap")], {}),
                ("a head the draw cannot give", [(90, "cheap"), (250, "cheap")], {}),
                ("a state that is not the head's", [(90, "cheap"), (91, "cheap")],
                 {"state": "{}"}),
                ("an answer that is not a tier", [(90, "cheap"), (91, "maybe")], {})):
            with self.subTest(name=name):
                self.folder = Path(tempfile.mkdtemp())       # no earlier run's manifest
                self.write_faithful(lines, **kwargs)
                self.assertEqual(self.replay("jev")[0], 2)
                self.assertEqual(Stub.seen, [])

    def test_a_faithfulness_record_not_as_step_one_writes_it_refuses(self):
        # Class pass, round 14:47Z: every field the replay reuses is checked
        # as `ask_jev` writes it, so a record cannot pass as an answer.
        three = {**dict.fromkeys(experiment.TIERS, 0.0), "cheap": 3.0}
        failed = {"exit": 1, "wait_ms": 300, "error": []}
        for name, fields in (
                ("an answer without a choice", {"answer": {"type": "choice", "choice": None}}),
                ("a probability above 1", {"answer": {"type": "choice", "choice": "cheap",
                                                      "confidence": 0.9, "probabilities": three}}),
                ("probabilities without every tier", {"answer": {
                    "type": "choice", "choice": "cheap", "confidence": 0.9,
                    "probabilities": {"cheap": 0.9}}}),
                ("a confidence that is not a number", {"answer": {
                    "type": "choice", "choice": "cheap", "confidence": "high",
                    "probabilities": dict.fromkeys(experiment.TIERS, 0.25)}}),
                ("an answer from a failed attempt", {"tries": [failed]}),
                ("three attempts", {"tries": [failed, failed, {"exit": 0, "wait_ms": 1}]}),
                ("no answer before the one retry", {"answer": None, "tries": [failed]}),
                ("attempts that are not a list", {"tries": "none"})):
            with self.subTest(name=name):
                self.folder = Path(tempfile.mkdtemp())       # no earlier run's manifest
                self.write_faithful([(90, "cheap"), (91, "cheap", fields)])
                self.assertEqual(self.replay("jev")[0], 2)
                self.assertEqual(Stub.seen, [])
        for name, text in (("a line that is not JSON", "{\n"),
                           ("a record that is not an object", "[1]\n"),
                           ("a head that is not text", json.dumps(
                               {"repository": ["owner/repo"], "head": sha(91)}) + "\n")):
            with self.subTest(name=name):
                self.folder = Path(tempfile.mkdtemp())
                self.write_faithful([(90, "cheap")])
                with (self.faithful / "answers.jsonl").open("a") as fh:
                    fh.write(text)
                self.assertEqual(self.replay("jev")[0], 2)
                self.assertEqual(Stub.seen, [])

    def test_a_draw_that_is_not_the_registered_stratification_refuses(self):
        # Round 14:47Z: half the draw comes from moved heads, or every moved
        # head of the public pool when it holds fewer, all from public managed
        # repositories. The stored `moved` flags all claim a moved head; the
        # gate reads the checkpoints and the public checkouts.
        self.enterContext(unittest.mock.patch.object(experiment, "FAITHFUL_COUNT", 4))
        self.checkpoint(92, 93, 96, tier="standard")
        self.checkpoint(98, repository="other/repo")
        self.checkpoint(99, tier="standard", repository="other/repo")
        fewer = [(90, "cheap"), (92, "standard"), (93, "standard"), (96, "standard")]
        self.write_faithful(fewer)
        self.assertEqual(self.replay("kev")[0], 0)    # 90 is the pool's one moved head
        self.checkpoint(94, 95, 97)                    # now the pool holds four
        other = {"repository": "other/repo"}
        for name, lines, git in (
                ("one moved head of four in the pool", fewer, None),
                ("every head moved", [(90, "cheap"), (94, "cheap"), (95, "cheap"),
                                      (97, "cheap")], None),
                ("half moved, two from a repository that is not public",
                 [(90, "cheap"), (98, "cheap", other), (99, "standard", other),
                  (92, "standard")], None),
                ("half moved, git cannot read the public checkout",
                 [(90, "cheap"), (94, "cheap"), (92, "standard"), (93, "standard")],
                 lambda *_argv: None)):
            with self.subTest(name=name):
                self.folder = Path(tempfile.mkdtemp())
                self.write_faithful(lines)
                with contextlib.ExitStack() as stack:
                    if git is not None:
                        stack.enter_context(unittest.mock.patch.object(experiment, "git", git))
                    self.assertEqual(self.replay("jev")[0], 2)
                self.assertEqual(Stub.seen, [])
        self.folder = Path(tempfile.mkdtemp())
        self.write_faithful([(90, "cheap"), (94, "cheap"), (92, "standard"), (93, "standard")])
        self.assertEqual(self.replay("jev")[0], 0)

    def test_a_step_one_no_answer_is_reused_never_asked_again(self):
        # Round 14:47Z: the reuse map dropped every no-answer record, so the
        # replay asked those heads again: another retry pair, and a later
        # answer in place of the registered failure.
        self.checkpoint(0)
        self.checkpoint(1, tier="standard")
        for exits, outcome in (((1, 1), "unavailable"), ((None, None), "timeout"),
                               ((1, 0), "invalid")):
            with self.subTest(outcome=outcome):
                self.folder = Path(tempfile.mkdtemp())
                tries = [{"exit": code, "wait_ms": 300, "error": []} for code in exits]
                self.write_faithful([(0, "cheap"), (1, None, {"tries": tries})])
                before = len(Stub.seen)
                self.assertEqual(self.replay("jev")[0], 0)
                found = {a["head"]: a for a in self.answers("jev")}
                self.assertEqual({h: (found[sha(h)]["reused"], found[sha(h)]["tier"],
                                      found[sha(h)]["outcome"]) for h in (0, 1)},
                                 {0: ("faithful-2", "cheap", "ok"),
                                  1: ("faithful-2", None, outcome)})
                self.assertEqual(found[sha(1)]["attempts"], tries)
                self.assertEqual(len(Stub.seen) - before, 3)      # heads 2 to 4 only
                manifest = json.loads((self.folder / "manifest.json").read_text())
                self.assertEqual((manifest["arms"]["jev"]["reused"],
                                  manifest["arms"]["jev"]["no_answer"]), (2, 1))

    def test_a_reused_line_that_is_not_step_ones_answer_refuses_a_resume(self):
        # Codex, class pass: a resume took a line's `reused` claim on its word.
        self.checkpoint(0)
        self.checkpoint(1, tier="standard")
        self.write_faithful([(0, "cheap"), (1, "cheap")])
        for name, change in (("another tier", lambda first, second: (
                                  dict(first, tier="skip"), second)),
                             ("a head step 1 never asked", lambda first, second: (
                                  first, dict(second, head=sha(3))))):
            with self.subTest(name=name):
                self.folder = Path(tempfile.mkdtemp())
                replay = self.stop_after_first_call("jev")   # heads 0 and 1 reused, 2 asked
                lines = change(*map(json.loads, (replay / "jev.jsonl").read_text().splitlines()))
                (replay / "jev.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
                before = len(Stub.seen)
                self.assertEqual(self.replay("jev")[0], 2)
                self.assertEqual(len(Stub.seen), before)

    def test_a_reused_line_that_is_not_step_ones_answer_does_not_seal_c1(self):
        # Codex, class pass: the seal took a line's `reused` claim on its word.
        self.checkpoint(0)
        self.checkpoint(1, tier="standard")
        self.write_faithful([(0, "cheap"), (1, "cheap")])
        for arm in ("kev", "jev", "haiku"):
            self.assertEqual(self.replay(arm)[0], 0)
        path = self.folder / "replay" / "jev.jsonl"
        honest = path.read_bytes()
        lines = [json.loads(line) for line in honest.splitlines()]
        lines[0]["tier"] = "skip"
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        self.seal("kev", "jev", "haiku")
        self.assertEqual(self.labels_c1()[0], 2)          # head 0's step 1 answer was cheap
        path.write_bytes(honest)
        self.seal("jev")
        answers = self.faithful / "answers.jsonl"
        step1 = answers.read_text()
        answers.write_text(step1 + "\n")
        self.assertEqual(self.labels_c1()[0], 2)          # step 1's result changed since
        self.assertFalse((self.folder / "labels").exists())
        answers.write_text(step1)
        self.assertEqual(self.labels_c1()[0], 0)

    def test_only_jevs_lines_may_claim_a_reused_answer(self):
        # Only Jev was asked in step 1: a reused line on another arm is not
        # that arm's answer, and its missing cost would escape the cap.
        line = {"repository": "owner/repo", "head": sha(0), "arm": "haiku",
                "run_id": experiment.RUN_ID, "outcome": "ok", "tier": "cheap", "usd": None,
                "reused": "faithful-2", "at": "2026-10-11T00:00:00+00:00"}
        self.assertEqual(len(experiment.record_faults([line], "haiku", experiment.RUN_ID)), 1)
        self.assertEqual(experiment.record_faults([dict(line, arm="jev")], "jev",
                                                  experiment.RUN_ID), [])

    def test_a_faithfulness_result_for_another_registration_or_population_refuses(self):
        self.attest(registration="0" * 64)
        self.assertEqual(self.replay("jev", attest=False)[0], 2)
        self.attest()
        self.write_population(["pkg/mod.py"])
        self.assertEqual(self.replay("jev", attest=False)[0], 2)
        self.assertEqual(Stub.seen, [])

    def test_a_faithfulness_result_edited_after_its_note_refuses(self):
        self.attest()
        self.write_faithful([(91, "cheap"), (90, "cheap")])
        self.assertEqual(self.replay("jev", attest=False)[0], 2)
        self.assertEqual(Stub.seen, [])

    def test_a_faithfulness_result_changed_between_arms_refuses(self):
        self.assertEqual(self.replay("kev")[0], 0)
        self.write_faithful([(91, "cheap"), (90, "cheap")])
        self.assertEqual(self.replay("jev")[0], 2)
        self.assertEqual(Stub.seen, [])
