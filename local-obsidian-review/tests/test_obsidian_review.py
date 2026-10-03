"""The digest must be the same digest with Jev as without it.

Jev is experimental, so the thing worth testing is not that it ranks well --
nothing here can know that -- but that the tool is unchanged when it is off,
unavailable or broken, and that turning it on moves cards around without
adding, dropping or re-wiring one. The twelve-card cap is the sharp edge:
Blog Ideas holds fourteen notes in the fixture, and the same twelve must
reach the mail either way. The stub in `harness.py` stands in for `jev.sh`,
so no test here touches the network.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import harness  # noqa: E402

GOLDEN_LIST = (harness.FIXTURES / "golden_list.txt").read_text()
GOLDEN_HTML = (harness.FIXTURES / "golden_body.html").read_text().rstrip("\n")

#: Unset is on, and unset is what every deployed run has, so the fixtures
#: run the stage that way (sd:1183). `1` is checked once, explicitly, below.
ON: dict = {}
#: `JEV_OBSIDIAN_REVIEW` switches the stage off and nothing switches it on:
#: unset is on. Yesterday's digest is therefore the run that sets it to 0.
OFF = {"JEV_OBSIDIAN_REVIEW": "0"}


def shown(text: str):
    """{queue: [note stems shown]}, in the order the digest shows them."""
    out, current = {}, None
    for line in text.splitlines():
        m = re.match(r"^(.+): \d+ awaiting a decision$", line)
        if m:
            current = m.group(1)
            out[current] = []
        elif line.startswith("- ") and current:
            out[current].append(line[2:].split(" (score")[0])
    return out


def sections(text: str):
    return [m.group(1) for m in
            re.finditer(r"^(.+): \d+ awaiting a decision$", text, re.M)]


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

        The ages in it ("(14d ago)") are not templated, so they hold only
        while the script counts from the same day the fixture was written
        on. Neither date here is the real one: one crosses a leap day and
        one a year end. A script that read the clock instead of
        OBSIDIAN_REVIEW_TODAY fails both, which is the midnight race that
        broke runs 35935415274 and 35935854678.
        """
        for day in (datetime.date(2028, 3, 1), datetime.date(2027, 1, 5)):
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
        self.assertEqual(self.digest(), self.digest({"JEV_OBSIDIAN_REVIEW": "1"}))
        self.assertNotEqual(self.digest(), self.digest(OFF))

    def test_jev_disabled_is_byte_for_byte_yesterdays_digest(self):
        out, html = self.digest({**ON, "JEV_STUB_ENABLED_EXIT": "3"})
        self.assertEqual(out, GOLDEN_LIST)
        self.assertEqual(html, GOLDEN_HTML)

    def test_a_failing_jev_leaves_the_digest_intact(self):
        out, html = self.digest({**ON, "JEV_STUB_ASK_FAIL": "1"})
        self.assertEqual(out, GOLDEN_LIST)
        self.assertEqual(html, GOLDEN_HTML)

    def test_a_failing_jev_says_so_on_stderr(self):
        env = dict(os.environ)
        env.update({
            "OBSIDIAN_VAULT": str(self.root / "vault"),
            "NOTIFY_RECORD": str(self.root / "notify.args"),
            "JEV_STUB_ASK_FAIL": "1",
        })
        env.pop("JEV_OBSIDIAN_REVIEW", None)
        proc = subprocess.run(
            ["sh", str(self.root / "local-obsidian-review" / "obsidian-review.sh"),
             "run"],
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("jev did not rank this digest", proc.stderr)
        self.assertIn("keeping the usual order", proc.stderr)

    # --- an unreachable signer is a bounded failure, not a crash ---------

    def test_a_hanging_signer_does_not_kill_the_digest(self):
        """The signer that never answers costs one timeout, not the run.

        `task-actions.sh url` signs the URL on this machine; it never calls the
        service on port 8766, so a listening service does not make this call
        safe. It shells out to `tailscale funnel status`, and a stalled daemon
        makes it unbounded. The digest used to die on `subprocess.TimeoutExpired`
        -- which `except OSError` never caught -- after blocking. It must now
        mail the same Open-only digest it mails when the signer is absent.
        """
        out, html = self.digest({**OFF, "ACTIONS_STUB_HANG": "30"})
        self.assertEqual(out, GOLDEN_LIST)
        self.assertEqual(html, GOLDEN_HTML)

    def test_a_hanging_signer_names_the_endpoint_it_could_not_reach(self):
        env = dict(os.environ)
        env.update({
            "OBSIDIAN_VAULT": str(self.root / "vault"),
            "NOTIFY_RECORD": str(self.root / "notify.args"),
            "ACTIONS_STUB_HANG": "30",
        })
        proc = subprocess.run(
            ["sh", str(self.root / "local-obsidian-review" / "obsidian-review.sh"),
             "run"],
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("TimeoutExpired", proc.stderr)
        self.assertIn("action links unsigned", proc.stderr)
        self.assertIn("task-actions.sh", proc.stderr)
        self.assertIn("did not answer within", proc.stderr)
        # One timeout retires the signer: the message is said once, not once
        # per card, and the run does not pay the timeout sixty times over.
        self.assertEqual(proc.stderr.count("action links unsigned"), 1)

    def test_the_signer_is_asked_for_a_base_url_once_and_never_again(self):
        """sd:1203, review round 1. One probe a run, not one a button.

        Bounding `url` was not enough. `task-actions.sh` answered every call
        with exit 0 and a `http://127.0.0.1:8766` base whenever its funnel
        probe timed out, so nothing outside could see the stall: every button
        on every card paid the timeout again, and every link it signed pointed
        at a host only this machine can reach.

        The base is now discovered once, by `base-url`, and handed to each
        `url` call in TASK_ACTIONS_BASE_URL. This reads the stub's own log of
        what it was called with, so it fails if the digest ever goes back to
        asking per link.
        """
        record = self.root / "actions.calls"
        self.digest({**OFF,
                     "ACTIONS_STUB_RECORD": str(record),
                     "ACTIONS_STUB_SIGNS": "https://mac.example.ts.net"})
        calls = [line.split(" ", 1) for line in
                 record.read_text().splitlines() if line]
        verbs = [verb for verb, _ in calls]
        self.assertEqual(verbs.count("base-url"), 1, calls)
        self.assertGreater(verbs.count("url"), 1, calls)
        # Every signing call got the answer the one probe produced, so none of
        # them had any reason to reach the daemon.
        self.assertEqual(
            {base for verb, base in calls if verb == "url"},
            {"https://mac.example.ts.net"})

    def test_one_signing_timeout_retires_the_signer_for_the_run(self):
        """sd:1203, review round 2. Caching discovery is not the whole breaker.

        The base URL being in hand stops the daemon being asked again; it does
        not stop signing itself being slow. Each `url` call still starts an
        interpreter and reads the secret file, so a loaded machine or a secret
        on a stalled mount is its own stall, and 192 buttons times the signing
        timeout is sixteen minutes. The first signing timeout ends it.
        """
        record = self.root / "actions.calls"
        self.digest({**OFF,
                     "ACTIONS_STUB_RECORD": str(record),
                     "ACTIONS_STUB_SIGNS": "https://mac.example.ts.net",
                     "ACTIONS_STUB_SIGN_HANG": "3",
                     "OBSIDIAN_REVIEW_SIGN_TIMEOUT": "1"})
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
            "ACTIONS_STUB_SIGNS": "https://mac.example.ts.net", **extra})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verbs = [line.split(" ", 1)[0] for line in record.read_text().splitlines() if line]
        return proc.stderr, html, verbs.count("url")

    def test_a_signer_slower_than_five_seconds_still_signs(self):
        # 0.1 s by day, over 5 s under the nightly load: the old bound gave up.
        err, html, _ = self.signing(ACTIONS_STUB_FIRST_SIGN_HANG="6")
        self.assertNotIn("action links unsigned", err)
        self.assertIn("https://mac.example.ts.net/task?stub=1", html)

    def test_a_signing_timeout_is_tried_once_more(self):
        _, _, links = self.signing()
        err, html, calls = self.signing(ACTIONS_STUB_FIRST_SIGN_HANG="3", OBSIDIAN_REVIEW_SIGN_TIMEOUT="1")
        self.assertNotIn("action links unsigned", err)
        self.assertIn("https://mac.example.ts.net/task?stub=1", html)
        self.assertEqual(calls, links + 1)

    def test_a_signer_that_never_answers_says_why_with_the_load(self):
        err, html, calls = self.signing(ACTIONS_STUB_SIGN_HANG="3", OBSIDIAN_REVIEW_SIGN_TIMEOUT="1")
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

    def test_a_stalled_daemon_refuses_rather_than_signing_a_localhost_link(self):
        """The real script, not the stub: `url` exits non-zero on a stall.

        A link signed against 127.0.0.1 opens on this machine and nowhere the
        digest is read, so returning one after a timeout is worse than
        returning nothing. `tailscale` here is a stub that never answers.
        """
        bin_dir = self.root / "stalled-bin"
        bin_dir.mkdir()
        stub = bin_dir / "tailscale"
        stub.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        stub.chmod(0o755)
        env = dict(os.environ)
        env.update({
            "PATH": f"{bin_dir}:{env['PATH']}",
            "TASK_ACTIONS_FUNNEL_TIMEOUT": "1",
            "TASK_ACTIONS_SECRET_FILE": str(self.root / "secret"),
        })
        env.pop("TASK_ACTIONS_BASE_URL", None)
        script = harness.TOOL_DIR.parent / "local-task-actions" / "task-actions.sh"
        proc = subprocess.run(["sh", str(script), "url", "-b", "blog",
                               "some-stem", "accept"],
                              capture_output=True, text=True, env=env,
                              timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("127.0.0.1", proc.stdout)
        self.assertIn("did not answer", proc.stderr)

    def test_a_machine_with_no_funnel_still_signs_a_localhost_link(self):
        """Not-configured is not a stall, and must not be treated as one.

        `tailscale` answers here, with no line naming our port. That is the
        ordinary state of a machine without a funnel, and a localhost base is
        the right answer for it.
        """
        bin_dir = self.root / "quiet-bin"
        bin_dir.mkdir()
        stub = bin_dir / "tailscale"
        stub.write_text("#!/bin/sh\necho 'no serve config'\n", encoding="utf-8")
        stub.chmod(0o755)
        env = dict(os.environ)
        env.update({
            "PATH": f"{bin_dir}:{env['PATH']}",
            "TASK_ACTIONS_FUNNEL_TIMEOUT": "5",
            "TASK_ACTIONS_SECRET_FILE": str(self.root / "secret"),
        })
        env.pop("TASK_ACTIONS_BASE_URL", None)
        script = harness.TOOL_DIR.parent / "local-task-actions" / "task-actions.sh"
        proc = subprocess.run(["sh", str(script), "base-url"],
                              capture_output=True, text=True, env=env,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "http://127.0.0.1:8766")

    # --- and the one way it does something -------------------------------

    def test_jev_reorders_the_cards_and_the_sections(self):
        out, html = self.digest(ON)
        self.assertEqual(sections(out),
                         list(reversed(harness.EXPECTED_SECTIONS)))
        for queue, notes in harness.EXPECTED_SHOWN.items():
            self.assertEqual(shown(out)[queue], list(reversed(notes)))
        self.assertNotEqual(out, GOLDEN_LIST)
        self.assertIn("Ordered by Jev", html)
        self.assertIn("(ordered by Jev, most worth deciding first)", out)

    def test_the_item_set_never_changes(self):
        for env in (None, {**ON, "JEV_STUB_ENABLED_EXIT": "3"},
                    {**ON, "JEV_STUB_ASK_FAIL": "1"}, ON):
            out, html = self.digest(env)
            self.assertEqual(out.splitlines()[0], "20 awaiting decisions")
            picked = shown(out)
            self.assertEqual(sorted(picked), sorted(harness.EXPECTED_SHOWN))
            for queue, notes in harness.EXPECTED_SHOWN.items():
                self.assertEqual(sorted(picked[queue]), sorted(notes))
            # the two notes over the twelve-card cap stay over it
            self.assertNotIn("blog-01", out)
            self.assertNotIn("blog-02", out)
            self.assertIn("… and 2 more in Obsidian", html)
            # and nothing whose status is not a pending decision appears
            for hidden in ("blog-accepted", "skill-done", "tip-ready-a"):
                self.assertNotIn(hidden, out)
            self.assertIn("2 tip(s) are `ready`", html)

    def test_the_buttons_and_their_links_are_untouched(self):
        plain = self.digest()[1]
        ranked = self.digest(ON)[1]
        hrefs = lambda doc: sorted(re.findall(r'href="([^"]+)"', doc))
        self.assertEqual(hrefs(plain), hrefs(ranked))
        self.assertEqual(sorted(re.findall(r">([^<>]+)</a>", plain)),
                         sorted(re.findall(r">([^<>]+)</a>", ranked)))

    # --- what leaves the machine -----------------------------------------

    def test_only_titles_queues_and_dates_leave_the_machine(self):
        record = self.root / "jev.request.json"
        self.digest({**ON, "JEV_STUB_RECORD": str(record)})
        sent = json.loads(record.read_text())
        payload = json.dumps(sent)
        self.assertNotIn("A body nobody outside this machine", payload)
        self.assertNotIn("what blog-14 is about", payload)   # description
        self.assertNotIn("System/Databases", payload)
        self.assertNotIn(str(self.root), payload)
        self.assertNotIn("obsidian://", payload)
        for item in sent["state"]["items"]:
            self.assertEqual(sorted(item), ["added", "id", "queue", "title"])

    def test_every_shown_note_is_one_question_in_one_request(self):
        record = self.root / "jev.request.json"
        self.digest({**ON, "JEV_STUB_RECORD": str(record)})
        sent = json.loads(record.read_text())
        expected = sum(len(v) for v in harness.EXPECTED_SHOWN.values())
        self.assertEqual(len(sent["questions"]), expected)
        self.assertEqual(len(sent["state"]["items"]), expected)


if __name__ == "__main__":
    unittest.main()
