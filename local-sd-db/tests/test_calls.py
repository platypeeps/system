"""The library's `url` call: reserve, claim, one request on the wire, settle
or lose. Requirement 6's call path (`prd.md`, the reserve -> claim -> wire
-> settle/lose contract) and criterion 15's clauses 15.6 and 15.7, the
library half: every cost row a call leaves is the ledger's own, and the
attempt on the wire is exactly one.

The wire is a fake for most of it, a callable the test hands in; the
urllib path's own tests run a `http.server` on a loopback port, which the
cleartext rule admits, and count what it receives.
"""

import http.server
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

import sd_db
from sd_db import connect, create_assignment, create_item, seed
from sd_db.calls import (
    BYTES_PER_TOKEN,
    MAX_TOKENS_FIELD,
    CallRefused,
    CallResult,
    bound_for,
    call,
    estimate_tokens,
)
from sd_db.ledger import OVERSHOOT_JOB, LedgerRefused
from sd_db.migrate import initialise
from sd_db.registry import parse

#: The ledger tests' registry with one more entry: `bare`, a `url` entry on
#: the open bill with no price and no `max_tokens`, which an uncapped bill
#: admits at a bound of zero and a budget or a cap refuses by name.
REGISTRY = """\
bills:
  capped: { cost: company, cap_usd_month: 10 }
  open:   { cost: subscription }
providers:
  kimi:   { url: "https://moonshot.example/v1", model: kimi-k3, vendor: moonshot, bill: capped,
            roles: [author, reviewer], max_tokens: 16384, price: { in: 3.00, out: 15.00 },
            env: [MOONSHOT_API_KEY] }
  mini:   { url: "https://minimax.example/v1", model: MiniMax-M3, vendor: minimax, bill: open,
            roles: [author, reviewer], max_tokens: 1000, price: { in: 1.00, out: 2.00 },
            env: [MINIMAX_API_KEY] }
  bare:   { url: "https://bare.example/v1", model: bare-1, vendor: bare, bill: open,
            roles: [reviewer], env: [BARE_API_KEY] }
  plain:  { url: "http://plain.example/v1", model: plain-1, vendor: plain, bill: open,
            roles: [reviewer], max_tokens: 10, price: { in: 1, out: 1 }, env: [PLAIN_API_KEY] }
  claude: { start: "claude -p", vendor: anthropic, bill: open, roles: [author, reviewer], reader: claude-json }
roles:
  author:   [kimi, mini, claude]
  reviewer: [mini, kimi, bare, plain]
"""

ENVIRON = {"MOONSHOT_API_KEY": "moon-key", "MINIMAX_API_KEY": "mini-key",
           "BARE_API_KEY": "bare-key", "PLAIN_API_KEY": "plain-key"}
PROMPT = "x" * 4000  # 1000 tokens at four bytes each
ME = os.getpid()


def answer(prompt_tokens, completion_tokens, **extra):
    """A vendor's answer body, the usage keys as the pack's reader names them."""
    body = {"id": "chatcmpl-1", "model": "m", "choices": [{"index": 0, "finish_reason": "stop",
                                                            "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens}}
    body.update(extra)
    return json.dumps(body).encode("utf-8")


class Wire:
    """A transport that records every request and answers from a script.

    Each entry of `script` is `(status, body)` or an exception to raise;
    `calls` is what went out, so a test can assert the count is one.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class CallCase(unittest.TestCase):
    registry = REGISTRY

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        state = self.home / ".local/share/sd"
        state.mkdir(parents=True)
        (state / "providers.yaml").write_text(self.registry, encoding="utf-8")
        initialise(home=self.home)
        self.db = connect(home=self.home)
        self.addCleanup(self.db.close)
        self.parsed = parse(self.registry, "providers.yaml")
        seed(self.db, self.parsed)
        self.item = create_item(self.db, kind="work", title="a called item")

    def entry(self, name):
        return self.parsed.providers[name]

    def assignment(self, *, provider="mini", budget_usd=None):
        return create_assignment(self.db, role="author", status="queued", item=self.item,
                                 provider=provider, budget_usd=budget_usd)

    def rows(self):
        return [tuple(row) for row in self.db.execute(
            "SELECT call_id, source, usd, tokens_in, tokens_out, provider, bill, assignment FROM cost ORDER BY id"
        ).fetchall()]

    def reports(self):
        return self.db.execute(
            "SELECT title, external_id, fields, body FROM item WHERE kind = 'report' ORDER BY id"
        ).fetchall()

    def call(self, name="mini", wire=None, **kw):
        kw.setdefault("call_id", "c1")
        return call(self.db, entry=self.entry(name), prompt=PROMPT, environ=ENVIRON,
                    registry=self.parsed, transport=wire, owner_pid=ME, **kw)


class TheBound(CallCase):
    def test_the_estimate_is_four_bytes_a_token_and_the_bound_prices_it_with_max_tokens(self):
        """`mini`: 1000 prompt tokens at $1/M plus 1000 `max_tokens` at $2/M
        is $0.003. The ratio is the module's stated assumption."""
        self.assertEqual(BYTES_PER_TOKEN, 4)
        self.assertEqual(estimate_tokens(PROMPT), 1000)
        self.assertEqual(estimate_tokens("é" * 2), 1)
        self.assertAlmostEqual(bound_for(self.entry("mini"), PROMPT), 0.003)
        self.assertAlmostEqual(bound_for(self.entry("kimi"), PROMPT), (1000 * 3.0 + 16384 * 15.0) / 1e6)

    def test_prices_multiply_as_the_decimals_the_registry_wrote(self):
        """sd:1176's review: three tokens at $0.10/M is 3e-7, and float
        arithmetic made it 3.0000000000000004e-7, which the exact ledger
        compare then refused against a limit of exactly 3e-7."""
        entry = sd_db.registry.Provider(
            name="bare", vendor="bare", bill="open", url="https://bare.example/v1", model="bare-1",
            max_tokens=3, price={"in": 0.10, "out": 0.10}, env=["BARE_API_KEY"])
        self.assertEqual(bound_for(entry, ""), 3e-7)
        wire = Wire((200, answer(0, 3)))
        result = call(self.db, entry=entry, prompt="", environ=ENVIRON, registry=self.parsed,
                      transport=wire, owner_pid=ME, call_id="cheap")
        self.assertEqual((result.bound, result.usd), (3e-7, 3e-7))


class ReserveClaimWireSettle(CallCase):
    def test_a_call_reserves_before_the_wire_claims_and_settles_at_the_usage_the_response_carries(self):
        """The order the prd names. The reservation is in the ledger before
        the request leaves, the request carries the claimed row's id's
        prompt, and the row ends `run` at the vendor's own count."""
        seen = []

        def wire(request, timeout):
            seen.append([tuple(row) for row in self.db.execute(
                "SELECT call_id, source, usd FROM cost").fetchall()])
            return 200, answer(1100, 300)

        row = self.assignment()
        result = self.call(wire=wire, assignment=row, role="author", pass_="review-1")
        # On the wire the row was `sending` at the bound: reserved and claimed first.
        self.assertEqual(seen, [[("c1", "sending", 0.003)]])
        self.assertIsInstance(result, CallResult)
        self.assertEqual((result.outcome, result.http_status, result.reason), ("run", 200, ""))
        self.assertAlmostEqual(result.usd, (1100 * 1.0 + 300 * 2.0) / 1e6)
        self.assertEqual((result.tokens_in, result.tokens_out), (1100, 300))
        self.assertEqual(self.rows(), [("c1", "run", result.usd, 1100, 300, "mini", "open", row)])
        self.assertEqual(self.reports(), [])

    def test_the_request_is_the_packs_shape(self):
        wire = Wire((200, answer(1, 1)))
        self.call(wire=wire, name="kimi")
        (request, timeout), = wire.calls
        self.assertEqual(request.full_url, "https://moonshot.example/v1/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer moon-key")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {
            "model": "kimi-k3", "max_tokens": 16384,
            "messages": [{"role": "user", "content": PROMPT}]})
        self.assertEqual(timeout, 1800)
        self.assertEqual(self.call(wire=Wire((200, answer(1, 1))), call_id="c2", timeout=7).call_id, "c2")
        self.assertEqual(self.rows()[1][:2], ("c2", "run"))

    def test_a_lost_response_binds_the_row_and_the_wire_saw_exactly_one_request(self):
        """A timeout, a dropped connection: `OSError` from the transport, the
        row `bound` at its full bound, and no second attempt."""
        for error in (socket.timeout("timed out"), ConnectionResetError("reset"), OSError("gone")):
            with self.subTest(error=error):
                wire = Wire(error, (200, answer(1, 1)))
                result = self.call(wire=wire, call_id=f"lost-{type(error).__name__}")
                self.assertEqual((result.outcome, result.usd), ("bound", None))
                self.assertIn("response lost", result.reason)
                self.assertEqual(len(wire.calls), 1)
                self.assertEqual(self.rows()[-1][1:3], ("bound", 0.003))

    def test_an_answer_without_usage_binds_the_row(self):
        """A non-JSON body, an HTTP error with no usage (429 among them), a
        redirect the client refused: none can cost the call, and the
        provider may have billed it."""
        cases = [("garbage", (200, b"<html>not json</html>"), "HTTP 200 with no usage"),
                 ("limited", (429, b'{"error": {"message": "rate limited"}}'), "HTTP 429 with no usage"),
                 ("moved", (302, b""), "HTTP 302, a redirect the client does not follow"),
                 ("half", (200, b'{"usage": {"prompt_tokens": 5}}'), "HTTP 200 with no usage"),
                 ("huge", (200, b'{"usage": {"prompt_tokens": 1, "completion_tokens": 10000000000000}}'),
                  "HTTP 200 with no usage")]
        for call_id, step, reason in cases:
            with self.subTest(call_id=call_id):
                wire = Wire(step)
                result = self.call(wire=wire, call_id=call_id)
                self.assertEqual((result.outcome, result.http_status), ("bound", step[0]))
                self.assertIn(reason, result.reason)
                self.assertEqual(len(wire.calls), 1)
        self.assertEqual([row[1:3] for row in self.rows()], [("bound", 0.003)] * len(cases))

    def test_an_http_error_whose_body_carries_usage_settles_at_it(self):
        result = self.call(wire=Wire((500, answer(10, 0, error={"message": "late"}))))
        self.assertEqual((result.outcome, result.http_status, result.tokens_in), ("run", 500, 10))
        self.assertAlmostEqual(result.usd, 10 / 1e6)

    def test_no_cost_row_is_written_outside_the_ledger(self):
        """Clause 15.7: the module inserts nothing into `cost` itself."""
        source = Path(sd_db.calls.__file__).read_text(encoding="utf-8")
        self.assertNotIn("INSERT", source)
        self.assertNotIn("record_cost", source)


class WhatIsRefusedBeforeTheWire(CallCase):
    def refused(self, name, message, **kw):
        wire = Wire((200, answer(1, 1)))
        with self.assertRaises(CallRefused) as raised:
            self.call(wire=wire, name=name, **kw)
        self.assertIn(message, str(raised.exception))
        self.assertEqual(raised.exception.entry, name)
        self.assertEqual(wire.calls, [])
        self.assertEqual(self.rows(), [])

    def test_a_start_entry_is_refused_by_name(self):
        self.refused("claude", "claude is a 'start' entry")

    def test_cleartext_is_refused_by_name_and_loopback_is_not_cleartext(self):
        self.refused("plain", "plain points at 'http://plain.example/v1', which reaches 'plain.example' in the clear")
        for url in ("http://127.0.0.1:1/v1", "http://localhost:1/v1", "http://[::1]:1/v1"):
            with self.subTest(url=url):
                self.assertIsNone(sd_db.calls.cleartext(
                    sd_db.registry.Provider(name="l", vendor="v", bill="open", url=url)))

    def test_an_unset_key_variable_is_refused_by_name(self):
        wire = Wire((200, answer(1, 1)))
        with self.assertRaises(CallRefused) as raised:
            call(self.db, entry=self.entry("mini"), prompt=PROMPT, environ={}, registry=self.parsed,
                 transport=wire, owner_pid=ME)
        self.assertIn("mini has no value for MINIMAX_API_KEY", str(raised.exception))
        self.assertEqual(wire.calls, [])

    def test_an_entry_without_price_or_max_tokens_is_refused_on_a_capped_bill_and_a_budget(self):
        """sd:788's refusal: a bound the library cannot compute is a limit it
        cannot hold. On an open bill with no budget the same entry is
        called at a bound of zero and its row records no money."""
        capped = self.registry.replace("bill: open,\n            roles: [reviewer], env: [BARE_API_KEY]",
                                       "bill: capped,\n            roles: [reviewer], env: [BARE_API_KEY]")
        parsed = parse(capped, "providers.yaml")
        wire = Wire((200, answer(1, 1)))
        with self.assertRaises(CallRefused) as raised:
            call(self.db, entry=parsed.providers["bare"], prompt=PROMPT, environ=ENVIRON,
                 registry=parsed, transport=wire, owner_pid=ME)
        self.assertIn("bare has no price.in, price.out, max_tokens, and its bill 'capped' is capped",
                      str(raised.exception))
        budgeted = self.assignment(provider="bare", budget_usd=1.0)
        with self.assertRaises(CallRefused) as raised:
            self.call(wire=wire, name="bare", assignment=budgeted)
        self.assertIn(f"assignment {budgeted} carries a budget", str(raised.exception))
        self.assertEqual(wire.calls, [])
        self.assertEqual(self.rows(), [])
        result = self.call(wire=wire, name="bare", assignment=self.assignment(provider="bare"))
        self.assertEqual((result.outcome, result.bound, result.usd), ("run", 0.0, 0.0))


class TheBudgetEndsTheRow(CallCase):
    def test_criterion_4s_two_rows_the_budget_admits_one_call_and_refuses_the_second_with_budget_spent(self):
        """sd:235, criterion 4 of `the-runner-works-the-queue/prd.md`, moved
        here by the owner's 2026-09-16 decision: two rows on a `url` author,
        the first with a `budget_usd` of one call's bound and a brief that
        needs two calls, the second with no budget. The first's second call
        is refused by the budget with `budget spent` and the amount, and
        that refusal is what the runner ends its row `blocked` on; the
        second row completes both calls. Two `cost` rows for the budgeted
        row's one call and the refused attempt none; two for the other."""
        bound = bound_for(self.entry("mini"), PROMPT)
        budgeted = self.assignment(budget_usd=bound)
        free = self.assignment()
        wire = Wire(*[(200, answer(1000, 500))] * 4)
        first = self.call(wire=wire, assignment=budgeted, call_id="b1")
        self.assertEqual(first.outcome, "run")
        with self.assertRaises(LedgerRefused) as raised:
            self.call(wire=wire, assignment=budgeted, call_id="b2")
        refusal = raised.exception
        self.assertIn("budget spent", str(refusal))
        self.assertEqual((refusal.scope, refusal.name, refusal.limit), ("assignment", budgeted, bound))
        self.assertAlmostEqual(refusal.exposure, first.usd)
        self.assertAlmostEqual(refusal.bound, bound)
        self.assertEqual(len(wire.calls), 1)
        for call_id in ("f1", "f2"):
            self.assertEqual(self.call(wire=wire, assignment=free, call_id=call_id).outcome, "run")
        self.assertEqual(len(wire.calls), 3)
        self.assertEqual([(row[0], row[1], row[7]) for row in self.rows()],
                         [("b1", "run", budgeted), ("f1", "run", free), ("f2", "run", free)])
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM cost WHERE assignment = ?", (budgeted,)).fetchone()[0], 1)

    def test_a_refused_cap_leaves_no_row_and_sends_nothing(self):
        wire = Wire((200, answer(1, 1)))
        self.db.execute("UPDATE bill SET cap_usd_month = 0.01 WHERE name = 'capped'")
        with self.assertRaises(LedgerRefused) as raised:
            self.call(wire=wire, name="kimi")
        self.assertEqual(raised.exception.scope, "bill")
        self.assertEqual(wire.calls, [])
        self.assertEqual(self.rows(), [])


class TheOvershoot(CallCase):
    def test_usage_above_the_bound_settles_at_the_actual_cost_and_files_the_cap_overshot_report(self):
        """C-59: the row is `run` at the actual cost, and one attention row
        names the call, the bound and the actual."""
        wire = Wire((200, answer(1000, 5000)))  # 5000 out against max_tokens 1000
        result = self.call(wire=wire, call_id="over")
        actual = (1000 * 1.0 + 5000 * 2.0) / 1e6
        self.assertEqual(result.outcome, "run")
        self.assertAlmostEqual(result.usd, actual)
        self.assertEqual(self.rows()[0][1:3], ("run", actual))
        (report,) = self.reports()
        fields = json.loads(report["fields"])
        self.assertEqual(report["external_id"], f"{OVERSHOOT_JOB}:over")
        self.assertEqual(report["title"], f"{OVERSHOOT_JOB}: needs attention")
        self.assertIs(fields["attention"], True)
        self.assertEqual(fields["report"]["attention_basis"], "cap overshot on call over")
        body = json.loads(report["body"])["text"]
        for named in ("call over", f"{actual:.6f}", f"{0.003:.6f}"):
            self.assertIn(named, body)


class WhatTheReviewFound(CallCase):
    """#426's Copilot pass: what a caller could hand `call` that reached the
    wire, the row, or the body wrong."""

    def test_a_timeout_that_is_not_a_positive_finite_number_is_refused_before_any_row(self):
        """`0` and `-1` reached urllib after the claim and raised there,
        leaving the row `sending` for a live owner that would never settle
        it; now each is refused by name with nothing reserved."""
        for timeout in (0, -1, float("nan"), float("inf"), "5", True):
            with self.subTest(timeout=timeout):
                wire = Wire((200, answer(1, 1)))
                with self.assertRaises(CallRefused) as raised:
                    self.call(wire=wire, timeout=timeout)
                self.assertIn("timeout must be a finite number of seconds above zero", str(raised.exception))
                self.assertIn(repr(timeout), str(raised.exception))
                self.assertEqual(wire.calls, [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.call(wire=Wire((200, answer(1, 1))), timeout=0.5).outcome, "run")

    def test_a_transport_fault_after_the_claim_binds_the_row_and_reaches_the_caller(self):
        """The request may have gone; the row is `bound`, not `sending`,
        and the fault itself is not swallowed."""
        wire = Wire(RuntimeError("the transport broke"))
        with self.assertRaises(RuntimeError):
            self.call(wire=wire, call_id="broke")
        self.assertEqual([row[:2] for row in self.rows()], [("broke", "bound")])
        self.assertEqual(len(wire.calls), 1)

    def test_a_price_too_large_for_a_float_is_no_price(self):
        """`registry.parse` does not validate prices; `float(10 ** 1000)`
        raised `OverflowError` out of `bound_for`. Now it is a missing
        price: refused on a capped bill; on an open bill that side of the
        bound is zero and the other side still prices."""
        huge = self.registry.replace("price: { in: 3.00, out: 15.00 }", f"price: {{ in: {10 ** 1000}, out: 15.00 }}")
        huge = huge.replace("max_tokens: 1000, price: { in: 1.00, out: 2.00 }",
                            f"max_tokens: 1000, price: {{ in: {10 ** 1000}, out: 2.00 }}")
        parsed = parse(huge, "providers.yaml")
        self.assertEqual(parsed.providers["kimi"].price["in"], 10 ** 1000)
        wire = Wire((200, answer(1, 1)))
        with self.assertRaises(CallRefused) as raised:
            call(self.db, entry=parsed.providers["kimi"], prompt=PROMPT, environ=ENVIRON,
                 registry=parsed, transport=wire, owner_pid=ME)
        self.assertIn("kimi has no price.in, and its bill 'capped' is capped", str(raised.exception))
        self.assertEqual(wire.calls, [])
        self.assertEqual(bound_for(parsed.providers["mini"], PROMPT), 1000 * 2.0 / 1e6)
        result = call(self.db, entry=parsed.providers["mini"], prompt=PROMPT, environ=ENVIRON,
                      registry=parsed, transport=wire, owner_pid=ME)
        self.assertEqual((result.outcome, result.bound, result.usd), ("run", 0.002, 1 * 2.0 / 1e6))

    # --- sd:2553, the system half of sd:1827: a strict response_format -----

    SCHEMA = {"type": "object", "additionalProperties": False, "required": ["findings"],
              "properties": {"findings": {"type": "array", "maxItems": 50, "items": {
                  "type": "object", "additionalProperties": False, "required": ["title", "line"],
                  "properties": {"title": {"type": "string", "minLength": 1},
                                 "line": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}}}}

    def strict(self):
        """The registry with `kimi` opted in to a strict response_format."""
        self.parsed = parse(REGISTRY.replace("model: kimi-k3,", "model: kimi-k3, response_format: json_schema,"),
                            "providers.yaml")

    def test_an_opted_in_entry_sends_the_schema_strict(self):
        self.strict()
        wire = Wire((200, answer(1, 1)))
        self.call(wire=wire, name="kimi", response_schema=self.SCHEMA, schema_name="review_findings")
        body = json.loads(wire.calls[0][0].data)
        sent = body["response_format"]
        self.assertEqual((sent["type"], sent["json_schema"]["name"], sent["json_schema"]["strict"]),
                         ("json_schema", "review_findings", True))
        # Strict mode rejects minLength and maxItems (the operator's ruling on
        # sd:1827); the copy drops them and keeps every other keyword. The
        # caller's schema is not changed: its parser keeps those checks.
        findings = sent["json_schema"]["schema"]["properties"]["findings"]
        self.assertNotIn("maxItems", findings)
        self.assertNotIn("minLength", findings["items"]["properties"]["title"])
        self.assertEqual(findings["items"]["properties"]["line"],
                         {"anyOf": [{"type": "integer"}, {"type": "null"}]})
        self.assertEqual(self.SCHEMA["properties"]["findings"]["maxItems"], 50)
        # A property that happens to be named like a dropped keyword is a name, and stays.
        named = {"type": "object", "properties": {"maxItems": {"type": "integer"}}}
        wire = Wire((200, answer(1, 1)))
        self.call(wire=wire, name="kimi", call_id="c-named", response_schema=named)
        self.assertEqual(json.loads(wire.calls[0][0].data)["response_format"]["json_schema"],
                         {"name": "response", "strict": True, "schema": named})
        self.assertEqual(self.SCHEMA["properties"]["findings"]["items"]["properties"]["title"]["minLength"], 1)

    def test_an_entry_that_does_not_opt_in_sends_todays_body(self):
        # MiniMax accepts response_format and ignores it, so it is never sent
        # unasked: the same body as before, no key added, schema or not.
        self.strict()
        for name, kw in (("mini", {"response_schema": self.SCHEMA}), ("mini", {}), ("kimi", {})):
            with self.subTest(name=name, schema=bool(kw)):
                wire = Wire((200, answer(1, 1)))
                self.call(wire=wire, name=name, call_id=f"c-{name}-{bool(kw)}", **kw)
                body = json.loads(wire.calls[0][0].data)
                self.assertNotIn("response_format", body)
                self.assertEqual(set(body), {"model", "max_tokens", "messages"})

    def test_a_schema_name_strict_mode_refuses_is_refused_before_the_wire(self):
        self.strict()
        for name, schema in (("has spaces", self.SCHEMA), ("x" * 65, self.SCHEMA), ("ok", ["not", "a", "dict"])):
            with self.subTest(name=name):
                wire = Wire()
                with self.assertRaises(CallRefused):
                    self.call(wire=wire, name="kimi", call_id=f"r-{len(name)}", response_schema=schema,
                              schema_name=name)
                self.assertEqual(wire.calls, [])
        self.assertEqual(self.rows(), [])

    def test_an_entry_without_max_tokens_sends_no_max_tokens_key(self):
        """A JSON `null` is not "no limit" to every endpoint; the key is
        left out."""
        wire = Wire((200, answer(1, 1)))
        self.call(wire=wire, name="bare", assignment=self.assignment(provider="bare"))
        ((request, _),) = wire.calls
        body = json.loads(request.data)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["model"], "bare-1")
        wire = Wire((200, answer(1, 1)))
        self.call(wire=wire, name="mini", call_id="c2")
        self.assertEqual(json.loads(wire.calls[0][0].data)["max_tokens"], 1000)

    def test_a_max_tokens_the_bound_treats_as_missing_is_not_sent_either(self):
        """The verification round: `-1` or `"lots"` is missing to `bound_for`
        and to the refusal, so it is missing to the request body too."""
        for value in (-1, 0, "lots", 2.5):
            with self.subTest(max_tokens=value):
                entry = sd_db.registry.Provider(
                    name="bare", vendor="bare", bill="open", url="https://bare.example/v1", model="bare-1",
                    max_tokens=value, env=["BARE_API_KEY"])
                self.assertEqual(bound_for(entry, PROMPT), 0.0)
                wire = Wire((200, answer(1, 1)))
                call(self.db, entry=entry, prompt=PROMPT, environ=ENVIRON, registry=self.parsed,
                     transport=wire, owner_pid=ME, call_id=f"m{len(wire.calls)}{value!r}"[:20])
                ((request, _),) = wire.calls
                self.assertNotIn("max_tokens", json.loads(request.data))

    def test_a_max_tokens_too_large_for_a_float_is_no_max_tokens(self):
        """`registry.parse` validates no `max_tokens`, so `10 ** 1000` is a
        positive int that reached `bound_for`'s multiplication by a float and
        raised `OverflowError` -- out of a function documented to answer with
        `CallRefused`, before anything was reserved and without naming the
        entry. Now it is a missing `max_tokens`, which is the case the
        refusal already had a sentence for."""
        entry = sd_db.registry.Provider(
            name="kimi", vendor="moonshot", bill="capped", url="https://moonshot.example/v1",
            model="kimi-k3", max_tokens=10 ** 1000, price={"in": 3.0, "out": 15.0},
            env=["MOONSHOT_API_KEY"])
        self.assertEqual(bound_for(entry, PROMPT), 1000 * 3.0 / 1e6)
        wire = Wire((200, answer(1, 1)))
        with self.assertRaises(CallRefused) as raised:
            call(self.db, entry=entry, prompt=PROMPT, environ=ENVIRON, registry=self.parsed,
                 transport=wire, owner_pid=ME, call_id="huge")
        self.assertIn("kimi has no max_tokens, and its bill 'capped' is capped", str(raised.exception))
        self.assertEqual(wire.calls, [])
        self.assertEqual(self.rows(), [])
        # The ceiling is `MAX_TOKENS_FIELD`, the same count a usage field is
        # believed at, and the value on it still passes.
        entry = sd_db.registry.Provider(
            name="bare", vendor="bare", bill="open", url="https://bare.example/v1", model="bare-1",
            max_tokens=MAX_TOKENS_FIELD, env=["BARE_API_KEY"])
        wire = Wire((200, answer(1, 1)))
        call(self.db, entry=entry, prompt=PROMPT, environ=ENVIRON, registry=self.parsed,
             transport=wire, owner_pid=ME, call_id="ceiling")
        self.assertEqual(json.loads(wire.calls[0][0].data)["max_tokens"], MAX_TOKENS_FIELD)

    def test_a_request_that_cannot_be_built_reserves_nothing(self):
        """The reservation was made before `json.dumps` and `Request`, and
        neither the endpoint nor the body values are validated on the way in.
        A value no JSON encoder takes therefore left a `reserved` row holding
        this call's bound against the bill, with nothing on the wire to
        settle it and only a later caller's orphan sweep to release it."""
        entry = sd_db.registry.Provider(
            name="bare", vendor="bare", bill="open", url="https://bare.example/v1",
            model=object(), env=["BARE_API_KEY"])
        wire = Wire((200, answer(1, 1)))
        with self.assertRaises(CallRefused) as raised:
            call(self.db, entry=entry, prompt=PROMPT, environ=ENVIRON, registry=self.parsed,
                 transport=wire, owner_pid=ME, call_id="unbuildable")
        self.assertIn("bare does not make a request", str(raised.exception))
        self.assertIn("nothing was reserved and nothing went on the wire", str(raised.exception))
        self.assertEqual(raised.exception.entry, "bare")
        self.assertEqual(wire.calls, [])
        self.assertEqual(self.rows(), [])

    def test_a_redirect_carrying_usage_is_bound_and_is_not_billed(self):
        """A 3xx is refused and never followed, so no model ran and the
        `usage` in its body is a number from a host nobody named. The
        redirect test sat inside the `usage is None` arm, so such a body
        settled to `run` and was billed."""
        wire = Wire((302, answer(1000, 2000)))
        result = self.call(wire=wire, call_id="moved")
        self.assertEqual((result.outcome, result.usd), ("bound", None))
        self.assertEqual((result.tokens_in, result.tokens_out), (None, None))
        self.assertIn("HTTP 302, a redirect the client does not follow", result.reason)
        self.assertEqual([row[:3] for row in self.rows()], [("moved", "bound", 0.003)])

    def test_a_cost_the_ledger_cannot_hold_binds_the_row_instead_of_raising(self):
        """Two finite prices and two counts inside `MAX_TOKENS_FIELD` still
        multiply to `inf` near the float ceiling. `ledger._money` refuses a
        non-finite amount, so `settle` raised `LedgerRefused` out of `call`
        with the response in hand and the row left `sending` -- the one state
        this function never leaves a row in. Priced as exact decimals since
        sd:1176, the product is only `inf` past the float ceiling itself, so
        the prices sit at 1e308 rather than 1e300."""
        entry = sd_db.registry.Provider(
            name="bare", vendor="bare", bill="open", url="https://bare.example/v1", model="bare-1",
            max_tokens=1, price={"in": 1e308, "out": 1e308}, env=["BARE_API_KEY"])
        wire = Wire((200, answer(MAX_TOKENS_FIELD, MAX_TOKENS_FIELD)))
        result = call(self.db, entry=entry, prompt="xxxx", environ=ENVIRON, registry=self.parsed,
                      transport=wire, owner_pid=ME, call_id="inf")
        self.assertEqual((result.outcome, result.usd), ("bound", None))
        self.assertEqual((result.tokens_in, result.tokens_out), (MAX_TOKENS_FIELD, MAX_TOKENS_FIELD))
        self.assertIn("is not a finite cost", result.reason)
        self.assertEqual([row[:2] for row in self.rows()], [("inf", "bound")])
        self.assertEqual(len(wire.calls), 1)


class Handler(http.server.BaseHTTPRequestHandler):
    """A vendor on a loopback port. The path before `/chat/completions`
    picks the behaviour; `hits` counts what arrived, by that path."""

    hits: dict = {}
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_POST(self):
        kind = self.path.split("/")[1]
        with self.lock:
            self.hits[kind] = self.hits.get(kind, 0) + 1
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if kind == "ok":
            self.reply(200, answer(7, 3))
        elif kind == "slow":
            time.sleep(1.5)
            self.reply(200, answer(7, 3))
        elif kind == "moved":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/ok/chat/completions")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif kind == "limited":
            self.reply(429, b'{"error": {"message": "slow down"}}')
        elif kind == "garbage":
            self.reply(200, b"not json at all")
        elif kind == "drop":
            self.connection.close()
        else:
            self.reply(404, b"")

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TheUrllibPath(CallCase):
    """The default transport against a real socket: no redirect followed,
    no retry made, the usage read from the body, and a timeout bound."""

    def setUp(self):
        super().setUp()
        Handler.hits = {}
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port
        # `cost.provider` is a foreign key; the loopback entry is not in the
        # seeded registry, so its row is written here.
        self.db.execute("INSERT INTO provider (name, enabled) VALUES ('local', 1)")

    def entry_at(self, kind):
        return sd_db.registry.Provider(
            name="local", vendor="v", bill="open", url=f"http://127.0.0.1:{self.port}/{kind}",
            model="m", max_tokens=1000, price={"in": 1.0, "out": 2.0}, env=["LOCAL_KEY"])

    def wired(self, kind, **kw):
        kw.setdefault("call_id", kind)
        kw.setdefault("timeout", 5)
        return call(self.db, entry=self.entry_at(kind), prompt=PROMPT, environ={"LOCAL_KEY": "k"},
                    registry=self.parsed, owner_pid=ME, **kw)

    def test_the_usage_is_read_from_the_wire(self):
        result = self.wired("ok")
        self.assertEqual((result.outcome, result.http_status, result.tokens_in, result.tokens_out),
                         ("run", 200, 7, 3))
        self.assertEqual(Handler.hits, {"ok": 1})

    def test_a_redirect_is_refused_not_followed(self):
        """The target counts zero hits: the key never went to the second host."""
        result = self.wired("moved")
        self.assertEqual((result.outcome, result.http_status), ("bound", 302))
        self.assertIn("redirect", result.reason)
        self.assertEqual(Handler.hits, {"moved": 1})
        self.assertEqual(Handler.hits.get("ok", 0), 0)

    def test_a_timeout_binds_the_row_after_exactly_one_request(self):
        """urllib makes no retry: the slow handler is hit once, and the row
        is `bound` at its full bound."""
        result = self.wired("slow", timeout=0.3)
        self.assertEqual(result.outcome, "bound")
        self.assertIn("timed out", result.reason)
        time.sleep(1.6)  # let the slow handler finish before counting
        self.assertEqual(Handler.hits, {"slow": 1})
        self.assertEqual(self.rows()[-1][1:3], ("bound", 0.003))

    def test_a_429_a_non_json_body_and_a_dropped_connection_bind_the_row(self):
        limited = self.wired("limited")
        self.assertEqual((limited.outcome, limited.http_status), ("bound", 429))
        garbage = self.wired("garbage")
        self.assertEqual((garbage.outcome, garbage.http_status), ("bound", 200))
        dropped = self.wired("drop")
        self.assertEqual((dropped.outcome, dropped.http_status), ("bound", None))
        self.assertIn("response lost", dropped.reason)
        self.assertEqual(Handler.hits, {"limited": 1, "garbage": 1, "drop": 1})
        self.assertEqual([row[1] for row in self.rows()], ["bound"] * 3)


#: The loopback registry: `home` is a server on this machine that
#: authenticates nobody, `homekey` is one on this machine that does declare
#: a variable, and `away` is a public host that declares none. The three
#: together separate "the socket is the boundary" from "no key was asked
#: for", which is the pair a single entry cannot tell apart.
LOOPBACK_REGISTRY = """\
bills:
  open: { cost: subscription }
providers:
  home:    { url: "http://localhost:8084/v1", model: local-1, vendor: local, bill: open,
             roles: [reviewer], max_tokens: 16, price: { in: 0, out: 0 }, env: [] }
  homekey: { url: "http://127.0.0.1:8084/v1", model: local-1, vendor: local, bill: open,
             roles: [reviewer], max_tokens: 16, price: { in: 0, out: 0 }, env: [HOME_API_KEY] }
  away:    { url: "https://away.example/v1", model: away-1, vendor: away, bill: open,
             roles: [reviewer], max_tokens: 16, price: { in: 0, out: 0 }, env: [] }
roles:
  reviewer: [home, homekey, away]
"""


class ALoopbackEntryNeedsNoKey(CallCase):
    """A server on this machine is reachable only by processes running as
    this user, so the socket already decides the recipient and a key adds
    nothing. The exemption is deliberately narrow: loopback *and* no
    declared variable. Either half alone still refuses, because a public
    host with no variable is the security hole this could have opened, and
    an entry that names a variable was configured to send one.
    """

    registry = LOOPBACK_REGISTRY

    def call(self, name, wire=None, environ=None, **kw):
        kw.setdefault("call_id", "c1")
        return call(self.db, entry=self.entry(name), prompt=PROMPT,
                    environ={} if environ is None else environ, registry=self.parsed,
                    transport=wire, owner_pid=ME, **kw)

    def test_a_loopback_entry_with_no_variable_is_called_and_sends_no_authorization(self):
        wire = Wire((200, answer(1, 1)))
        result = self.call("home", wire=wire)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(len(wire.calls), 1)
        request = wire.calls[0][0]
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(request.get_header("Content-type"), "application/json")

    def test_a_public_entry_with_no_variable_is_still_refused(self):
        """The half that must not regress: dropping the key requirement for
        every entry with an empty `env` would send prompts to a public host
        unauthenticated, and `away` is https so the cleartext rule does not
        catch it."""
        wire = Wire((200, answer(1, 1)))
        with self.assertRaises(CallRefused) as raised:
            self.call("away", wire=wire)
        self.assertIn("away has no value for any key variable", str(raised.exception))
        self.assertEqual(wire.calls, [])
        self.assertEqual(self.rows(), [])

    def test_a_loopback_entry_that_declares_a_variable_still_needs_its_value(self):
        wire = Wire((200, answer(1, 1)))
        with self.assertRaises(CallRefused) as raised:
            self.call("homekey", wire=wire)
        self.assertIn("homekey has no value for HOME_API_KEY", str(raised.exception))
        self.assertEqual(wire.calls, [])
        with self.subTest("and is called once the value is there"):
            result = self.call("homekey", wire=wire, environ={"HOME_API_KEY": "k"})
            self.assertEqual(result.http_status, 200)
            self.assertEqual(wire.calls[0][0].get_header("Authorization"), "Bearer k")

    def test_which_hosts_count_as_this_machine(self):
        """A `127.` prefix is not a test: `127.evil.com` resolves in public
        DNS. `127.1` is refused although curl accepts it, because a second
        address parser is a second chance to disagree."""
        def entry(host):
            return sd_db.registry.Provider(name="l", vendor="v", bill="open",
                                           url=f"http://{host}/v1")
        for host in ("localhost", "127.0.0.1", "[::1]", "127.0.0.2"):
            with self.subTest(accepted=host):
                self.assertTrue(sd_db.calls.loopback(entry(host)))
        for host in ("127.evil.com", "127.0.0.1.evil.com", "127.1",
                     "localhost.evil.com", "example.com", ""):
            with self.subTest(rejected=host):
                self.assertFalse(sd_db.calls.loopback(entry(host)))


class AProxyNeverSeesALoopbackCall(CallCase):
    """`build_opener` installs a `ProxyHandler` that reads `HTTP_PROXY` at
    import, and urllib's bypass list does not special-case loopback. On a
    box where `HTTP_PROXY` is set and `NO_PROXY` omits `localhost`, the
    default opener forwards a loopback request to the proxy, and the prompt
    leaves the machine -- the more so now that such a call carries no key
    and so looks harmless.

    Two servers, both real, both on loopback: the vendor and a stand-in
    proxy. The call must reach the vendor and the proxy must count zero.
    """

    def setUp(self):
        super().setUp()
        Handler.hits = {}
        self.vendor = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.vendor.daemon_threads = True
        threading.Thread(target=self.vendor.serve_forever, daemon=True).start()
        self.addCleanup(self.vendor.server_close)
        self.addCleanup(self.vendor.shutdown)
        self.proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProxyRecorder)
        self.proxy.daemon_threads = True
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)
        ProxyRecorder.seen = []
        self.db.execute("INSERT INTO provider (name, enabled) VALUES ('local', 1)")
        # What a process launched with HTTP_PROXY set would hold: the module
        # builds `_OPENER` at import, so the variable cannot be set later in
        # this process and still reach it.
        # `no_proxy` is read at request time by `proxy_bypass`, not at build
        # time, and CI runners export `no_proxy=*`. Left in place it bypasses
        # the stand-in proxy entirely, and every assertion below passes
        # without exercising anything.
        cleared = unittest.mock.patch.dict(
            os.environ, {k: "" for k in ("no_proxy", "NO_PROXY")})
        cleared.start()
        self.addCleanup(cleared.stop)
        proxied = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{self.proxy.server_port}"}),
            sd_db.calls._NoRedirect)
        patch = unittest.mock.patch.object(sd_db.calls, "_OPENER", proxied)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_loopback_call_reaches_the_vendor_and_not_the_proxy(self):
        entry = sd_db.registry.Provider(
            name="local", vendor="v", bill="open", model="m", max_tokens=1000,
            price={"in": 0, "out": 0}, env=[],
            url=f"http://127.0.0.1:{self.vendor.server_port}/ok")
        result = call(self.db, entry=entry, prompt=PROMPT, environ={}, registry=self.parsed,
                      owner_pid=ME, call_id="direct", timeout=5)
        self.assertEqual((result.outcome, result.http_status), ("run", 200))
        self.assertEqual(Handler.hits, {"ok": 1})
        self.assertEqual(ProxyRecorder.seen, [])

    def test_the_patched_opener_really_does_proxy_a_public_host(self):
        """Without this the test above passes on a box with no proxy at all,
        proving nothing. A non-loopback host on the same patched opener must
        land in the recorder."""
        request = urllib.request.Request(
            "http://vendor.example/v1/chat/completions", data=b"{}",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError):
            sd_db.calls._OPENER.open(request, timeout=5)
        self.assertEqual(ProxyRecorder.seen, ["http://vendor.example/v1/chat/completions"])

    def test_the_two_recipes_differ_under_a_configured_proxy(self):
        """Both openers are built at import, so what they hold depends on the
        environment this process was launched with -- which is why this
        rebuilds each recipe under a forced `http_proxy` instead of reading
        the module's own two. `build_opener` drops a `ProxyHandler({})`
        entirely rather than registering an inert one, so "no ProxyHandler"
        is what a proxy-free opener looks like.
        """
        with unittest.mock.patch.dict(os.environ, {"http_proxy": "http://proxy.example:3128"}):
            default = urllib.request.build_opener(sd_db.calls._NoRedirect)
            direct = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), sd_db.calls._NoRedirect)
        # The `http` entry only: `getproxies_environment` also carries `no`
        # when the environment sets `no_proxy`, which CI runners do, and this
        # is about which opener holds a proxy at all.
        self.assertEqual(
            [h.proxies.get("http") for h in default.handlers
             if isinstance(h, urllib.request.ProxyHandler)],
            ["http://proxy.example:3128"])
        self.assertEqual(
            [h for h in direct.handlers if isinstance(h, urllib.request.ProxyHandler)], [])


class ProxyRecorder(http.server.BaseHTTPRequestHandler):
    """A proxy that records the absolute URL it was asked for and refuses."""

    seen: list = []
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_POST(self):
        with self.lock:
            ProxyRecorder.seen.append(self.path)
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()


if __name__ == "__main__":
    unittest.main()
