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
import math
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

#: Unset is on, and unset is what every deployed run has, so the fixtures
#: run the stage that way (sd:1183). `1` is checked once, explicitly, below.
ON: dict = {}
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
        self.assertEqual(self.digest(), self.digest({"JEV_OBSIDIAN_TASKS": "1"}))
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
            "JEV_STUB_ASK_FAIL": "1",
        })
        env.pop("JEV_OBSIDIAN_TASKS", None)
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

    def test_the_ask_names_the_digest_by_hash_and_the_run_is_one_id(self):
        """`--subject` hashes the stems, never a title; both calls share `JEV_RUN` (sd:2953)."""
        calls = self.root / "jev.calls"
        self.digest({**ON, "JEV_STUB_CALLS": str(calls)})
        lines = calls.read_text().splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["enabled", "ask"])
        found = re.search(r"--subject (\S+) run=", lines[1])
        self.assertIsNotNone(found, lines[1])
        self.assertRegex(found.group(1), r"^obsidian-tasks:[0-9a-f]{16}$")
        runs = {line.rsplit("run=", 1)[1] for line in lines}
        self.assertEqual(len(runs), 1, runs)
        self.assertRegex(runs.pop(), r"^obsidian-tasks-\d{8}T\d{6}-[0-9a-f]{4}$")

    def test_every_task_is_one_question_in_one_request(self):
        record = self.root / "jev.request.json"
        self.digest({**ON, "JEV_STUB_RECORD": str(record)})
        sent = json.loads(record.read_text())
        self.assertEqual(len(sent["state"]["items"]), len(sent["questions"]))


# --- the mail wears the foundation's light palette (sd:3590) -------------

TOKENS = (pathlib.Path(__file__).resolve().parents[2] / "local-project-dashboard"
          / "sd_dashboard" / "v2" / "static" / "tokens.css")


def light_hex(name: str) -> str:
    """The light side of one foundation colour token, as sRGB hex."""
    m = re.search(rf"--color-{name}:\s*light-dark\(oklch\(([\d.]+)%\s+([\d.]+)\s+([\d.]+)\)",
                  TOKENS.read_text())
    lightness, chroma, hue = float(m[1]) / 100, float(m[2]), math.radians(float(m[3]))
    a, b = chroma * math.cos(hue), chroma * math.sin(hue)
    l, m_, s = ((lightness + x) ** 3 for x in (0.3963377774 * a + 0.2158037573 * b,
                                               -0.1055613458 * a - 0.0638541728 * b,
                                               -0.0894841775 * a - 1.2914855480 * b))
    lin = (4.0767416621 * l - 3.3077115913 * m_ + 0.2309699292 * s,
           -1.2684380046 * l + 2.6097574011 * m_ - 0.3413193965 * s,
           -0.0041960863 * l - 0.7034186147 * m_ + 1.7076147010 * s)
    gamma = [12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055 for v in lin]
    return "#" + "".join(f"{round(min(1, max(0, v)) * 255):02x}" for v in gamma)


#: Every colour a mail may carry: foundation light tokens, by name.
MAIL_TOKENS = ("paper", "panel", "rule", "ink", "ink-2", "muted", "ok", "caution", "warning")


class MailPaletteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = harness.build_root(self.tmp.name)
        # A signer that answers, so the Mark done, Accept and Decline buttons are in the mail.
        state = pathlib.Path(self.tmp.name) / "stub-state"
        state.mkdir()
        proc, self.html = harness.run_proc(self.root, {
            "ACTIONS_STUB_STATE": str(state), "ACTIONS_STUB_SIGNS": "https://signer.example.test"})
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_every_colour_is_a_foundation_light_value_in_srgb_hex(self):
        allowed = {light_hex(name) for name in MAIL_TOKENS}
        found = re.findall(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|oklch)\(", self.html)
        self.assertTrue(found)
        self.assertEqual(set(found) - allowed, set(), "a colour the foundation does not name")
        self.assertNotRegex(self.html, r"\b(?:purple|violet|indigo)\b")

    def test_every_style_attribute_is_well_formed(self):
        # A double quote inside style="..." ends the attribute: the font stack
        # once did, and the card lost its font and its ground.
        for style in re.findall(r'style="([^"]*)"', self.html):
            self.assertNotIn("<", style)
            self.assertRegex(style.rstrip(";"), r"^(?:[a-z-]+:[^;]+;)*[a-z-]+:[^;]+$", style)
        self.assertIn("background:" + light_hex("paper"), self.html.split(">", 1)[0])

    def test_there_is_no_purple_button(self):
        for colour in re.findall(r"background:(#[0-9a-f]{6})", self.html):
            red, green, blue = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
            self.assertFalse(blue > red > green, f"{colour} reads purple")

    def test_a_coloured_button_carries_a_glyph_beside_its_label(self):
        buttons = re.findall(r"<a [^>]*background:#[0-9a-f]{6}[^>]*>([^<]+)</a>", self.html)
        self.assertTrue(buttons)
        for label in buttons:
            self.assertRegex(label, r"^[^\w\s] ", f"{label!r} has no glyph")

    def test_a_coloured_state_carries_its_glyph(self):
        warning, caution = light_hex("warning"), light_hex("caution")
        states = re.findall(rf'<span style="color:({warning}|{caution});[^"]*">([^<]+)</span>', self.html)
        self.assertTrue(states)
        for _, text in states:
            self.assertRegex(text, r"^[\u25a0\u25b2] ", f"{text!r} has no glyph")

    def test_text_is_readable_on_its_ground(self):
        def luminance(colour):
            channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

        def ratio(a, b):
            hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
            return (hi + 0.05) / (lo + 0.05)

        panel, paper = light_hex("panel"), light_hex("paper")
        for button in re.findall(r"color:" + panel + r";background:(#[0-9a-f]{6})", self.html):
            self.assertGreaterEqual(ratio(panel, button), 4.5, button)
        for name in ("muted", "warning", "caution", "ink-2"):
            self.assertGreaterEqual(ratio(light_hex(name), panel), 4.5, name)
        self.assertGreaterEqual(ratio(light_hex("muted"), paper), 4.5)


if __name__ == "__main__":
    unittest.main()
