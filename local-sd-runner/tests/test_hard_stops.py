"""Criterion 3 and `prd.md:608-610`: the runner stops on the hard stops and names them.

Real clones, real supervisors, real remotes, through `test_runtime.Fixture`.
The failing test is the fixture repository's own check exiting non-zero; the
write outside the repository is the session's signal file; the blocking
finding is what `sd-ship prepare` leaves in its receipt when it refuses.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import runner as store
from sd_runner import hard_stops, runtime

from . import test_runtime

git = test_runtime.git


class HardStops(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.Fixture(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for name in ("root", "checkout", "remote", "database", "db", "item", "config", "runner", "provider"):
            setattr(self, name, getattr(self.fixture, name))
        self.environment = {"PATH": os.environ["PATH"], "HOME": str(self.root)}

    def script(self, name, source):
        path = self.root / name
        path.write_text(source)
        return [sys.executable, str(path)]

    def run_fixture(self, **extra):
        request = self.fixture.claim()
        result = self.runner.execute(self.db, request, command=[sys.executable, str(self.provider)], environment=self.environment, **extra)
        return request, result

    def notes(self, kind):
        return [dict(row) for row in self.db.execute("SELECT * FROM note WHERE item=? AND kind=? ORDER BY id", (self.item, kind))]

    def assert_stopped(self, request, result, kind, *fragments):
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertEqual(result["end_step"], "released", result)
        self.assertTrue(result["detail"].startswith(f"hard stop: {kind}: "), result["detail"])
        for fragment in fragments:
            self.assertIn(fragment, result["detail"])
        self.assertEqual(store.queue_state(self.db, request["id"])["status"], "blocked")
        self.assertEqual(self.db.execute("SELECT status FROM item WHERE id=?", (self.item,)).fetchone()["status"], "blocked")
        # The failure is named in a note: the run's exec note carries the
        # detail, and an open followup carries it into the next session's brief.
        exec_notes = [note for note in self.notes("exec") if note["session"] == "runner"]
        self.assertEqual(exec_notes[-1]["body"], result["detail"])
        followups = [note for note in self.notes("followup") if note["session"] == result["id"]]
        self.assertEqual(len(followups), 1, followups)
        self.assertEqual(followups[0]["body"], result["detail"])
        self.assertIsNone(followups[0]["resolved_at"])

    def test_failing_test_blocks_item_naming_the_failure_and_leaves_the_branch(self):
        check = self.script("check.py", "import json,sys\n"
            "print(json.dumps({'checks':[{'name':'test','status':'fail','command':['make','test']},{'name':'lint','status':'pass'}]}))\n"
            "sys.stderr.write('FAIL: test_fixture (tests.test_fixture.Case)\\n')\nsys.exit(1)\n")
        request, result = self.run_fixture(check=check)
        self.assert_stopped(request, result, hard_stops.FAILING_TEST, "test: make test exited 1", "FAIL: test_fixture")
        # The branch is left in place: the authored commit is on the remote,
        # the clone retained, and nothing deleted it.
        authored = git(Path(result["retained_path"]), "rev-parse", "HEAD")
        self.assertEqual(git(self.checkout, "ls-remote", "--heads", "origin", "work/item").split()[0], authored)
        self.assertEqual(git(Path(result["retained_path"]), "show", "--stat", "--format=%s", "HEAD").splitlines()[0], "Implement fixture")
        self.assertFalse(Path(result["work_path"]).exists())

    def test_passing_check_ends_done(self):
        marker = self.root / "check-ran"
        check = self.script("check.py", f"from pathlib import Path\nimport os\nPath({str(marker)!r}).write_text(os.getcwd())\n")
        request, result = self.run_fixture(check=check)
        self.assertEqual(result["outcome"], "done", result)
        self.assertEqual(store.queue_state(self.db, request["id"])["status"], "done")
        # The check ran in the clone, not in the operator's checkout.
        self.assertEqual(Path(marker.read_text()).resolve(), Path(result["work_path"]).resolve())
        self.assertEqual(self.notes("followup"), [])

    def test_passing_check_is_recorded_on_the_row_keyed_by_the_tree_it_checked(self):
        """sd:495: the record sd-review will read instead of running the same gate again."""
        check = self.script("check.py", "import json\n"
            "print(json.dumps({'checks':[{'name':'test','status':'pass','command':['make','test']}],'padding':'x'*8000}))\n")
        request, result = self.run_fixture(check=check)
        self.assertEqual(result["outcome"], "done", result)
        record = store.check_record(self.db, result["id"])
        self.assertIsNotNone(record, "a passed check leaves a check row")
        clone = Path(result["retained_path"])
        self.assertEqual(record["tree"], git(clone, "rev-parse", "HEAD^{tree}"))
        self.assertEqual(record["head"], result["authored_head"])
        self.assertEqual(record["head"], git(clone, "rev-parse", "HEAD"))
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["argv"], check)
        # The checks list is read from the whole output, not the 4000-byte
        # tail the stop classifier sees.
        self.assertEqual(record["checks"], [{"name": "test", "status": "pass", "command": ["make", "test"]}])
        self.assertTrue(record["recorded_at"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM state WHERE kind = 'check'").fetchone()[0], 1)

    def test_failing_check_records_nothing(self):
        check = self.script("check.py", "import sys\nsys.exit(1)\n")
        request, result = self.run_fixture(check=check)
        self.assert_stopped(request, result, hard_stops.FAILING_TEST, "the repository check exited 1")
        self.assertIsNone(store.check_record(self.db, result["id"]))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM state WHERE kind = 'check'").fetchone()[0], 0)

    def test_check_runs_after_the_session_in_the_clone_and_sees_its_commit(self):
        check = self.script("check.py", "import subprocess,sys\nfrom pathlib import Path\n"
            "assert Path('work.txt').read_text()=='authored work\\n'\n"
            "assert 'Implement fixture' in subprocess.run(['git','log','-1','--format=%s'],capture_output=True,text=True).stdout\n")
        request, result = self.run_fixture(check=check)
        self.assertEqual(result["outcome"], "done", result)

    def test_session_signal_write_outside_repository_stops_before_the_check(self):
        self.provider.write_text(self.provider.read_text() + "import json\n"
            "Path('.git/sd-stop.json').write_text(json.dumps({'kind':'write outside the repository','detail':'refused to write /Users/operator/Documents/vault/note.md'}))\n")
        marker = self.root / "check-ran"
        check = self.script("check.py", f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
        request, result = self.run_fixture(check=check)
        self.assert_stopped(request, result, hard_stops.OUTSIDE_WRITE, "refused to write /Users/operator/Documents/vault/note.md")
        self.assertFalse(marker.exists(), "a stopped session's check must not run")

    def test_session_signal_is_read_whatever_the_exit_code(self):
        self.provider.write_text(self.provider.read_text() + "import json\n"
            "Path('.git/sd-stop.json').write_text(json.dumps({'kind':'failing test','detail':'tests/test_x.py::test_y cannot pass without the missing fixture'}))\n"
            "raise SystemExit(3)\n")
        request, result = self.run_fixture()
        self.assert_stopped(request, result, hard_stops.FAILING_TEST, "tests/test_x.py::test_y")

    def test_malformed_signal_is_a_stop_and_never_done(self):
        self.provider.write_text(self.provider.read_text() + "Path('.git/sd-stop.json').write_text('not json')\n")
        request, result = self.run_fixture()
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertIn("unreadable stop signal", result["detail"])
        self.assertEqual(store.queue_state(self.db, request["id"])["status"], "blocked")
        self.assertEqual(self.notes("followup"), [], "an unreadable signal names no hard stop")

    def test_unknown_signal_kind_is_refused(self):
        self.provider.write_text(self.provider.read_text() + "import json\n"
            "Path('.git/sd-stop.json').write_text(json.dumps({'kind':'bored','detail':'x'}))\n")
        request, result = self.run_fixture()
        self.assertEqual(result["outcome"], "blocked")
        self.assertIn("known kind", result["detail"])

    def test_ship_refusal_names_the_blocking_finding_from_the_receipt(self):
        pack = self.root / "pack"
        (pack / "bin").mkdir(parents=True)
        (pack / "bin/sd-ship").write_text("import sys\nsys.stderr.write('sd-ship: one code review and one fix verification are spent\\n')\nsys.exit(1)\n")
        # The last pass names the head this run authored; that is what makes it
        # this run's receipt and not the branch's earlier one (sd:1221).
        def receipt_for(connection, ident):
            return {"protocol": 1, "passes": [
                {"head": "a" * 40, "report": {"status": "blocking", "findings": []}},
                {"head": store.run_state(connection, ident)["authored_head"],
                 "report": {"status": "blocking", "check": {"status": "pass"}, "findings": [
                     {"disposition": "blocking", "path": "sd_runner/runtime.py", "line": 12, "summary": "retry loop never ends"},
                     {"disposition": "advisory", "path": "README.md", "line": 1, "summary": "typo"}]}}]}
        resolved = ([sys.executable, str(self.provider)], self.environment, {"provider": "fixture", "vendor": "fixture"})
        self.runner.config = runtime.Config(**{**self.config.__dict__, "pack": pack})
        with patch.object(runtime, "provider_command", return_value=resolved), \
                patch.object(store, "ship_receipt", side_effect=receipt_for) as read:
            request = self.fixture.claim()
            result = self.runner.execute(self.db, request)
        self.assertEqual(read.call_args.args[1], request["run"]["id"])
        self.assert_stopped(request, result, hard_stops.BLOCKING_FINDING,
                            "sd_runner/runtime.py:12 retry loop never ends", "2 of 2 automatic passes spent")  # [quoted: a finding string from the fixture, not a citation]
        self.assertNotIn("README.md", result["detail"], "advisory findings hold nothing")

    def test_a_receipt_left_by_an_earlier_run_does_not_stop_this_one(self):
        """sd:1221: `ship_receipt` returns the branch's latest pass, which can precede this run."""
        pack = self.root / "pack"
        (pack / "bin").mkdir(parents=True)
        (pack / "bin/sd-ship").write_text("import sys\nsys.stderr.write('sd-ship: remote unreachable\\n')\nsys.exit(1)\n")
        stale = {"protocol": 1, "passes": [{"head": "c" * 40, "report": {"status": "blocking", "findings": [
            {"disposition": "blocking", "path": "old.py", "line": 1, "summary": "a finding from the run before"}]}}]}
        resolved = ([sys.executable, str(self.provider)], self.environment, {"provider": "fixture", "vendor": "fixture"})
        self.runner.config = runtime.Config(**{**self.config.__dict__, "pack": pack})
        with patch.object(runtime, "provider_command", return_value=resolved), \
                patch.object(store, "ship_receipt", return_value=stale):
            request = self.fixture.claim()
            result = self.runner.execute(self.db, request)
        self.assertEqual(result["outcome"], "blocked")
        self.assertIn("remote unreachable", result["detail"])
        self.assertNotIn("hard stop", result["detail"])
        self.assertNotIn("a finding from the run before", result["detail"])
        self.assertEqual(self.notes("followup"), [])

    def test_ship_refusal_without_a_stop_in_the_receipt_keeps_its_own_reason(self):
        pack = self.root / "pack"
        (pack / "bin").mkdir(parents=True)
        (pack / "bin/sd-ship").write_text("import sys\nsys.stderr.write('sd-ship: remote unreachable\\n')\nsys.exit(1)\n")
        resolved = ([sys.executable, str(self.provider)], self.environment, {"provider": "fixture", "vendor": "fixture"})
        self.runner.config = runtime.Config(**{**self.config.__dict__, "pack": pack})
        with patch.object(runtime, "provider_command", return_value=resolved):
            request = self.fixture.claim()
            result = self.runner.execute(self.db, request)
        self.assertEqual(result["outcome"], "blocked")
        self.assertIn("remote unreachable", result["detail"])
        self.assertNotIn("hard stop", result["detail"])
        self.assertEqual(self.notes("followup"), [])


class Classifiers(unittest.TestCase):
    def test_receipt_failed_check_is_the_failing_test(self):
        receipt = {"passes": [{"report": {"status": "gate_failed", "check": {"status": "fail", "detail": "3 failed",
                                                                                  "checks": [{"name": "test", "status": "fail"}, {"name": "lint", "status": "pass"}]}}}]}
        stop = hard_stops.from_receipt(receipt)
        self.assertEqual(stop.kind, hard_stops.FAILING_TEST)
        self.assertIn("test failed under sd-ship: 3 failed", stop.evidence)

    def test_receipt_shapes_that_are_not_stops(self):
        for receipt in ({}, {"passes": []}, {"passes": [{"report": {"status": "clean", "findings": []}}]},
                        {"passes": [{"report": {"status": "advisory", "findings": [{"disposition": "advisory"}]}}]},
                        {"passes": [{"report": None}]}, {"passes": "x"}):
            self.assertIsNone(hard_stops.from_receipt(receipt), receipt)

    def test_one_spent_pass_says_so(self):
        stop = hard_stops.from_receipt({"passes": [{"report": {"status": "blocking", "findings": [{"disposition": "blocking", "path": "a.py", "line": 3, "summary": "s"}]}}]})
        self.assertEqual(stop.kind, hard_stops.BLOCKING_FINDING)
        self.assertEqual(stop.evidence, "a.py:3 s (1 of 2 automatic passes spent)")  # [quoted: fixture evidence text, not a citation]

    def test_check_result_names_failed_checks_or_the_tail(self):
        self.assertIsNone(hard_stops.from_check({"exit_code": 0, "stdout": "", "stderr": ""}))
        named = hard_stops.from_check({"exit_code": 2, "stdout": json.dumps({"checks": [{"name": "test", "status": "fail", "command": ["make", "test"]}]}), "stderr": "boom"})
        self.assertEqual(named.evidence, "test: make test exited 2: boom")
        plain = hard_stops.from_check({"exit_code": 1, "stdout": "not json", "stderr": ""})
        self.assertEqual(plain.evidence, "the repository check exited 1: not json")

    def test_check_result_names_failed_checks_from_the_parsed_list_not_the_tail(self):
        # The supervisor hands over stdout[-4000:]. On a repository whose
        # report is longer than that, the tail is not JSON; `checks` is the
        # list it parsed from the whole output (sd:235).
        report = json.dumps({"checks": [{"name": f"c{i}", "status": "pass", "command": ["x"] * 40} for i in range(60)]
                             + [{"name": "test", "status": "fail", "command": ["make", "test"]},
                                {"name": "lint", "status": "fail", "command": ["make", "lint"]}]})
        self.assertGreater(len(report), 4000)
        tail = report[-4000:]
        with self.assertRaises(ValueError):
            json.loads(tail)
        stop = hard_stops.from_check({"exit_code": 2, "stdout": tail, "stderr": "",
                                      "checks": [{"name": "test", "status": "fail", "command": ["make", "test"]},
                                                 {"name": "lint", "status": "fail", "command": ["make", "lint"]}]})
        self.assertEqual(stop.kind, hard_stops.FAILING_TEST)
        self.assertTrue(stop.evidence.startswith("test: make test; lint: make lint exited 2: "), stop.evidence)

    def test_check_result_falls_back_to_stdout_only_without_a_checks_key(self):
        stdout = json.dumps({"checks": [{"name": "test", "status": "fail", "command": ["make", "test"]}]})
        self.assertNotIn("checks", {"exit_code": 2, "stdout": stdout, "stderr": ""})
        fallback = hard_stops.from_check({"exit_code": 2, "stdout": stdout, "stderr": ""})
        self.assertEqual(fallback.evidence, f"test: make test exited 2: {stdout}")
        # A key that is present and empty is the supervisor's answer, not an
        # invitation to reparse the tail: the check exited non-zero without
        # naming a failed check.
        empty = hard_stops.from_check({"exit_code": 3, "stdout": stdout, "stderr": "", "checks": []})
        self.assertEqual(empty.evidence, f"the repository check exited 3: {stdout}")
        unparsed = hard_stops.from_check({"exit_code": 3, "stdout": "not json", "stderr": "", "checks": None})
        self.assertEqual(unparsed.evidence, "the repository check exited 3: not json")
        # A null value is the key being present too. Reading absence from the
        # value reparsed the tail behind the supervisor's own answer and named
        # a check it had rejected (sd:1221).
        null = hard_stops.from_check({"exit_code": 3, "stdout": stdout, "stderr": "", "checks": None})
        self.assertEqual(null.evidence, f"the repository check exited 3: {stdout}")

    def test_evidence_is_one_line_and_bounded(self):
        stop = hard_stops.HardStop(hard_stops.FAILING_TEST, "a\n" * 5000)
        self.assertNotIn("\n", stop.evidence)
        self.assertLessEqual(len(stop.evidence), hard_stops.EVIDENCE_LIMIT)
        with self.assertRaises(ValueError):
            hard_stops.HardStop("bored", "x")

    def test_signal_reads_only_a_file_and_refuses_an_oversized_one(self):
        import tempfile
        with tempfile.TemporaryDirectory() as raw:
            clone = Path(raw)
            (clone / ".git").mkdir()
            self.assertIsNone(hard_stops.signal(clone))
            (clone / hard_stops.SIGNAL_FILE).write_text("x" * (hard_stops.SIGNAL_LIMIT + 1))
            with self.assertRaisesRegex(store.RunnerRefused, "exceeds"):
                hard_stops.signal(clone)

    def test_a_signal_that_is_not_a_regular_file_is_a_stop_not_an_absence(self):
        """sd:1221: `is_file()` read a directory and a dangling link as no signal at all."""
        import tempfile
        for make in (lambda path: path.mkdir(),
                     lambda path: path.symlink_to(path.parent / "nowhere.json")):
            with tempfile.TemporaryDirectory() as raw, self.subTest(make=make):
                clone = Path(raw)
                (clone / ".git").mkdir()
                make(clone / hard_stops.SIGNAL_FILE)
                with self.assertRaisesRegex(store.RunnerRefused, "not a regular file"):
                    hard_stops.signal(clone)

    def test_a_receipt_pass_from_another_run_classifies_nothing(self):
        """sd:1221: the receipt is the branch's, so the head this run authored selects its pass."""
        receipt = {"passes": [{"head": "a" * 40, "report": {"status": "blocking", "findings": [
            {"disposition": "blocking", "path": "a.py", "line": 3, "summary": "s"}]}}]}
        self.assertIsNone(hard_stops.from_receipt(receipt, head="b" * 40))
        self.assertEqual(hard_stops.from_receipt(receipt, head="a" * 40).kind, hard_stops.BLOCKING_FINDING)
        # A pass with no head is an older receipt shape and names no run.
        self.assertIsNone(hard_stops.from_receipt({"passes": [{"report": {"status": "blocking", "findings": []}}]}, head="a" * 40))


if __name__ == "__main__":
    unittest.main()
