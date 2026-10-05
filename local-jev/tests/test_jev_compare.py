"""The comparison arms: a Kev and a Haiku row beside every live Jev call.

Every endpoint here is a stub on loopback, and `claude` is a stub on PATH, so
no case reaches Kev, Anthropic, OpenRouter, Baseten or a real Claude login.
The arms run in a detached child process, so each case waits for the rows to
appear in a database of its own, bounded, and never sleeps a fixed time.

The promises checked:

* the caller's stdout, exit code and wait are Jev's alone, including when an
  arm hangs and the caller reads through a pipe;
* every arm gets the redacted request and nothing else;
* each arm writes one row with the Jev row's pair id, its own provider and
  model, the answer as a number, the distribution as numbers, tokens, cost;
* an unkeyed, refused, slow or switched-off arm costs the caller nothing.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ArmServer(ThreadingHTTPServer):
    # The Haiku arm opens MAX_HAIKU_QUESTIONS connections at once; the default
    # listen backlog of 5 lets macOS reset the rest under load.
    request_queue_size = 64
from pathlib import Path

import jev
import jev_compare

from .test_jev import ENTRYPOINT, Stub
from .test_jev_metering import SENTINEL, MeteringCase, connect

import sd_db  # noqa: E402  (test_jev_metering puts it on the path)

#: The child is a fresh interpreter; it finds `sd_db` the way this one did.
SD_DB_PATH = str(Path(sd_db.__file__).resolve().parents[1])


class Arm(BaseHTTPRequestHandler):
    """One stub for every HTTP arm: Kev's System One, Anthropic's Messages,
    and the OpenAI-compatible chat completions OpenRouter and Baseten speak."""

    delay = 0.0
    status = 200
    seen = []
    # What a prompt-adapted model answers, as the JSON text it would write.
    reply = None
    # What OpenRouter reports the call cost; json.dumps writes inf as Infinity.
    cost = 0.000185
    # An answer object Kev returns in place of its usual one, when set.
    kev_answer = None
    # A chat completion with usage but no message, when set.
    no_message = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        Arm.seen.append({"path": self.path, "body": body,
                         "headers": {k.lower(): v for k, v in self.headers.items()}})
        if Arm.delay:
            time.sleep(Arm.delay)
        if Arm.status != 200:
            self.send_response(Arm.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.endswith("/v1/systemone"):
            out = self.kev(body)
        elif self.path.endswith("/v1/messages"):
            out = {"content": [{"type": "text", "text": self.text(body)}],
                   "usage": {"input_tokens": 120, "output_tokens": 9}}
        else:
            out = {"choices": [] if Arm.no_message else
                       [{"message": {"content": self.text(body)}}],
                   "usage": {"prompt_tokens": 130, "completion_tokens": 11}}
            # OpenRouter reports the call's cost; Baseten does not.
            if self.path.startswith("/api/"):
                out["usage"]["cost"] = Arm.cost
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def kev(body):
        answers = {}
        for qid, question in body["questions"].items():
            if Arm.kev_answer is not None:
                answers[qid] = Arm.kev_answer
            elif question["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": 0.81}
            elif question["type"] == "choice":
                keys = list(question["criteria"])
                answers[qid] = {"type": "choice", "choice": keys[-1], "confidence": 0.4,
                                "probabilities": {k: (0.6 if k == keys[-1] else 0.4 / (len(keys) - 1))
                                                  for k in keys}}
            else:
                answers[qid] = {"type": "score", "score": 1.44, "confidence": 0.34,
                                "probabilities": {"0": 0.0, "1": 0.56, "2": 0.44}}
        return {"model": body["model"], "answers": answers,
                "usage": {"input_tokens": 101, "output_tokens": 16},
                # Kev's server reports a float rounded to one decimal.
                "latency_ms": 495.4}

    @staticmethod
    def text(body):
        if Arm.reply is not None:
            return Arm.reply
        return json.dumps({"probability": 0.3})


class CompareCase(MeteringCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.arms = ArmServer(("127.0.0.1", 0), Arm)
        cls.arms.daemon_threads = True
        threading.Thread(target=cls.arms.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.arms.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.arms.shutdown()
        cls.arms.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        Arm.delay = 0.0
        Arm.status = 200
        Arm.seen = []
        Arm.reply = None
        Arm.cost = 0.000185
        Arm.kev_answer = None
        Arm.no_message = False
        self.bin = Path(tempfile.mkdtemp())

    def env(self, **extra):
        settings = {
            "JEV_COMPARE_KEV": "1",
            "JEV_COMPARE_KEV_URL": self.base + "/v1/systemone",
            "JEV_COMPARE_HAIKU_VIA": "off",
            "JEV_COMPARE_ANTHROPIC_URL": self.base + "/v1/messages",
            "JEV_COMPARE_OPENROUTER_URL": self.base + "/api/v1/chat/completions",
            "JEV_COMPARE_BASETEN_URL": self.base + "/v1/chat/completions",
            "JEV_COMPARE_TIMEOUT": "10",
            "PYTHONPATH": SD_DB_PATH,
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.config),
        }
        settings.update(extra)
        return super().env(**settings)

    def wait_rows(self, count, timeout=20.0):
        """Every row once `count` exist, or a failure naming what did."""
        deadline = time.monotonic() + timeout
        while True:
            found = self.rows()
            if len(found) >= count or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        self.assertGreaterEqual(len(found), count,
                                f"expected {count} rows, got {[dict(r) for r in found]}")
        return found

    def by_arm(self, rows):
        return {row["arm"]: row for row in rows}


class TheKevArm(CompareCase):
    def test_a_noul_writes_a_kev_row_paired_with_the_jev_row(self):
        code, out = self.run_main(["noul", "is it?", "--stage", "JEV_NOTIFY"])
        self.assertEqual((code, out), (0, "0.97\n"))
        rows = self.by_arm(self.wait_rows(2))
        jev_row, kev = rows["jev"], rows["kev"]
        self.assertIsNotNone(jev_row["pair"])
        self.assertEqual(kev["pair"], jev_row["pair"])
        self.assertEqual((kev["stage"], kev["primitive"]), ("JEV_NOTIFY", "noul"))
        self.assertEqual(kev["provider"], "local")
        self.assertEqual(kev["model"], "jaredpalmer/kev-4b@v1.0")
        self.assertEqual(kev["answer"], "0.81")
        self.assertEqual((kev["tokens_in"], kev["tokens_out"]), (101, 16))
        self.assertEqual(kev["server_ms"], 495)
        self.assertEqual(kev["usd"], 0.0)
        self.assertEqual(kev["changed"], "no")
        self.assertEqual(kev["outcome"], "ok")
        self.assertIsNotNone(kev["duration_ms"])

    def test_under_the_shadow_switch_the_kev_row_joins_the_pair(self):
        """sd:2761: the arm, the judgment and the caller's own answer are one pair."""
        code, out = self.run_main(["score", "how urgent?", "--levels", "a,b,c",
                                   "--fallback", "1", "--stage", "JEV_NOTIFY"],
                                  JEV_SHADOW="1")
        self.assertEqual((code, out), (0, "1\n"))
        rows = self.by_arm(self.wait_rows(3))
        self.assertEqual(set(rows), {"jev", "baseline", "kev"})
        self.assertIsNotNone(rows["jev"]["pair"])
        self.assertEqual({row["pair"] for row in rows.values()}, {rows["jev"]["pair"]})

    def test_kev_gets_the_same_redacted_request_under_its_own_model_name(self):
        secret = "ghp_" + "a" * 36
        self.run_main(["noul", "is it?"], stdin=f"token {secret} here")
        self.wait_rows(2)
        sent = [s for s in Arm.seen if s["path"].endswith("/v1/systemone")]
        self.assertEqual(len(sent), 1)
        body = sent[0]["body"]
        self.assertEqual(body["model"], "kev-latest")
        self.assertNotIn(secret, json.dumps(body))
        self.assertIn("[REDACTED]", body["state"])
        jev_sent = Stub.seen[0]["payload"]
        self.assertEqual(body["state"], jev_sent["state"])
        self.assertEqual(body["questions"], jev_sent["questions"])

    def test_a_choice_records_the_position_and_the_distribution(self):
        self.run_main(["choice", "which?", "--criteria", "desk,phone,mail"])
        rows = self.by_arm(self.wait_rows(2))
        kev = rows["kev"]
        self.assertEqual(kev["answer"], "3")
        self.assertAlmostEqual(kev["confidence"], 0.4)
        self.assertEqual(kev["probabilities"], "0.2000,0.2000,0.6000")
        # The jev row keeps its distribution too, in the caller's order.
        self.assertEqual(rows["jev"]["probabilities"], "0.0000,0.0000,0.0000")

    def test_a_kev_answer_with_no_usable_value_is_invalid(self):
        cases = ((["noul", "is it?"], {"type": "noul"}),
                 (["choice", "which?", "--criteria", "desk,phone"],
                  {"type": "choice", "choice": "fax", "confidence": 0.9}))
        for argv, answer in cases:
            with self.subTest(primitive=argv[0]):
                Arm.kev_answer = answer
                before = len(self.rows())
                self.run_main(argv)
                kev = self.by_arm(self.wait_rows(before + 2)[before:])["kev"]
                self.assertEqual((kev["outcome"], kev["cause"]), ("invalid", "invalid"))
                self.assertIsNone(kev["answer"])

    def test_a_refused_kev_records_unavailable(self):
        code, out = self.run_main(["noul", "is it?"],
                                  JEV_COMPARE_KEV_URL="http://127.0.0.1:9/v1/systemone")
        self.assertEqual((code, out), (0, "0.97\n"))
        kev = self.by_arm(self.wait_rows(2))["kev"]
        self.assertEqual((kev["outcome"], kev["cause"]), ("unavailable", "unavailable"))
        self.assertIsNone(kev["answer"])

    def test_a_slow_kev_past_its_timeout_records_timeout(self):
        Arm.delay = 3.0
        self.run_main(["noul", "is it?"], JEV_COMPARE_TIMEOUT="1")
        kev = self.by_arm(self.wait_rows(2))["kev"]
        self.assertEqual((kev["outcome"], kev["cause"]), ("timeout", "timeout"))

    def test_an_off_word_sends_nothing_and_writes_nothing(self):
        for word in ("0", "off", "False", "no", "disabled"):
            with self.subTest(word=word):
                Arm.seen = []
                self.run_main(["noul", "is it?"], JEV_COMPARE_KEV=word)
                time.sleep(0.5)
                self.assertEqual(Arm.seen, [])
        self.assertEqual({row["arm"] for row in self.rows()}, {"jev"})
        self.assertTrue(all(row["pair"] is None for row in self.rows()))

    def test_no_arm_runs_when_the_meter_is_off(self):
        self.run_main(["noul", "is it?"], JEV_METER="0")
        time.sleep(0.5)
        self.assertEqual(Arm.seen, [])

    def test_no_arm_calls_out_when_there_is_no_ledger_to_record_in(self):
        missing = Path(tempfile.mkdtemp()) / "absent.db"
        self.run_main(["noul", "is it?"], JEV_METER_DB=str(missing),
                      JEV_COMPARE_HAIKU_VIA="anthropic", JEV_COMPARE_ANTHROPIC_KEY="k")
        time.sleep(1.0)
        self.assertEqual(Arm.seen, [])

    def test_no_arm_calls_out_when_the_ledger_predates_the_arms(self):
        old = Path(tempfile.mkdtemp()) / "old.db"
        old.write_bytes(self.store.read_bytes())
        connection = connect(old)
        connection.execute("PRAGMA user_version = 16")
        connection.close()
        self.run_main(["noul", "is it?"], JEV_METER_DB=str(old),
                      JEV_COMPARE_HAIKU_VIA="anthropic", JEV_COMPARE_ANTHROPIC_KEY="k")
        time.sleep(1.0)
        self.assertEqual(Arm.seen, [])

    def test_no_arm_calls_out_when_the_library_predates_the_arms(self):
        # An sd_db from before migration 017: it names two arms and would
        # refuse a kev or haiku row after the call had been paid for.
        lib = Path(tempfile.mkdtemp())
        (lib / "sd_db").mkdir()
        (lib / "sd_db" / "__init__.py").write_text("")
        (lib / "sd_db" / "database.py").write_text(
            "class Connection:\n"
            "    def close(self):\n"
            "        pass\n"
            "def connect(path=None, busy_timeout=0, **kw):\n"
            "    return Connection()\n")
        (lib / "sd_db" / "judgment.py").write_text(
            "ARMS = ('jev', 'baseline')\n"
            "def record(connection, **event):\n"
            "    raise ValueError('arm must be one of jev, baseline')\n")
        self.run_main(["noul", "is it?"], PYTHONPATH=str(lib),
                      JEV_COMPARE_HAIKU_VIA="anthropic", JEV_COMPARE_ANTHROPIC_KEY="k")
        time.sleep(1.0)
        self.assertEqual(Arm.seen, [])

    def test_no_arm_runs_when_jev_is_not_called(self):
        self.run_main(["noul", "is it?", "--fallback", "0.5"], TYPESAFE_API_KEY="")
        time.sleep(0.5)
        self.assertEqual(Arm.seen, [])

    def test_a_refused_key_starts_no_arm(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"ghp_" + "b" * 36: 1}, fh)
        self.addCleanup(os.unlink, fh.name)
        self.run_main(["noul", "is it?", "--state", fh.name, "--state-format", "json",
                       "--fallback", "0.5"])
        time.sleep(0.5)
        self.assertEqual(Arm.seen, [])

    def test_retries_start_the_arms_once(self):
        Stub.throttle_first = 2
        self.run_main(["noul", "is it?"])
        self.wait_rows(2)
        time.sleep(0.3)
        self.assertEqual(len(Arm.seen), 1)

    def test_nothing_submitted_reaches_the_ledger_from_any_arm(self):
        Arm.reply = json.dumps({"probability": 0.2})
        self.run_main(["noul", f"is {SENTINEL}?"], stdin=f"state {SENTINEL}",
                      JEV_COMPARE_HAIKU_VIA="anthropic", JEV_COMPARE_ANTHROPIC_KEY="k")
        self.wait_rows(3)
        self.assertNotIn(SENTINEL.encode(), self.store.read_bytes())
        sent = [json.dumps(s["body"]) for s in Arm.seen]
        self.assertTrue(all(SENTINEL in body for body in sent))


class TheHaikuArm(CompareCase):
    def haiku(self, via, argv=("noul", "is it?"), **extra):
        env = {"JEV_COMPARE_KEV": "0", "JEV_COMPARE_HAIKU_VIA": via}
        env.update(extra)
        before = len(self.rows())    # a subtest's earlier rows must not count
        code, _out = self.run_main(list(argv), **env)
        self.assertEqual(code, 0)
        return self.by_arm(self.wait_rows(before + 2)[before:])["haiku"]

    def test_anthropic_messages(self):
        Arm.reply = json.dumps({"probability": 0.25})
        row = self.haiku("anthropic", JEV_COMPARE_ANTHROPIC_KEY="test-anthropic-key")
        self.assertEqual((row["provider"], row["model"]), ("anthropic", "claude-haiku-4-5"))
        self.assertEqual(row["answer"], "0.25")
        self.assertEqual((row["tokens_in"], row["tokens_out"]), (120, 9))
        # Priced from the configured list price: 120 x $1 + 9 x $5 per million.
        self.assertAlmostEqual(row["usd"], (120 * 1 + 9 * 5) / 1e6)
        sent = Arm.seen[0]
        self.assertEqual(sent["headers"]["x-api-key"], "test-anthropic-key")
        self.assertEqual(sent["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(sent["body"]["model"], "claude-haiku-4-5")
        self.assertIn("json_schema", json.dumps(sent["body"]["output_config"]))
        self.assertIn("is it?", sent["body"]["messages"][0]["content"])
        self.assertIn("a sentence", sent["body"]["messages"][0]["content"])

    def test_haiku_prices_apply_only_to_the_haiku_model(self):
        # Another model's tokens cost what that model costs: without its
        # prices the row has no cost, as on Baseten.
        Arm.reply = json.dumps({"probability": 0.25})
        row = self.haiku("anthropic", JEV_COMPARE_ANTHROPIC_KEY="k",
                         JEV_COMPARE_HAIKU_MODEL="claude-sonnet-4-5")
        self.assertEqual((row["outcome"], row["model"]), ("ok", "claude-sonnet-4-5"))
        self.assertEqual((row["tokens_in"], row["tokens_out"]), (120, 9))
        self.assertIsNone(row["usd"])
        row = self.haiku("anthropic", JEV_COMPARE_ANTHROPIC_KEY="k",
                         JEV_COMPARE_HAIKU_MODEL="claude-sonnet-4-5",
                         JEV_COMPARE_HAIKU_USD_IN="3", JEV_COMPARE_HAIKU_USD_OUT="15")
        self.assertAlmostEqual(row["usd"], (120 * 3 + 9 * 15) / 1e6)

    def test_the_operator_key_variable_is_never_read(self):
        row = self.haiku("anthropic", ANTHROPIC_API_KEY="from-the-profile")
        self.assertEqual((row["outcome"], row["cause"]), ("unavailable", "unkeyed"))
        self.assertEqual(Arm.seen, [])

    def test_openrouter_reports_its_own_cost(self):
        Arm.reply = "```json\n" + json.dumps({"probability": 0.9}) + "\n```"
        row = self.haiku("openrouter", JEV_COMPARE_OPENROUTER_KEY="or-key")
        self.assertEqual((row["provider"], row["model"]),
                         ("openrouter", "anthropic/claude-haiku-4.5"))
        self.assertEqual(row["answer"], "0.9")
        self.assertEqual((row["tokens_in"], row["tokens_out"]), (130, 11))
        self.assertAlmostEqual(row["usd"], 0.000185)
        sent = Arm.seen[0]
        self.assertEqual(sent["headers"]["authorization"], "Bearer or-key")
        self.assertEqual(sent["body"]["response_format"]["type"], "json_schema")

    def test_baseten_needs_a_model_and_a_key(self):
        row = self.haiku("baseten", JEV_COMPARE_BASETEN_KEY="bt-key")
        self.assertEqual((row["outcome"], row["cause"]), ("unavailable", "unkeyed"))
        self.assertEqual(Arm.seen, [])

    def test_baseten(self):
        Arm.reply = json.dumps({"probability": 0.6})
        row = self.haiku("baseten", JEV_COMPARE_BASETEN_KEY="bt-key",
                         JEV_COMPARE_BASETEN_MODEL="example/model")
        self.assertEqual((row["provider"], row["model"]), ("baseten", "example/model"))
        self.assertEqual(row["answer"], "0.6")
        self.assertEqual(Arm.seen[0]["headers"]["authorization"], "Bearer bt-key")
        # Haiku's list price is not this model's: no price set, no cost.
        self.assertEqual((row["tokens_in"], row["tokens_out"]), (130, 11))
        self.assertIsNone(row["usd"])

    def test_baseten_cost_comes_from_its_configured_prices(self):
        Arm.reply = json.dumps({"probability": 0.6})
        row = self.haiku("baseten", JEV_COMPARE_BASETEN_KEY="bt-key",
                         JEV_COMPARE_BASETEN_MODEL="example/model",
                         JEV_COMPARE_HAIKU_USD_IN="0.5", JEV_COMPARE_HAIKU_USD_OUT="1")
        self.assertAlmostEqual(row["usd"], (130 * 0.5 + 11 * 1) / 1e6)

    def test_claude_cli(self):
        log = self.bin / "claude.log"
        stub = self.bin / "claude"
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$@" > "{log}"\n'
            f'cat >> "{log}"\n'
            f'pwd >> "{log}"\n'
            "cat <<'EOF'\n"
            + json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": "", "structured_output": {"probability": 0.7},
                          "duration_ms": 2100, "duration_api_ms": 1500,
                          "total_cost_usd": 0.0123,
                          "usage": {"input_tokens": 10, "cache_creation_input_tokens": 3000,
                                    "cache_read_input_tokens": 200, "output_tokens": 20}})
            + "\nEOF\n")
        stub.chmod(0o755)
        row = self.haiku("claude-cli")
        self.assertEqual((row["provider"], row["model"]), ("claude-cli", "haiku"))
        self.assertEqual(row["answer"], "0.7")
        self.assertEqual((row["tokens_in"], row["tokens_out"]), (3210, 20))
        self.assertAlmostEqual(row["usd"], 0.0123)
        self.assertEqual(row["server_ms"], 1500)
        argv = log.read_text()
        for flag in ("-p", "--model", "haiku", "--output-format", "json", "--json-schema",
                     "--system-prompt", "--tools", "--strict-mcp-config",
                     "--no-session-persistence", "--setting-sources"):
            self.assertIn(flag + "\n", argv)
        self.assertIn("is it?", argv)
        # Run from an empty folder, never from the caller's checkout.
        self.assertNotIn(os.getcwd() + "\n", argv)

    def claude_stub(self, result, status=0):
        stub = self.bin / "claude"
        stub.write_text("#!/bin/sh\ncat > /dev/null\ncat <<'EOF'\n"
                        + json.dumps(result) + f"\nEOF\nexit {status}\n")
        stub.chmod(0o755)

    def test_a_claude_cli_error_result_keeps_the_usage_it_reported(self):
        failed = {"type": "result", "subtype": "error_during_execution",
                  "is_error": True, "result": "", "total_cost_usd": 0.004,
                  "duration_api_ms": 900,
                  "usage": {"input_tokens": 10, "cache_read_input_tokens": 200,
                            "output_tokens": 3}}
        for status in (0, 1):
            with self.subTest(status=status):
                self.claude_stub(failed, status)
                row = self.haiku("claude-cli")
                self.assertEqual((row["outcome"], row["cause"]),
                                 ("unavailable", "unavailable"))
                self.assertIsNone(row["answer"])
                self.assertEqual((row["tokens_in"], row["tokens_out"]), (210, 3))
                self.assertAlmostEqual(row["usd"], 0.004)

    def test_a_response_with_no_message_keeps_the_usage_it_reported(self):
        Arm.no_message = True
        row = self.haiku("openrouter", JEV_COMPARE_OPENROUTER_KEY="k")
        self.assertEqual((row["outcome"], row["cause"]), ("invalid", "invalid"))
        self.assertIsNone(row["answer"])
        self.assertEqual((row["tokens_in"], row["tokens_out"]), (130, 11))
        self.assertAlmostEqual(row["usd"], 0.000185)

    def test_an_unknown_transport_is_invalid_and_sends_nothing(self):
        row = self.haiku("carrier-pigeon")
        self.assertEqual((row["outcome"], row["cause"]), ("invalid", "invalid"))
        self.assertEqual(Arm.seen, [])

    def test_a_probability_that_is_not_a_finite_number_is_invalid(self):
        # json.loads reads NaN and Infinity, and float() takes a bool or a string.
        for value in ("NaN", "Infinity", "-Infinity", "true", '"0.5"'):
            with self.subTest(value=value):
                Arm.reply = '{"probability": %s}' % value
                row = self.haiku("anthropic", JEV_COMPARE_ANTHROPIC_KEY="k")
                self.assertEqual((row["outcome"], row["cause"]), ("invalid", "invalid"))
                self.assertIsNone(row["answer"])

    def test_an_unusable_reply_keeps_the_tokens_and_cost_it_was_billed(self):
        Arm.reply = "I think probably yes."
        row = self.haiku("anthropic", JEV_COMPARE_ANTHROPIC_KEY="k")
        self.assertEqual((row["outcome"], row["cause"]), ("invalid", "invalid"))
        self.assertEqual((row["tokens_in"], row["tokens_out"]), (120, 9))
        self.assertAlmostEqual(row["usd"], (120 * 1 + 9 * 5) / 1e6)

    def test_a_reported_cost_that_is_not_finite_is_no_cost(self):
        Arm.cost = float("inf")    # 1.0 would read as one dollar, not as a fallback
        row = self.haiku("openrouter", JEV_COMPARE_OPENROUTER_KEY="k")
        self.assertEqual(row["outcome"], "ok")
        self.assertEqual((row["tokens_in"], row["tokens_out"]), (130, 11))
        # The reported cost is dropped, so the list price prices the tokens.
        self.assertAlmostEqual(row["usd"], (130 * 1 + 11 * 5) / 1e6)

    def test_a_reply_that_is_not_json_is_invalid(self):
        Arm.reply = "I think probably yes."
        row = self.haiku("anthropic", JEV_COMPARE_ANTHROPIC_KEY="k")
        self.assertEqual((row["outcome"], row["cause"]), ("invalid", "invalid"))
        self.assertIsNone(row["answer"])

    def test_a_choice_is_asked_over_the_callers_keys_and_mapped_back(self):
        Arm.reply = json.dumps({"probabilities": {"desk": 0.1, "phone": 0.7, "mail": 0.2}})
        row = self.haiku("anthropic", ("choice", "which?", "--criteria",
                                       "desk=front desk,phone,mail"),
                         JEV_COMPARE_ANTHROPIC_KEY="k")
        self.assertEqual(row["answer"], "2")
        self.assertEqual(row["probabilities"], "0.1000,0.7000,0.2000")
        # (0.7 - 1/3) / (1 - 1/3), the reference adapter's choice confidence.
        self.assertAlmostEqual(row["confidence"], 0.55, places=4)
        prompt = Arm.seen[0]["body"]["messages"][0]["content"]
        self.assertIn("desk: front desk", prompt)
        schema = Arm.seen[0]["body"]["output_config"]["format"]["schema"]
        self.assertEqual(schema["properties"]["probabilities"]["required"],
                         ["desk", "phone", "mail"])

    def test_a_choice_reply_missing_an_option_is_invalid(self):
        Arm.reply = json.dumps({"probabilities": {"phone": 1.0}})
        row = self.haiku("anthropic", ("choice", "which?", "--criteria", "desk,phone,mail"),
                         JEV_COMPARE_ANTHROPIC_KEY="k")
        self.assertEqual((row["outcome"], row["cause"]), ("invalid", "invalid"))
        self.assertIsNone(row["answer"])
        self.assertIsNone(row["probabilities"])

    def test_a_choice_reply_with_an_option_nobody_asked_for_is_invalid(self):
        Arm.reply = json.dumps({"probabilities": {"desk": 0.1, "phone": 0.6,
                                                  "mail": 0.2, "fax": 0.1}})
        row = self.haiku("anthropic", ("choice", "which?", "--criteria", "desk,phone,mail"),
                         JEV_COMPARE_ANTHROPIC_KEY="k")
        self.assertEqual((row["outcome"], row["cause"]), ("invalid", "invalid"))
        self.assertIsNone(row["answer"])

    def test_a_score_is_the_expected_level(self):
        Arm.reply = json.dumps({"probabilities": {"0": 0.0, "1": 0.5, "2": 0.5}})
        row = self.haiku("anthropic", ("score", "how bad?", "--levels", "calm,cross,angry"),
                         JEV_COMPARE_ANTHROPIC_KEY="k")
        self.assertEqual(row["answer"], "1.5")
        self.assertEqual(row["probabilities"], "0.0000,0.5000,0.5000")

    def test_a_batch_asks_one_request_per_question(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"a": {"type": "noul", "instructions": "first?"},
                       "b": {"type": "noul", "instructions": "second?"}}, fh)
        self.addCleanup(os.unlink, fh.name)
        row = self.haiku("anthropic", ("ask", "--questions", fh.name),
                         JEV_COMPARE_ANTHROPIC_KEY="k")
        self.assertEqual(len(Arm.seen), 2)
        prompts = sorted(s["body"]["messages"][0]["content"] for s in Arm.seen)
        self.assertIn("first?", prompts[0])
        self.assertNotIn("second?", prompts[0])
        self.assertEqual((row["questions"], row["tokens_in"]), (2, 240))
        self.assertIsNone(row["answer"])


class TheCallerDoesNotWait(CompareCase):
    """The reason the arms run in a detached child, through the real entrypoint."""

    def test_a_hung_arm_does_not_delay_a_piped_caller(self):
        Arm.delay = 4.0
        env = dict(os.environ)
        env.update(self.env(PYTHON=sys.executable))
        started = time.monotonic()
        result = subprocess.run(["sh", str(ENTRYPOINT), "noul", "is it?"],
                                input="a sentence", capture_output=True, text=True,
                                env=env, timeout=30)
        elapsed = time.monotonic() - started
        self.assertEqual((result.returncode, result.stdout), (0, "0.97\n"), result.stderr)
        self.assertLess(elapsed, 2.5, "the caller waited for the arm")
        kev = self.by_arm(self.wait_rows(2))["kev"]
        self.assertGreaterEqual(kev["duration_ms"], 4000)


class TheConfigFile(CompareCase):
    """jev.sh reads the arms' settings from <config>/jev/.env; the environment wins."""

    def test_the_config_file_sets_the_arms_and_an_exported_value_wins(self):
        folder = self.config / "jev"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".env").write_text(
            'JEV_COMPARE_KEV_URL="http://192.0.2.1:9/v1/systemone"\n'
            'JEV_COMPARE_HAIKU_VIA="openrouter"\n'
            'JEV_COMPARE_OPENROUTER_KEY="or-key"\n')
        Arm.reply = json.dumps({"probability": 0.6})
        env = dict(os.environ)
        env.update(self.env(PYTHON=sys.executable))
        del env["JEV_COMPARE_HAIKU_VIA"]
        result = subprocess.run(["sh", str(ENTRYPOINT), "noul", "is it?"],
                                input="a sentence", capture_output=True, text=True,
                                env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.by_arm(self.wait_rows(3))
        self.assertEqual(rows["kev"]["outcome"], "ok")
        self.assertEqual((rows["haiku"]["provider"], rows["haiku"]["outcome"]),
                         ("openrouter", "ok"))

    def test_an_exported_empty_switch_beats_an_on_value_in_the_config_file(self):
        folder = self.config / "jev"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".env").write_text(
            'JEV_COMPARE_KEV="1"\n'
            'JEV_COMPARE_HAIKU_VIA="openrouter"\n'
            'JEV_COMPARE_OPENROUTER_KEY="or-key"\n')
        result = self.via_entrypoint(JEV_COMPARE_KEV="", JEV_COMPARE_HAIKU_VIA="")
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(self.rows()) < 2:
            time.sleep(0.05)
        self.assertEqual([row["arm"] for row in self.rows()], ["jev"])
        self.assertEqual(Arm.seen, [])

    def via_entrypoint(self, drop=(), **extra):
        env = dict(os.environ)
        env.update(self.env(PYTHON=sys.executable, **extra))
        for name in ("JEV_COMPARE_KEV_URL", "JEV_COMPARE_KEV_MODEL", "KEV_MODEL",
                     "KEV_PORT", "KEV_API_KEY", *drop):
            if name not in extra:
                env.pop(name, None)
        return subprocess.run(["sh", str(ENTRYPOINT), "noul", "is it?"],
                              input="a sentence", capture_output=True, text=True,
                              env=env, timeout=30)

    def kev_config(self, text):
        folder = self.config / "kev"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".env").write_text(text)

    def test_the_kev_arm_follows_the_kev_service_config(self):
        # The service and the arm read one file: a checkpoint, port or key
        # changed for kev.sh serve is the one the arm asks and records.
        port = self.base.rsplit(":", 1)[1]
        self.kev_config(f'KEV_PORT="{port}"\n'
                        'KEV_MODEL="example/kev-other"\n'
                        'KEV_API_KEY="kev-test-key"\n')
        result = self.via_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        kev = self.by_arm(self.wait_rows(2))["kev"]
        self.assertEqual((kev["outcome"], kev["model"]), ("ok", "example/kev-other"))
        sent = [s for s in Arm.seen if s["path"] == "/v1/systemone"]
        self.assertEqual(sent[0]["headers"]["authorization"], "Bearer kev-test-key")

    def test_an_exported_empty_key_beats_the_kev_service_config(self):
        # An exported empty KEV_API_KEY switches auth off: the service key
        # must not reach a server the caller pointed the arm at.
        port = self.base.rsplit(":", 1)[1]
        self.kev_config(f'KEV_PORT="{port}"\nKEV_API_KEY="kev-test-key"\n')
        result = self.via_entrypoint(KEV_API_KEY="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.by_arm(self.wait_rows(2))["kev"]["outcome"], "ok")
        sent = [s for s in Arm.seen if s["path"] == "/v1/systemone"]
        self.assertNotIn("authorization", sent[0]["headers"])

    def test_a_jev_compare_setting_beats_the_kev_service_config(self):
        self.kev_config('KEV_PORT="9"\nKEV_MODEL="example/kev-other"\n')
        result = self.via_entrypoint(
            JEV_COMPARE_KEV_URL=self.base + "/v1/systemone",
            JEV_COMPARE_KEV_MODEL="example/kev-pinned")
        self.assertEqual(result.returncode, 0, result.stderr)
        kev = self.by_arm(self.wait_rows(2))["kev"]
        self.assertEqual((kev["outcome"], kev["model"]), ("ok", "example/kev-pinned"))


class TheArmsAreOptIn(CompareCase):
    """Unset means off for both arms: a live Jev call stays what it was."""

    def call_with(self, **settings):
        from unittest import mock
        env = self.env()
        for name in ("JEV_COMPARE_KEV", "JEV_COMPARE_HAIKU_VIA"):
            env.pop(name, None)
        env.update(settings)
        spawned = []
        saved = sys.stdin
        with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as fake, \
                mock.patch("subprocess.Popen",
                           side_effect=lambda argv, **kw: spawned.append(argv)):
            fake.write("a sentence")
            fake.seek(0)
            sys.stdin = fake
            try:
                code = jev.main(["noul", "is it?"], out=out, env=env,
                                sleep=lambda _s: None)
            finally:
                sys.stdin = saved
        self.assertEqual(code, 0)
        return spawned

    def test_with_nothing_set_no_arm_starts_and_no_row_is_written(self):
        self.assertEqual(self.call_with(), [])
        self.assertEqual(Arm.seen, [])
        rows = self.rows()
        self.assertEqual([row["arm"] for row in rows], ["jev"])
        self.assertIsNone(rows[0]["pair"])

    def test_only_an_on_word_starts_the_kev_arm(self):
        for word in ("1", "on", "true", "yes", "enabled"):
            with self.subTest(word=word):
                self.assertEqual(len(self.call_with(JEV_COMPARE_KEV=word)), 1)
        for word in ("", "0", "off", "maybe"):
            with self.subTest(word=word):
                self.assertEqual(self.call_with(JEV_COMPARE_KEV=word), [])

    def test_only_a_named_transport_starts_the_haiku_arm(self):
        for via in ("anthropic", "openrouter", "claude-cli", "baseten"):
            with self.subTest(via=via):
                self.assertEqual(len(self.call_with(JEV_COMPARE_HAIKU_VIA=via)), 1)
        for word in ("", "off", "0", "disabled"):
            with self.subTest(word=word):
                self.assertEqual(self.call_with(JEV_COMPARE_HAIKU_VIA=word), [])


class TheOutputCap(CompareCase):
    """The Haiku request's output cap holds the whole reply it asks for."""

    def ask(self, keys):
        job = {"payload": {"state": "s", "questions": {"q": {
            "type": "choice", "instructions": "which?",
            "criteria": {key: None for key in keys}}}}}
        env = {"JEV_COMPARE_ANTHROPIC_URL": self.base + "/v1/messages",
               "JEV_COMPARE_ANTHROPIC_KEY": "k", "JEV_COMPARE_TIMEOUT": "10"}
        return jev_compare.haiku_arm(job, env, "anthropic")

    def test_the_cap_fits_a_reply_over_every_option_kev_takes(self):
        keys = [f"option-number-{i:03d}" for i in range(255)]
        reply = json.dumps({"probabilities": {key: 0.0039 for key in keys}})
        Arm.reply = reply
        self.ask(keys)
        self.assertGreaterEqual(Arm.seen[0]["body"]["max_tokens"], len(reply.encode()))

    def test_a_reply_no_cap_could_hold_is_declined_before_the_call(self):
        keys = [f"{i:03d}-" + "x" * 600 for i in range(255)]
        with self.assertRaises(jev_compare.Declined) as caught:
            self.ask(keys)
        self.assertEqual(caught.exception.cause, "invalid")
        self.assertEqual(Arm.seen, [])


class TheHaikuOptionLimit(CompareCase):
    def test_a_choice_over_the_ledgers_option_limit_is_declined_before_any_call(self):
        job = {"payload": {"state": "s", "questions": {"q": {
            "type": "choice", "instructions": "which?",
            "criteria": {f"k{i:03d}": None for i in range(256)}}}}}
        env = {"JEV_COMPARE_ANTHROPIC_URL": self.base + "/v1/messages",
               "JEV_COMPARE_ANTHROPIC_KEY": "k", "JEV_COMPARE_TIMEOUT": "10"}
        Arm.reply = json.dumps({"probabilities": {f"k{i:03d}": 1 / 256 for i in range(256)}})
        with self.assertRaises(jev_compare.Declined) as caught:
            jev_compare.haiku_arm(job, env, "anthropic")
        self.assertEqual(caught.exception.cause, "invalid")
        self.assertEqual(Arm.seen, [])


class TheHaikuBatchLimit(CompareCase):
    """One Haiku request per question, so a batch is bounded before any call."""

    def ask(self, count):
        job = {"payload": {"state": "s", "questions": {
            f"q{i}": {"type": "noul", "instructions": "is it?"} for i in range(count)}}}
        env = {"JEV_COMPARE_ANTHROPIC_URL": self.base + "/v1/messages",
               "JEV_COMPARE_ANTHROPIC_KEY": "k", "JEV_COMPARE_TIMEOUT": "10"}
        return jev_compare.haiku_arm(job, env, "anthropic")

    def test_a_batch_at_the_limit_is_asked(self):
        self.ask(jev_compare.MAX_HAIKU_QUESTIONS)
        self.assertEqual(len(Arm.seen), jev_compare.MAX_HAIKU_QUESTIONS)

    def test_a_batch_over_the_limit_is_declined_before_any_call(self):
        with self.assertRaises(jev_compare.Declined) as caught:
            self.ask(getattr(jev_compare, "MAX_HAIKU_QUESTIONS", 8) + 1)
        self.assertEqual(caught.exception.cause, "invalid")
        self.assertEqual(Arm.seen, [])


class LocalOnly(CompareCase):
    """sd:2761: `--local-only` asks the local Kev and nothing else. Every arm
    is switched on and Jev is keyed, so a request that went anywhere but Kev
    would show in `Stub.seen` (Jev), `Arm.seen` (the Haiku path) or the
    mocked `start_arms`."""

    def env(self, **extra):
        settings = {"JEV_COMPARE_HAIKU_VIA": "anthropic",
                    "JEV_COMPARE_ANTHROPIC_KEY": "test-key"}
        settings.update(extra)
        return super().env(**settings)

    def run_local(self, argv, **extra):
        with unittest.mock.patch.object(jev, "start_arms") as arms:
            result = self.run_verbose(argv + ["--local-only"], **extra)
        self.assertEqual(arms.call_count, 0, "local-only started the arms")
        return result

    def kev_requests(self):
        return [s for s in Arm.seen if s["path"].endswith("/v1/systemone")]

    def test_it_asks_kev_alone_and_prints_its_answer(self):
        code, out, _ = self.run_local(["noul", "is it?", "--stage", "JEV_DOCS"])
        self.assertEqual((code, out), (0, "0.81\n"))
        self.assertEqual(Stub.seen, [])
        self.assertEqual([s["path"] for s in Arm.seen], ["/v1/systemone"])
        row = self.only()
        self.assertEqual((row["arm"], row["provider"], row["model"], row["stage"],
                          row["answer"], row["outcome"]),
                         ("kev", "local", "jaredpalmer/kev-4b@v1.0", "JEV_DOCS", "0.81", "ok"))

    def test_kev_gets_the_text_unredacted_and_no_typesafe_key(self):
        secret = "ghp_" + "a" * 36
        self.run_local(["noul", "is it?"], stdin=f"token {secret} here")
        sent, = self.kev_requests()
        self.assertIn(secret, sent["body"]["state"])
        self.assertEqual(sent["body"]["model"], "kev-latest")
        self.assertNotIn("authorization", sent["headers"])

    def test_a_rejected_kev_key_names_kev_api_key(self):
        Arm.status = 401
        code, _, err = self.run_local(["noul", "is it?"])
        self.assertEqual(code, 1)
        self.assertIn("KEV_API_KEY", err)
        self.assertNotIn("TYPESAFE_API_KEY", err)

    def test_no_typesafe_key_is_needed(self):
        code, out, _ = self.run_local(["noul", "is it?"], TYPESAFE_API_KEY="")
        self.assertEqual((code, out), (0, "0.81\n"))

    def test_a_stage_named_local_only_is_local_without_the_flag(self):
        with unittest.mock.patch.object(jev, "start_arms") as arms:
            code, out = self.run_main(["noul", "is it?", "--stage", "JEV_SECRET_SCAN"])
        self.assertEqual((code, out, arms.call_count, Stub.seen), (0, "0.81\n", 0, []))
        self.assertEqual(len(self.kev_requests()), 1)

    def test_a_kev_url_that_is_not_loopback_is_refused_before_sending(self):
        for url in ("http://198.51.100.7:8009/v1/systemone",
                    "http://localhost:8009/v1/systemone",
                    "http://127.0.0.1@example.test/v1/systemone",
                    "http://127.0.0.1:invalid/v1/systemone",
                    "file:///etc/hosts"):
            with self.subTest(url=url), \
                    unittest.mock.patch.object(jev.LOCAL_OPENER, "open") as send:
                code, out, err = self.run_local(["noul", "is it?", "--fallback", "keep"],
                                                JEV_COMPARE_KEV_URL=url)
                self.assertEqual((code, out), (0, "keep\n"))
                self.assertIn("not a loopback address", err)
                self.assertEqual((send.call_count, Stub.seen, Arm.seen), (0, [], []))
                self.assertEqual(self.rows()[-1]["cause"], "unavailable")

    def test_post_itself_refuses_a_remote_url(self):
        """The second check, at the one place a request is sent."""
        conf = dict(jev.settings({}), local=True, url="http://203.0.113.9/v1/systemone")
        send = unittest.mock.Mock()
        with self.assertRaises(jev.JevError):
            jev.post(conf, {"state": "x", "questions": {}}, opener=send)
        self.assertEqual(send.call_count, 0)

    def test_kev_down_is_a_decline_with_the_fallback(self):
        code, out, _ = self.run_local(["noul", "is it?", "--fallback", "keep"],
                                      JEV_COMPARE_KEV_URL="http://127.0.0.1:9/v1/systemone",
                                      JEV_RETRIES="0")
        self.assertEqual((code, out, Stub.seen), (0, "keep\n", []))
        row = self.only()
        self.assertEqual((row["arm"], row["outcome"], row["cause"]),
                         ("kev", "fallback", "unavailable"))

    def test_the_kill_switch_still_stops_it(self):
        self.write_switch("off")
        code, out, _ = self.run_local(["noul", "is it?", "--fallback", "keep"])
        self.assertEqual((code, out, Arm.seen), (0, "keep\n", []))

    def test_a_shadow_pair_is_recorded_against_the_old_answer(self):
        code, out, _ = self.run_local(["noul", "real?", "--gate", "0.5", "--shadow", "yes",
                                       "--stage", "JEV_SECRET_SCAN"])
        self.assertEqual((code, out), (0, "yes\n"))
        kev, baseline = self.rows()
        self.assertEqual((kev["arm"], kev["changed"], baseline["answer"]),
                         ("kev", "no", "1"))
        self.assertEqual(kev["pair"], baseline["pair"])

    def test_enabled_answers_for_the_local_path(self):
        def enabled(*extra, **env):
            return self.run_main(["enabled", "JEV_SECRET_SCAN", *extra], **env)[0]
        self.assertEqual(enabled("--local-only", TYPESAFE_API_KEY=""), 0)
        self.assertEqual(enabled(TYPESAFE_API_KEY=""), 0)
        self.assertEqual(enabled("--local-only", JEV_SECRET_SCAN="0"), 3)
        self.assertEqual(enabled("--local-only",
                                 JEV_COMPARE_KEV_URL="http://198.51.100.7/v1/systemone"), 3)
        self.assertEqual(enabled("--local-only",
                                 JEV_COMPARE_KEV_URL="http://127.0.0.1:invalid/v1/systemone"), 3)
        self.assertEqual(Arm.seen, [])


class TheRequestFile(unittest.TestCase):
    """The redacted request never outlives the spawn."""

    def test_no_request_file_is_left_when_the_child_never_reads_it(self):
        from unittest import mock
        folder = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, folder)
        started = []
        saved = (jev._EVENT, jev._ENV, tempfile.tempdir)
        self.addCleanup(lambda: (setattr(jev, "_EVENT", saved[0]),
                                 setattr(jev, "_ENV", saved[1]),
                                 setattr(tempfile, "tempdir", saved[2])))
        jev._EVENT = {"caller": "c", "stage": "S", "primitive": "noul", "questions": 1}
        jev._ENV = {"JEV_METER": "1", "JEV_COMPARE_KEV": "1",
                    "JEV_COMPARE_HAIKU_VIA": "off"}
        tempfile.tempdir = folder
        # A child that dies before it reads: Popen starts nothing.
        with mock.patch("subprocess.Popen",
                        side_effect=lambda argv, **kw: started.append((argv, kw))):
            jev.start_arms({"questions": {"q": {"type": "noul"}}})
        self.assertEqual(len(started), 1)
        self.assertEqual(os.listdir(folder), [])
        argv, kw = started[0]
        self.assertNotIn(folder, " ".join(argv))


class Shaping(unittest.TestCase):
    """The adapter's arithmetic, without a process."""

    def test_a_distribution_the_ledger_would_refuse_is_no_distribution(self):
        # Jev's choice and score take any number of options. A distribution
        # the ledger refuses would cost the whole Jev row, so it is omitted.
        from sd_db.judgment import MAX_OPTIONS
        self.assertEqual(jev.MAX_DISTRIBUTION, MAX_OPTIONS)
        many = {f"k{i}": None for i in range(MAX_OPTIONS + 1)}
        answer = {"probabilities": {key: 1 / len(many) for key in many}}
        self.assertIsNone(jev.distribution_of(answer, {"type": "choice", "criteria": many}))
        fits = {f"k{i}": None for i in range(MAX_OPTIONS)}
        answer = {"probabilities": {key: 1 / len(fits) for key in fits}}
        self.assertIsNotNone(jev.distribution_of(answer, {"type": "choice", "criteria": fits}))
        self.assertIsNone(jev.distribution_of(
            {"probabilities": {"a": 1.2, "b": 0.0}},
            {"type": "choice", "criteria": {"a": None, "b": None}}))

    def test_a_distribution_that_is_not_finite_is_no_distribution(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                self.assertIsNone(jev.distribution_of(
                    {"probabilities": {"a": bad, "b": 0.5}},
                    {"type": "choice", "criteria": {"a": None, "b": None}}))

    def test_a_price_or_timeout_that_is_not_finite_is_the_default(self):
        for text in ("inf", "Infinity", "1e309", "nan", "-inf"):
            with self.subTest(text=text):
                self.assertEqual(jev_compare.price({"P": text}, "P", 1.0), 1.0)
                self.assertEqual(jev_compare.timeout_of({"JEV_COMPARE_TIMEOUT": text}),
                                 jev_compare.DEFAULT_TIMEOUT)

    def test_confidences_mirror_the_reference_adapter(self):
        self.assertAlmostEqual(jev_compare.choice_confidence([0.47, 0.28, 0.25]),
                               (0.47 - 1 / 3) / (2 / 3))
        self.assertEqual(jev_compare.choice_confidence([1.0]), 1.0)
        self.assertEqual(jev_compare.score_confidence([0.0, 1.0, 0.0]), 1.0)
        self.assertAlmostEqual(jev_compare.score_confidence([1 / 3] * 3), 0.0)

    def test_a_distribution_is_clipped_and_normalised(self):
        self.assertEqual(jev_compare.normalised([2.0, 2.0]), [0.5, 0.5])
        self.assertEqual(jev_compare.normalised([0.0, 0.0]), [0.5, 0.5])
        self.assertEqual(jev_compare.normalised([-1.0, 1.0]), [0.0, 1.0])

    def test_the_jev_row_distribution_is_in_the_callers_order(self):
        answer = {"probabilities": {"b": 0.25, "a": 0.75}}
        self.assertEqual(jev.distribution_of(answer, {"type": "choice",
                                                      "criteria": {"a": None, "b": None}}),
                         "0.7500,0.2500")
        self.assertIsNone(jev.distribution_of({"probabilities": {"a": "x"}},
                                              {"type": "choice", "criteria": {"a": None}}))


if __name__ == "__main__":
    unittest.main()
