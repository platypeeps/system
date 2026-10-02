"""What a call sends to a trace collector, and what it may never send.

The API stub from `test_jev` answers, and a second loopback server stands for
the collector and keeps every body it receives. Three promises are checked:

* a call with `JEV_TRACES_URL` set sends one span carrying the event fields;
* no part of what was submitted reaches the span;
* a missing or failing collector changes neither the answer nor the exit code.
"""

import json
import threading
import time
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jev_trace

from .test_jev import Stub, StubServer

#: Appears in the question and the state and nowhere else.
SENTINEL = "zqx-quarterly-severance-terms-9f31"


class Collector(BaseHTTPRequestHandler):
    status = 200
    bodies = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        Collector.bodies.append(self.rfile.read(length).decode("utf-8"))
        self.send_response(Collector.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class Trickle(BaseHTTPRequestHandler):
    """Answers 200, then sends its body a byte every 0.1 s for 3 s."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Length", "30")
        self.end_headers()
        try:
            for _ in range(30):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass

    def log_message(self, *args):
        pass


class TraceCase(StubServer):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.collector = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
        threading.Thread(target=cls.collector.serve_forever, daemon=True).start()
        cls.traces_url = "http://127.0.0.1:%d/v1/traces" % cls.collector.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.collector.shutdown()
        cls.collector.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        Collector.status = 200
        Collector.bodies = []

    def spans(self):
        out = []
        for body in Collector.bodies:
            for rs in json.loads(body)["resourceSpans"]:
                resource = {a["key"]: a["value"] for a in rs["resource"]["attributes"]}
                for ss in rs["scopeSpans"]:
                    for span in ss["spans"]:
                        span["_resource"] = resource
                        span["_attrs"] = {a["key"]: next(iter(a["value"].values()))
                                          for a in span["attributes"]}
                        out.append(span)
        return out


class TheEnvFile(TraceCase):
    """`jev.sh` keeps exported trace settings over <config>/jev/.env."""

    def link(self):
        from .test_jev import TestEnvFile
        helper = TestEnvFile("test_the_config_env_is_found_through_the_symlink")
        helper.url, helper.switch = self.url, self.switch
        link = helper.copy("TYPESAFE_API_KEY=k\nJEV_TRACES_URL=%s\n" % self.traces_url)
        self.addCleanup(helper.doCleanups)
        trace = Path(__file__).resolve().parent.parent / "jev_trace.py"
        (link.resolve().parent / "jev_trace.py").write_bytes(trace.read_bytes())
        return helper, link

    def test_the_env_file_url_is_used_when_nothing_is_exported(self):
        helper, link = self.link()
        done = helper.run_link(link, ["noul", "q"], {})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(self.spans()), 1)

    def test_an_exported_off_beats_the_env_file(self):
        # PR #61 review: JEV_TRACES_URL was not captured around the .env.
        helper, link = self.link()
        done = helper.run_link(link, ["noul", "q"], {"JEV_TRACES_URL": "off"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.spans(), [])


class TheSpan(TraceCase):
    def test_a_call_sends_one_span_with_the_event_fields(self):
        code, out = self.run_main(["noul", "Is this urgent?"],
                                  JEV_TRACES_URL=self.traces_url)
        self.assertEqual(code, 0)
        spans = self.spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span["name"], "jev.noul")
        self.assertEqual(span["_resource"]["service.name"], {"stringValue": "jev"})
        self.assertEqual(span["_resource"]["openinference.project.name"], {"stringValue": "jev"})
        self.assertEqual(span["_attrs"]["jev.primitive"], "noul")
        self.assertEqual(span["_attrs"]["jev.outcome"], "ok")
        self.assertEqual(span["_attrs"]["openinference.span.kind"], "LLM")
        self.assertEqual(span["_attrs"]["llm.token_count.prompt"], "1")
        self.assertEqual(span["status"]["code"], jev_trace.STATUS_OK)

    def test_nothing_submitted_reaches_the_span(self):
        self.run_main(["noul", f"Is {SENTINEL} urgent?"],
                      stdin=f"state with {SENTINEL}",
                      JEV_TRACES_URL=self.traces_url)
        self.assertEqual(len(Collector.bodies), 1)
        self.assertNotIn(SENTINEL, Collector.bodies[0])

    def test_text_that_is_not_an_identifier_never_leaves(self):
        # PR #61 review: the span is built whether or not the ledger takes
        # the row, so a path or a sentence given to `jev record` must be
        # dropped here, not trusted to the ledger's refusal.
        body = json.dumps(jev_trace.span_of({
            "primitive": f"/tmp/{SENTINEL}.txt",
            "caller": f"a sentence about {SENTINEL}",
            "stage": "JEV_OK_STAGE",
            "outcome": "refused",
            "cause": f"{SENTINEL} because",
            "ordering": [1, 2],
        }))
        self.assertNotIn(SENTINEL, body)
        self.assertIn("JEV_OK_STAGE", body)
        self.assertIn('"name": "jev.call"', body)

    def ordering_of(self, value):
        body = jev_trace.span_of({"primitive": "record", "ordering": value})
        attrs = body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        return {a["key"]: a["value"] for a in attrs}.get("jev.ordering")

    def test_a_positions_ordering_is_kept(self):
        # PR #61 review: `jev record --positions 3,1,2` lost its ordering,
        # since commas are no identifier.
        self.assertEqual(self.ordering_of("3,1,2"), {"stringValue": "3,1,2"})

    def test_an_ordering_outside_the_ledger_grammar_is_dropped(self):
        self.assertIsNone(self.ordering_of(f"3,{SENTINEL}"))
        self.assertIsNone(self.ordering_of("1, 2"))
        self.assertIsNone(self.ordering_of(",".join(["1"] * 300)))

    def test_a_failed_call_is_an_error_span(self):
        Stub.status = 500
        self.run_main(["noul", "Is this urgent?"], JEV_RETRIES="0",
                      JEV_TRACES_URL=self.traces_url)
        spans = self.spans()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["status"]["code"], jev_trace.STATUS_ERROR)


class TheSwitch(TraceCase):
    def test_unset_sends_nothing(self):
        self.run_main(["noul", "Is this urgent?"])
        self.assertEqual(Collector.bodies, [])

    def test_an_off_word_sends_nothing(self):
        self.run_main(["noul", "Is this urgent?"], JEV_TRACES_URL="off")
        self.assertEqual(Collector.bodies, [])


class ACollectorThatFails(TraceCase):
    def test_a_refusing_collector_changes_nothing(self):
        _code, expected = self.run_main(["noul", "Is this urgent?"])
        Collector.status = 500
        code, out = self.run_main(["noul", "Is this urgent?"],
                                  JEV_TRACES_URL=self.traces_url)
        self.assertEqual((code, out), (0, expected))

    def test_nothing_listening_changes_nothing(self):
        _code, expected = self.run_main(["noul", "Is this urgent?"])
        closed = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
        url = "http://127.0.0.1:%d/v1/traces" % closed.server_address[1]
        closed.server_close()
        code, out = self.run_main(["noul", "Is this urgent?"], JEV_TRACES_URL=url)
        self.assertEqual((code, out), (0, expected))

    def test_a_trickling_collector_costs_the_bound_and_no_more(self):
        # Ship-lane review: the socket timeout bounds each read, so a body
        # sent a byte at a time held the caller for as long as it lasted.
        server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d/v1/traces" % server.server_address[1]
        started = time.monotonic()
        did = jev_trace.export({"primitive": "noul"},
                               {"JEV_TRACES_URL": url, "JEV_TRACES_TIMEOUT": "0.3"})
        self.assertEqual(did, jev_trace.FAILED)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_export_never_raises(self):
        self.assertEqual(jev_trace.export({"primitive": "noul"},
                                          {"JEV_TRACES_URL": "not a url"}),
                         jev_trace.FAILED)


if __name__ == "__main__":
    unittest.main()
