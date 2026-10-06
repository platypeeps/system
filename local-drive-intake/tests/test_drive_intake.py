"""Tests for local-drive-intake.

The one that matters is `test_fetch_never_modifies_the_source`. Everything
else here checks behaviour; that one checks the promise the module makes to
the hard rule it was built under, which is that reading a Drive is allowed and
writing one is not. It is empirical on purpose: asserting "the code contains no open()
for write" would pass forever while someone added os.utime.
"""

import contextlib
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import drive_intake as di  # noqa: E402

#: A path no rule matches reaches the real sibling `jev.sh` with this
#: process's environment (`test_unmatched_is_noise`). Switched off, it
#: declines and sends nothing, whatever key `<config>/jev/.env` holds;
#: unmetered and with no corpus, it writes nothing. Unpinned, an operator's
#: run made a real hosted call and wrote it to their own ledger and trace
#: corpus (sd:2790). The trace URL is an off word, not empty: `jev.sh` lets
#: `<config>/jev/.env` fill an empty one (sd:2799).
_ISOLATED = unittest.mock.patch.dict(os.environ, {
    "JEV_ENABLED": "0", "TYPESAFE_API_KEY": "", "JEV_METER": "0", "JEV_CORPUS": "0",
    "JEV_TRACES_URL": "0"})


def setUpModule():
    _ISOLATED.start()


def tearDownModule():
    _ISOLATED.stop()


def snapshot(root: Path) -> dict:
    """Every file under root as path -> (size, mtime_ns, sha256).

    mtime in nanoseconds and a real content hash, so a walk that merely
    touched a file would fail this even though its size never changed.
    """
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        info = path.stat()
        out[str(path.relative_to(root))] = (
            info.st_size,
            info.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return out


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / "drive"
        self.state = self.base / "state"
        self.source.mkdir()
        self.state.mkdir()
        self.config = self.base / "drive-intake.conf"
        self.config.write_text(
            f"root|test|{self.source}\n"
            "route|noise|*.jpg\n"
            "route|data-export|*.csv\n"
            "route|correspondence|*minutes*\n"
            "route|document|*.pdf\n",
            encoding="utf-8",
        )
        self.addCleanup(self.tmp.cleanup)

    def make(self, rel: str, text: str = "x") -> Path:
        path = self.source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_verb(self, verb: str, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        code = di.main(
            [verb, *args],
            environ={
                "DRIVE_INTAKE_CONFIG": str(self.config),
                "DRIVE_INTAKE_STATE": str(self.state),
            },
            out=out,
        )
        return code, out.getvalue()


class TestReadOnly(Harness):
    def test_fetch_never_modifies_the_source(self):
        """A fetch leaves every byte, size and mtime of the tree as it found it."""
        self.make("Operations/report.pdf", "a document")
        self.make("Operations/Telemetry Data/2026-04.csv", "ts,value\n1,2\n")
        self.make("Photos/well.jpg", "not really a jpeg")
        self.make("Meetings/2026-09 minutes.pdf", "minutes")

        before = snapshot(self.source)
        self.assertEqual(len(before), 4)

        self.run_verb("fetch")   # baseline
        self.make("Operations/new.pdf", "arrived later")
        self.run_verb("fetch")   # reporting run
        self.run_verb("report")
        self.run_verb("status")

        after = snapshot(self.source)
        for name, value in before.items():
            self.assertIn(name, after, f"{name} disappeared during a fetch")
            self.assertEqual(value, after[name], f"{name} was modified during a fetch")

    def test_nothing_is_written_inside_the_root(self):
        """The state and report land outside the walked tree, never in it."""
        self.make("a.pdf")
        self.run_verb("fetch")
        self.make("b.pdf")
        self.run_verb("fetch")
        names = {p.name for p in self.source.rglob("*")}
        self.assertNotIn("intake-state.csv", names)
        self.assertNotIn("arrivals.csv", names)
        self.assertTrue((self.state / "intake-state.csv").exists())


class TestBaseline(Harness):
    def test_first_run_is_a_baseline_and_reports_nothing(self):
        for n in range(5):
            self.make(f"doc{n}.pdf")
        code, text = self.run_verb("fetch")
        self.assertEqual(code, di.EXIT_NONE)
        self.assertIn("baseline established: 5 files", text)
        self.assertFalse((self.state / "arrivals.csv").exists(),
                         "baseline wrote arrival rows, expected none")

    def test_second_run_with_no_change_exits_3(self):
        self.make("a.pdf")
        self.run_verb("fetch")
        code, text = self.run_verb("fetch")
        self.assertEqual(code, di.EXIT_NONE)
        self.assertIn("no change", text)

    def test_new_file_is_found_and_routed(self):
        self.make("a.pdf")
        self.run_verb("fetch")
        self.make("Operations/2026-09.csv", "a,b\n")
        code, text = self.run_verb("fetch")
        self.assertEqual(code, di.EXIT_FOUND)
        self.assertIn("1 data-export", text)

    def test_modified_file_is_found(self):
        path = self.make("a.pdf", "one")
        self.run_verb("fetch")
        os.utime(path, (0, 0))
        path.write_text("one but longer", encoding="utf-8")
        code, text = self.run_verb("fetch")
        self.assertEqual(code, di.EXIT_FOUND)
        body = (self.state / "arrivals.csv").read_text(encoding="utf-8")
        self.assertIn("modified", body)

    def test_deletion_is_not_reported(self):
        self.make("a.pdf")
        self.make("b.pdf")
        self.run_verb("fetch")
        (self.source / "b.pdf").unlink()
        code, _ = self.run_verb("fetch")
        self.assertEqual(code, di.EXIT_NONE)


class TestRouting(unittest.TestCase):
    def setUp(self):
        _, self.rules, _ = di.parse_config(
            "root|r|/tmp\n"
            "route|noise|*/reference (generated)/*\n"
            "route|noise|*.jpg\n"
            "route|data-export|*/operations/telemetry data/*\n"
            "route|data-export|*.csv\n"
            "route|correspondence|*/meetings/*\n"
            "route|correspondence|*minutes*\n"
            "route|document|*.pdf\n"
        )

    def test_first_match_wins(self):
        # Lives under Meetings and ends in .pdf. The narrow folder rule comes
        # first in the file, so correspondence wins over document.
        self.assertEqual(di.classify("Example Board/Meetings/2026-09.pdf", self.rules), "correspondence")

    def test_publish_surface_is_noise(self):
        self.assertEqual(
            di.classify("Example Board/Reference (generated)/contacts.gsheet", self.rules),
            "noise",
        )

    def test_case_is_ignored(self):
        self.assertEqual(di.classify("OPERATIONS/TELEMETRY DATA/x.txt", self.rules), "data-export")

    def test_unmatched_is_noise(self):
        self.assertEqual(di.classify("weird/thing.xyz", self.rules), "noise")


class TestConfig(unittest.TestCase):
    def test_tilde_expands(self):
        roots, _, problems = di.parse_config("root|home|~/somewhere\n")
        self.assertEqual(problems, [])
        self.assertTrue(str(roots[0].path).startswith(str(Path.home())))

    def test_path_with_spaces_and_at_sign_survives(self):
        roots, _, _ = di.parse_config("root|board|~/My Drive (board@example.org)\n")
        self.assertTrue(str(roots[0].path).endswith("My Drive (board@example.org)"))

    def test_unknown_route_is_a_problem_not_a_crash(self):
        _, rules, problems = di.parse_config("root|r|/tmp\nroute|invoices|*.pdf\nroute|document|*.doc\n")
        self.assertEqual(len(rules), 1)
        self.assertTrue(any("unknown route" in p for p in problems))

    def test_no_roots_is_a_problem(self):
        _, _, problems = di.parse_config("route|document|*.pdf\n")
        self.assertTrue(any("no root rows" in p for p in problems))


class TestConfigLocation(unittest.TestCase):
    """The conf names the mounts, and a mount name carries an address."""

    def status(self, environ):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as state:
            code = di.main(["status"], environ=dict(environ, DRIVE_INTAKE_STATE=state), out=out)
        return code, out.getvalue()

    def test_default_is_the_config_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output = self.status({"SYSTEM_TOOLS_CONFIG": tmp})
        self.assertEqual(code, di.EXIT_ERROR)
        expected = Path(tmp) / "drive-intake" / "drive-intake.conf"
        self.assertIn(f"config : {expected}", output)
        self.assertIn("drive-intake.conf.example", output)

    def test_the_variable_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "elsewhere.conf"
            conf.write_text(f"root|x|{tmp}\n", encoding="utf-8")
            code, output = self.status({"SYSTEM_TOOLS_CONFIG": tmp,
                                        "DRIVE_INTAKE_CONFIG": str(conf)})
        self.assertIn(f"config : {conf}", output)
        self.assertNotIn("does not exist", output)

    def test_the_example_parses(self):
        example = Path(di.__file__).resolve().parent / "drive-intake.conf.example"
        roots, rules, problems = di.parse_config(example.read_text(encoding="utf-8"))
        self.assertEqual(problems, [])
        self.assertEqual([r.label for r in roots], ["board", "committee"])
        self.assertEqual(di.classify("board/minutes/2026-09.pdf", rules,
                                     environ={"JEV_DRIVE_INTAKE": "0"}), "correspondence")


class TestErrors(Harness):
    def test_missing_mount_is_an_error_not_an_empty_walk(self):
        """A signed-out Drive must not look like a quiet day."""
        self.config.write_text(f"root|gone|{self.base / 'not-there'}\n", encoding="utf-8")
        code, text = self.run_verb("fetch")
        self.assertEqual(code, di.EXIT_ERROR)
        self.assertIn("is not a directory", text)

    def test_status_reports_a_missing_mount(self):
        self.config.write_text(f"root|gone|{self.base / 'not-there'}\n", encoding="utf-8")
        code, text = self.run_verb("status")
        self.assertEqual(code, di.EXIT_ERROR)
        self.assertIn("MISSING", text)

    def test_status_before_any_fetch(self):
        code, text = self.run_verb("status")
        self.assertEqual(code, di.EXIT_NONE)
        self.assertIn("no fetch has run", text)

    def test_status_fresh_after_fetch(self):
        self.make("a.pdf")
        self.run_verb("fetch")
        code, text = self.run_verb("status")
        self.assertEqual(code, di.EXIT_FOUND)
        self.assertIn("1 files", text)

    def test_report_without_a_fetch_is_an_error(self):
        code, text = self.run_verb("report")
        self.assertEqual(code, di.EXIT_ERROR)
        self.assertIn("run fetch first", text)

    def test_truncated_state_does_not_crash(self):
        self.make("a.pdf")
        self.run_verb("fetch")
        (self.state / "intake-state.csv").write_text("root,rel_path,size\nbad,row\n", encoding="utf-8")
        code, _ = self.run_verb("fetch")
        self.assertIn(code, (di.EXIT_FOUND, di.EXIT_NONE))


class TestSkips(Harness):
    def test_finder_and_drive_artefacts_are_not_walked(self):
        self.make("real.pdf")
        self.make(".DS_Store")
        self.make("._real.pdf")
        (self.source / ".tmp.drivedownload").mkdir()
        self.make(".tmp.drivedownload/staging.bin")
        self.run_verb("fetch")
        body = (self.state / "intake-state.csv").read_text(encoding="utf-8")
        self.assertIn("real.pdf", body)
        self.assertNotIn("DS_Store", body)
        self.assertNotIn("staging.bin", body)
        self.assertNotIn("._real.pdf", body)


class TestReport(Harness):
    def test_noise_is_counted_not_listed(self):
        self.make("seed.pdf")
        self.run_verb("fetch")
        self.make("a.jpg")
        self.make("b.jpg")
        self.make("real.pdf")
        self.run_verb("fetch")
        code, text = self.run_verb("report")
        self.assertEqual(code, di.EXIT_FOUND)
        self.assertIn("noise: 2 files, not listed", text)
        self.assertNotIn("a.jpg", text)
        self.assertIn("real.pdf", text)


if __name__ == "__main__":
    unittest.main()


class TestArrivalsLog(Harness):
    """Recommendation 2: a hand-run fetch must not eat the digest's queue."""

    def test_arrivals_accumulate_across_runs(self):
        self.make("seed.pdf")
        self.run_verb("fetch")
        self.make("one.pdf")
        self.run_verb("fetch")
        self.make("two.pdf")
        self.run_verb("fetch")
        rows = di.read_arrivals(self.state / "arrivals.csv")
        names = {r["rel_path"] for r in rows}
        self.assertEqual(names, {"one.pdf", "two.pdf"},
                         "a later fetch replaced an earlier run's arrivals")

    def test_reading_the_report_does_not_consume_it(self):
        self.make("seed.pdf")
        self.run_verb("fetch")
        self.make("real.pdf")
        self.run_verb("fetch")
        first = self.run_verb("report")[1]
        second = self.run_verb("report")[1]
        self.assertIn("real.pdf", first)
        self.assertIn("real.pdf", second)

    def test_stamp_marks_delivered_and_report_then_empties(self):
        self.make("seed.pdf")
        self.run_verb("fetch")
        self.make("real.pdf")
        self.run_verb("fetch")
        code, text = self.run_verb("stamp")
        self.assertEqual(code, di.EXIT_FOUND)
        self.assertIn("stamped 1", text)
        code, _ = self.run_verb("report")
        self.assertEqual(code, di.EXIT_NONE)
        # The row is still in the log, just delivered.
        self.assertEqual(len(di.read_arrivals(self.state / "arrivals.csv", unreported_only=False)), 1)

    def test_stamp_through_a_cutoff_leaves_later_rows_pending(self):
        """A fetch racing the digest must not have its rows marked delivered."""
        self.make("seed.pdf")
        self.run_verb("fetch")
        self.make("early.pdf")
        self.run_verb("fetch")
        log = self.state / "arrivals.csv"
        cutoff = di.read_arrivals(log)[0]["detected_at"]
        # A row that arrives after the consumer read the log.
        di.append_arrivals(log, [{
            "detected_at": "2099-01-01T00:00:00+00:00", "root": "test",
            "rel_path": "late.pdf", "route": "document", "change": "new",
            "size": 1, "mtime": "2099-01-01T00:00:00+00:00",
            "from_path": "", "reported_at": "",
        }])
        di.stamp_arrivals(log, cutoff, di.now_iso())
        pending = [r["rel_path"] for r in di.read_arrivals(log)]
        self.assertEqual(pending, ["late.pdf"])


class TestPeek(Harness):
    def test_peek_changes_nothing(self):
        self.make("seed.pdf")
        self.run_verb("fetch")
        self.make("real.pdf")
        before_state = (self.state / "intake-state.csv").read_text(encoding="utf-8")
        code, text = self.run_verb("peek")
        self.assertEqual(code, di.EXIT_FOUND)
        self.assertIn("real.pdf", text)
        self.assertEqual(before_state, (self.state / "intake-state.csv").read_text(encoding="utf-8"))
        self.assertFalse((self.state / "arrivals.csv").exists())
        # And the fetch that follows still sees it.
        code, _ = self.run_verb("fetch")
        self.assertEqual(code, di.EXIT_FOUND)

    def test_peek_on_a_quiet_tree(self):
        self.make("a.pdf")
        self.run_verb("fetch")
        code, text = self.run_verb("peek")
        self.assertEqual(code, di.EXIT_NONE)
        self.assertIn("no change", text)


class TestMoveDetection(Harness):
    """Recommendation 3: a folder move must not read as a folder of new files."""

    def move(self, src: str, dst: str):
        """Mimic Drive: remove and re-create, preserving name, size and mtime."""
        source = self.source / src
        info = source.stat()
        data = source.read_bytes()
        target = self.source / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        source.unlink()
        target.write_bytes(data)
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))

    def test_a_moved_file_is_moved_not_new(self):
        self.make("Inbox/2026-09 minutes.pdf", "board minutes")
        self.run_verb("fetch")
        self.move("Inbox/2026-09 minutes.pdf", "Meetings/2026-09 minutes.pdf")
        code, text = self.run_verb("fetch")
        self.assertEqual(code, di.EXIT_FOUND)
        self.assertIn("1 of them moved", text)
        row = di.read_arrivals(self.state / "arrivals.csv")[0]
        self.assertEqual(row["change"], "moved")
        self.assertEqual(row["from_path"], "Inbox/2026-09 minutes.pdf")

    def test_a_folder_move_does_not_bury_a_real_arrival(self):
        for n in range(30):
            self.make(f"Inbox/doc{n}.pdf", f"content {n}")
        self.run_verb("fetch")
        for n in range(30):
            self.move(f"Inbox/doc{n}.pdf", f"Archive/doc{n}.pdf")
        self.make("Inbox/genuinely new.pdf", "this one matters")
        self.run_verb("fetch")
        rows = di.read_arrivals(self.state / "arrivals.csv")
        moved = [r for r in rows if r["change"] == "moved"]
        fresh = [r for r in rows if r["change"] == "new"]
        self.assertEqual(len(moved), 30)
        self.assertEqual([r["rel_path"] for r in fresh], ["Inbox/genuinely new.pdf"])

    def test_an_ambiguous_pair_is_refused_not_guessed(self):
        """Two identical files moving at once must not be paired arbitrarily."""
        self.make("A/same.pdf", "identical bytes")
        self.make("B/same.pdf", "identical bytes")
        for path in (self.source / "A/same.pdf", self.source / "B/same.pdf"):
            os.utime(path, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        self.run_verb("fetch")
        self.move("A/same.pdf", "C/same.pdf")
        self.move("B/same.pdf", "D/same.pdf")
        self.run_verb("fetch")
        rows = di.read_arrivals(self.state / "arrivals.csv")
        self.assertTrue(all(r["change"] == "new" for r in rows),
                        "an ambiguous pair was guessed at instead of refused")

    def test_a_rename_is_not_a_move(self):
        self.make("report.pdf", "body")
        self.run_verb("fetch")
        self.move("report.pdf", "report-final.pdf")
        self.run_verb("fetch")
        row = di.read_arrivals(self.state / "arrivals.csv")[0]
        self.assertEqual(row["change"], "new")


class TestCollapse(Harness):
    def test_a_flood_collapses_to_folders(self):
        self.make("seed.pdf")
        self.run_verb("fetch")
        for n in range(di.COLLAPSE_AT + 5):
            self.make(f"Dump/doc{n}.pdf", f"body {n}")
        self.run_verb("fetch")
        code, text = self.run_verb("report")
        self.assertEqual(code, di.EXIT_FOUND)
        self.assertIn("collapsed to folders", text)
        self.assertIn("Dump/", text)
        self.assertNotIn("doc7.pdf", text)

    def test_a_small_batch_still_lists_files(self):
        self.make("seed.pdf")
        self.run_verb("fetch")
        for n in range(3):
            self.make(f"Dump/doc{n}.pdf", f"body {n}")
        self.run_verb("fetch")
        _, text = self.run_verb("report")
        self.assertIn("doc1.pdf", text)
        self.assertNotIn("collapsed", text)


class TestAQuietDayIsNotAnError(Harness):
    """`report` and `stamp` with no arrivals log: 3 after a baseline, 1 before one.

    Both verbs returned 1 for an absent log, so a machine whose baseline walk
    found nothing new since failed every report with advice to run the fetch it
    had already run. The walk state is what separates the two cases.
    """

    def test_report_after_a_baseline_is_nothing_to_do(self):
        self.make("seed.pdf")
        self.run_verb("fetch")
        self.assertFalse((self.state / "arrivals.csv").exists())
        code, text = self.run_verb("report")
        self.assertEqual(code, di.EXIT_NONE)
        self.assertIn("nothing undelivered", text)

    def test_report_before_any_fetch_still_says_run_fetch(self):
        code, text = self.run_verb("report")
        self.assertEqual(code, di.EXIT_ERROR)
        self.assertIn("run fetch first", text)

    def test_stamp_after_a_baseline_is_nothing_to_do(self):
        self.make("seed.pdf")
        self.run_verb("fetch")
        code, text = self.run_verb("stamp")
        self.assertEqual(code, di.EXIT_NONE)
        self.assertIn("nothing to stamp", text)

    def test_stamp_before_any_fetch_still_says_run_fetch(self):
        code, text = self.run_verb("stamp")
        self.assertEqual(code, di.EXIT_ERROR)
        self.assertIn("run fetch first", text)


class TestJevFallback(unittest.TestCase):
    """The optional Jev step, and the promise that it changes nothing by default.

    Every test here runs against a fake `jev.sh` written into a temp directory
    and injected by path. Nothing reaches the network: a suite that called the
    real endpoint would spend tokens on every `make check`, and `tests/ci-native.sh`
    treats a skipped test as a failure, so "skip when offline" is not an
    option.
    """

    CONF = (
        "root|r|/tmp\n"
        "route|noise|*.jpg\n"
        "route|data-export|*.csv\n"
        "route|correspondence|*minutes*\n"
        "route|document|*.pdf\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        _, self.rules, _ = di.parse_config(self.CONF)
        self.argv_log = self.base / "argv.txt"
        self.stdin_log = self.base / "stdin.txt"

    def fake_jev(self, body: str) -> Path:
        """A stand-in entrypoint that records how it was called.

        It answers `enabled STAGE` the way the real one does -- by reading
        that variable -- because the module delegates the whole gate to it:
        a fake that ignored the stage could not express "switched off".
        """
        script = self.base / "jev.sh"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = enabled ]; then\n'
            # `--why` prints the reason on STDOUT, as the real one does, so a
            # caller that captures output has something to say out loud.
            '  why=0; for a in "$@"; do [ "$a" = --why ] && why=1; done\n'
            '  if [ -n "${2:-}" ] && [ "${2#-}" = "$2" ]; then\n'
            '    eval "word=\\${$2:-}"\n'
            "    case \"$(printf %s \"$word\" | tr 'A-Z' 'a-z')\" in\n"
            '      0|off|false|no|disabled)\n'
            '        [ "$why" = 1 ] && echo "jev: $2 switched this stage off here"\n'
            "        exit 3 ;;\n"
            "    esac\n"
            "  fi\n"
            '  if [ -n "${JEV_FAKE_UNKEYED:-}" ]; then\n'
            '    [ "$why" = 1 ] && echo "jev: no key on this machine"\n'
            "    exit 3\n"
            "  fi\n"
            '  [ "$why" = 1 ] && echo "jev: enabled"\n'
            "  exit 0\n"
            "fi\n"
            f'printf "%s\\n" "$@" > {self.argv_log}\n'
            f"cat > {self.stdin_log}\n"
            f"{body}\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def env(self, stage: str | None = None, command: str | None = None) -> dict:
        """`JEV_DRIVE_INTAKE` switches the stage off; nothing switches it on.

        Unset is on, so the default here is unset. `stage="0"` is what a caller
        that wants today's answer writes.
        """
        out = {}
        if stage is not None:
            out["JEV_DRIVE_INTAKE"] = stage
        if command is not None:
            out[di.JEV_COMMAND_VAR] = command
        return out

    def classify(self, rel_path: str, environ: dict) -> tuple[str, str]:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            route = di.classify(rel_path, self.rules, environ)
        return route, errors.getvalue()

    def test_the_stage_switched_off_asks_the_probe_and_nothing_else(self):
        """What a caller writes to get the module that shipped before Jev.

        It is the *probe* that reads the variable now, not this module, so one
        call is made and it is `enabled JEV_DRIVE_INTAKE --why`. What must not
        happen is a second call: no judgment, so nothing leaves the machine.

        This case used to hand in a runner that raised, on the reasoning that
        any call was a failure. That was vacuous twice over: `jev_route`
        catches `Exception`, and `AssertionError` is one, so the raise was
        swallowed and the case passed however many times jev was called.
        """

        calls = []

        def record(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 3, "jev: switched off\n", "")

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            route = di.classify("weird/thing.xyz", self.rules,
                                {"JEV_DRIVE_INTAKE": "0"}, record)
        self.assertEqual(route, "noise")
        self.assertEqual(len(calls), 1, f"more than the probe was called: {calls}")
        self.assertEqual(calls[0][1:],
                         ["enabled", "JEV_DRIVE_INTAKE", "--why",
                          "--record", "--caller", "local-drive-intake"])

    def test_the_probe_records_its_decline_under_this_tools_own_name(self):
        """The one call this path makes is also the one that counts it.

        `jev_route` runs once per unmatched path, so a sweep of a Drive is
        thousands of declines. `--record` costs nothing extra -- the row is
        written by the process the probe already starts -- which is why the
        decline is counted here and nowhere else. A second `jev record`
        subprocess per path is the cost `_SAID` exists to avoid.
        """
        calls = []

        def probe(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 3, "jev: switched off\n", "")

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            route = di.classify("weird/thing.xyz", self.rules,
                                {"JEV_DRIVE_INTAKE": "0"}, probe)
        self.assertEqual(route, "noise")
        self.assertEqual(len(calls), 1, f"a second call was made: {calls}")
        argv = calls[0]
        self.assertIn("--record", argv)
        self.assertEqual(argv[argv.index("--caller") + 1], "local-drive-intake")
        self.assertEqual(di.JEV_CALLER, "local-drive-intake")

    def test_the_judgment_call_names_this_caller_and_its_stage(self):
        """One name across the switch, the ledger and the per-stage report.

        `JEV_DRIVE_INTAKE` is the word the probe above is handed, so the two
        arms of one decision are filed under one stage rather than two.
        """
        script = self.fake_jev('echo document')
        self.classify("weird/thing.xyz", self.env(command=str(script)))
        argv = self.argv_log.read_text().split("\n")
        self.assertIn("--caller", argv)
        self.assertEqual(argv[argv.index("--caller") + 1], "local-drive-intake")
        self.assertEqual(argv[argv.index("--stage") + 1], "JEV_DRIVE_INTAKE")

    def test_the_stage_switched_off_ignores_a_working_jev(self):
        script = self.fake_jev('echo document')
        route, _ = self.classify("weird/thing.xyz",
                                 self.env(stage="0", command=str(script)))
        self.assertEqual(route, "noise")
        self.assertFalse(self.argv_log.exists())

    def test_the_stage_unset_reaches_jev(self):
        # The flip. This was opt-in, and an opt-in that defaults to off makes
        # every integration added after it silently never run. `jev enabled`
        # still decides whether the machine can answer at all.
        script = self.fake_jev('echo document')
        route, _ = self.classify("weird/thing.xyz", self.env(command=str(script)))
        self.assertEqual(route, "document")

    def test_a_declined_probe_says_why_on_stderr(self):
        """The defect the flip introduced, and the reason this case exists.

        While this was an opt-in, a silent return meant "nobody asked" and
        saying so would have been noise. Unset now means on, so the same
        return covers an unkeyed machine and a switched-off Jev -- which
        `jev_route`'s docstring promises are never silent.
        """

        script = self.fake_jev('echo document')
        route, errors = self.classify(
            "weird/thing.xyz",
            {**self.env(command=str(script)), "JEV_FAKE_UNKEYED": "1"})
        self.assertEqual(route, "noise")
        self.assertIn("no key on this machine", errors)
        self.assertIn("stay noise", errors)

    def test_a_switched_off_stage_says_which_variable_did_it(self):
        script = self.fake_jev('echo document')
        route, errors = self.classify("weird/thing.xyz",
                                      self.env(stage="0", command=str(script)))
        self.assertEqual(route, "noise")
        self.assertIn("JEV_DRIVE_INTAKE", errors)

    def test_the_reason_is_said_once_and_not_once_per_path(self):
        """`jev_route` runs once per unmatched path. An unkeyed machine
        sweeping a Drive would print the same line thousands of times, which
        is how a true statement becomes noise nobody reads."""

        di._SAID.clear()
        self.addCleanup(di._SAID.clear)
        script = self.fake_jev('echo document')
        environ = {**self.env(command=str(script)), "JEV_FAKE_UNKEYED": "1"}
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            for name in ("a.xyz", "b.xyz", "c.xyz"):
                di.classify(f"weird/{name}", self.rules, environ)
        self.assertEqual(errors.getvalue().count("no key on this machine"), 1,
                         "the reason was repeated once per path")

    def test_a_rule_hit_never_reaches_jev(self):
        script = self.fake_jev('echo correspondence')
        route, _ = self.classify("board/minutes/2026-09.pdf", self.env(command=str(script)))
        self.assertEqual(route, "correspondence")
        # Matched by rule, not by judgment: the fake was never run.
        self.assertFalse(self.argv_log.exists())

    def test_jev_switched_off_falls_back_to_noise(self):
        # What the real entrypoint does when the switch is off, the key is
        # missing, or the call fails: it prints the `--fallback` answer, says
        # why on stderr, and exits 0.
        script = self.fake_jev(
            'echo "jev: switched off here" >&2\n'
            'shift $(($# - 1)); echo "$1"'
        )
        route, errors = self.classify("weird/thing.xyz", self.env(command=str(script)))
        self.assertEqual(route, "noise")
        # The reason reaches a human. Capturing it and dropping it would be the
        # silence `--fallback` exists to prevent.
        self.assertIn("switched off here", errors)

    def test_jev_answering_names_the_route(self):
        script = self.fake_jev('echo document')
        route, _ = self.classify("weird/thing.xyz", self.env(command=str(script)))
        self.assertEqual(route, "document")

    def test_jev_no_match_answer_is_noise(self):
        script = self.fake_jev(f'echo {di.JEV_NO_MATCH}')
        route, errors = self.classify("weird/thing.xyz", self.env(command=str(script)))
        self.assertEqual(route, "noise")
        # A clean "none of these fits" is an answer, not a degradation, so it
        # is not reported as one.
        self.assertEqual(errors, "")

    def test_an_answer_that_is_not_a_route_is_noise_and_is_loud(self):
        script = self.fake_jev('echo invoice')
        route, errors = self.classify("weird/thing.xyz", self.env(command=str(script)))
        self.assertEqual(route, "noise")
        self.assertIn("invoice", errors)

    def test_a_failing_jev_is_noise_and_is_loud(self):
        # It answers `enabled` and fails the judgment. A fake that failed both
        # would be testing the probe and not the fallback.
        script = self.fake_jev(
            '[ "$1" = enabled ] && exit 0\necho "boom" >&2; exit 1')
        route, errors = self.classify("weird/thing.xyz", self.env(command=str(script)))
        self.assertEqual(route, "noise")
        self.assertIn("exit 1", errors)

    def test_a_missing_jev_is_noise_and_does_not_raise(self):
        missing = self.base / "no-such-jev.sh"
        route, errors = self.classify("weird/thing.xyz", self.env(command=str(missing)))
        self.assertEqual(route, "noise")
        # The probe is what a missing entrypoint fails now, and it says so:
        # the degradation is still named, which is the whole point of it.
        self.assertIn("could not be asked", errors)

    def test_the_criteria_come_from_the_loaded_rules(self):
        # Not a list written down in the module. A conf with a route this
        # module has never seen named in its own constants would still be
        # offered, and a conf that drops one would stop offering it.
        script = self.fake_jev('echo document')
        self.classify("weird/thing.xyz", self.env(command=str(script)))
        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        criteria = argv[argv.index("--criteria") + 1]
        self.assertEqual(
            criteria.split(",")[:4],
            ["noise", "data-export", "correspondence", "document"],
        )
        self.assertIn(di.JEV_NO_MATCH, criteria)

    def test_fewer_routes_in_the_conf_means_fewer_criteria(self):
        _, rules, _ = di.parse_config("root|r|/tmp\nroute|document|*.pdf\n")
        self.assertEqual(di.route_names(rules), ["document"])

    def test_only_the_path_is_sent(self):
        # Never file contents. Every call leaves the machine, and this module
        # reads two Drives full of governance paper.
        script = self.fake_jev('echo document')
        self.classify("board/Budget 2027 draft.xyz", self.env(command=str(script)))
        self.assertEqual(
            self.stdin_log.read_text(encoding="utf-8").strip(),
            "board/Budget 2027 draft.xyz",
        )

    def test_the_fallback_answer_is_outside_the_route_namespace(self):
        # It used to be `noise`, which this fixture's own conf defines as a
        # route. A degraded call then answered exactly like a confident one:
        # `answer in routes` was true either way, and the caller could not
        # tell a judgment from a shrug.
        script = self.fake_jev('echo document')
        self.classify("weird/thing.xyz", self.env(command=str(script)))
        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(argv[argv.index("--fallback") + 1], di.JEV_DEGRADED)
        self.assertIn("noise", di.route_names(self.rules))
        self.assertNotIn(di.JEV_DEGRADED, di.route_names(self.rules))

    def test_the_degraded_marker_routes_noise_and_never_a_route(self):
        script = self.fake_jev(f'echo {di.JEV_DEGRADED}')
        route, _ = self.classify("weird/thing.xyz", self.env(command=str(script)))
        self.assertEqual(route, "noise")

    def test_the_marker_can_never_be_a_route(self):
        # Not a convention: ROUTES is closed and parse_config rejects any
        # other name, so no conf can create the collision. That is what makes
        # the marker safe, and it stops being true the day ROUTES grows it.
        self.assertNotIn(di.JEV_DEGRADED, di.ROUTES)
        _, _, errors = di.parse_config(
            f"root|r|/tmp\nroute|{di.JEV_DEGRADED}|*.xyz\n"
        )
        self.assertTrue(any(di.JEV_DEGRADED in e for e in errors), errors)

    def test_the_default_command_is_the_sibling_entrypoint(self):
        # Resolved from this module's own directory, never from PATH: cron and
        # CI run with a PATH that `local-bin-links` never touched.
        command = di.jev_command({})
        self.assertEqual(len(command), 1)
        self.assertTrue(command[0].endswith("/local-jev/jev.sh"))
        self.assertTrue(Path(command[0]).is_absolute())
