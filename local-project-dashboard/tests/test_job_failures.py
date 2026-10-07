"""Failed jobs carry a rule's class, and Jev only shadows it (sd:1166, sd:2095).

No test here reaches the real `jev`: `document` asks nothing unless handed a
command, and the command here is a stub that writes down what it was asked.
The stub answers `enabled STAGE` from that stage's variable, as `jev` does.
"""

import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import job_failures, now_screen

from support import NOW, ScreenCase
from test_now_screen import JobsBackend, fleet_document, repo
from .test_operations_actions import FakeBackend
from test_workflow_actions import BrowserSession

#: A stub `jev`: one line of argv per call and the state it read, then
#: `enabled` honours JEV_JOB_TRIAGE and `score` prints the loudest level, so a
#: row that used Jev's answer would read differently from one that did not.
STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$JEV_STUB_LOG"
case "$1" in
  enabled) case "${JEV_JOB_TRIAGE:-}" in 0|off|false|no|disabled) exit 3 ;; esac; exit 0 ;;
  score) cat >> "$JEV_STUB_STATE"; printf '\\n' >> "$JEV_STUB_STATE"; echo 3 ;;
esac
"""

SECRET = "private-log-line-example"


def failed(name="nightly-sync", code=1, killed=None):
    return {"name": name, "state": "failed", "last_exit": code, "last_signal": killed}


class Classify(ScreenCase):
    """The first rule that matches wins; the reason names the rule, not the log."""

    def log(self, *runs):
        path = Path(self.tmp.name) / "nightly-sync.log"
        path.write_text("".join(f"[nightly-sync] 2026-09-27T02:00:00-0600 starting (cwd: /)\n{run}" for run in runs))
        return path

    def test_a_crash_signal_needs_a_person_and_a_stop_is_probably_transient(self):
        self.assertEqual(job_failures.classify(failed(code=0, killed=11), None),
                         {"score": 3, "class": "needs a person", "why": "crashed with signal 11"})
        self.assertEqual(job_failures.classify(failed(code=None, killed=9), None)["class"], "probably transient")

    def test_an_exit_code_that_says_what_happened_decides_before_the_log(self):
        log = self.log("Could not resolve host: example.test\n")
        self.assertEqual(job_failures.classify(failed(code=127), log)["why"], "command not found (exit 127)")
        self.assertEqual(job_failures.classify(failed(code=124), log)["class"], "probably transient")

    def test_a_dns_failure_in_the_last_run_is_transient(self):
        found = job_failures.classify(failed(), self.log("curl: (6) Could not resolve host: example.test\n"))
        self.assertEqual(found, {"score": 0, "class": "transient", "why": "a DNS failure in the last run's log"})

    def test_a_person_signature_outranks_a_transient_one_in_the_same_run(self):
        found = job_failures.classify(failed(), self.log("connection refused\nHTTP 401 Unauthorized\n"))
        self.assertEqual(found["class"], "needs a person")
        self.assertEqual(found["why"], "an authentication failure in the last run's log")

    def test_only_the_last_run_is_read(self):
        log = self.log("Could not resolve host: example.test\nFAILED rc=1\n", "something else went wrong\n")
        self.assertEqual(job_failures.classify(failed(), log),
                         {"score": 2, "class": "unclear", "why": "no rule matched exit 1"})

    def test_no_log_and_no_rule_is_unclear(self):
        self.assertEqual(job_failures.classify(failed(code=3), Path(self.tmp.name) / "absent.log")["class"], "unclear")

    def test_the_reason_never_carries_log_text(self):
        found = job_failures.classify(failed(), self.log(f"{SECRET}\nCould not resolve host: secret.example.test\n"))
        self.assertNotIn("secret", json.dumps(found))
        self.assertNotIn("private-log-line", json.dumps(found))


class NowRows(ScreenCase):
    def fleet(self, area):
        return fleet_document(area, repos=[repo("pushy", ahead=1)])

    def test_a_failed_job_row_ends_with_its_class_and_keeps_its_rank(self):
        jobs = JobsBackend(self.tmp.name, jobs=[("nightly-sync", "failed", 127, None), ("stopped", "interrupted", None, 9)])
        rows = now_screen.document(self.connection, now=NOW, fleet=self.fleet, jobs=jobs)["rows"]
        row = rows[0]
        self.assertEqual((row["rank"], row["id"]), (now_screen.FAILED, "job:nightly-sync:127"))
        self.assertTrue(row["detail"].endswith(" · triage: needs a person, command not found (exit 127)"), row["detail"])
        self.assertEqual(row["triage"]["score"], 3)
        self.assertEqual(row["retry"], "launchctl kickstart fixture/nightly-sync")
        # Only a failed job is classified.
        self.assertNotIn("triage", rows[1])


class Shadow(ScreenCase):
    """Jev is asked in the background and what it answers changes nothing."""

    def setUp(self):
        super().setUp()
        job_failures._asked.clear()
        self.addCleanup(job_failures._asked.clear)
        folder = Path(self.tmp.name)
        self.stub = folder / "jev"
        self.stub.write_text(STUB)
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IEXEC)
        self.calls, self.state = folder / "calls", folder / "state"
        pins = {"JEV_STUB_LOG": str(self.calls), "JEV_STUB_STATE": str(self.state), "JEV_METER": "0",
                "JEV_CORPUS": "0", "JEV_TRACES_URL": "0"}
        environment = patch.dict(os.environ, pins)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("JEV_JOB_TRIAGE", None)
        self.jobs = JobsBackend(self.tmp.name, jobs=[("nightly-sync", "failed", 1, None), ("quiet", "idle", 0, None)])
        (self.jobs.cron_root / "logs" / "nightly-sync.log").write_text(
            f"[nightly-sync] 2026-09-27T02:00:00-0600 starting (cwd: /)\n{SECRET}\nCould not resolve host: example.test\n")

    def document(self, jev):
        threads = []
        real = job_failures.shadow

        def kept(rows, command):
            thread = real(rows, command)
            threads.append(thread)
            return thread

        with patch.object(job_failures, "shadow", kept):
            document = now_screen.document(self.connection, now=NOW, fleet=lambda area: fleet_document(area), jobs=self.jobs,
                                           jev=jev)
        for thread in threads:
            if thread is not None:
                thread.join(30)
        return document, threads

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_jev_is_gated_then_asked_with_the_rules_answer_as_its_shadow(self):
        plain, _ = self.document(None)
        shadowed, _ = self.document([str(self.stub)])
        self.assertEqual(shadowed, plain)
        calls = self.calls_made()
        self.assertEqual(calls[0], "enabled JEV_JOB_TRIAGE --record --caller local-project-dashboard --stage JEV_JOB_TRIAGE")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].startswith("score "), calls[1])
        self.assertIn("--shadow 0 --caller local-project-dashboard --stage JEV_JOB_TRIAGE", calls[1])
        self.assertIn("--levels transient,probably transient,unclear,needs a person", calls[1])

    def test_what_jev_sees_is_the_name_the_outcome_and_the_rule_and_no_log_line(self):
        self.document([str(self.stub)])
        state = json.loads(self.state.read_text())
        self.assertEqual(state, {"job": "nightly-sync", "state": "failed", "outcome": "nightly-sync failed with exit 1",
                                 "rule_class": "transient", "rule_reason": "a DNS failure in the last run's log"})
        self.assertNotIn("private-log-line", self.state.read_text())

    def test_a_stage_switched_off_asks_only_the_gate(self):
        os.environ["JEV_JOB_TRIAGE"] = "off"
        self.document([str(self.stub)])
        self.assertEqual([call.split()[0] for call in self.calls_made()], ["enabled"])

    def test_one_failure_is_asked_once(self):
        self.document([str(self.stub)])
        _, threads = self.document([str(self.stub)])
        self.assertEqual(threads, [None])
        self.assertEqual(len(self.calls_made()), 2)

    def test_a_later_run_with_the_same_outcome_is_asked_again(self):
        """The memo keys on the run, not on the outcome (sd:2904)."""
        self.document([str(self.stub)])
        seen = self.jobs.inspect
        # launchd's run counter moved; the exit code, and so the row id, did not.
        with patch.object(self.jobs, "inspect", lambda name: {**seen(name), "runs": 2,
                                                             "_observation": name + "run 2"}):
            self.document([str(self.stub)])
        self.assertEqual([call.split()[0] for call in self.calls_made()], ["enabled", "score"] * 2)

    def test_no_command_asks_nothing(self):
        _, threads = self.document(None)
        self.assertEqual(threads, [None])
        self.assertEqual(self.calls_made(), [])


class OperationsCard(BrowserSession):
    def setUp(self):
        self.backend = FakeBackend()
        super().setUp()

    def test_a_failed_job_card_shows_its_class_and_no_other_card_does(self):
        status, _, page = self.request("/operations?area=jobs")
        self.assertEqual(status, 200)
        start = page.index("crashed fixture")
        crashed = page[start:page.index("</article>", start)]
        self.assertIn("Triage: needs a person, crashed with signal 11. Advisory only; nothing retries it.", crashed)
        self.assertEqual(page.count("Advisory only; nothing retries it."), 2)
