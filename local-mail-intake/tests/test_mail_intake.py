"""Tests for local-mail-intake.

None of these touch the network. The server's replies are prose with fields
embedded rather than JSON, so the parsers are the part most likely to break
when the server changes, and they are tested against captured samples of the
real format.
"""

import io
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mail_intake as mi  # noqa: E402


SEARCH_SAMPLE = """Found 3 messages matching 'x':

\U0001F4E7 MESSAGES:
  1. Message ID: aaa1
     Web Link: https://mail.google.com/mail/u/0/#all/aaa1
     Thread ID: t1
     Thread Link: https://mail.google.com/mail/u/0/#all/t1

  2. Message ID: aaa2
     Web Link: https://mail.google.com/mail/u/0/#all/aaa2
     Thread ID: t1
     Thread Link: https://mail.google.com/mail/u/0/#all/t1

  3. Message ID: bbb1
     Web Link: https://mail.google.com/mail/u/0/#all/bbb1
     Thread ID: t2
     Thread Link: https://mail.google.com/mail/u/0/#all/t2

\U0001F4C4 PAGINATION: To get the next page, call search_gmail_messages again with page_token='TOK123'
"""

METADATA_SAMPLE = """Retrieved 2 messages:

Message ID: aaa2
Subject: Re: Meeting follow up - Action Items
From: Pat Quimby <pat.quimby@example.net>
Date: Sat, 19 Sep 2026 10:22:10 -0600
Message-ID: <7F4F@mail.example.net>
To: Sam Roe <sam.roe@example.net>
Cc: Lee <lee@example.org>, Example Board <board@example.org>
Web Link: https://mail.google.com/mail/u/0/#all/aaa2

---

Message ID: bbb1
Subject: Weed Control Service Confirmation
From: Acme Grounds Inc <mail@acme.example>
Date: 18 Sep 2026 17:09:59 -0500
Message-ID: <2026@mail.acme.example>
To: board@example.org
Cc: [not present in Gmail response]
Web Link: https://mail.google.com/mail/u/0/#all/bbb1
"""

CONF = (
    "account|owner@example.com\n"
    "me|owner@example.com\n"
    "alias|board|board@example.org\n"
    "alias|committee|committee@example.org\n"
    "window|21\n"
)


def config() -> mi.Config:
    parsed, problems = mi.parse_config(CONF)
    assert not problems, problems
    return parsed


class TestParsers(unittest.TestCase):
    def test_search_pairs_messages_to_threads(self):
        self.assertEqual(
            mi.parse_search(SEARCH_SAMPLE),
            [("aaa1", "t1"), ("aaa2", "t1"), ("bbb1", "t2")],
        )

    def test_metadata_blocks(self):
        blocks = mi.parse_metadata(METADATA_SAMPLE)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["Subject"], "Re: Meeting follow up - Action Items")
        self.assertEqual(blocks[0]["From"], "Pat Quimby <pat.quimby@example.net>")
        self.assertIn("board@example.org", blocks[0]["Cc"])

    def test_absent_cc_is_not_an_address(self):
        blocks = mi.parse_metadata(METADATA_SAMPLE)
        self.assertEqual(mi.addresses(blocks[1]["Cc"]), set())

    def test_display_name_falls_back_to_the_address(self):
        self.assertEqual(mi.display_name("Pat Quimby <pat.quimby@example.net>"), "Pat Quimby")
        self.assertEqual(mi.display_name("bare@example.com"), "bare@example.com")

    def test_date_without_a_weekday_still_parses(self):
        self.assertIsNotNone(mi.when("18 Sep 2026 17:09:59 -0500"))


class TestAliasAttribution(unittest.TestCase):
    def setUp(self):
        self.config = config()

    def test_alias_found_in_cc(self):
        blocks = mi.parse_metadata(METADATA_SAMPLE)
        self.assertEqual(mi.alias_for(blocks[0], self.config.aliases), "board")

    def test_alias_found_in_to(self):
        blocks = mi.parse_metadata(METADATA_SAMPLE)
        self.assertEqual(mi.alias_for(blocks[1], self.config.aliases), "board")

    def test_mail_touching_no_alias_is_somebody_elses(self):
        self.assertEqual(
            mi.alias_for({"To": "a@b.com", "From": "c@d.com"}, self.config.aliases), "")


class TestWaitingOn(unittest.TestCase):
    def setUp(self):
        self.config = config()

    def message(self, sender: str) -> mi.Message:
        return mi.Message("m", "t", "s", "who", sender,
                          datetime(2026, 9, 19, tzinfo=timezone.utc), "board")

    def test_their_reply_leaves_it_with_you(self):
        self.assertEqual(mi.waiting_on(self.message("someone@else.com"), self.config), "you")

    def test_your_reply_leaves_it_with_them(self):
        self.assertEqual(mi.waiting_on(self.message("owner@example.com"), self.config), "them")

    def test_case_in_the_header_does_not_matter(self):
        self.assertEqual(mi.waiting_on(self.message("OWNER@example.com".lower()), self.config), "them")


class TestThreads(unittest.TestCase):
    def setUp(self):
        self.config = config()

    def make(self, mid, tid, day, sender="x@y.com"):
        return mi.Message(mid, tid, "subj", "Who", sender,
                          datetime(2026, 9, day, tzinfo=timezone.utc), "board")

    def test_latest_wins_within_a_thread(self):
        latest = mi.latest_per_thread([
            self.make("m1", "t1", 1), self.make("m3", "t1", 3), self.make("m2", "t1", 2)])
        self.assertEqual(latest["t1"].message_id, "m3")

    def test_a_thread_with_no_new_message_produces_no_row(self):
        latest = {"t1": self.make("m3", "t1", 3)}
        rows = mi.diff(latest, {"t1": "m3"}, self.config, "now")
        self.assertEqual(rows, [])

    def test_one_more_reply_produces_exactly_one_row(self):
        latest = {"t1": self.make("m4", "t1", 4)}
        rows = mi.diff(latest, {"t1": "m3"}, self.config, "now")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], "m4")

    def test_direction_reflects_the_sender(self):
        latest = {"t1": self.make("m1", "t1", 1, sender="owner@example.com")}
        rows = mi.diff(latest, {}, self.config, "now")
        self.assertEqual(rows[0]["direction"], "sent")
        self.assertEqual(rows[0]["waiting_on"], "them")


class TestLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = self.dir / "mail-arrivals.csv"
        self.addCleanup(self.tmp.cleanup)

    def row(self, mid, at):
        return {"detected_at": at, "thread_id": "t" + mid, "message_id": mid,
                "alias": "board", "subject": "s", "who": "w", "direction": "received",
                "waiting_on": "you", "sent_at": at, "reported_at": ""}

    def test_appends_never_replace(self):
        mi.append_arrivals(self.log, [self.row("a", "2026-09-01T00:00:00+00:00")])
        mi.append_arrivals(self.log, [self.row("b", "2026-09-02T00:00:00+00:00")])
        self.assertEqual(len(mi.read_arrivals(self.log)), 2)

    def test_stamp_through_leaves_later_rows_pending(self):
        mi.append_arrivals(self.log, [self.row("a", "2026-09-01T00:00:00+00:00")])
        mi.append_arrivals(self.log, [self.row("b", "2026-09-09T00:00:00+00:00")])
        stamped = mi.stamp_arrivals(self.log, "2026-09-01T00:00:00+00:00", "now")
        self.assertEqual(stamped, 1)
        pending = [r["message_id"] for r in mi.read_arrivals(self.log)]
        self.assertEqual(pending, ["b"])

    def test_stamped_rows_stay_in_the_history(self):
        mi.append_arrivals(self.log, [self.row("a", "2026-09-01T00:00:00+00:00")])
        mi.stamp_arrivals(self.log, "2026-12-01T00:00:00+00:00", "now")
        self.assertEqual(len(mi.read_arrivals(self.log)), 0)
        self.assertEqual(len(mi.read_arrivals(self.log, unreported_only=False)), 1)


class TestConfig(unittest.TestCase):
    def test_missing_me_is_refused(self):
        parsed, problems = mi.parse_config("account|a@b.com\nalias|x|y@z.com\n")
        self.assertIsNone(parsed)
        self.assertTrue(any("no me rows" in p for p in problems))

    def test_missing_alias_is_refused(self):
        parsed, problems = mi.parse_config("account|a@b.com\nme|a@b.com\n")
        self.assertIsNone(parsed)
        self.assertTrue(any("no alias rows" in p for p in problems))

    def test_the_account_always_counts_as_me(self):
        parsed, _ = mi.parse_config("account|a@b.com\nme|other@b.com\nalias|x|y@z.com\n")
        self.assertIn("a@b.com", parsed.me)


class TestConfigLocation(unittest.TestCase):
    """The conf names real addresses, so it lives in the config folder."""

    def status(self, environ):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as state:
            environ = dict(environ, MAIL_INTAKE_STATE=state,
                           WORKSPACE_MCP_URL="http://127.0.0.1:9/mcp")
            mi.main(["status"], environ=environ, out=out)
        return out.getvalue()

    def test_default_is_the_config_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = self.status({"SYSTEM_TOOLS_CONFIG": tmp})
        expected = Path(tmp) / "mail-intake" / "mail-intake.conf"
        self.assertIn(f"config : {expected}", output)
        self.assertIn("mail-intake.conf.example", output)

    def test_the_variable_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "elsewhere.conf"
            conf.write_text(CONF, encoding="utf-8")
            output = self.status({"SYSTEM_TOOLS_CONFIG": tmp,
                                  "MAIL_INTAKE_CONFIG": str(conf)})
        self.assertIn(f"config : {conf}", output)
        self.assertNotIn("does not exist", output)

    def test_the_example_parses(self):
        example = Path(mi.__file__).resolve().parent / "mail-intake.conf.example"
        config, problems = mi.parse_config(example.read_text(encoding="utf-8"))
        self.assertEqual(problems, [])
        self.assertEqual(sorted(config.aliases), ["board", "committee"])


class TestNeverSends(unittest.TestCase):
    def test_only_read_tools_are_named_anywhere_in_the_module(self):
        """A send would be a rule violation, so it is asserted, not assumed."""
        source = (Path(__file__).resolve().parent.parent / "mail_intake.py").read_text(encoding="utf-8")
        for forbidden in ("send_gmail_message", "draft_gmail_message",
                          "modify_gmail_message_labels", "manage_gmail_filter"):
            self.assertNotIn(forbidden, source, f"{forbidden} must never appear here")
        self.assertIn("search_gmail_messages", source)
        self.assertIn("get_gmail_messages_content_batch", source)

    def test_bodies_are_never_requested(self):
        source = (Path(__file__).resolve().parent.parent / "mail_intake.py").read_text(encoding="utf-8")
        self.assertIn('"format": "metadata"', source)
        self.assertNotIn('"format": "full"', source)


THROTTLED_SAMPLE = """Retrieved 3 messages:

Message ID: aaa1
Subject: Good one
From: A <a@y.com>
Date: Fri, 18 Sep 2026 16:22:28 -0600
To: Example Board <board@x.com>

---

\u26a0\ufe0f Message bbb9: <HttpError 429 when requesting https://gmail.googleapis.com/gmail/v1/users/me/messages/bbb9?format=metadata returned "Too many concurrent requests for user.". Details: "[{'message': 'Too many concurrent requests for user.', 'reason': 'rateLimitExceeded'}]">

---

\u26a0\ufe0f Message ccc7: <HttpError 429 when requesting https://gmail.googleapis.com/gmail/v1/users/me/messages/ccc7?format=metadata returned "Too many concurrent requests for user.".>
"""


class TestThrottling(unittest.TestCase):
    """Gmail 429s an over-wide batch, and a dropped message is a false alarm.

    Measured on the real mailboxes: at a batch of 25 the server lost eight to
    fifteen of 104 messages per run, a different set each time. Each loss hid a
    thread, and the next run reported that thread as newly arrived.
    """

    def test_a_throttled_message_is_collected_not_ignored(self):
        self.assertEqual(mi.parse_failures(THROTTLED_SAMPLE), ["bbb9", "ccc7"])

    def test_a_throttled_block_yields_no_bogus_headers(self):
        blocks = mi.parse_metadata(THROTTLED_SAMPLE)
        self.assertEqual([b["Message ID"] for b in blocks], ["aaa1"])

    def test_a_clean_batch_reports_no_failures(self):
        self.assertEqual(mi.parse_failures(METADATA_SAMPLE), [])

    def test_the_batch_is_small_enough_that_gmail_answers(self):
        self.assertLessEqual(mi.BATCH, 10)


class TestStateSurvivesAPartialRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "threads.csv"
        self.config = config()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, mid, tid, day):
        return mi.Message(mid, tid, "subj", "Who", "x@y.com",
                          datetime(2026, 9, day, tzinfo=timezone.utc), "board")

    def test_a_thread_this_run_could_not_see_keeps_its_row(self):
        mi.write_threads(self.path, {"t1": self.make("m1", "t1", 1),
                                     "t2": self.make("m2", "t2", 2)}, "then")
        carry = mi.read_thread_rows(self.path)
        # t2 was throttled away: it is simply absent from this run's results.
        mi.write_threads(self.path, {"t1": self.make("m1", "t1", 1)}, "now", carry)
        self.assertEqual(sorted(mi.read_threads(self.path)), ["t1", "t2"])

    def test_a_thread_lost_to_a_429_is_not_reported_as_new_next_run(self):
        mi.write_threads(self.path, {"t1": self.make("m1", "t1", 1),
                                     "t2": self.make("m2", "t2", 2)}, "then")
        carry = mi.read_thread_rows(self.path)
        mi.write_threads(self.path, {"t1": self.make("m1", "t1", 1)}, "now", carry)
        # Next run sees t2 again, unchanged. It must produce no arrival.
        rows = mi.diff({"t2": self.make("m2", "t2", 2)},
                       mi.read_threads(self.path), self.config, "now")
        self.assertEqual(rows, [])

    def test_a_real_new_message_still_reports(self):
        mi.write_threads(self.path, {"t1": self.make("m1", "t1", 1)}, "then")
        carry = mi.read_thread_rows(self.path)
        mi.write_threads(self.path, {"t1": self.make("m5", "t1", 5)}, "now", carry)
        row = mi.read_thread_rows(self.path)["t1"]
        self.assertEqual(row[1], "m5")


if __name__ == "__main__":
    unittest.main()


class TestAQuietDayIsNotAnError(unittest.TestCase):
    """`report` and `stamp` with no arrivals log: 3 when fetch has run, 1 when not.

    The README's own contract is "0 something changed, 3 nothing to do, 1 a
    real error. A quiet day must be 3 and not 1." Both verbs returned 1 for an
    absent log, so a machine whose baseline run found nothing new ever since
    failed every report with advice to run the fetch it had already run.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def report(self):
        out = io.StringIO()
        return mi.cmd_report(self.state, out), out.getvalue()

    def stamp(self):
        out = io.StringIO()
        return mi.cmd_stamp(self.state, None, out), out.getvalue()

    def test_report_with_state_and_no_log_is_nothing_to_do(self):
        (self.state / "threads.csv").write_text("thread_id\n", encoding="utf-8")
        code, text = self.report()
        self.assertEqual(code, mi.EXIT_NONE)
        self.assertIn("nothing undelivered", text)

    def test_report_with_no_state_still_says_run_fetch(self):
        code, text = self.report()
        self.assertEqual(code, mi.EXIT_ERROR)
        self.assertIn("run fetch first", text)

    def test_stamp_with_state_and_no_log_is_nothing_to_do(self):
        (self.state / "threads.csv").write_text("thread_id\n", encoding="utf-8")
        code, text = self.stamp()
        self.assertEqual(code, mi.EXIT_NONE)
        self.assertIn("nothing to stamp", text)

    def test_stamp_with_no_state_still_says_run_fetch(self):
        code, text = self.stamp()
        self.assertEqual(code, mi.EXIT_ERROR)
        self.assertIn("run fetch first", text)


class TestAsksIsOptionalAndOnUnlessSwitchedOff(unittest.TestCase):
    """The Jev signal is additive, and the report is identical without it.

    Jev is experimental, so the repo's rule is that the machine runs the same
    without it as with it. That is checked here rather than asserted in prose:
    the report from a run with the stage switched off is captured and every
    other case is compared against those exact bytes -- switched off, not
    unset, because unset reaches Jev now. `baseline` below says the same
    thing; this sentence used to say the opposite of it. No test spawns `jev`; `run_jev` is stubbed, the
    same way nothing here talks to the MCP server either.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.state / "threads.csv").write_text("thread_id\n", encoding="utf-8")
        mi.append_arrivals(self.state / "mail-arrivals.csv", [
            self.row("m1", "t1", "Minutes from August", "you"),
            self.row("m2", "t2", "Please approve the fence variance", "you"),
            self.row("m3", "t3", "Thanks all", "them"),
        ])

    def row(self, mid, tid, subject, waiting):
        return {"detected_at": "2026-09-19T08:00:00+00:00", "thread_id": tid,
                "message_id": mid, "alias": "board", "subject": subject,
                "who": "Pat Quimby", "direction": "received",
                "waiting_on": waiting, "sent_at": "2026-09-19T07:00:00+00:00",
                "reported_at": ""}

    def report(self, environ):
        out, err = io.StringIO(), io.StringIO()
        code = mi.cmd_report(self.state, out, environ=environ, err=err)
        return code, out.getvalue(), err.getvalue()

    def baseline(self):
        """Today's report, with the stage switched off.

        `JEV_MAIL_INTAKE` only switches this stage off now -- unset means on --
        so the run with no Jev in it is the one that sets it to 0, and
        `jev enabled JEV_MAIL_INTAKE` is what reads that and exits 3.
        """
        with mock.patch.object(mi, "run_jev", return_value=(3, "")) as runner:
            code, text, err = self.report({mi.JEV_STAGE: "0"})
        # `enabled` asked, and nothing else: no question left the machine.
        self.assertEqual([c.args[0][1] for c in runner.call_args_list], ["enabled"])
        self.assertEqual([c.args[0][2] for c in runner.call_args_list],
                         [mi.JEV_STAGE])
        return code, text, err

    def test_the_stage_switched_off_asks_nothing_and_prints_todays_report(self):
        code, text, err = self.baseline()
        self.assertEqual(code, mi.EXIT_FOUND)
        self.assertIn("Please approve the fence variance", text)
        self.assertNotIn("(asks)", text)
        # It says why it did nothing rather than going quiet: a lane that
        # stops running without saying so is the defect this shape avoids.
        self.assertIn("not enabled here", err)

    def test_the_stage_unset_reaches_jev(self):
        # The flip. Every one of these integrations was opt-in, and an opt-in
        # that defaults to off makes each one added after it silently never
        # run. `jev enabled` still decides whether the machine can answer.
        with mock.patch.object(mi, "run_jev", return_value=(3, "")) as runner:
            self.report({})
        self.assertEqual(runner.call_args_list[0].args[0][1:],
                         ["enabled", mi.JEV_STAGE,
                          "--record", "--caller", mi.JEV_CALLER])

    def test_the_gate_records_its_decline_under_this_tools_own_name(self):
        """`enabled` alone counts nothing, and today's order is the control arm.

        Most reports end at this gate, so without `--record` the old path runs
        and no row anywhere says it did: the stage reads as one nobody used.
        `--caller` rides the same call because a row with no name is filed
        under `unknown`, where it groups with every other caller's.
        """
        with mock.patch.object(mi, "run_jev", return_value=(3, "")) as runner:
            self.report({})
        argv = runner.call_args_list[0].args[0]
        self.assertIn("--record", argv)
        self.assertEqual(argv[argv.index("--caller") + 1], "local-mail-intake")
        self.assertEqual(mi.JEV_CALLER, "local-mail-intake")

    def test_every_judgment_call_names_this_caller_and_its_stage(self):
        """One name across the switch, the ledger and the per-stage report.

        The stage is the same word `jev enabled` is handed, so the two arms of
        one decision are filed under one stage rather than two.
        """
        answers = [(0, "")]

        def fake(argv, state, environ=None):
            if argv[1] == "enabled":
                return 0, ""
            answers.append((argv, state))
            return 0, "no"

        with mock.patch.object(mi, "run_jev", side_effect=fake):
            self.report({})
        asked = [argv for argv, _ in answers[1:]]
        self.assertTrue(asked, "no judgment call was made at all")
        for argv in asked:
            self.assertEqual(argv[argv.index("--caller") + 1],
                             "local-mail-intake")
            self.assertEqual(argv[argv.index("--stage") + 1], "JEV_MAIL_INTAKE")

    def test_the_stage_unset_is_not_enough_when_jev_is_disabled(self):
        _, expected, _ = self.baseline()
        with mock.patch.object(mi, "run_jev", return_value=(3, "")) as runner:
            code, text, err = self.report({})
        self.assertEqual(text, expected)
        self.assertEqual(code, mi.EXIT_FOUND)
        self.assertIn("not enabled here", err)
        # `enabled` asked, and nothing else. It calls nothing and costs nothing.
        self.assertEqual(len(runner.call_args_list), 1)
        self.assertEqual(runner.call_args_list[0].args[0][1], "enabled")

    def test_a_thread_that_asks_for_something_sorts_to_the_top_of_its_group(self):
        def answers(args, state, environ=None):
            if args[1] == "enabled":
                return 0, ""
            return 0, "yes" if "fence variance" in state else "no"

        with mock.patch.object(mi, "run_jev", side_effect=answers):
            code, text, _ = self.report({})
        self.assertEqual(code, mi.EXIT_FOUND)
        you = text.index("WAITING ON YOU")
        variance = text.index("Please approve the fence variance", you)
        minutes = text.index("Minutes from August", you)
        self.assertLess(variance, minutes, "the thread that asks must come first")
        # Nothing is dropped, hidden or moved between groups.
        self.assertIn("Thanks all", text)
        self.assertIn("WAITING ON YOU (2)", text)
        self.assertIn("waiting on them (1)", text)
        self.assertIn("(asks)", text)

    def test_a_jev_failure_leaves_the_report_and_the_exit_code_intact(self):
        _, expected, _ = self.baseline()

        def blows_up(args, state, environ=None):
            if args[1] == "enabled":
                return 0, ""
            raise OSError("boom")

        with mock.patch.object(mi, "run_jev", side_effect=blows_up):
            code, text, err = self.report({})
        self.assertEqual(text, expected)
        self.assertEqual(code, mi.EXIT_FOUND)
        self.assertIn("boom", err, "a lane that degrades must say so")

    def test_a_refused_answer_leaves_that_thread_where_it_was(self):
        _, expected, _ = self.baseline()
        with mock.patch.object(mi, "run_jev",
                               side_effect=lambda a, s, e=None: (0, "" if a[1] != "enabled" else "")):
            code, text, err = self.report({})
        self.assertEqual(text, expected)
        self.assertEqual(code, mi.EXIT_FOUND)
        self.assertIn("no answer", err)


class TestTheSwitchReachesTheChild(unittest.TestCase):
    """`run_jev` spawns the only subprocess, and `jev enabled JEV_MAIL_INTAKE`
    reads that variable in the *child*.

    This module's `environ` is an injected dict, not the process environment,
    so a caller that sets the switch in it -- the only way a test or an
    embedding caller can set it -- reaches the child only because `run_jev`
    passes `env=`. Without that the kill switch looks set and does nothing,
    which is the failure the whole shape exists to avoid. These two cases
    fail against a `run_jev` that omits `env=`.
    """

    def test_the_injected_environ_reaches_the_subprocess(self):
        seen = {}

        def fake_run(args, **kwargs):
            seen.update(kwargs.get("env") or {})
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(mi.subprocess, "run", fake_run):
            mi.run_jev(["jev", "enabled", mi.JEV_STAGE], "",
                       {mi.JEV_STAGE: "0", "MAIL_INTAKE_MARKER": "x"})
        self.assertEqual(seen.get(mi.JEV_STAGE), "0",
                         "the switch did not reach the child")
        self.assertEqual(seen.get("MAIL_INTAKE_MARKER"), "x")

    def test_it_is_layered_over_the_process_environment_not_swapped_for_it(self):
        """The child still needs PATH. Replacing rather than layering would
        hand `jev` an environment with no interpreter to find."""

        seen = {}

        def fake_run(args, **kwargs):
            seen.update(kwargs.get("env") or {})
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(mi.subprocess, "run", fake_run):
            mi.run_jev(["jev", "enabled"], "", {"MAIL_INTAKE_MARKER": "x"})
        self.assertIn("PATH", seen)
        self.assertEqual(seen.get("MAIL_INTAKE_MARKER"), "x")


class TestNothingPrivateLeavesTheMachine(unittest.TestCase):
    """Every Jev call leaves the machine, so the payload is asserted, not assumed.

    The mailbox carries real names and addresses and the repo's rule keeps
    those out of git; sending them to a third party is worse. The subject line
    and one direction word are the whole payload, and this is the test that
    fails when a helpful edit widens it.
    """

    ROW = {"detected_at": "2026-09-19T08:00:00+00:00", "thread_id": "t9f3",
           "message_id": "m8ab", "alias": "board",
           "subject": "Re:   Fence variance   decision", "who": "Pat Quimby",
           "direction": "received", "waiting_on": "you",
           "sent_at": "2026-09-19T07:00:00+00:00", "reported_at": ""}

    def test_the_payload_is_a_subject_and_a_direction_word(self):
        state = mi.state_for_jev(self.ROW)
        self.assertEqual(state, "direction: inbound\nsubject: Re: Fence variance decision\n")

    def test_an_outbound_thread_says_so_without_naming_anyone(self):
        state = mi.state_for_jev({**self.ROW, "direction": "sent"})
        self.assertEqual(state, "direction: outbound\nsubject: Re: Fence variance decision\n")

    def test_no_address_name_body_or_id_reaches_jev(self):
        sent = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_dir = Path(tmp.name)
        (state_dir / "threads.csv").write_text("thread_id\n", encoding="utf-8")
        mi.append_arrivals(state_dir / "mail-arrivals.csv", [self.ROW])

        def capture(args, state, environ=None):
            sent.append((args, state))
            return (0, "") if args[1] == "enabled" else (0, "yes")

        with mock.patch.object(mi, "run_jev", side_effect=capture):
            mi.cmd_report(state_dir, io.StringIO(), environ={},
                          err=io.StringIO())

        everything = "\n".join(" ".join(args) + "\n" + state for args, state in sent)
        for forbidden in ("Pat", "Quimby", "@", "t9f3", "m8ab",
                          "example.org", "pat.quimby"):
            self.assertNotIn(forbidden, everything,
                             f"{forbidden!r} must never leave the machine")
        self.assertIn("Fence variance decision", everything)

    def test_the_sibling_is_reached_by_path_and_not_by_name(self):
        """cron and CI have no `jev` on PATH, so the name would silently stop."""
        script = mi.jev_script()
        self.assertTrue(script.is_absolute())
        self.assertEqual(script.name, "jev.sh")
        self.assertEqual(script.parent.name, "local-jev")

    def test_the_module_never_shells_out_to_a_bare_jev(self):
        source = (Path(__file__).resolve().parent.parent / "mail_intake.py").read_text(encoding="utf-8")
        self.assertNotIn('"jev"', source)
        self.assertIn("jev_script()", source)
