"""The suite runs against a stub of the API on loopback, not against TypeSafe.

That is deliberate twice over. A suite that called the real endpoint would
spend tokens on every `make check` and go red when someone else's network did,
and `tests/ci-native.sh` treats a skip as a failure, so "skip when offline" is not an
option this repo has. The stub also lets a test assert the exact payload that
went out, which is the half of the contract a live call cannot show.
"""

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jev  # noqa: E402

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "jev.sh"


class Stub(BaseHTTPRequestHandler):
    """Answers every question in the request, in the shape the API uses."""

    status = 200
    throttle_first = 0          # how many 429s to serve before answering
    noul_value = 0.97
    score_value = 2.0
    # Which criterion the stub picks. None means the first one the caller
    # named, which is what every test that does not care expects; a test that
    # does care sets it, including to a key the caller never offered.
    choice_value = None
    confidence = 1.0
    seen = []
    retry_after = None
    # The counts the response has always carried. Settable, so a test can
    # prove they were read out of this body rather than defaulted to
    # something that happens to match.
    input_tokens = 1
    output_tokens = 1
    # A 200 that carries no answers: the endpoint is up and what came back is
    # unusable, which is its own outcome class.
    drop_answers = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        Stub.seen.append({"payload": payload,
                          "auth": self.headers.get("Authorization", "")})
        if Stub.throttle_first > 0:
            Stub.throttle_first -= 1
            self.send_response(429)
            if Stub.retry_after is not None:
                self.send_header("Retry-After", str(Stub.retry_after))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if Stub.status != 200:
            body = b'{"error":"refused"}'
            self.send_response(Stub.status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        answers = {}
        for qid, question in payload["questions"].items():
            kind = question["type"]
            if kind == "noul":
                answers[qid] = {"type": "noul", "noul": Stub.noul_value}
            elif kind == "choice":
                keys = list(question["criteria"])
                chosen = Stub.choice_value or keys[0]
                answers[qid] = {"type": "choice", "choice": chosen,
                                "confidence": Stub.confidence,
                                "probabilities": {k: 0.0 for k in keys}}
            else:
                answers[qid] = {"type": "score", "score": Stub.score_value,
                                "confidence": Stub.confidence}
        if Stub.drop_answers:
            answers = {}
        body = json.dumps({"model": "jev-stub", "answers": answers,
                           "usage": {"input_tokens": Stub.input_tokens,
                                     "output_tokens": Stub.output_tokens}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StubServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d/v1/systemone" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.switch = str(Path(tempfile.mkdtemp()) / "enabled")
        # The config root is pinned too, so the operator's own
        # privacy-patterns never shape what this suite sends.
        self.config = Path(tempfile.mkdtemp())
        Stub.status = 200
        Stub.throttle_first = 0
        Stub.noul_value = 0.97
        Stub.score_value = 2.0
        Stub.choice_value = None
        Stub.confidence = 1.0
        Stub.retry_after = None
        Stub.input_tokens = 1
        Stub.output_tokens = 1
        Stub.drop_answers = False
        Stub.seen = []

    def env(self, **extra):
        # JEV_FLAG_FILE is pinned to a temp path in every test. Without it the
        # suite would read the operator's own switch out of ~/.config and go
        # red on the machine where someone had run `jev off`.
        # JEV_METER is pinned off for the same reason JEV_FLAG_FILE is pinned
        # to a temp path: without it every call in this suite would write a
        # `judgment` row into the operator's own sd.db. The metering tests
        # below turn it back on against a database of their own.
        env = {"TYPESAFE_API_KEY": "test-key", "JEV_URL": self.url,
               "JEV_RETRIES": "3", "JEV_TIMEOUT": "10",
               "JEV_METER": "0",
               "JEV_FLAG_FILE": self.switch,
               "SYSTEM_TOOLS_CONFIG": str(self.config)}
        env.update(extra)
        return env

    def write_switch(self, word):
        Path(self.switch).write_text(word)

    def run_main(self, argv, stdin="a sentence", **extra):
        code, out, _err = self.run_verbose(argv, stdin, **extra)
        return code, out

    def run_verbose(self, argv, stdin="a sentence", **extra):
        saved = sys.stdin
        err = io.StringIO()
        with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as fake:
            fake.write(stdin)
            fake.seek(0)
            sys.stdin = fake
            try:
                with contextlib.redirect_stderr(err):
                    code = jev.main(argv, out=out, env=self.env(**extra),
                                    sleep=lambda _s: None)
            finally:
                sys.stdin = saved
            out.seek(0)
            return code, out.read(), err.getvalue()


class TestCriteria(unittest.TestCase):
    def test_bare_names_become_keys_with_no_description(self):
        self.assertEqual(jev.parse_mapping("a, b ,c"),
                         {"a": None, "b": None, "c": None})

    def test_first_equals_splits_name_from_description(self):
        self.assertEqual(jev.parse_mapping("fix=corrects x=y"),
                         {"fix": "corrects x=y"})

    def test_at_file_carries_a_json_object(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"a": "one, with a comma"}, fh)
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(jev.parse_mapping("@" + fh.name),
                         {"a": "one, with a comma"})

    def test_a_nameless_entry_is_an_error(self):
        with self.assertRaises(jev.JevError):
            jev.parse_mapping("a,=orphan")

    def test_empty_criteria_is_an_error(self):
        with self.assertRaises(jev.JevError):
            jev.parse_mapping(" , ")

    def test_levels_keep_their_order(self):
        self.assertEqual(jev.parse_levels("can wait,this week,today"),
                         ["can wait", "this week", "today"])

    def test_levels_at_file_must_be_an_array(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"not": "a list"}, fh)
        self.addCleanup(os.unlink, fh.name)
        with self.assertRaises(jev.JevError):
            jev.parse_levels("@" + fh.name)


class TestState(unittest.TestCase):
    def write(self, text):
        fh = tempfile.NamedTemporaryFile("w", delete=False)
        fh.write(text)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_text_is_sent_as_text_even_when_it_looks_like_json(self):
        # The guess this avoids: a state file holding only `2026` is a string
        # the caller wrote, not the number the parser would have made of it.
        self.assertEqual(jev.load_state(self.write("2026"), "text"), "2026")

    def test_json_is_parsed(self):
        self.assertEqual(jev.load_state(self.write('{"a": 1}'), "json"), {"a": 1})

    def test_bad_json_names_the_file(self):
        path = self.write("{oops")
        with self.assertRaises(jev.JevError) as caught:
            jev.load_state(path, "json")
        self.assertIn(path, str(caught.exception))


class TestBackoff(unittest.TestCase):
    def test_retry_after_wins_when_it_is_a_number(self):
        self.assertEqual(jev.retry_after({"Retry-After": "7"}, 0), 7.0)

    def test_a_date_shaped_retry_after_falls_back_to_doubling(self):
        headers = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        self.assertEqual(jev.retry_after(headers, 1), jev.BACKOFF * 2)

    def test_backoff_doubles_without_a_header(self):
        self.assertEqual(jev.retry_after({}, 2), jev.BACKOFF * 4)


class TestVerbs(StubServer):
    def test_noul_prints_only_the_probability(self):
        code, out = self.run_main(["noul", "Is this urgent?"])
        self.assertEqual((code, out), (0, "0.97\n"))

    def test_gate_prints_yes_at_or_above_the_threshold(self):
        self.assertEqual(self.run_main(["noul", "q", "--gate", "0.97"])[1], "yes\n")

    def test_gate_prints_no_below_the_threshold(self):
        Stub.noul_value = 0.4
        self.assertEqual(self.run_main(["noul", "q", "--gate", "0.8"])[1], "no\n")

    def test_json_prints_the_whole_answer(self):
        code, out = self.run_main(["noul", "q", "--json"])
        self.assertEqual(json.loads(out), {"type": "noul", "noul": 0.97})

    def test_choice_prints_the_chosen_name(self):
        code, out = self.run_main(["choice", "q", "--criteria", "fix,feat"])
        self.assertEqual((code, out), (0, "fix\n"))

    def test_choice_says_unsure_under_the_confidence_floor(self):
        Stub.confidence = 0.3
        out = self.run_main(["choice", "q", "--criteria", "fix,feat",
                             "--unsure-below", "0.6"])[1]
        self.assertEqual(out, "unsure\n")

    def test_choice_answers_normally_above_the_floor(self):
        out = self.run_main(["choice", "q", "--criteria", "fix,feat",
                             "--unsure-below", "0.6"])[1]
        self.assertEqual(out, "fix\n")

    def test_score_prints_the_number(self):
        self.assertEqual(self.run_main(["score", "q", "--levels", "a,b,c"])[1], "2.0\n")

    def test_ask_sends_every_question_in_one_request(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"one": {"type": "noul", "instructions": "a"},
                       "two": {"type": "choice", "instructions": "b",
                               "criteria": {"x": None}}}, fh)
        self.addCleanup(os.unlink, fh.name)
        code, out = self.run_main(["ask", "--questions", fh.name])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(out)["answers"]), ["one", "two"])
        self.assertEqual(len(Stub.seen), 1)

    def test_the_request_carries_state_model_and_the_bearer_key(self):
        self.run_main(["noul", "Is this urgent?"], stdin="payouts failing")
        sent = Stub.seen[0]
        self.assertEqual(sent["payload"]["state"], "payouts failing")
        self.assertEqual(sent["payload"]["model"], "jev-latest")
        self.assertEqual(sent["auth"], "Bearer test-key")

    def test_model_flag_overrides_the_default(self):
        self.run_main(["noul", "q", "--model", "jev-1.13.0"])
        self.assertEqual(Stub.seen[0]["payload"]["model"], "jev-1.13.0")

    def test_id_names_the_answer_the_caller_reads(self):
        self.run_main(["noul", "q", "--id", "urgent"])
        self.assertEqual(list(Stub.seen[0]["payload"]["questions"]), ["urgent"])


class TestFailures(StubServer):
    def test_a_throttled_request_is_retried_and_then_answered(self):
        Stub.throttle_first = 2
        code, out = self.run_main(["noul", "q"])
        self.assertEqual((code, out), (0, "0.97\n"))
        self.assertEqual(len(Stub.seen), 3)

    def test_retries_run_out_and_the_verb_fails(self):
        Stub.throttle_first = 99
        code, _ = self.run_main(["noul", "q"], JEV_RETRIES="1")
        self.assertEqual(code, 1)
        self.assertEqual(len(Stub.seen), 2)

    def test_a_rejected_key_is_not_retried(self):
        Stub.status = 401
        code, _ = self.run_main(["noul", "q"])
        self.assertEqual(code, 1)
        self.assertEqual(len(Stub.seen), 1)

    def test_a_422_is_not_retried_because_the_request_is_the_problem(self):
        Stub.status = 422
        code, _ = self.run_main(["noul", "q"])
        self.assertEqual(code, 1)
        self.assertEqual(len(Stub.seen), 1)

    def test_no_key_exits_three_without_calling_the_api(self):
        code, _ = self.run_main(["noul", "q"], TYPESAFE_API_KEY="")
        self.assertEqual(code, 3)
        self.assertEqual(Stub.seen, [])

    def test_bad_state_json_fails_without_a_traceback(self):
        code, _ = self.run_main(["noul", "q", "--state-format", "json"], stdin="{oops")
        self.assertEqual(code, 1)


class TestStatus(StubServer):
    def test_status_is_zero_when_the_api_answers(self):
        code, out = self.run_main(["status"])
        self.assertEqual(code, 0)
        self.assertIn("jev-stub", out)

    def test_status_is_three_without_a_key(self):
        code, out = self.run_main(["status"], TYPESAFE_API_KEY="")
        self.assertEqual(code, 3)
        self.assertIn("TYPESAFE_API_KEY", out)
        self.assertEqual(Stub.seen, [])

    def test_status_is_one_when_configured_and_broken(self):
        # The distinction local-health-check reads: 3 is "not this machine's
        # job", 1 is "it is this machine's job and it is failing".
        Stub.status = 503
        code, out = self.run_main(["status"])
        self.assertEqual(code, 1)
        self.assertIn("unreachable or refusing", out)

    def test_status_probes_once_and_does_not_retry(self):
        Stub.throttle_first = 1
        self.assertEqual(self.run_main(["status"])[0], 1)
        self.assertEqual(len(Stub.seen), 1)


class TestEntrypoint(StubServer):
    def link_dir(self, target=None):
        """A directory holding a `jev` symlink, for the front of PATH."""
        where = Path(tempfile.mkdtemp())
        (where / "jev").symlink_to(target or ENTRYPOINT)
        return where

    def sh(self, args, env=None, cwd=None, path_first=None):
        environ = dict(os.environ)
        environ.pop("TYPESAFE_API_KEY", None)
        environ.update(self.env())
        environ.update(env or {})
        environ["JEV_FLAG_FILE"] = self.switch
        # PATH is pinned, because `status` now resolves `jev` through it. The
        # operator's own ~/bin/common/jev points at their primary checkout,
        # so an unpinned run reports DIFFERS and the suite goes red on the
        # machine it is developed on -- which is exactly what it did.
        first = self.link_dir() if path_first is None else path_first
        if first is not False:
            environ["PATH"] = f"{first}{os.pathsep}{environ.get('PATH', '')}"
        return subprocess.run([str(ENTRYPOINT)] + args, capture_output=True,
                              text=True, env=environ, cwd=cwd)

    def test_no_argument_prints_usage_to_stderr_and_exits_one(self):
        done = self.sh([])
        self.assertEqual(done.returncode, 1)
        self.assertIn("usage:", done.stderr)
        self.assertEqual(done.stdout, "")

    def test_help_exits_zero_and_names_every_subcommand(self):
        for flag in ("-h", "--help", "help"):
            done = self.sh([flag])
            self.assertEqual(done.returncode, 0, flag)
            for verb in ("ask", "noul", "choice", "score", "status", "test"):
                self.assertIn(verb, done.stdout)

    def test_help_declares_itself_to_the_health_sweep(self):
        # local-health-check runs `status` on the tools whose help says this,
        # and there is no list of tools anywhere else to add one to.
        self.assertIn("local-health-check", self.sh(["help"]).stdout)

    def test_an_unknown_verb_is_a_usage_error(self):
        self.assertEqual(self.sh(["explain"]).returncode, 1)

    def test_status_runs_through_the_entrypoint(self):
        done = self.sh(["status"])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("path ok", done.stdout)

    def test_status_is_one_when_jev_is_not_on_path(self):
        # 2026-09-20: every gate on, the switch on, the key good, the probe
        # 0.98 -- and `sd-docs-lint` skipped its Jev reading because
        # `local-bin-links install` had never run. `status` said ok. This is
        # that machine, and it must not say ok.
        # The real PATH minus every directory holding a `jev`. An empty PATH
        # would be a different test: the shell loses `dirname` and python3,
        # and the script dies at 127 before it can report anything.
        clean = os.pathsep.join(
            d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and not (Path(d) / "jev").exists()
        )
        done = self.sh(["status"], env={"PATH": clean}, path_first=False)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("PATH MISSING", done.stdout)
        self.assertIn("bin-links.sh install", done.stdout)
        # local-health-check quotes the FIRST line as the finding. An `ok`
        # line above this one made a failure read as a success.
        self.assertTrue(done.stdout.splitlines()[0].startswith("jev: PATH MISSING"),
                        done.stdout)

    def test_status_is_one_when_path_finds_another_checkout(self):
        other = Path(tempfile.mkdtemp()) / "jev.sh"
        other.write_text("#!/bin/sh\nexit 0\n")
        other.chmod(0o755)
        done = self.sh(["status"], path_first=self.link_dir(other))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("PATH DIFFERS", done.stdout)

    def test_a_probe_failure_is_reported_before_the_path(self):
        # Order matters: an unreachable endpoint is the bigger fact, and a
        # machine that cannot answer at all should not be told about PATH.
        Stub.status = 503
        done = self.sh(["status"])
        self.assertEqual(done.returncode, 1)
        self.assertIn("unreachable or refusing", done.stdout)
        self.assertNotIn("PATH", done.stdout)


class TestEnvFile(StubServer):
    """A copy of the tool, reached through a symlink, with its .env in the
    config directory the copy resolves."""

    def copy(self, env_text):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        folder = home / "local-jev"
        folder.mkdir()
        for name in ("jev.sh", "jev.py"):
            (folder / name).write_bytes((FOLDER / name).read_bytes())
        (folder / "jev.sh").chmod(0o755)
        (home / "lib").mkdir()
        for name in ("config.sh", "system_tools_config.py"):
            (home / "lib" / name).write_bytes((FOLDER.parent / "lib" / name).read_bytes())
        self.config = home / "config"
        (self.config / "jev").mkdir(parents=True)
        (self.config / "jev" / ".env").write_text(env_text)
        bindir = home / "bin"
        bindir.mkdir()
        (bindir / "jev").symlink_to(folder / "jev.sh")
        return bindir / "jev"

    def run_link(self, link, args, env):
        environ = dict(os.environ)
        environ.pop("TYPESAFE_API_KEY", None)
        environ.pop("JEV_MODEL", None)
        environ["JEV_URL"] = self.url
        environ["JEV_FLAG_FILE"] = self.switch
        environ["JEV_METER"] = "0"
        environ["SYSTEM_TOOLS_CONFIG"] = str(self.config)
        environ.update(env)
        return subprocess.run([str(link)] + args, capture_output=True,
                              text=True, env=environ, input="a sentence")

    def test_the_config_env_is_found_through_the_symlink(self):
        link = self.copy("TYPESAFE_API_KEY=from-env-file\n")
        done = self.run_link(link, ["noul", "q"], {})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Stub.seen[-1]["auth"], "Bearer from-env-file")

    def test_an_exported_value_beats_the_env_file(self):
        link = self.copy("TYPESAFE_API_KEY=from-env-file\nJEV_MODEL=from-env-file\n")
        done = self.run_link(link, ["noul", "q"],
                             {"TYPESAFE_API_KEY": "exported", "JEV_MODEL": "exported"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Stub.seen[-1]["auth"], "Bearer exported")
        self.assertEqual(Stub.seen[-1]["payload"]["model"], "exported")

    def test_without_a_key_anywhere_the_link_exits_three(self):
        link = self.copy("#JEV_MODEL=unset\n")
        self.assertEqual(self.run_link(link, ["noul", "q"], {}).returncode, 3)

    def test_status_without_a_key_names_the_config_path(self):
        link = self.copy("#JEV_MODEL=unset\n")
        done = self.run_link(link, ["status"], {})
        self.assertEqual(done.returncode, 3)
        self.assertIn("copy local-jev/.env.example to %s" % (self.config / "jev" / ".env"),
                      done.stdout + done.stderr)


class TestSwitch(StubServer):
    """Jev is experimental. Nothing may depend on it, and this is where that
    is a test rather than a promise."""

    def test_an_absent_switch_file_means_enabled(self):
        self.assertEqual(self.run_main(["enabled"])[0], 0)

    def test_the_switch_file_turns_it_off(self):
        self.write_switch("off\n")
        self.assertEqual(self.run_main(["enabled"])[0], 3)

    def test_an_unreadable_switch_is_not_an_outage(self):
        # A switch nobody set, or one that says something unrecognised, leaves
        # the tool usable. A switch is not a health check.
        self.write_switch("perhaps")
        self.assertEqual(self.run_main(["enabled"])[0], 0)

    def test_the_variable_beats_the_file_both_ways(self):
        self.write_switch("off\n")
        self.assertEqual(self.run_main(["enabled"], JEV_ENABLED="1")[0], 0)
        self.write_switch("on\n")
        self.assertEqual(self.run_main(["enabled"], JEV_ENABLED="0")[0], 3)

    def test_no_key_reads_as_off_not_as_broken(self):
        self.assertEqual(self.run_main(["enabled"], TYPESAFE_API_KEY="")[0], 3)

    def test_why_names_the_reason(self):
        self.write_switch("off")
        self.assertIn("switched off", self.run_main(["enabled", "--why"])[1])

    def test_enabled_calls_nothing(self):
        self.run_main(["enabled"])
        self.assertEqual(Stub.seen, [])

    # --- the per-caller stages --------------------------------------------
    #
    # Every integration was an opt-in at first -- `JEV_ADVERSARIAL_GATE=1` and one
    # per caller beside it -- and a per-caller switch that defaults to off
    # makes each one added after it silently never run, which is the failure
    # the fleet switch above was written to avoid, repeated once per caller.
    # So a stage nobody set is on, and its variable only switches it off.
    # `enabled STAGE` is where that reading lives, so no caller carries a copy.
    # The count is deliberately absent: it read "seven like it" while a ninth
    # caller was being found. KNOWN_CALLERS in tests/test_jev_contract.py is
    # the enumeration, and that suite fails when a folder joins or leaves.

    def test_a_stage_nobody_set_is_on(self):
        self.assertEqual(self.run_main(["enabled", "JEV_ADVERSARIAL_GATE"])[0], 0)

    def test_a_stage_switched_off_exits_three(self):
        self.assertEqual(
            self.run_main(["enabled", "JEV_ADVERSARIAL_GATE"], JEV_ADVERSARIAL_GATE="0")[0], 3)

    def test_the_words_that_switch_a_stage_off_are_the_switch_file_s_words(self):
        for word in jev.FLAG_OFF + tuple(w.upper() for w in jev.FLAG_OFF):
            with self.subTest(word=word):
                self.assertEqual(
                    self.run_main(["enabled", "JEV_ADVERSARIAL_GATE"],
                                  JEV_ADVERSARIAL_GATE=word)[0], 3)

    def test_any_other_word_leaves_a_stage_on(self):
        # A switch set to something unrecognised is not an outage, exactly as
        # `read_flag` treats an unreadable file.
        for word in ("", "1", "on", "yes", "true", "perhaps"):
            with self.subTest(word=word):
                self.assertEqual(
                    self.run_main(["enabled", "JEV_ADVERSARIAL_GATE"],
                                  JEV_ADVERSARIAL_GATE=word)[0], 0)

    def test_a_stage_cannot_switch_itself_on_against_the_machine(self):
        # The stage variable only ever subtracts. A machine with no key, or
        # with `jev off`, answers 3 whatever a caller's own variable says.
        self.write_switch("off\n")
        self.assertEqual(
            self.run_main(["enabled", "JEV_ADVERSARIAL_GATE"], JEV_ADVERSARIAL_GATE="1")[0], 3)
        self.assertEqual(
            self.run_main(["enabled", "JEV_ADVERSARIAL_GATE"],
                          TYPESAFE_API_KEY="", JEV_ADVERSARIAL_GATE="1")[0], 3)

    def test_why_names_the_stage_that_switched_it_off(self):
        # Which half declined is a question for a human reading --why, and the
        # answer has to name the variable or it is not actionable.
        out = self.run_main(["enabled", "JEV_ADVERSARIAL_GATE", "--why"],
                            JEV_ADVERSARIAL_GATE="off")[1]
        self.assertIn("JEV_ADVERSARIAL_GATE", out)

    def test_a_stage_name_calls_nothing_either(self):
        self.run_main(["enabled", "JEV_ADVERSARIAL_GATE"])
        self.assertEqual(Stub.seen, [])

    def test_on_and_off_write_the_file(self):
        self.assertEqual(self.run_main(["off"])[0], 0)
        self.assertEqual(Path(self.switch).read_text().strip(), "off")
        self.assertEqual(self.run_main(["enabled"])[0], 3)
        self.run_main(["on"])
        self.assertEqual(self.run_main(["enabled"])[0], 0)

    def test_off_stops_a_verb_before_the_network(self):
        self.write_switch("off")
        code, _out, err = self.run_verbose(["noul", "q"])
        self.assertEqual(code, 3)
        self.assertEqual(Stub.seen, [])
        self.assertIn("switched off", err)

    def test_status_is_three_and_silent_when_switched_off(self):
        self.write_switch("off")
        code, out = self.run_main(["status"])
        self.assertEqual(code, 3)
        self.assertIn("switched off", out)
        self.assertEqual(Stub.seen, [])


class TestFallback(StubServer):
    def test_a_switched_off_call_returns_the_callers_own_answer(self):
        self.write_switch("off")
        code, out, err = self.run_verbose(["noul", "q", "--fallback", "yes"])
        self.assertEqual((code, out), (0, "yes\n"))
        self.assertIn("switched off", err)

    def test_an_unkeyed_machine_behaves_exactly_like_a_switched_off_one(self):
        off = self.run_verbose(["noul", "q", "--fallback", "yes"],
                               JEV_ENABLED="0")
        unkeyed = self.run_verbose(["noul", "q", "--fallback", "yes"],
                                   TYPESAFE_API_KEY="")
        self.assertEqual(off[0], unkeyed[0])
        self.assertEqual(off[1], unkeyed[1])

    def test_a_failing_api_falls_back_rather_than_failing_the_job(self):
        Stub.status = 503
        code, out, err = self.run_verbose(["noul", "q", "--fallback", "no"],
                                          JEV_RETRIES="0")
        self.assertEqual((code, out), (0, "no\n"))
        self.assertIn("503", err)

    def test_the_fallback_is_never_silent(self):
        # The failure this shape exists to avoid is a lane that quietly stops
        # running, so the reason reaches stderr on every degraded call.
        self.write_switch("off")
        for args in (["noul", "q"], ["noul", "q", "--fallback", "yes"]):
            self.assertNotEqual(self.run_verbose(args)[2], "")

    def test_without_a_fallback_a_disabled_call_exits_three(self):
        self.write_switch("off")
        self.assertEqual(self.run_verbose(["choice", "q", "--criteria", "a,b"])[0], 3)

    def test_choice_and_score_take_a_fallback_too(self):
        self.write_switch("off")
        self.assertEqual(self.run_main(["choice", "q", "--criteria", "a,b",
                                        "--fallback", "a"])[1], "a\n")
        self.assertEqual(self.run_main(["score", "q", "--levels", "a,b",
                                        "--fallback", "0"])[1], "0\n")

    def test_a_batch_takes_a_fallback_too(self):
        self.write_switch("off")
        questions = json.dumps({"a": {"type": "noul", "instructions": "q"}})
        path = os.path.join(os.path.dirname(self.switch), "questions.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(questions)
        code, out, err = self.run_verbose(["ask", "--questions", path, "--fallback", "{}"])
        self.assertEqual((code, out), (0, "{}\n"))
        self.assertIn("switched off", err)

    def test_every_verb_that_calls_out_takes_a_fallback(self):
        """Read off the parser, because the list above missed one for a week.

        The two tests above name their verbs, which is an inventory, and an
        inventory drifts: `ask` had no `--fallback` at all until a caller
        reached for one. It is the verb built for a batch -- the case where a
        lane has the most questions and can least afford any of them taking
        it down -- so it was the worst one to be missing it, and nothing here
        could say so. This enumerates the parser instead. A verb added later
        either takes a fallback or is named in `local` below, which is the
        set that never calls out; `status` is among them because it is the
        diagnostic that reports the outage, and a fallback would hide the
        thing it reports, and `record` because the whole verb is one row about
        work that already happened somewhere else.
        """
        local = {"status", "enabled", "on", "off", "record"}
        parser = jev.build_parser()
        subs = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        for verb, sub in subs.choices.items():
            options = {name for action in sub._actions for name in action.option_strings}
            with self.subTest(verb=verb):
                self.assertEqual(verb in local, "--fallback" not in options)

    def test_a_working_call_ignores_the_fallback(self):
        self.assertEqual(self.run_main(["noul", "q", "--fallback", "no"])[1], "0.97\n")


class TestUnconfigured(StubServer):
    """A machine that cannot answer, for every reason but the switch.

    One exit code and three reasons, which is the point: a caller that has to
    tell them apart to stay correct is a caller that will eventually not.
    """

    def test_the_env_example_placeholder_is_not_a_key(self):
        # `.env.example` ships `change-me`, so a machine where someone copied
        # it and stopped is a machine that will exist. Its key is a non-empty
        # string, and the check that only asked whether the key was empty read
        # that machine as configured.
        code, out, err = self.run_verbose(["enabled", "--why"],
                                          TYPESAFE_API_KEY="change-me")
        self.assertEqual(code, 3)
        self.assertIn("placeholder", out)

    def test_a_placeholder_key_sends_nothing(self):
        # Unconfigured, not broken: `Bearer change-me` earns a 401 and
        # `status` would report 1, which is a nightly health-check finding
        # saying the service is down when nobody has configured it yet.
        code, _out, _err = self.run_verbose(["status"], TYPESAFE_API_KEY="change-me")
        self.assertEqual(code, 3)
        self.assertEqual(Stub.seen, [])

    def test_the_placeholders_are_matched_without_regard_to_case(self):
        for spelling in ("CHANGE-ME", "Change_Me", " changeme "):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    self.run_verbose(["enabled"], TYPESAFE_API_KEY=spelling)[0], 3)

    def test_a_real_key_that_merely_looks_odd_still_works(self):
        self.assertEqual(self.run_main(["noul", "q"], TYPESAFE_API_KEY="changed-me")[1],
                         "0.97\n")


class TestMalformedSettings(StubServer):
    """A typo in a `.env` number, which used to end the process.

    `settings` is read before `main` reaches its error handling and before the
    switch is consulted, so `float("thirty")` was an uncaught traceback out of
    every verb -- `enabled` included, whose whole promise is that it costs
    nothing and answers 0 or 3. The one call a caller makes to find out
    whether it should call at all was the one a `.env` typo could take down.
    """

    def test_a_malformed_timeout_answers_three_rather_than_raising(self):
        code, out, err = self.run_verbose(["enabled", "--why"], JEV_TIMEOUT="thirty")
        self.assertEqual(code, 3)
        self.assertIn("JEV_TIMEOUT", out)
        self.assertNotIn("Traceback", err)

    def test_a_malformed_retry_count_answers_three_too(self):
        code, out, _err = self.run_verbose(["enabled", "--why"], JEV_RETRIES="lots")
        self.assertEqual(code, 3)
        self.assertIn("JEV_RETRIES", out)

    def test_a_caller_with_a_fallback_still_gets_its_fallback(self):
        code, out, err = self.run_verbose(["noul", "q", "--fallback", "yes"],
                                          JEV_TIMEOUT="thirty")
        self.assertEqual((code, out), (0, "yes\n"))
        self.assertIn("JEV_TIMEOUT", err)

    def test_a_malformed_number_sends_nothing(self):
        self.run_verbose(["noul", "q", "--fallback", "yes"], JEV_RETRIES="lots")
        self.assertEqual(Stub.seen, [])

    def test_an_empty_setting_is_the_default_and_not_a_problem(self):
        self.assertEqual(self.run_main(["noul", "q"], JEV_TIMEOUT="")[1], "0.97\n")

    def test_the_switch_can_still_be_set_on_a_misconfigured_machine(self):
        # `on` and `off` consult no setting, and must not: the remedy for a
        # broken machine cannot be unreachable because the machine is broken.
        self.assertEqual(self.run_verbose(["off"], JEV_TIMEOUT="thirty")[0], 0)
        self.assertEqual(Path(self.switch).read_text().strip(), "off")


class TestStdinIsReadOnce(StubServer):
    """Two options defaulting to stdin, which used to send an empty state.

    `--questions -` consumed stdin and the default `--state -` then read EOF,
    so the request went out with an empty state and came back answered: a
    judgment about nothing, reported as a judgment. Found by writing the first
    caller that batched questions.
    """

    def questions_file(self):
        path = os.path.join(os.path.dirname(self.switch), "q.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"a": {"type": "noul", "instructions": "q"}}, handle)
        return path

    def test_two_stdin_readers_are_refused(self):
        code, _out, err = self.run_verbose(["ask", "--questions", "-"],
                                           stdin='{"a": {"type": "noul", "instructions": "q"}}')
        self.assertEqual(code, 1)
        self.assertIn("stdin can only be read once", err)

    def test_the_refusal_sends_nothing(self):
        self.run_verbose(["ask", "--questions", "-"],
                         stdin='{"a": {"type": "noul", "instructions": "q"}}')
        self.assertEqual(Stub.seen, [])

    def test_a_file_for_one_of_them_is_the_remedy_and_it_works(self):
        code, out, _err = self.run_verbose(
            ["ask", "--questions", self.questions_file(), "--state", "-"])
        self.assertEqual(code, 0)
        self.assertIn('"answers"', out)

    def test_the_refusal_honours_a_fallback(self):
        code, out, err = self.run_verbose(
            ["ask", "--questions", "-", "--fallback", "{}"],
            stdin='{"a": {"type": "noul", "instructions": "q"}}')
        self.assertEqual((code, out), (0, "{}\n"))
        self.assertIn("stdin", err)

    def test_one_invocation_does_not_spend_the_next_ones_stdin(self):
        # The latch is process-wide, so `main` clears it. Without that, this
        # suite would pass once and fail on every test after the first.
        self.assertEqual(self.run_main(["noul", "q"])[1], "0.97\n")
        self.assertEqual(self.run_main(["noul", "q"])[1], "0.97\n")


if __name__ == "__main__":
    unittest.main()


class TestRedaction(StubServer):
    """Nothing that looks like a credential, and nothing the operator's
    privacy-patterns name, reaches the endpoint; a pattern file that cannot
    be used sends nothing at all."""

    # Built at run time, so this file holds no credential-shaped literal for
    # a scanner to find.
    GITHUB = "ghp_" + "A1b2C3d4" * 4
    AWS = "AKIA" + "ABCDEFGH23456789"

    def sent(self):
        return json.dumps(Stub.seen[-1]["payload"])

    def patterns(self, *lines):
        (self.config / "privacy-patterns").write_text("\n".join(lines) + "\n")

    def test_credential_shapes_are_replaced_in_the_state(self):
        _code, _out, err = self.run_verbose(
            ["noul", "q"], stdin=f"push with {self.GITHUB} and {self.AWS}")
        self.assertNotIn(self.GITHUB, self.sent())
        self.assertNotIn(self.AWS, self.sent())
        self.assertIn(jev.REDACTED, self.sent())
        self.assertIn("redacted 2 span(s)", err)

    def test_a_privacy_pattern_is_replaced_in_state_and_questions(self):
        self.patterns("# a comment", "", "Example[[:space:]]Person")
        self.run_main(["noul", "Did Example Person write this?"],
                      stdin="authored by Example Person")
        self.assertNotIn("Example Person", self.sent())
        self.assertEqual(self.sent().count(jev.REDACTED), 2)

    def test_privacy_patterns_are_case_sensitive_as_grep_reads_them(self):
        self.patterns("Example Person")
        self.run_main(["noul", "q"], stdin="example person")
        self.assertIn("example person", self.sent())

    def test_json_state_is_redacted_field_by_field(self):
        self.patterns("secret-host")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"host": "secret-host.example.test", "n": 3,
                       "paths": ["a/secret-host/b"]}, fh)
        self.addCleanup(os.unlink, fh.name)
        self.run_main(["noul", "q", "--state", fh.name, "--state-format", "json"])
        state = Stub.seen[-1]["payload"]["state"]
        self.assertEqual(state["n"], 3)
        self.assertNotIn("secret-host", json.dumps(state))

    def test_ordinary_words_and_paths_pass_untouched(self):
        text = ("task-list-of-things homeassistant/components/unifi/sensor "
                "the password field bin/sd_ship_bindings.py")
        _code, _out, err = self.run_verbose(["noul", "q"], stdin=text)
        self.assertEqual(Stub.seen[-1]["payload"]["state"], text)
        self.assertNotIn("redacted", err)

    def test_an_uncompilable_pattern_sends_nothing_and_honours_the_fallback(self):
        self.patterns("fine", "broken(")
        code, out, err = self.run_verbose(["noul", "q", "--fallback", "0.5"])
        self.assertEqual((code, out), (0, "0.5\n"))
        self.assertEqual(Stub.seen, [])
        self.assertIn("line 2 does not compile", err)
        self.assertNotIn("broken(", err)

    def test_an_anchored_pattern_matches_each_line_as_grep_does(self):
        """grep -E reads line by line, so ^...$ holds for a line inside
        multi-line state; a Python regex without MULTILINE missed it."""
        self.patterns("^private-host$")
        self.run_main(["noul", "q"], stdin="header\nprivate-host\nfooter")
        self.assertNotIn("private-host", self.sent())

    def test_a_protected_json_key_sends_nothing_and_honours_the_fallback(self):
        """A key is refused, not renamed: renaming could collide with another
        key or break an identifier the caller reads back."""
        self.patterns("private-host")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"private-host": "ok"}, fh)
        self.addCleanup(os.unlink, fh.name)
        code, out, err = self.run_verbose(
            ["noul", "q", "--state", fh.name, "--state-format", "json",
             "--fallback", "0.5"])
        self.assertEqual((code, out), (0, "0.5\n"))
        self.assertEqual(Stub.seen, [])
        self.assertNotIn("private-host", err)

    def test_a_protected_criterion_key_sends_nothing(self):
        self.patterns("private-host")
        code, out = self.run_main(["choice", "q", "--criteria", "private-host,other",
                                   "--fallback", "other"])
        self.assertEqual((code, out), (0, "other\n"))
        self.assertEqual(Stub.seen, [])

    def test_an_unknown_posix_class_sends_nothing(self):
        self.patterns("[[:nosuch:]]x")
        code, _out = self.run_main(["enabled"])
        self.assertEqual(code, 3)
        self.run_main(["noul", "q", "--fallback", "0.5"])
        self.assertEqual(Stub.seen, [])
