"""The digest must be the same digest with Jev as without it.

Jev is experimental, so the thing worth testing is not that it ranks well --
nothing here can know that -- but that the tool is unchanged when it is off,
unavailable or broken, and that turning it on moves cards around without
adding, dropping or re-wiring one. The stub in `harness.py` stands in for
`jev.sh`, so no test here touches the network.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness  # noqa: E402

GOLDEN_LIST = (harness.FIXTURES / "golden_list.txt").read_text()
GOLDEN_HTML = (harness.FIXTURES / "golden_body.html").read_text().rstrip("\n")

ON = {"JEV_OBSIDIAN_TASKS": "1"}
#: `JEV_OBSIDIAN_TASKS` switches the stage off and nothing switches it on:
#: unset is on. Yesterday's digest is therefore the run that sets it to 0.
OFF = {"JEV_OBSIDIAN_TASKS": "0"}


def titles(text: str):
    """The task titles a digest actually shows, in the order it shows them."""
    return [m.group(1) for m in re.finditer(r"^- (.+?)(?: \[| —)", text, re.M)]


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = harness.build_root(self.tmp.name)

    def digest(self, env=None):
        out, html, rc = harness.run(self.root, env)
        self.assertEqual(rc, 0)
        return harness.templated(out), harness.templated(html)

    # --- the three ways Jev stays out of the way -------------------------

    def test_the_stage_switched_off_is_byte_for_byte_yesterdays_digest(self):
        out, html = self.digest(OFF)
        self.assertEqual(out, GOLDEN_LIST)
        self.assertEqual(html, GOLDEN_HTML)

    def test_the_golden_holds_on_any_day_the_run_is_pinned_to(self):
        """The golden is yesterday's digest on every day, not only today.

        "3 day(s) overdue" and "due today" are not templated, so they hold
        only while the script counts from the day the fixture was written
        on. Neither date here is the real one: one crosses a leap day and
        one a year end. A script that read the clock instead of
        OBSIDIAN_TASKS_TODAY fails both.
        """
        for day in (datetime.date(2028, 3, 1), datetime.date(2027, 1, 1)):
            with self.subTest(day=day.isoformat()), \
                    tempfile.TemporaryDirectory() as tmp:
                root = harness.build_root(tmp, today=day)
                out, html, rc = harness.run(root, OFF, today=day)
                self.assertEqual(rc, 0)
                self.assertEqual(harness.templated(out, day), GOLDEN_LIST)
                self.assertEqual(harness.templated(html, day), GOLDEN_HTML)

    def test_the_stage_unset_orders_like_the_stage_switched_on(self):
        # The flip: every one of these integrations was opt-in, and an opt-in
        # that defaults to off makes each one added after it silently never
        # run. Unset now reaches Jev; the machine-wide answers still decide.
        self.assertEqual(self.digest(), self.digest(ON))
        self.assertNotEqual(self.digest(), self.digest(OFF))

    def test_jev_disabled_is_byte_for_byte_yesterdays_digest(self):
        out, html = self.digest({**ON, "JEV_STUB_ENABLED_EXIT": "3"})
        self.assertEqual(out, GOLDEN_LIST)
        self.assertEqual(html, GOLDEN_HTML)

    def test_a_failing_jev_leaves_the_digest_intact(self):
        env = {**ON, "JEV_STUB_ASK_FAIL": "1"}
        o, html, rc = harness.run(self.root, env)
        self.assertEqual(rc, 0)
        self.assertEqual(harness.templated(o), GOLDEN_LIST)
        self.assertEqual(harness.templated(html), GOLDEN_HTML)

    def test_a_failing_jev_says_so_on_stderr(self):
        import os
        import subprocess
        env = dict(os.environ)
        env.update({
            "OBSIDIAN_VAULT": str(self.root / "vault"),
            "NOTIFY_RECORD": str(self.root / "notify.args"),
            "JEV_OBSIDIAN_TASKS": "1",
            "JEV_STUB_ASK_FAIL": "1",
        })
        proc = subprocess.run(
            ["sh", str(self.root / "local-obsidian-tasks" / "obsidian-tasks.sh"),
             "run"],
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("jev did not rank this digest", proc.stderr)
        self.assertIn("keeping the usual order", proc.stderr)

    # --- and the one way it does something -------------------------------

    def test_jev_reorders_the_digest(self):
        out, html = self.digest(ON)
        self.assertEqual(titles(out), list(reversed(harness.EXPECTED_TITLES)))
        self.assertNotEqual(out, GOLDEN_LIST)
        self.assertIn("Ordered by Jev", html)
        self.assertIn("(ordered by Jev, most urgent first)", out)

    def test_the_item_set_never_changes(self):
        seen = []
        for env in (None, {**ON, "JEV_STUB_ENABLED_EXIT": "3"},
                    {**ON, "JEV_STUB_ASK_FAIL": "1"}, ON):
            out, html = self.digest(env)
            seen.append(sorted(titles(out)))
            self.assertEqual(out.splitlines()[0], GOLDEN_LIST.splitlines()[0])
            for title in harness.EXPECTED_TITLES:
                self.assertIn(title, html)
            self.assertNotIn("Water the plants", out)
            self.assertNotIn("Something finished", out)
        self.assertEqual(seen, [sorted(harness.EXPECTED_TITLES)] * 4)

    def test_the_buttons_and_their_links_are_untouched(self):
        plain = self.digest()[1]
        ranked = self.digest(ON)[1]
        hrefs = lambda doc: sorted(re.findall(r'href="([^"]+)"', doc))
        self.assertEqual(hrefs(plain), hrefs(ranked))
        self.assertEqual(sorted(re.findall(r">([^<>]+)</a>", plain)),
                         sorted(re.findall(r">([^<>]+)</a>", ranked)))

    # --- an unreachable signer is a bounded failure, not a crash ---------

    def test_a_hanging_signer_does_not_kill_the_digest(self):
        """sd:1770. The nightly died on one `url` call that hit its timeout.

        `except OSError` never caught `subprocess.TimeoutExpired`, so a slow
        `tailscale funnel status` killed the whole digest. It must now mail
        the same Open-only digest it mails when the signer is absent.
        """
        out, html = self.digest({**OFF, "ACTIONS_STUB_HANG": "30"})
        self.assertEqual(out, GOLDEN_LIST)
        self.assertEqual(html, GOLDEN_HTML)

    def test_a_hanging_signer_says_so_once(self):
        import os
        import subprocess
        env = dict(os.environ)
        env.update({
            "OBSIDIAN_VAULT": str(self.root / "vault"),
            "NOTIFY_RECORD": str(self.root / "notify.args"),
            "JEV_OBSIDIAN_TASKS": "0",
            "ACTIONS_STUB_HANG": "30",
        })
        proc = subprocess.run(
            ["sh", str(self.root / "local-obsidian-tasks" / "obsidian-tasks.sh"),
             "run"],
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("TimeoutExpired", proc.stderr)
        self.assertIn("did not answer within", proc.stderr)
        self.assertEqual(proc.stderr.count("action links unsigned"), 1)

    def test_the_signer_is_asked_for_a_base_url_once_and_never_again(self):
        """One tailscale probe a run, not one a button (sd:1770, as sd:1203).

        Each `url` call probed `tailscale funnel status` itself, three times a
        card. The base is now discovered once by `base-url` and handed down in
        TASK_ACTIONS_BASE_URL; this reads the stub's log of its calls.
        """
        record = self.root / "actions.calls"
        self.digest({**OFF,
                     "ACTIONS_STUB_RECORD": str(record),
                     "ACTIONS_STUB_SIGNS": "https://signer.example.test"})
        calls = [line.split(" ", 1) for line in
                 record.read_text().splitlines() if line]
        verbs = [verb for verb, _ in calls]
        self.assertEqual(verbs.count("base-url"), 1, calls)
        self.assertGreater(verbs.count("url"), 1, calls)
        self.assertEqual(
            {base for verb, base in calls if verb == "url"},
            {"https://signer.example.test"})

    def test_one_signing_timeout_retires_the_signer_for_the_run(self):
        record = self.root / "actions.calls"
        self.digest({**OFF,
                     "ACTIONS_STUB_RECORD": str(record),
                     "ACTIONS_STUB_SIGNS": "https://signer.example.test",
                     "ACTIONS_STUB_SIGN_HANG": "3",
                     "OBSIDIAN_TASKS_SIGN_TIMEOUT": "1"})
        verbs = [line.split(" ", 1)[0] for line in
                 record.read_text().splitlines() if line]
        self.assertEqual(verbs.count("base-url"), 1, verbs)
        # The first link is tried once more (sd:2536); no later link is tried.
        self.assertEqual(verbs.count("url"), 2, verbs)

    # --- a slow signer at night (sd:2536) ---------------------------------

    def signing(self, **extra):
        """A run against a stub that signs; returns (stderr, html, url calls)."""
        record = self.root / "actions.calls"
        record.unlink(missing_ok=True)
        state = self.root / "stub-state"
        shutil.rmtree(state, ignore_errors=True)
        state.mkdir()
        proc, html = harness.run_proc(self.root, {
            **OFF, "ACTIONS_STUB_RECORD": str(record), "ACTIONS_STUB_STATE": str(state),
            "ACTIONS_STUB_SIGNS": "https://signer.example.test", **extra})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verbs = [line.split(" ", 1)[0] for line in record.read_text().splitlines() if line]
        return proc.stderr, html, verbs.count("url")

    def test_a_signer_slower_than_five_seconds_still_signs(self):
        # 0.1 s by day, over 5 s under the nightly load: the old bound gave up.
        err, html, _ = self.signing(ACTIONS_STUB_FIRST_SIGN_HANG="6")
        self.assertNotIn("action links unsigned", err)
        self.assertIn("https://signer.example.test/task?stub=1", html)

    def test_a_signing_timeout_is_tried_once_more(self):
        _, _, links = self.signing()
        err, html, calls = self.signing(ACTIONS_STUB_FIRST_SIGN_HANG="3", OBSIDIAN_TASKS_SIGN_TIMEOUT="1")
        self.assertNotIn("action links unsigned", err)
        self.assertIn("https://signer.example.test/task?stub=1", html)
        self.assertEqual(calls, links + 1)

    def test_a_signer_that_never_answers_says_why_with_the_load(self):
        err, html, calls = self.signing(ACTIONS_STUB_SIGN_HANG="3", OBSIDIAN_TASKS_SIGN_TIMEOUT="1")
        self.assertEqual(err.count("action links unsigned"), 1, err)
        self.assertRegex(err, r"action links unsigned -- \S+task-actions\.sh did not sign "
                              r"within 1s, 2 times \(load averages [0-9.]+ [0-9.]+ [0-9.]+\)")
        self.assertNotIn("/task?stub=1", html)
        self.assertEqual(calls, 2)

    def test_a_signer_that_exits_non_zero_says_why(self):
        # Before sd:2536 a failed `url` gave an empty link and said nothing.
        err, html, calls = self.signing(ACTIONS_STUB_SIGN_EXIT="1")
        self.assertEqual(err.count("action links unsigned"), 1, err)
        self.assertRegex(err, r"task-actions\.sh url exited 1 \(load averages ")
        self.assertNotIn("/task?stub=1", html)
        self.assertEqual(calls, 1)

    def test_a_base_url_failure_says_why(self):
        err, _, calls = self.signing(ACTIONS_STUB_SIGNS="")
        self.assertRegex(err, r"no base URL could be discovered \(base-url exited 1, load averages ")
        self.assertEqual(calls, 0)

    # --- what leaves the machine -----------------------------------------

    def test_only_titles_and_dates_leave_the_machine(self):
        record = self.root / "jev.request.json"
        self.digest({**ON, "JEV_STUB_RECORD": str(record)})
        sent = json.loads(record.read_text())
        payload = json.dumps(sent)
        self.assertNotIn("A body nobody outside this machine", payload)
        self.assertNotIn("TaskNotes/Tasks", payload)
        self.assertNotIn(str(self.root), payload)
        self.assertNotIn("obsidian://", payload)
        for item in sent["state"]["items"]:
            self.assertEqual(sorted(item),
                             ["days_overdue", "id", "scheduled", "title"])
        self.assertEqual(len(sent["questions"]), len(harness.EXPECTED_TITLES))

    def test_every_task_is_one_question_in_one_request(self):
        record = self.root / "jev.request.json"
        self.digest({**ON, "JEV_STUB_RECORD": str(record)})
        sent = json.loads(record.read_text())
        self.assertEqual(len(sent["state"]["items"]), len(sent["questions"]))


if __name__ == "__main__":
    unittest.main()
