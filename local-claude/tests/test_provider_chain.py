"""The provider chain of `claude.sh mem-pro-watchdog`.

`CLAUDE_MEM_PROVIDER_CHAIN` in settings.json orders the providers the
watchdog may move claude-mem's observer between. Each case runs the real
script with `--apply` against a scratch `CLAUDE_MEM_DATA_DIR`, and points the
cmem gateway and the Gemini endpoint at a stub server on 127.0.0.1. Nothing
here reads ~/.claude-mem or reaches the network. Notifications are switched
off, and every one the watchdog would post still prints a `notify:` line.
"""

import http.server
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "claude.sh"
TOKEN = "cm_pro_" + "x" * 24
GEMINI_KEY = "AIza-test-" + "g" * 20


class Stub(http.server.BaseHTTPRequestHandler):
    """Answers the gateway and Gemini probes with the status a case sets."""

    answers = {}
    calls = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        kind = "gateway" if self.path.startswith("/gateway/") else "gemini"
        Stub.calls.append({"kind": kind, "path": self.path,
                           "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        # A gateway answer keyed "gateway:<model>" wins for that model alone.
        model = json.loads(body).get("model") if kind == "gateway" else None
        status, payload = Stub.answers.get("%s:%s" % (kind, model),
                                           Stub.answers.get(kind, (599, {"error": "unset"})))
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


OK = (200, {"ok": True})
PRO_EXHAUSTED = (402, {"error": {"code": "allowance_exhausted"}})
GEMINI_EXHAUSTED = (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                    "message": "Quota exceeded"}})
GEMINI_BAD_KEY = (400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                  "message": "API key not valid.",
                                  "details": [{"reason": "API_KEY_INVALID"}]}})


class ChainCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.gateway = base + "/gateway/v1"
        cls.gemini = base + "/gemini/v1beta/models"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Stub.answers = {}
        Stub.calls = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = pathlib.Path(tmp.name)
        (self.data / "logs").mkdir()

    # -- fixture writers -------------------------------------------------

    def settings(self, provider, chain=None, gemini_in_settings=False, **extra):
        s = {"CLAUDE_MEM_CLOUD_SYNC_TOKEN": TOKEN,
             "CLAUDE_MEM_OPENROUTER_BASE_URL": self.gateway,
             "CLAUDE_MEM_OPENROUTER_API_KEY": TOKEN,
             "CLAUDE_MEM_PROVIDER": provider,
             "CLAUDE_MEM_GEMINI_MODEL": "gemini-flash-lite-latest",
             "CLAUDE_MEM_PRO_FALLBACK_AT": ""}
        if chain is not None:
            s["CLAUDE_MEM_PROVIDER_CHAIN"] = chain
        if gemini_in_settings:
            s["CLAUDE_MEM_GEMINI_API_KEY"] = GEMINI_KEY
        s.update(extra)
        self.write("settings.json", s)

    def env_file(self, key=GEMINI_KEY):
        (self.data / ".env").write_text("# claude-mem\nGEMINI_API_KEY=%s\n" % key)

    def cooldown(self, *providers, age=0):
        """Cooldown entries armed `age` seconds ago; a (provider, age) pair
        sets one entry's age alone."""
        entries = []
        for p in providers:
            name, seconds = p if isinstance(p, tuple) else (p, age)
            entries.append({"provider": name, "message": "Provider reported the "
                            "inference allowance exhausted",
                            "armedAtMs": int((time.time() - seconds) * 1000)})
        self.write("quota-cooldown.json", entries)

    def state(self, data):
        self.write("pro-watchdog.json", data)

    def write(self, name, data):
        (self.data / name).write_text(json.dumps(data))

    def read(self, name):
        return json.loads((self.data / name).read_text())

    def provider(self):
        return self.read("settings.json")["CLAUDE_MEM_PROVIDER"]

    def run_watchdog(self, extra_env=None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CLAUDE_MEM_") and k != "GEMINI_API_KEY"}
        env.update(CLAUDE_MEM_DATA_DIR=str(self.data),
                   CLAUDE_MEM_PRO_GATEWAY=self.gateway,
                   CLAUDE_MEM_GEMINI_ENDPOINT=self.gemini,
                   CLAUDE_MEM_PRO_WATCHDOG_NOTIFY="0")
        env.update(extra_env or {})
        done = subprocess.run(["sh", str(SCRIPT), "mem-pro-watchdog", "--apply"],
                              capture_output=True, text=True, env=env, timeout=120)
        return done.returncode, done.stdout + done.stderr

    def assert_ran(self, result):
        code, output = result
        self.assertEqual(code, 0, output)
        self.assertNotIn("not managing claude-mem here", output)
        return output

    def gemini_calls(self):
        return [c for c in Stub.calls if c["kind"] == "gemini"]


class TheDefaultChainKeepsTodaysBehaviour(ChainCase):
    """No CLAUDE_MEM_PROVIDER_CHAIN means openrouter,claude, as before."""

    def test_pro_exhausted_moves_to_the_claude_subscription(self):
        self.settings("openrouter")
        self.cooldown("openrouter")
        Stub.answers = {"gateway": PRO_EXHAUSTED}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "claude", output)
        self.assertEqual(output.count("notify:"), 1, output)

    def test_pro_back_for_the_delay_returns_and_clears_the_fallback_stamp(self):
        self.settings("claude", CLAUDE_MEM_PRO_FALLBACK_AT="2026-09-12T00:00:00Z")
        self.state({"provider": "claude", "okSince": {"openrouter": time.time() - 3700}})
        Stub.answers = {"gateway": OK}
        output = self.assert_ran(self.run_watchdog())
        settings = self.read("settings.json")
        self.assertEqual(settings["CLAUDE_MEM_PROVIDER"], "openrouter", output)
        self.assertEqual(settings["CLAUDE_MEM_PRO_FALLBACK_AT"], "")

    def test_pro_back_but_not_for_long_enough_stays(self):
        self.settings("claude")
        Stub.answers = {"gateway": OK}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "claude", output)
        self.assertIsNotNone(self.read("pro-watchdog.json")["okSince"].get("openrouter"))

    def test_the_first_state_shape_is_migrated(self):
        # The shape pro-watchdog.json had before the chain: mode pro|fallback
        # and one scalar okSince, which tracked the gateway.
        self.settings("claude")
        self.state({"mode": "fallback", "since": "2026-09-12T08:46:57-0600",
                    "okSince": time.time() - 3700, "note": "by hand"})
        Stub.answers = {"gateway": OK}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "openrouter", output)
        state = self.read("pro-watchdog.json")
        self.assertNotIn("mode", state)
        self.assertEqual(state["provider"], "openrouter")
        self.assertEqual(state["note"], "by hand")


class FailingDownTheChain(ChainCase):
    CHAIN = "openrouter,gemini,claude"

    def test_pro_exhausted_moves_to_gemini_without_writing_its_key(self):
        self.settings("openrouter", chain=self.CHAIN)
        self.env_file()
        self.cooldown("openrouter")
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": OK}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "gemini", output)
        self.assertEqual(output.count("notify:"), 1, output)
        # The probe carries the key in a header and asks for the configured
        # model; the key is never written to any file the watchdog owns.
        call = self.gemini_calls()[0]
        self.assertIn("gemini-flash-lite-latest:generateContent", call["path"])
        self.assertNotIn(GEMINI_KEY, call["path"])
        self.assertEqual(call["headers"].get("x-goog-api-key"), GEMINI_KEY)
        for name in ("settings.json", "pro-watchdog.json", "logs/pro-watchdog.log"):
            self.assertNotIn(GEMINI_KEY, (self.data / name).read_text(), name)
        self.assertNotIn(GEMINI_KEY, output)

    def test_gemini_exhausted_moves_to_the_claude_subscription(self):
        self.settings("gemini", chain=self.CHAIN, gemini_in_settings=True)
        self.cooldown("gemini")
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": GEMINI_EXHAUSTED}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "claude", output)
        self.assertEqual(output.count("notify:"), 1, output)

    def test_a_rejected_gemini_key_also_moves_down(self):
        self.settings("gemini", chain=self.CHAIN, gemini_in_settings=True)
        self.cooldown("gemini")
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": GEMINI_BAD_KEY}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "claude", output)

    def test_a_worker_signal_the_probe_does_not_confirm_moves_nothing(self):
        # "unknown never moves state": a 500 from Gemini is not exhaustion.
        self.settings("gemini", chain=self.CHAIN, gemini_in_settings=True)
        self.cooldown("gemini")
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": (500, {"error": "boom"})}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "gemini", output)
        self.assertEqual(output.count("notify:"), 0, output)

    def test_gemini_exhausted_with_claude_in_cooldown_stays_and_notifies_once(self):
        self.settings("gemini", chain=self.CHAIN, gemini_in_settings=True)
        self.cooldown("gemini", "claude")
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": GEMINI_EXHAUSTED}
        first = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "gemini", first)
        self.assertEqual(first.count("notify:"), 1, first)
        second = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "gemini", second)
        self.assertEqual(second.count("notify:"), 0,
                         "a stuck chain notifies once, not every run: " + second)

    def test_a_candidate_the_worker_still_refuses_is_passed_over(self):
        # A probe answering is not enough: the worker refuses a provider for
        # 30 minutes after arming its cooldown, so Gemini would stall the
        # observer while the Claude subscription could take it.
        self.settings("openrouter", chain=self.CHAIN, gemini_in_settings=True)
        self.cooldown("openrouter", "gemini")
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": OK}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "claude", output)
        self.assertEqual(self.gemini_calls(), [], output)

    def test_a_stuck_provider_moves_up_at_once_to_one_that_answers(self):
        # The return delay guards a preference. When the current provider
        # cannot work and nothing below can take over, a provider above that
        # answers now takes the observer without waiting.
        self.settings("gemini", chain=self.CHAIN, gemini_in_settings=True)
        self.cooldown("gemini", "claude")
        Stub.answers = {"gateway": OK, "gemini": GEMINI_EXHAUSTED}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "openrouter", output)
        [notice] = [line for line in output.splitlines() if "notify:" in line]
        self.assertIn("moved up to the cmem Pro gateway", notice)
        self.assertIsNone(self.read("pro-watchdog.json")["stuck"], output)

    def test_moving_up_out_of_a_stuck_state_also_respects_worker_cooldowns(self):
        self.settings("gemini", chain=self.CHAIN, gemini_in_settings=True)
        self.cooldown("openrouter", "gemini", "claude")
        Stub.answers = {"gateway": OK, "gemini": GEMINI_EXHAUSTED}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "gemini", output)
        self.assertEqual(self.read("pro-watchdog.json")["stuck"]["provider"], "gemini", output)
        self.assertEqual([c for c in Stub.calls if c["kind"] == "gateway"], [], output)

    def test_a_claude_cooldown_the_worker_no_longer_enforces_does_not_block(self):
        # The worker admits a probe 30 minutes after arming a cooldown, and
        # only a success with that provider removes the entry. While Gemini
        # runs, Claude's entry never clears, so its age decides.
        self.settings("gemini", chain=self.CHAIN, gemini_in_settings=True)
        self.cooldown("gemini", ("claude", 2 * 3600))
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": GEMINI_EXHAUSTED}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "claude", output)

    def test_a_failure_seen_looking_down_ends_that_providers_streak(self):
        # Gemini had a long streak, then fails while the gateway is being
        # left. Its next success must start the delay over, not return at once.
        self.settings("openrouter", chain=self.CHAIN, gemini_in_settings=True)
        self.state({"provider": "openrouter", "okSince": {"gemini": time.time() - 3700}})
        self.cooldown("openrouter")
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": (503, {"error": "x"})}
        first = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "claude", first)
        self.assertIsNone(self.read("pro-watchdog.json")["okSince"].get("gemini"), first)
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": OK}
        second = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "claude", second)

    def test_a_healthy_worker_is_not_probed_downwards(self):
        self.settings("gemini", chain=self.CHAIN, gemini_in_settings=True)
        Stub.answers = {"gateway": PRO_EXHAUSTED}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "gemini", output)
        self.assertEqual(self.gemini_calls(), [], output)


class FailingBackUpTheChain(ChainCase):
    CHAIN = "openrouter,gemini,claude"

    def test_claude_returns_to_gemini_after_the_delay(self):
        self.settings("claude", chain=self.CHAIN, gemini_in_settings=True)
        self.state({"provider": "claude", "okSince": {"gemini": time.time() - 3700}})
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": OK}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "gemini", output)
        self.assertEqual(output.count("notify:"), 1, output)

    def test_claude_waits_for_the_delay_before_returning_to_gemini(self):
        self.settings("claude", chain=self.CHAIN, gemini_in_settings=True)
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": OK}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "claude", output)
        state = self.read("pro-watchdog.json")
        self.assertIsNotNone(state["okSince"].get("gemini"))
        self.assertIsNone(state["okSince"].get("openrouter"))

    def test_a_streak_shorter_than_the_delay_does_not_return(self):
        self.settings("claude", chain=self.CHAIN, gemini_in_settings=True)
        self.state({"provider": "claude", "okSince": {"gemini": time.time() - 600}})
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": OK}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "claude", output)
        self.assertEqual(output.count("notify:"), 0, output)

    def test_a_failed_probe_restarts_the_delay(self):
        self.settings("claude", chain=self.CHAIN, gemini_in_settings=True)
        self.state({"provider": "claude", "okSince": {"gemini": time.time() - 3700}})
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": (503, {"error": "x"})}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "claude", output)
        self.assertIsNone(self.read("pro-watchdog.json")["okSince"].get("gemini"))

    def test_the_delay_is_counted_per_provider(self):
        # Gemini's streak does not return the observer to the gateway.
        self.settings("claude", chain=self.CHAIN, gemini_in_settings=True)
        self.state({"provider": "claude", "okSince": {"gemini": time.time() - 3700}})
        Stub.answers = {"gateway": OK, "gemini": OK}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "gemini", output)


class WhichProvidersCount(ChainCase):
    CHAIN = "openrouter,gemini,claude"

    def test_a_provider_outside_the_chain_is_unmanaged(self):
        self.settings("gemini", chain="openrouter,claude")
        code, output = self.run_watchdog()
        self.assertEqual(code, 0, output)
        self.assertIn("not managing claude-mem here", output)
        self.assertIn("CLAUDE_MEM_PROVIDER is 'gemini'", output)
        self.assertEqual(Stub.calls, [])

    def test_a_gemini_key_only_in_the_env_file_counts(self):
        self.settings("openrouter", chain=self.CHAIN)
        self.env_file()
        self.cooldown("openrouter")
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": OK}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "gemini", output)

    def test_a_gemini_key_only_in_the_process_environment_does_not_count(self):
        # The worker reads GEMINI_API_KEY from ~/.claude-mem/.env, never from
        # its own environment, so a key only the cron shell exports would
        # leave a worker on "gemini" silently running claude.
        self.settings("openrouter", chain=self.CHAIN)
        self.cooldown("openrouter")
        Stub.answers = {"gateway": PRO_EXHAUSTED, "gemini": OK}
        output = self.assert_ran(self.run_watchdog({"GEMINI_API_KEY": GEMINI_KEY}))
        self.assertEqual(self.provider(), "claude", output)
        self.assertEqual(self.gemini_calls(), [], output)

    def notices(self, output):
        return [line for line in output.splitlines() if "notify:" in line]

    def test_a_pro_token_that_is_not_pro_is_reported_as_configuration(self):
        # Nothing was probed or rejected: the settings disable the gateway,
        # so the notice must not send the operator to cmem.ai/pro.
        self.settings("openrouter", chain=self.CHAIN,
                      CLAUDE_MEM_CLOUD_SYNC_TOKEN="cm_free_x",
                      CLAUDE_MEM_OPENROUTER_API_KEY="cm_free_x")
        self.env_file()
        Stub.answers = {"gemini": OK}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "gemini", output)
        [notice] = self.notices(output)
        self.assertIn("not a cm_pro_ key", notice)
        self.assertNotIn("rejected", notice)
        self.assertNotIn("lapsed", notice)

    def test_a_missing_gemini_key_is_reported_as_configuration(self):
        self.settings("gemini", chain=self.CHAIN)
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.provider(), "claude", output)
        [notice] = self.notices(output)
        self.assertIn("no Gemini key", notice)
        self.assertNotIn("rejected", notice)

    def test_a_chain_with_one_usable_provider_is_unmanaged(self):
        self.settings("gemini", chain="gemini,claude")
        code, output = self.run_watchdog()
        self.assertEqual(code, 0, output)
        self.assertIn("not managing claude-mem here", output)
        self.assertIn("GEMINI_API_KEY", output)


class TheGatewayProbe(ChainCase):
    """What the gateway probe asks for, and how it reads the answer. Only a
    clear answer about the key or the allowance may move the observer."""

    def gateway_calls(self):
        return [c for c in Stub.calls if c["kind"] == "gateway"]

    def run_down(self, answer, **extra):
        self.settings("openrouter", **extra)
        self.cooldown("openrouter")
        Stub.answers = {"gateway": answer}
        return self.assert_ran(self.run_watchdog())

    def test_the_probe_asks_for_the_model_the_worker_uses(self):
        # The worker reads a comma or space separated list and sends the
        # first entry; the rest are its fallbacks.
        self.settings("claude", CLAUDE_MEM_OPENROUTER_MODEL=" cmem-observer-2, other-model")
        Stub.answers = {"gateway": OK}
        output = self.assert_ran(self.run_watchdog())
        [call] = self.gateway_calls()
        self.assertEqual(json.loads(call["body"])["model"], "cmem-observer-2", output)

    def test_no_configured_model_probes_the_gateway_alias(self):
        self.settings("claude", CLAUDE_MEM_OPENROUTER_MODEL=["not", "a", "string"])
        Stub.answers = {"gateway": OK}
        output = self.assert_ran(self.run_watchdog())
        [call] = self.gateway_calls()
        self.assertEqual(json.loads(call["body"])["model"], "cmem-observer", output)

    def models_asked(self):
        return [json.loads(c["body"])["model"] for c in self.gateway_calls()]

    def test_a_model_the_key_may_not_use_falls_through_to_the_next(self):
        # The worker falls back to the next model, so one refused model does
        # not make the provider unusable.
        self.settings("claude", CLAUDE_MEM_OPENROUTER_MODEL="retired-model,cmem-observer")
        self.state({"provider": "claude", "okSince": {"openrouter": time.time() - 3700}})
        Stub.answers = {"gateway:retired-model": (403, {"error": {"code": "model_not_allowed"}}),
                        "gateway": OK}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.models_asked(), ["retired-model", "cmem-observer"], output)
        self.assertEqual(self.provider(), "openrouter", output)

    def test_a_provider_wide_answer_stops_at_the_first_model(self):
        self.settings("openrouter", CLAUDE_MEM_OPENROUTER_MODEL="first,second")
        self.cooldown("openrouter")
        # The code decides whatever the status, so it stops the walk even on
        # a status that would otherwise read as model-specific.
        Stub.answers = {"gateway": (429, {"error": {"code": "allowance_exhausted"}})}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.models_asked(), ["first"], output)
        self.assertEqual(self.provider(), "claude", output)

    def test_at_most_three_models_are_probed(self):
        self.settings("openrouter", CLAUDE_MEM_OPENROUTER_MODEL="a b c d e")
        self.cooldown("openrouter")
        Stub.answers = {"gateway": (403, {"error": {"code": "model_not_allowed"}})}
        output = self.assert_ran(self.run_watchdog())
        self.assertEqual(self.models_asked(), ["a", "b", "c"], output)
        self.assertEqual(self.provider(), "openrouter", output)

    def test_a_403_without_a_key_code_moves_nothing(self):
        # A forbidden model or endpoint says nothing about the subscription.
        output = self.run_down((403, {"error": {"code": "model_not_allowed"}}))
        self.assertEqual(self.provider(), "openrouter", output)
        self.assertEqual(output.count("notify:"), 0, output)
        self.assertEqual(self.read("pro-watchdog.json")["probes"]["openrouter"]["verdict"],
                         "unknown", output)

    def test_a_403_naming_a_rejected_key_moves_down_as_inactive(self):
        output = self.run_down((403, {"error": {"code": "key_invalid"}}))
        self.assertEqual(self.provider(), "claude", output)
        self.assertIn("key rejected", output)

    def test_a_401_moves_down_as_inactive(self):
        output = self.run_down((401, {"error": "unauthorized"}))
        self.assertEqual(self.provider(), "claude", output)
        self.assertIn("key rejected", output)

    def test_the_error_code_wins_over_a_402_status(self):
        # A lapsed subscription answered with 402 is not a monthly reset.
        output = self.run_down((402, {"error": {"code": "subscription_inactive"}}))
        self.assertEqual(self.provider(), "claude", output)
        self.assertIn("key rejected", output)
        self.assertNotIn("allowance exhausted", output)


if __name__ == "__main__":
    unittest.main()
