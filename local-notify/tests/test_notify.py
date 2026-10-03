"""Tests for notify.sh, and for the optional Jev routing added on top of it.

Nothing here reaches the network and nothing here sends a notification. Every
fixture builds a throwaway two-folder tree -- a copy of `notify.sh` beside a
stub `local-jev/jev.sh` -- and puts stub `osascript` and `curl` on PATH, so a
"delivery" is a line in a log file. That is the only shape that can assert the
one thing worth asserting about this change: what was NOT sent, and to whom.

The contract under test is that the routing is additive and on. It runs
unless JEV_NOTIFY switches this stage off (`0`, `off`, `false`, `no`,
`disabled`) or the stub `jev enabled` declines, and every failure past that
point must still deliver the notification on the defaults.

The fixtures leave JEV_NOTIFY unset, as every deployed run does (sd:1183):
`1` is not an on-switch, only a word that is not an off-word.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = pathlib.Path(os.environ.get("NOTIFY_TEST_SCRIPT") or FOLDER / "notify.sh")

# The stub answers `enabled` from the environment and never calls anything. It
# records its argv and the state it was handed on stdin, which is how the
# privacy case can assert what left the machine.
JEV_STUB = r"""#!/bin/sh
{
  printf 'ARGV'
  for a in "$@"; do printf '\t%s' "$a"; done
  printf '\n'
} >> "$JEV_LOG"
verb="$1"
if [ "$verb" = enabled ]; then
  # `enabled STAGE` answers both halves, as the real one does: can Jev answer
  # on this machine (JEV_STUB_ENABLED stands in for key and switch), and has
  # the named stage variable been used to switch this stage off. Unset means
  # on, and only the FLAG_OFF words switch it off -- an unrecognised word
  # leaves the stage on, so a typo cannot silently stop a lane.
  # `--why` prints the reason on stdout, as the real one does.
  why=""
  case " $* " in *" --why "*) why=1 ;; esac
  if [ "${JEV_STUB_ENABLED:-1}" != 1 ]; then
    [ -z "$why" ] || echo "jev: no TYPESAFE_API_KEY on this machine"
    exit 3
  fi
  if [ -n "${2:-}" ]; then
    eval "word=\${$2:-}"
    case "$(printf '%s' "$word" | tr '[:upper:]' '[:lower:]')" in
      0|off|false|no|disabled)
        [ -z "$why" ] || echo "jev: $2 switched this stage off here"
        exit 3 ;;
    esac
  fi
  [ -z "$why" ] || echo "jev: enabled"
  exit 0
fi
cat >> "$JEV_STATE_LOG"
if [ -n "${JEV_STUB_SLEEP:-}" ]; then sleep "$JEV_STUB_SLEEP"; fi
case "$verb" in
  choice)
    if [ "${JEV_STUB_CHOICE_FAIL:-0}" = 1 ]; then exit 1; fi
    printf '%s\n' "${JEV_STUB_CHOICE:-phone}"
    ;;
  score)
    if [ "${JEV_STUB_SCORE_FAIL:-0}" = 1 ]; then exit 1; fi
    printf '%s\n' "${JEV_STUB_SCORE:-2.0}"
    ;;
esac
exit 0
"""

# Both delivery paths this repo can exercise offline. osascript carries the
# macOS banner and iMessage; curl carries the ntfy push.
OSASCRIPT_STUB = r"""#!/bin/sh
{
  printf 'ARGV'
  for a in "$@"; do printf '\t%s' "$a"; done
  printf '\n'
} >> "$OSASCRIPT_LOG"
exit 0
"""

CURL_STUB = r"""#!/bin/sh
{
  printf 'ARGV'
  for a in "$@"; do printf '\t%s' "$a"; done
  printf '\n'
} >> "$CURL_LOG"
exit 0
"""

# A temp file that cannot be created. BSD `mktemp` with no template ignores
# TMPDIR, so an unwritable directory does not simulate this -- shadowing the
# command does. Off unless MKTEMP_STUB_FAIL is set, so every other test runs
# against the real one.
MKTEMP_STUB = """#!/bin/sh
if [ "${MKTEMP_STUB_FAIL:-0}" = 1 ]; then
  echo "mktemp: stubbed failure" >&2
  exit 1
fi
exec %s "$@"
"""

TOPIC = "secret-topic-name"
TOKEN = "tk_secret_token"
IMESSAGE_TO = "+15551234567"
EMAIL_TO = "operator@example.test"

#: The one gate call notify.sh makes: `--record` counts the decline, `--why`
#: prints its reason for stderr.
GATE = "ARGV\tenabled\tJEV_NOTIFY\t--record\t--why\t--caller\tlocal-notify"


class Fixture:
    """A disposable copy of notify.sh with a stub jev next door."""

    def __init__(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="notify-test-"))
        (self.root / "local-notify").mkdir()
        (self.root / "local-jev").mkdir()
        self.script = self.root / "local-notify" / "notify.sh"
        shutil.copy(SCRIPT, self.script)
        (self.root / "lib").mkdir()
        shutil.copy(FOLDER.parent / "lib" / "config.sh", self.root / "lib" / "config.sh")
        self.config = self.root / "config"
        (self.config / "notify").mkdir(parents=True)
        self.script.chmod(0o755)
        self.jev = self.root / "local-jev" / "jev.sh"
        self._write(self.jev, JEV_STUB)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        self._write(bin_dir / "osascript", OSASCRIPT_STUB)
        self._write(bin_dir / "curl", CURL_STUB)
        self._write(bin_dir / "mktemp", MKTEMP_STUB % shutil.which("mktemp"))
        self.bin_dir = bin_dir
        self.jev_log = self.root / "jev.log"
        self.jev_state_log = self.root / "jev-state.log"
        self.osascript_log = self.root / "osascript.log"
        self.curl_log = self.root / "curl.log"

    @staticmethod
    def _write(path, body):
        path.write_text(body)
        path.chmod(0o755)

    def run(self, args, env=None):
        environ = {
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.root),
            "SYSTEM_TOOLS_CONFIG": str(self.config),
            "JEV_LOG": str(self.jev_log),
            "JEV_STATE_LOG": str(self.jev_state_log),
            "OSASCRIPT_LOG": str(self.osascript_log),
            "CURL_LOG": str(self.curl_log),
            "NTFY_TOPIC": TOPIC,
            "NTFY_TOKEN": TOKEN,
            "IMESSAGE_TO": IMESSAGE_TO,
            "NOTIFY_EMAIL_TO": EMAIL_TO,
        }
        environ.update(env or {})
        return subprocess.run(
            ["sh", str(self.script)] + args,
            env=environ, capture_output=True, text=True, timeout=60,
        )

    def read(self, path):
        return path.read_text() if path.exists() else ""

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class NotifyTestCase(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)

    # --- helpers ---------------------------------------------------------
    def jev_calls(self):
        return [ln for ln in self.fx.read(self.fx.jev_log).splitlines() if ln]

    def verbs(self):
        return [ln.split("\t")[1] for ln in self.jev_calls()]

    def ntfy_sent(self):
        return TOPIC in self.fx.read(self.fx.curl_log)

    def banner_sent(self):
        log = self.fx.read(self.fx.osascript_log)
        return "display notification" in log

    def imessage_sent(self):
        return "Messages" in self.fx.read(self.fx.osascript_log)

    def baseline_rows(self):
        """Every `jev record` this run made, as its argv fields."""
        return [ln.split("\t")[1:] for ln in self.jev_calls()
                if ln.split("\t")[1:2] == ["record"]]

    def priority_header(self):
        for line in self.fx.read(self.fx.curl_log).splitlines():
            for field in line.split("\t"):
                if field.startswith("Priority: "):
                    return field[len("Priority: "):]
        return None


class DefaultBehaviourTest(NotifyTestCase):
    """On by default: unset reaches Jev, and only JEV_NOTIFY=0 stops it."""

    def test_an_unset_switch_asks_jev(self):
        r = self.fx.run(["-t", "Build", "build finished"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.banner_sent())
        # The stub says `phone`, which is today's default channel set, so the
        # delivery is unchanged; what this asserts is that the stage ran.
        self.assertTrue(self.ntfy_sent())
        self.assertIn(GATE, self.jev_calls(),
                      "an unset JEV_NOTIFY did not reach jev")

    def test_the_switch_off_asks_nothing(self):
        """`0` is the kill switch for this stage, and it must call nothing.

        A control, not a proof of the flip: this passed before it too, when
        `0` was simply "not the opt-in". It guards the switch from here on.
        """
        r = self.fx.run(["-t", "Build", "build finished"],
                        env={"JEV_NOTIFY": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.banner_sent())
        self.assertTrue(self.ntfy_sent())
        self.assertEqual(self.priority_header(), "default")
        # `enabled` may be asked -- it calls nothing. No question may follow.
        self.assertEqual([c for c in self.jev_calls()
                          if not c.startswith("ARGV\tenabled")], [],
                         "a switched-off stage still asked jev a question")

    def test_every_off_word_switches_the_stage_off(self):
        # Case does not matter, and the whole FLAG_OFF vocabulary is checked
        # here rather than `0` alone: a caller who wrote `off` and got a lane
        # that kept running is the failure this asserts against.
        for word in ("0", "off", "false", "no", "disabled", "OFF", "No"):
            with self.subTest(word=word):
                fx = Fixture()
                self.addCleanup(fx.cleanup)
                r = fx.run(["msg"], env={"JEV_NOTIFY": word})
                self.assertEqual(r.returncode, 0, r.stderr)
                asked = [ln for ln in fx.read(fx.jev_log).splitlines()
                         if ln and not ln.startswith("ARGV\tenabled")]
                self.assertEqual(asked, [], f"{word!r} left the stage on")

    def test_a_caller_switching_the_stage_off_beats_a_dotenv_turning_it_on(self):
        """The kill switch must win from the command line, like NTFY_TOPIC.

        `.env` is defaults-only for every other variable in this script, by an
        explicit save-and-restore pair. JEV_NOTIFY was not in that list, so a
        caller writing `JEV_NOTIFY=0 notify.sh ...` to keep one message off the
        wire lost to whatever the file said. That is the wrong way for a kill
        switch to fail.
        """
        (self.fx.config / "notify" / ".env").write_text("JEV_NOTIFY=1\n")
        r = self.fx.run(["msg"], env={"JEV_NOTIFY": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([ln for ln in self.jev_calls()
                          if not ln.startswith("ARGV\tenabled")], [],
                         "a .env value overrode the caller's kill switch")

    def test_a_missing_topic_names_the_config_file_to_copy_to(self):
        """The settings live in <config>/notify/.env, and the error says so."""
        r = self.fx.run(["-c", "ntfy", "msg"], env={"NTFY_TOPIC": "", "JEV_NOTIFY": "0"})
        want = self.fx.config / "notify" / ".env"
        self.assertIn(f"copy local-notify/.env.example to {want}", r.stderr)

    def test_a_dotenv_still_supplies_the_stage_when_the_caller_says_nothing(self):
        """Defaults-only means the file is still read when nothing overrides."""
        (self.fx.config / "notify" / ".env").write_text("JEV_NOTIFY=0\n")
        r = self.fx.run(["msg"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([ln for ln in self.jev_calls()
                          if not ln.startswith("ARGV\tenabled")], [],
                         "a .env kill switch was ignored")

    def test_a_word_that_is_not_an_off_word_leaves_the_stage_on(self):
        """A typo must not silently stop the lane, so only FLAG_OFF counts."""
        r = self.fx.run(["msg"], env={"JEV_NOTIFY": "yes"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(GATE, self.jev_calls(),
                      "'yes' was read as a switch-off")

    def test_the_gate_records_its_decline_under_this_tools_own_name(self):
        """`enabled` alone counts nothing, and the old path is the control arm.

        Every notification this stage declines is a run of the defaults, and
        without `--record` on the gate no row anywhere says so: the stage
        reads as one nobody ever used. `--caller` is on the same call because
        a row filed under `unknown` groups with every other caller's.
        """
        r = self.fx.run(["msg"], env={"JEV_NOTIFY": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        gates = [c for c in self.jev_calls() if c.split("\t")[1] == "enabled"]
        self.assertEqual(gates, [GATE], gates)

    def test_every_judgment_call_names_this_caller_and_its_stage(self):
        """One name across the switch, the ledger and the per-stage report.

        The stage is spelled `JEV_NOTIFY` here because that is the variable
        `jev enabled` above is handed; a second spelling would file the two
        arms of one decision under two stages and compare nothing.
        """
        r = self.fx.run(["-t", "Build", "build finished"])
        self.assertEqual(r.returncode, 0, r.stderr)
        asked = [c for c in self.jev_calls()
                 if c.split("\t")[1] in ("choice", "score")]
        self.assertEqual(len(asked), 2, asked)
        for call in asked:
            self.assertIn("--caller\tlocal-notify", call)
            self.assertIn("--stage\tJEV_NOTIFY", call)

    def test_a_missing_jev_folder_is_not_fatal(self):
        self.fx.jev.unlink()
        r = self.fx.run(["msg"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ntfy_sent())
        self.assertEqual(self.priority_header(), "default")


class DeclineReasonTest(NotifyTestCase):
    """Every decline says why on stderr, as the README promises (sd:1364):
    the switch off, no key, and `jev.sh` missing. The notification still
    goes out on the defaults."""

    def test_the_switch_off_says_so(self):
        r = self.fx.run(["msg"], env={"JEV_NOTIFY": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ntfy_sent())
        self.assertIn("notify.sh: jev not asked: JEV_NOTIFY switched this stage off here; "
                      "using the defaults", r.stderr)

    def test_no_key_says_so(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_ENABLED": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ntfy_sent())
        self.assertIn("notify.sh: jev not asked: no TYPESAFE_API_KEY on this machine; "
                      "using the defaults", r.stderr)

    def test_a_missing_jev_sh_says_so(self):
        self.fx.jev.unlink()
        r = self.fx.run(["msg"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ntfy_sent())
        self.assertIn("notify.sh: jev not asked: ", r.stderr)
        self.assertIn("/local-jev/jev.sh is missing; using the defaults", r.stderr)

    def test_a_stage_that_runs_prints_no_decline(self):
        r = self.fx.run(["msg"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("jev not asked", r.stderr)


class ExplicitFlagsWinTest(NotifyTestCase):
    """An explicit -c or -p is an instruction. Jev is not consulted at all."""

    def test_explicit_channels_bypass_jev(self):
        r = self.fx.run(["-c", "local", "msg"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.jev_calls(), [])
        self.assertTrue(self.banner_sent())
        self.assertFalse(self.ntfy_sent())

    def test_explicit_priority_bypasses_jev(self):
        r = self.fx.run(["-p", "urgent", "msg"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.jev_calls(), [])
        self.assertEqual(self.priority_header(), "urgent")

    def test_explicit_priority_survives_a_jev_that_wants_another(self):
        r = self.fx.run(["-p", "min", "msg"],
                        env={"JEV_STUB_SCORE": "4.0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.priority_header(), "min")


class SwitchOffTest(NotifyTestCase):
    """`jev enabled` exiting 3 is a machine that is off or was never keyed."""

    def test_jev_disabled_gives_todays_default(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_ENABLED": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.verbs(), ["enabled"], "asked a question after `enabled` said no")
        self.assertTrue(self.ntfy_sent())
        self.assertEqual(self.priority_header(), "default")


class RoutingTest(NotifyTestCase):
    """What the stage actually buys, once both halves are true.

        Both halves: Jev can answer here, and `JEV_NOTIFY` has not been
        used to switch this stage off. There is no opting in any more --
        unset is on -- so the second half is something not done rather
        than something done.
        """

    def test_jev_answering_desk_keeps_the_alert_off_the_phone(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_CHOICE": "desk"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("choice", self.verbs())
        self.assertTrue(self.banner_sent())
        self.assertFalse(self.ntfy_sent())

    def test_jev_answering_phone_keeps_both_channels(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_CHOICE": "phone"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.banner_sent())
        self.assertTrue(self.ntfy_sent())

    def test_a_score_picks_the_priority(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_SCORE": "3.0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.priority_header(), "high")

    def test_a_score_out_of_range_is_clamped_not_pasted_through(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_SCORE": "97"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.priority_header(), "urgent")

    def test_unsure_keeps_the_default_route(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_CHOICE": "unsure"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.banner_sent())
        self.assertTrue(self.ntfy_sent())


class ImessageIsNotJevsToGiveTest(NotifyTestCase):
    """Escalating to someone's phone messages is a decision a human makes."""

    def test_imessage_is_not_among_the_criteria_offered(self):
        self.fx.run(["msg"])
        self.assertNotIn("imessage", self.fx.read(self.fx.jev_log))

    def test_an_answer_naming_imessage_is_ignored(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_CHOICE": "imessage"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.imessage_sent())
        self.assertTrue(self.ntfy_sent(), "an unexpected answer must leave the default alone")

    def test_an_answer_naming_a_channel_list_is_ignored(self):
        r = self.fx.run(["msg"],
                        env={"JEV_STUB_CHOICE": "local,ntfy,imessage"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.imessage_sent())
        self.assertTrue(self.ntfy_sent())


class FailureDegradesTest(NotifyTestCase):
    """Every failure degrades to today's defaults. Not silent, not fatal."""

    def test_a_failing_jev_still_delivers_on_the_defaults(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_CHOICE_FAIL": "1",
                                      "JEV_STUB_SCORE_FAIL": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.banner_sent())
        self.assertTrue(self.ntfy_sent())
        self.assertEqual(self.priority_header(), "default")
        self.assertIn("jev", r.stderr, "a degraded call must say so on stderr")

    def test_a_slow_jev_does_not_hold_the_alert(self):
        r = self.fx.run(["msg"], env={"JEV_STUB_SLEEP": "6",
                                      "JEV_TIMEOUT": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ntfy_sent())
        self.assertEqual(self.priority_header(), "default")
        self.assertIn("did not answer", r.stderr)

    def test_a_jev_killed_at_the_deadline_has_its_timeout_recorded_here(self):
        """The deadline is the one decline only this script can write down.

        The branch above sends SIGTERM. `jev` writes its measurement after
        the answer is printed and installs no signal handler, so the killed
        process leaves no row: driven against a scratch ledger, two timed-out
        notifications left `declines {}` and two `baseline/ok` rows carrying
        no cause at all, and the deadline was invisible in every report.

        So this row carries the cause, and it is the only row that can. The
        branch below, where `jev` answered unusably, must not -- `jev` wrote
        the cause there itself, and a second copy counts one decision twice.
        """
        self.fx.run(["msg"], env={"JEV_STUB_SLEEP": "6",
                                  "JEV_TIMEOUT": "1"})
        rows = self.baseline_rows()
        self.assertTrue(rows, "the timeout wrote no control-arm row at all")
        for row in rows:
            self.assertEqual(
                row,
                ["record", "--caller", "local-notify", "--stage",
                 "JEV_NOTIFY", "--arm", "baseline", "--outcome", "ok",
                 "--decline", "timeout"],
                "a killed jev recorded no cause, so nothing anywhere counts "
                "this timeout as a decline")

    def test_a_jev_that_answered_badly_leaves_the_cause_to_jev(self):
        """The other half of the same rule, so neither half can drift alone.

        Here `jev` ran to the end and exited non-zero, which means it reached
        its flush and wrote its own row with the cause on it. `judgment.py`'s
        DECLINES groups by stage and cause across both arms with no
        deduplication, so a cause repeated here reads as two declines for one
        decision.
        """
        self.fx.run(["msg"], env={"JEV_STUB_CHOICE_FAIL": "1",
                                  "JEV_STUB_SCORE_FAIL": "1"})
        rows = self.baseline_rows()
        self.assertTrue(rows, "a failed answer wrote no control-arm row")
        for row in rows:
            self.assertNotIn(
                "--decline", row,
                "jev answered and already recorded why; this row repeats it, "
                "so one decision counts as two declines")


class PrivacyTest(NotifyTestCase):
    """Every Jev call leaves the machine, and this tool carries other tools'
    alert text. Only the title and the message are allowed out."""

    def test_only_the_title_and_the_message_are_sent(self):
        self.fx.run(["-t", "Disk", "the volume is full"])
        state = self.fx.read(self.fx.jev_state_log)
        self.assertIn("Title: Disk", state)
        self.assertIn("Message: the volume is full", state)
        for line in state.splitlines():
            self.assertTrue(line.startswith(("Title: ", "Message: ")), line)

    def test_no_credential_reaches_jev(self):
        self.fx.run(["-t", "Disk", "the volume is full"])
        sent = self.fx.read(self.fx.jev_state_log) + self.fx.read(self.fx.jev_log)
        for secret in (TOPIC, TOKEN, IMESSAGE_TO, EMAIL_TO):
            self.assertNotIn(secret, sent, f"{secret!r} left the machine")


class RedactionTest(NotifyTestCase):
    """The payload is redacted before it leaves; the delivery is not.

    notify.sh carries the alert text of every other tool in this repository,
    including tools nobody has written yet, so what turns up in a message is
    not enumerable in advance. A `-c`/`-p` caller skips the call entirely and
    these tests do not cover that path -- this is the second line.
    """

    # A credential shape from local-scan-for-secrets' PATTERNS, not a real one.
    LEAKED_TOKEN = "ghp_" + "A" * 36
    LEAKED_PATH = "/Users/somebody/.config/shell/env.sh"

    def _run_with(self, message):
        return self.fx.run(["-t", "Scan", message])

    def test_a_credential_in_the_message_does_not_reach_jev(self):
        r = self._run_with(f"found {self.LEAKED_TOKEN} in a file")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = self.fx.read(self.fx.jev_state_log)
        self.assertIn("Message: ", state, "the stage did not run at all")
        self.assertNotIn(self.LEAKED_TOKEN, state, "a credential left the machine")
        self.assertIn("<redacted>", state)

    def test_a_home_path_in_the_message_does_not_reach_jev(self):
        r = self._run_with(f"could not read {self.LEAKED_PATH}")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = self.fx.read(self.fx.jev_state_log)
        self.assertIn("Message: ", state, "the stage did not run at all")
        self.assertNotIn("/Users/", state)
        self.assertIn("<path>", state)

    def _assert_spaced_path_does_not_reach_jev(self, folder):
        """A folder name can hold a space; the words after it are the path too.

        Stopping the redaction at the first space sent the tail (sd:1608).
        """
        r = self._run_with(f"backup failed: {folder}/Secret Project/budget.xlsx")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = self.fx.read(self.fx.jev_state_log)
        self.assertIn("Message: backup failed: <path>", state,
                      "the stage did not run, or the text before the path was lost")
        self.assertNotIn("Project", state, "the tail of a spaced path left the machine")
        self.assertNotIn("budget.xlsx", state, "the tail of a spaced path left the machine")

    def test_a_users_path_with_a_space_does_not_reach_jev(self):
        self._assert_spaced_path_does_not_reach_jev("/Users/alice")

    def test_a_private_path_with_a_space_does_not_reach_jev(self):
        self._assert_spaced_path_does_not_reach_jev("/private/var/alice")

    def test_a_home_path_with_a_space_does_not_reach_jev(self):
        self._assert_spaced_path_does_not_reach_jev(str(self.fx.root))

    def _assert_address_does_not_reach_jev(self, address):
        r = self._run_with(f"peer {address} did not answer")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = self.fx.read(self.fx.jev_state_log)
        self.assertIn("Message: peer <ip> did not answer", state,
                      "an IPv6 address left the machine, or the stage did not run")

    def test_a_compressed_ipv6_address_does_not_reach_jev(self):
        self._assert_address_does_not_reach_jev("fd7a:115c:a1e0::1a2b:3c4d")

    def test_a_full_ipv6_address_does_not_reach_jev(self):
        self._assert_address_does_not_reach_jev("2001:db8:85a3:0:0:8a2e:370:7334")

    def test_a_full_ipv4_embedded_ipv6_address_does_not_reach_jev(self):
        """The IPv4 rule runs first; without its own rule the six hex groups leaked."""
        self._assert_address_does_not_reach_jev("2001:db8:85a3:0:0:ffff:192.0.2.1")

    def test_a_compressed_ipv4_embedded_ipv6_address_does_not_reach_jev(self):
        self._assert_address_does_not_reach_jev("64:ff9b::192.0.2.1")

    def test_a_clock_time_is_not_taken_for_an_ipv6_address(self):
        """The IPv6 rules need a `::` or eight groups; a clock time has neither."""
        r = self._run_with("backup ran at 12:34:56 and failed")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = self.fx.read(self.fx.jev_state_log)
        self.assertIn("Message: backup ran at 12:34:56 and failed", state)

    def test_the_delivery_still_carries_the_raw_text(self):
        """The control. Redaction is for the payload, not for the person.

        Without this, a redaction that simply emptied the message would pass
        every assertion above.
        """
        r = self._run_with(f"found {self.LEAKED_TOKEN} in a file")
        self.assertEqual(r.returncode, 0, r.stderr)
        delivered = self.fx.read(self.fx.curl_log) + self.fx.read(self.fx.osascript_log)
        self.assertIn(self.LEAKED_TOKEN, delivered,
                      "the redaction reached the delivery, not just the payload")

    def test_a_redaction_that_cannot_be_built_asks_nothing(self):
        """Fail closed. Not asking costs a routing; asking raw costs a secret."""
        r = self.fx.run(["-t", "Scan", f"found {self.LEAKED_TOKEN} in a file"],
                        env={"MKTEMP_STUB_FAIL": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.banner_sent(), "the notification was not delivered")
        self.assertEqual(self.fx.read(self.fx.jev_state_log), "",
                         "a question was asked with no redaction in place")
        self.assertIn("could not build the redaction", r.stderr)


class HelpTest(NotifyTestCase):
    """The switch is documented where a caller looks for it."""

    def test_help_names_the_switch_and_exits_zero(self):
        r = self.fx.run(["--help"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("JEV_NOTIFY", r.stdout)


if __name__ == "__main__":
    unittest.main()
