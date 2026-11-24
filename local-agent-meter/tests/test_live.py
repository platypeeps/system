"""The live readings: `agent-meter.py usage` (sd:9256) and `services` (sd:9259).

Each reads a Settings list (a workflow database setting row), reads each
entry, and writes one `heartbeat` state row per entry. A failed read is a
row with status `unknown`, never `ok`; a provider with no usage API reads
`no-source`, never 0. Keys come from the environment by name, and no key
value reaches a row or the output.

The readers are driven in-process with a fake fetch, the HTTP helper and
the `http` kind against a server on 127.0.0.1, and the subcommands as the
job runs them: a subprocess on a store `initialise` made in a directory the
test owns. No test reaches the network.
"""

import contextlib
import datetime
import http.server
import io
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import sd_db
from sd_db import writes
from sd_db.migrate import initialise

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import service_status  # noqa: E402
import usage_sources  # noqa: E402
from usage_sources import ReadFailed  # noqa: E402

SCRIPT = HERE / "agent-meter.py"
NOW = datetime.datetime(2026, 11, 17, 20, 0, tzinfo=datetime.timezone.utc)
#: The UTC month start after NOW: a month spend's reset (sd:9701).
NEXT = "2026-12-01T00:00:00Z"
SECRET = "sk-test-0123456789-secret"
LOCAL = {"NO_PROXY": "*", "no_proxy": "*"}
#: urllib reads the lower-case name first; both go.
PROXIES = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")


class PinnedClock(datetime.datetime):
    """The readers' datetime here: a reading that falls back to the host
    clock fails. The fixtures are stamped 2026-11-17, so an unpinned read
    passed or failed by the host's date (sd:9575)."""

    @classmethod
    def now(cls, tz=None):
        raise AssertionError("a reader read the host clock: pass now=NOW")


def setUpModule():
    # usage_sources' own name only: the datetime module stays whole.
    clock = mock.patch.object(usage_sources, "datetime", types.SimpleNamespace(
        datetime=PinnedClock, timezone=datetime.timezone, timedelta=datetime.timedelta))
    clock.start()
    unittest.addModuleCleanup(clock.stop)


def local_only(test):
    """No proxy for this test's reads, the environment restored after it."""
    patcher = mock.patch.dict(os.environ, LOCAL)
    patcher.start()
    test.addCleanup(patcher.stop)
    for name in PROXIES:
        os.environ.pop(name, None)

#: Each keyed reader: its variable, an answer in the live shape (checked
#: 2026-11-17), and what the reading must hold.
#: One AI-credit item and one premium-request item, in the live shape.
AI_CREDITS = {"product": "Copilot", "sku": "Copilot AI Credits", "model": "m-1",
              "unitType": "ai-credits", "pricePerUnit": 0.01, "grossQuantity": 1800.0,
              "grossAmount": 18.0, "discountQuantity": 1500.0, "discountAmount": 15.0,
              "netQuantity": 300.0, "netAmount": 3.0}
PREMIUM = {"product": "Copilot", "sku": "Copilot Premium Request", "model": "m-1",
           "unitType": "requests", "pricePerUnit": 0.04, "grossQuantity": 350,
           "grossAmount": 14, "discountQuantity": 300, "discountAmount": 12,
           "netQuantity": 50, "netAmount": 2}

ANSWERS = {
    "openrouter": ("OPENROUTER_API_KEY",
                   {"data": {"usage_monthly": 12.5, "limit": 50, "limit_remaining": 37.5,
                             "limit_reset": "monthly"}},
                   {"spend": {"amount": 12.5, "unit": "usd", "period": "month",
                              "resets_at": NEXT, "limit": 50.0},
                    "balance": {"amount": 37.5, "unit": "usd"}}),
    "moonshot": ("MOONSHOT_API_KEY",
                 {"code": 0, "status": True, "data": {"available_balance": 4.25,
                                                      "voucher_balance": 0, "cash_balance": 4.25}},
                 {"balance": {"amount": 4.25, "unit": "usd"}}),
    "deepinfra": ("DEEPINFRA_API_KEY",
                  {"months": [{"period": "2026.11", "total_cost": 1234, "items": []}]},
                  {"spend": {"amount": 12.34, "unit": "usd", "period": "month",
                              "resets_at": NEXT}}),
    "minimax": ("MINIMAX_API_KEY",
                {"base_resp": {"status_code": 0}, "model_remains": [{
                    "model_name": "general", "start_time": 1795000000000,
                    "end_time": 1795018000000, "current_interval_remaining_percent": 80,
                    "weekly_start_time": 1794500000000, "weekly_end_time": 1795104800000,
                    "current_weekly_remaining_percent": 95}]},
                {"windows": [
                    {"name": "general", "used_percent": 20.0, "window_minutes": 300,
                     "resets_at": "2026-11-18T16:06:40Z"},
                    {"name": "general weekly", "used_percent": 5.0, "window_minutes": 10080,
                     "resets_at": "2026-11-19T16:13:20Z"}]}),
    "firecrawl": ("FIRECRAWL_API_KEY",
                  {"success": True, "data": {"remainingCredits": 250, "planCredits": 1000,
                                             "billingPeriodEnd": "2026-12-01T00:00:00Z"}},
                  {"balance": {"amount": 250.0, "unit": "credits"},
                   "windows": [{"name": "credits", "used_percent": 75.0, "window_minutes": None,
                                "resets_at": "2026-12-01T00:00:00Z"}]}),
    # /v2/user/remaining_quota answers 410 since 2026-11-18; /v3/users/me holds the wallet (sd:9415).
    "heygen": ("HEYGEN_API_KEY", {"data": {"billing_type": "wallet",
                                           "wallet": {"currency": "usd", "remaining_balance": 100.0}}},
               {"balance": {"amount": 100.0, "unit": "usd"}}),
    "runway": ("RUNWAYML_API_SECRET", {"creditBalance": 900, "tier": {}, "usage": {}},
               {"balance": {"amount": 900.0, "unit": "credits"}}),
    "openai": ("OPENAI_ADMIN_KEY",
               {"data": [{"results": [{"amount": {"value": 1.5, "currency": "usd"}}]},
                         {"results": [{"amount": {"value": 2.25, "currency": "usd"}}]}]},
               {"spend": {"amount": 3.75, "unit": "usd", "period": "month",
                              "resets_at": NEXT}}),
    # Checked 2026-11-19: Baseten's totals are decimal strings in usd.
    "baseten": ("BASETEN_API_KEY",
                {"dedicated_usage": {"subtotal": "0.00", "credits_used": "0.00", "total": "0.00",
                                     "minutes": 0, "breakdown": []},
                 "model_apis_usage": {"subtotal": "242.95", "credits_used": "0.00",
                                      "total": "242.95", "breakdown": []},
                 "training_usage": {"total": "1.05", "minutes": 3, "breakdown": []}},
                {"spend": {"amount": 244.0, "unit": "usd", "period": "month",
                              "resets_at": NEXT}}),
    # A metric with no use this month is null, not 0.
    "langsmith": ("LANGSMITH_ADMIN_KEY",
                  [{"billable_metric_name": "Base Traces", "value": 0.25,
                    "start_timestamp": "2026-11-01T00:00:00Z", "groups": {}},
                   {"billable_metric_name": "Sandbox", "value": None, "groups": None},
                   {"billable_metric_name": "Fleet", "value": 1.5, "groups": None}],
                  {"spend": {"amount": 1.75, "unit": "units", "period": "month",
                              "resets_at": NEXT}}),
    # You.com's API reference: the balance is in cents.
    "youcom": ("YDC_API_KEY",
               {"data": {"type": "account", "id": "abc", "attributes": {"balance": 718924.5}}},
               {"balance": {"amount": 7189.245, "unit": "usd"}}),
    "openwhispr": ("OPENWHISPR_API_KEY",
                   {"data": {"words_used": 2500, "words_remaining": 7500, "limit": 10000,
                             "plan": "basic", "is_subscribed": True,
                             "current_period_end": "2026-12-01T00:00:00.000Z"}},
                   {"spend": {"amount": 2500.0, "unit": "words", "period": "billing period",
                              "resets_at": "2026-12-01T00:00:00.000Z", "limit": 10000.0},
                    "balance": {"amount": 7500.0, "unit": "words"},
                    "windows": [{"name": "words", "used_percent": 25.0, "window_minutes": None,
                                 "resets_at": "2026-12-01T00:00:00.000Z"}]}),
    # Both GitHub readers ask /user for the login, then the month's usage
    # (checked 2026-11-19); one answer here serves both calls (sd:9592).
    "github": ("GH_TOKEN",
               {"login": "operator", "usageItems": [
                   {"date": "2026-11-02", "product": "actions", "sku": "actions_linux",
                    "quantity": 120, "unitType": "Minutes", "pricePerUnit": 0.008,
                    "grossAmount": 0.96, "discountAmount": 0.96, "netAmount": 0},
                   {"date": "2026-11-04", "product": "actions", "sku": "actions_macos",
                    "quantity": 30, "unitType": "Minutes", "pricePerUnit": 0.08,
                    "grossAmount": 2.4, "discountAmount": 2.4, "netAmount": 0},
                   # Actions storage is no minutes (sd:9928).
                   {"date": "2026-11-04", "product": "actions", "sku": "actions_storage",
                    "quantity": 5, "unitType": "GigabyteHours", "pricePerUnit": 0.0003,
                    "grossAmount": 0.0015, "discountAmount": 0.0015, "netAmount": 0},
                   {"date": "2026-11-03", "product": "copilot", "sku": "copilot_pro",
                    "quantity": 1, "unitType": "UserMonths", "pricePerUnit": 10,
                    "grossAmount": 10, "discountAmount": 0, "netAmount": 10}]},
               {"spend": {"amount": 10.0, "unit": "usd", "period": "month",
                              "resets_at": NEXT},
                "minutes": 150.0}),
    # A personal plan on AI credits (checked 2026-11-19): the AI-credit
    # report holds the month; its premium-request report is empty.
    "copilot": ("GH_TOKEN",
                {"login": "operator", "timePeriod": {"year": 2026, "month": 11},
                 "user": "operator", "usageItems": [AI_CREDITS]},
                {"spend": {"amount": 3.0, "unit": "usd", "period": "month",
                              "resets_at": NEXT},
                 "credits": 1800.0}),
    # Daily buckets; each amount a decimal string in cents (checked
    # 2026-11-19, sd:9594).
    "anthropic-api": ("ANTHROPIC_ADMIN_KEY",
                      {"data": [{"starting_at": "2026-11-01T00:00:00Z",
                                 "ending_at": "2026-11-02T00:00:00Z",
                                 "results": [{"currency": "USD", "amount": "150", "model": None}]},
                                {"starting_at": "2026-11-02T00:00:00Z",
                                 "ending_at": "2026-11-03T00:00:00Z",
                                 "results": [{"currency": "USD", "amount": "225.00"}]}],
                       "has_more": False, "next_page": None},
                      {"spend": {"amount": 3.75, "unit": "usd", "period": "month",
                              "resets_at": NEXT}}),
    # Resend's GET /usage (API reference, retrieve-usage; sd:9719): the
    # calendar month's emails, sent and received, against the plan.
    "resend": ("RESEND_API_KEY",
               {"object": "usage",
                "emails": {"daily": {"used": 10, "limit": None, "sent": 8, "received": 2,
                                     "resets_at": "2026-11-18T00:00:00.000Z"},
                           "monthly": {"used": 750, "limit": 3000, "sent": 700, "received": 50,
                                       "resets_at": "2026-12-01T00:00:00.000Z"}},
                "contacts": {"used": 10, "limit": 3000}, "broadcasts": {"used": 2, "limit": None},
                "rate_limit": {"limit": 1000, "duration": "1000ms"}},
               {"spend": {"amount": 750.0, "unit": "emails", "period": "month",
                          "resets_at": "2026-12-01T00:00:00.000Z", "limit": 3000.0},
                "balance": {"amount": 2250.0, "unit": "emails"},
                "windows": [{"name": "emails", "used_percent": 25.0, "window_minutes": None,
                             "resets_at": "2026-12-01T00:00:00.000Z"}]}),
}

#: Values no number in an answer may be taken from.
EXTREMES = ("12", True, None, float("nan"), float("inf"), float("-inf"), 10**400, [], {}, [1])


def answering(answer, calls=None):
    def fetch(url, headers=None, **_):
        if calls is not None:
            calls.append((url, headers))
        return json.loads(json.dumps(answer)) if not callable(answer) else answer()
    return fetch


def failing(message):
    def fetch(url, headers=None, **_):
        raise ReadFailed(message)
    return fetch


def replace_numbers(value, extreme):
    """Every number in value replaced by extreme (bools left alone)."""
    if isinstance(value, dict):
        return {k: replace_numbers(v, extreme) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_numbers(v, extreme) for v in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return extreme
    return value


class Readers(unittest.TestCase):
    def test_a_heygen_account_without_a_usd_wallet_is_no_usd_wallet(self):
        # Lead re-review on sd:9415: a plan account (no wallet, a null one,
        # or one in another currency) reads as one plain unknown.
        for data in ({"billing_type": "subscription"}, {"billing_type": "subscription", "wallet": None},
                     {"billing_type": "wallet", "wallet": {"currency": "eur", "remaining_balance": 5}}):
            with self.subTest(data=data):
                got = usage_sources.read_provider("heygen", {"HEYGEN_API_KEY": SECRET},
                                                  answering({"data": data}), NOW)
                self.assertEqual((got["status"], got["source"], got["error"]),
                                 ("unknown", "api", "no usd wallet"))
                self.assertIsNone(got["balance"])

    def test_each_reader_reads_its_answer(self):
        for name, (var, answer, expected) in ANSWERS.items():
            with self.subTest(provider=name):
                calls = []
                got = usage_sources.read_provider(name, {var: SECRET}, answering(answer, calls), NOW)
                self.assertEqual(got["status"], "ok", got)
                self.assertEqual(got["read_at"], "2026-11-17T20:00:00Z")
                for field, value in expected.items():
                    self.assertEqual(got[field], value)
                self.assertTrue(calls)
                for url, headers in calls:
                    self.assertTrue(url.startswith("https://"))
                    self.assertIn(SECRET, " ".join(headers.values()))

    def test_a_window_that_reset_is_never_current(self):
        # sd:9256 round 8: the live usage path stored a window whose reset
        # had passed as ok, while write_rows dropped it.
        accounts = [{"number": 2, "active": True, "pct": 67.0, "resetsAt": "2026-11-17T20:00:00Z",
                     "fetchedAt": "2026-11-17T12:00:00Z"}]
        got = usage_sources.claude_reading(accounts, None, NOW)
        self.assertEqual((got["status"], got["windows"], got.get("stale")), ("unknown", [], 1))
        self.assertEqual(got["error"], "every window reset after its reading")
        var, answer, _ = ANSWERS["firecrawl"]
        answer = put(answer, ("data", "billingPeriodEnd"), "2026-11-01T00:00:00Z")
        got = usage_sources.read_provider("firecrawl", {var: SECRET}, answering(answer), NOW)
        self.assertEqual((got["status"], got["windows"], got.get("stale")), ("ok", [], 1))

    def test_a_minimax_model_name_is_capped_and_kept_out_of_errors(self):
        # sd:9256 round 8: the reply's model name went whole into labels
        # and into the error text.
        var, answer, _ = ANSWERS["minimax"]
        long = json.loads(json.dumps(answer))
        long["model_remains"][0]["model_name"] = "m" * 5000
        got = usage_sources.read_provider("minimax", {var: SECRET}, answering(long), NOW)
        self.assertEqual(got["status"], "ok", got)
        self.assertTrue(all(len(w["name"]) <= usage_sources.LABEL for w in got["windows"]))
        long["model_remains"][0]["end_time"] = 0
        got = usage_sources.read_provider("minimax", {var: SECRET}, answering(long), NOW)
        self.assertEqual((got["status"], got["error"]), ("unknown", "end_time is not a window"))

    def test_a_missing_key_is_unknown_and_names_the_variable_only(self):
        for name, (var, answer, _) in ANSWERS.items():
            with self.subTest(provider=name):
                calls = []
                got = usage_sources.read_provider(name, {var: ""}, answering(answer, calls), NOW)
                self.assertEqual((got["status"], got["error"]), ("unknown", "%s not set" % var))
                self.assertEqual(calls, [])

    def test_a_failed_fetch_is_unknown_never_ok(self):
        for name, (var, _, _) in ANSWERS.items():
            with self.subTest(provider=name):
                got = usage_sources.read_provider(name, {var: SECRET}, failing("HTTP 401"), NOW)
                self.assertEqual((got["status"], got["error"]), ("unknown", "HTTP 401"))
                self.assertIsNone(got["read_at"])
                self.assertNotIn(SECRET, json.dumps(got))

    def test_an_answer_of_another_shape_is_unknown_never_ok(self):
        shapes = [{}, [], "x", None, 5, {"data": "x"}, {"data": None}, {"months": "x"},
                  {"base_resp": {"status_code": 1}, "model_remains": []}, {"code": 1, "data": {}}]
        for name, (var, _, _) in ANSWERS.items():
            for shape in shapes:
                with self.subTest(provider=name, shape=shape):
                    got = usage_sources.read_provider(name, {var: SECRET}, answering(shape), NOW)
                    self.assertEqual(got["status"], "unknown", got)

    def test_every_extreme_number_is_unknown_not_a_crash_or_a_reading(self):
        for name, (var, answer, _) in ANSWERS.items():
            for extreme in EXTREMES:
                with self.subTest(provider=name, extreme=extreme):
                    bent = replace_numbers(answer, extreme)
                    if name == "moonshot":
                        bent["code"] = 0
                    if name == "minimax":
                        bent["base_resp"]["status_code"] = 0
                    # Baseten's totals are decimal strings by its schema.
                    if name == "baseten":
                        bent = {k: {**v, "total": extreme} for k, v in answer.items()}
                    if name == "anthropic-api":
                        continue  # no JSON number: see the amount-string test
                    got = usage_sources.read_provider(name, {var: SECRET}, answering(bent), NOW)
                    # sd:9256 round 7: "12" read as a number; only Codex's
                    # balance and Baseten's totals are strings by schema.
                    # A LangSmith metric of null is a month with no use.
                    if (name, extreme) in (("baseten", "12"), ("langsmith", None)):
                        self.assertEqual(got["status"], "ok", got)
                        continue
                    self.assertEqual(got["status"], "unknown", got)
                    json.dumps(got, allow_nan=False)

    def test_an_openai_bucket_of_another_shape_is_unknown_not_zero(self):
        # sd:9256 review: a number raised TypeError; no results read as 0.
        for bucket in ({"results": 5}, {}, {"results": None}, {"results": "x"}, 5, None):
            with self.subTest(bucket=bucket):
                got = usage_sources.read_provider(
                    "openai", {"OPENAI_ADMIN_KEY": SECRET}, answering({"data": [bucket]}), NOW)
                self.assertEqual(got["status"], "unknown", got)
                self.assertIsNone(got["spend"])
        got = usage_sources.read_provider(
            "openai", {"OPENAI_ADMIN_KEY": SECRET}, answering({"data": [{"results": []}]}), NOW)
        self.assertEqual((got["status"], got["spend"]["amount"]), ("ok", 0.0))

    def test_an_unlimited_openwhispr_plan_reads_words_used_only(self):
        answer = {"data": {"words_used": 40, "words_remaining": -1, "limit": -1,
                           "current_period_end": "2027-08-20T15:38:34.000Z"}}
        got = usage_sources.read_provider("openwhispr", {"OPENWHISPR_API_KEY": SECRET},
                                          answering(answer), NOW)
        self.assertEqual((got["status"], got["windows"], got["balance"]), ("ok", [], None))
        self.assertEqual(got["spend"]["amount"], 40.0)
        for bad in ({"words_used": -1, "limit": -1}, {"words_used": 1, "limit": -2}):
            with self.subTest(bad=bad):
                got = usage_sources.read_provider("openwhispr", {"OPENWHISPR_API_KEY": SECRET},
                                                  answering({"data": bad}), NOW)
                self.assertEqual(got["status"], "unknown", got)

    def test_the_month_readers_ask_for_utc_days(self):
        calls = []
        first = datetime.datetime(2026, 12, 1, 0, 30, tzinfo=datetime.timezone.utc)
        usage_sources.read_provider("baseten", {"BASETEN_API_KEY": SECRET},
                                    answering(ANSWERS["baseten"][1], calls), first)
        self.assertIn("start_date=2026-12-01&end_date=2026-12-02", calls[0][0])
        # A clock in another zone still asks for the UTC day.
        late = datetime.datetime(2026, 11, 30, 23, 0, tzinfo=datetime.timezone(
            -datetime.timedelta(hours=7)))
        usage_sources.read_provider("baseten", {"BASETEN_API_KEY": SECRET},
                                    answering(ANSWERS["baseten"][1], calls), late)
        self.assertIn("start_date=2026-12-01&end_date=2026-12-02", calls[1][0])
        usage_sources.read_provider("langsmith", {"LANGSMITH_ADMIN_KEY": SECRET},
                                    answering(ANSWERS["langsmith"][1], calls), late)
        self.assertIn("starting_on=2026-12-01T00:00:00Z&ending_before=2027-01-01T00:00:00Z",
                      calls[2][0])

    def test_a_baseten_usage_section_of_another_shape_is_unknown(self):
        # Lane review: a malformed section was dropped and the rest
        # published as the month's total.
        for bad in ([], None, "x", 5, {"subtotal": "1.00"}):
            for name in ("dedicated_usage", "inference"):
                with self.subTest(bad=bad, name=name):
                    answer = {"model_apis_usage": {"total": "2.00"}, name: bad}
                    got = usage_sources.read_provider("baseten", {"BASETEN_API_KEY": SECRET},
                                                      answering(answer), NOW)
                    self.assertEqual((got["status"], got["spend"]), ("unknown", None), got)

    def test_a_baseten_section_under_a_new_name_still_counts(self):
        # A new usage kind is never left out of the month's total.
        answer = {"model_apis_usage": {"total": "2.00"}, "inference": {"total": "3.00"}}
        got = usage_sources.read_provider("baseten", {"BASETEN_API_KEY": SECRET},
                                          answering(answer), NOW)
        self.assertEqual((got["status"], got["spend"]["amount"]), ("ok", 5.0), got)

    def test_langsmith_reads_the_admin_key_not_the_tracing_key(self):
        # sd:9872: billing answers the admin key; the API key is tracing's.
        calls = []
        got = usage_sources.read_provider("langsmith", {"LANGSMITH_ADMIN_KEY": SECRET},
                                          answering(ANSWERS["langsmith"][1], calls), NOW)
        self.assertEqual(got["status"], "ok", got)
        self.assertEqual(calls[0][1]["x-api-key"], SECRET)
        calls = []
        got = usage_sources.read_provider("langsmith", {"LANGSMITH_API_KEY": SECRET},
                                          answering(ANSWERS["langsmith"][1], calls), NOW)
        self.assertEqual((got["status"], got["error"]),
                         ("unknown", "LANGSMITH_ADMIN_KEY not set"))
        self.assertEqual(calls, [])

    def test_a_langsmith_null_value_with_groups_is_unknown_not_zero(self):
        # A null value reads 0 only with no groups: groups would hold use.
        for groups in ({"traces": 4}, [1]):
            with self.subTest(groups=groups):
                answer = [{"billable_metric_name": "Base Traces", "value": None,
                           "groups": groups}]
                got = usage_sources.read_provider("langsmith", {"LANGSMITH_ADMIN_KEY": SECRET},
                                                  answering(answer), NOW)
                self.assertEqual((got["status"], got["spend"]), ("unknown", None), got)
        for groups in (None, {}, []):
            with self.subTest(groups=groups):
                answer = [{"billable_metric_name": "Fleet", "value": None, "groups": groups}]
                got = usage_sources.read_provider("langsmith", {"LANGSMITH_ADMIN_KEY": SECRET},
                                                  answering(answer), NOW)
                self.assertEqual((got["status"], got["spend"]["amount"]), ("ok", 0.0), got)

    def test_a_baseten_total_that_is_no_decimal_string_is_unknown(self):
        for total in ("", "-1.00", "1e400", "nan", " 1.00", "1,000.00", "inf"):
            with self.subTest(total=total):
                got = usage_sources.read_provider(
                    "baseten", {"BASETEN_API_KEY": SECRET},
                    answering({"model_apis_usage": {"total": total}}), NOW)
                self.assertEqual((got["status"], got["spend"]), ("unknown", None), got)

    def test_the_month_readers_ask_for_this_month(self):
        calls = []
        usage_sources.read_provider("baseten", {"BASETEN_API_KEY": SECRET},
                                    answering(ANSWERS["baseten"][1], calls), NOW)
        usage_sources.read_provider("langsmith", {"LANGSMITH_ADMIN_KEY": SECRET},
                                    answering(ANSWERS["langsmith"][1], calls), NOW)
        # Baseten's end date is exclusive: the next UTC day takes in today.
        self.assertIn("start_date=2026-11-01&end_date=2026-11-18", calls[0][0])
        self.assertIn("starting_on=2026-11-01T00:00:00Z&ending_before=2026-12-01T00:00:00Z",
                      calls[1][0])
    def test_the_github_readers_ask_this_month_for_the_login_they_read(self):
        # sd:9592: the billing endpoints are per user; /user names the login.
        for name, path in (("github", "/settings/billing/usage?"),
                           ("copilot", "/settings/billing/ai_credit/usage?")):
            with self.subTest(provider=name):
                var, answer, _ = ANSWERS[name]
                calls = []
                usage_sources.read_provider(name, {var: SECRET}, answering(answer, calls), NOW)
                self.assertEqual([url for url, _ in calls], [
                    "https://api.github.com/user",
                    "https://api.github.com/users/operator" + path + "year=2026&month=11"])

    def test_a_github_login_of_another_shape_is_unknown_and_never_sent(self):
        for login in (None, 5, "", "a/b", "../x", "x" * 40, "-x", "x?y"):
            for name in ("github", "copilot"):
                with self.subTest(provider=name, login=login):
                    var, answer, _ = ANSWERS[name]
                    calls = []
                    got = usage_sources.read_provider(
                        name, {var: SECRET}, answering(put(answer, ("login",), login), calls), NOW)
                    self.assertEqual((got["status"], got["error"]), ("unknown", "no login"))
                    self.assertEqual(len(calls), 1)

    def test_the_github_row_counts_the_months_actions_minutes(self):
        """sd:9928: Actions minutes beside the spend, so free-plan use shows;
        a quantity that is not a number is unknown, never 0."""
        def github(items):
            return usage_sources.read_provider("github", {"GH_TOKEN": SECRET},
                                               answering({"login": "operator", "usageItems": items}), NOW)
        minutes = {"product": "actions", "unitType": "Minutes", "netAmount": 0}
        got = github([{**minutes, "quantity": 7}, {**minutes, "grossQuantity": 3},
                      {"product": "copilot", "unitType": "Minutes", "quantity": 99, "netAmount": 1}])
        self.assertEqual((got["status"], got["minutes"], got["spend"]["amount"]), ("ok", 10.0, 1.0), got)
        self.assertEqual(github([])["minutes"], 0.0)
        # GitHub's documented user report names the product "Actions"
        # and the unit "minutes" (sd:9928 review): case does not count.
        documented = {"date": "2023-08-01", "product": "Actions", "sku": "Actions Linux",
                      "quantity": 100, "unitType": "minutes", "pricePerUnit": 0.008,
                      "grossAmount": 0.8, "discountAmount": 0, "netAmount": 0.8,
                      "repositoryName": "user/example"}
        self.assertEqual(github([documented])["minutes"], 100.0)
        # A quantity that is not a count makes the minutes unknown and
        # keeps the bill (sd:9928 lane review): never 0, never no row.
        bill = {"product": "copilot", "unitType": "Minutes", "quantity": 1, "netAmount": 4}
        for bad in (None, "7", True, float("nan"), float("inf"), -1):
            with self.subTest(quantity=bad):
                got = github([{**minutes, "quantity": bad}, bill])
                self.assertEqual((got["status"], got["minutes"], got["spend"]["amount"]),
                                 ("ok", None, 4.0), got)

    def test_an_empty_github_month_is_zero_and_a_missing_list_is_unknown(self):
        for name in ("github", "copilot"):
            with self.subTest(provider=name):
                got = usage_sources.read_provider(
                    name, {"GH_TOKEN": SECRET},
                    answering({"login": "operator", "usageItems": []}), NOW)
                self.assertEqual((got["status"], got["spend"]["amount"]), ("ok", 0.0))
                for items in (None, "x", 5, [5], [None]):
                    got = usage_sources.read_provider(
                        name, {"GH_TOKEN": SECRET},
                        answering({"login": "operator", "usageItems": items}), NOW)
                    self.assertEqual(got["status"], "unknown", got)

    def copilot(self, ai, premium, calls=None):
        """The copilot reading with each report's answer: items, or a
        ReadFailed to raise."""
        def fetch(url, headers=None, **_):
            if calls is not None:
                calls.append(url)
            if url.endswith("/user"):
                return {"login": "operator"}
            answer = ai if "/ai_credit/" in url else premium
            if isinstance(answer, ReadFailed):
                raise answer
            return {"timePeriod": {"year": 2026, "month": 11}, "user": "operator",
                    "usageItems": json.loads(json.dumps(answer))}
        return usage_sources.read_provider("copilot", {"GH_TOKEN": SECRET}, fetch, NOW)

    def test_an_ai_credit_plan_reads_credits_and_never_requests(self):
        # sd:9592 review: personal plans on AI credits bill in that report;
        # the premium-request report missed their usage and overage.
        calls = []
        got = self.copilot([AI_CREDITS, AI_CREDITS], [PREMIUM], calls)
        self.assertEqual((got["status"], got["spend"]["amount"], got["credits"]), ("ok", 6.0, 3600.0))
        self.assertNotIn("requests", got)
        self.assertFalse([url for url in calls if "premium_request" in url])

    def test_a_premium_request_plan_falls_back_and_reads_requests(self):
        for ai in ([], ReadFailed("HTTP 404")):
            with self.subTest(ai=ai):
                got = self.copilot(ai, [PREMIUM])
                self.assertEqual((got["status"], got["spend"]["amount"], got["requests"]),
                                 ("ok", 2.0, 350.0))
                self.assertNotIn("credits", got)

    def test_an_empty_copilot_month_and_a_missing_report(self):
        got = self.copilot([], [])
        self.assertEqual((got["status"], got["spend"]["amount"], got["credits"]), ("ok", 0.0, 0.0))
        # sd:9592 review: an empty AI-credit report does not establish the
        # plan, so a fallback that cannot be read is no reading, not zero.
        for failure in ("HTTP 404", "HTTP 500"):
            got = self.copilot([], ReadFailed(failure))
            self.assertEqual((got["status"], got["error"], got["spend"]),
                             ("unknown", failure, None))
        got = self.copilot(ReadFailed("HTTP 404"), ReadFailed("HTTP 404"))
        self.assertEqual((got["status"], got["error"]), ("unknown", "HTTP 404"))
        # Only a 404 says a report does not apply: any other failure is unknown.
        got = self.copilot(ReadFailed("HTTP 500"), [PREMIUM])
        self.assertEqual((got["status"], got["error"]), ("unknown", "HTTP 500"))

    def test_an_ai_credit_item_in_another_unit_is_unknown(self):
        for unit in ("requests", None, "AI-Credits", 5):
            with self.subTest(unit=unit):
                got = self.copilot([put(AI_CREDITS, ("unitType",), unit)], [])
                self.assertEqual((got["status"], got["error"]),
                                 ("unknown", "an AI-credit item in another unit"))

    def test_the_anthropic_cost_report_asks_for_this_month(self):
        var, answer, _ = ANSWERS["anthropic-api"]
        calls = []
        usage_sources.read_provider("anthropic-api", {var: SECRET}, answering(answer, calls), NOW)
        (url, headers), = calls
        self.assertEqual(url, "https://api.anthropic.com/v1/organizations/cost_report"
                              "?starting_at=2026-11-01T00:00:00Z&bucket_width=1d&limit=31")
        self.assertEqual((headers["x-api-key"], headers["anthropic-version"]),
                         (SECRET, "2023-06-01"))

    def test_an_anthropic_amount_that_is_not_decimal_cents_is_unknown(self):
        # sd:9594: the amount is a string, so a number, nan or an exponent
        # must not read as cents.
        var, answer, _ = ANSWERS["anthropic-api"]
        path = ("data", 0, "results", 0, "amount")
        for amount in (150, None, "", "nan", "inf", "1e400", "-5", "1,5", "12abc", " 12", "1" * 400):
            with self.subTest(amount=amount):
                got = usage_sources.read_provider(
                    "anthropic-api", {var: SECRET}, answering(put(answer, path, amount)), NOW)
                self.assertEqual(got["status"], "unknown", got)
                self.assertIsNone(got["spend"])
        for bent in (put(answer, ("data", 0, "results", 0, "currency"), "EUR"),
                     put(answer, ("has_more",), True), put(answer, ("has_more",), None),
                     put(answer, ("data", 0, "results"), None)):
            with self.subTest(bent=bent):
                got = usage_sources.read_provider("anthropic-api", {var: SECRET},
                                                  answering(bent), NOW)
                self.assertEqual(got["status"], "unknown", got)
        got = usage_sources.read_provider("anthropic-api", {var: SECRET},
                                          answering({"data": [], "has_more": False}), NOW)
        self.assertEqual((got["status"], got["spend"]["amount"]), ("ok", 0.0))

    def test_a_reader_that_raises_is_unknown_and_costs_no_other(self):
        def strange(env, fetch, now):
            raise TypeError("x")
        with mock.patch.dict(usage_sources.READERS, {"strange": strange}):
            got = usage_sources.read_provider("strange", {}, failing("unreached"), NOW)
        self.assertEqual((got["status"], got["error"]), ("unknown", "another shape: TypeError"))

    def test_no_source_providers_read_no_source_not_zero(self):
        for name in usage_sources.NO_SOURCE:
            with self.subTest(provider=name):
                got = usage_sources.read_provider(name, {}, failing("unreached"), NOW)
                self.assertEqual(got["status"], "no-source")
                self.assertEqual((got["windows"], got["balance"], got["spend"]), ([], None, None))

    def test_a_zerogpu_error_reads_the_seconds_left(self):
        # sd:9586: Hugging Face has no quota API; its error text is the source.
        got = usage_sources.huggingface_error(
            "You have exceeded your GPU quota (60s requested vs. 23s left). Try later.", NOW)
        self.assertEqual(got, usage_sources.reading(
            "huggingface", "ok", "error", read_at="2026-11-17T20:00:00Z",
            balance={"amount": 23.0, "unit": "seconds"}))
        for text in ("", "GPU task aborted", "(60s requested vs. -1s left)", "(xs requested vs. 1s left)"):
            with self.subTest(text=text):
                self.assertIsNone(usage_sources.huggingface_error(text, NOW))

    def test_a_name_with_no_reader_is_unknown(self):
        got = usage_sources.read_provider("nosuch", {}, failing("unreached"), NOW)
        self.assertEqual((got["status"], got["error"]), ("unknown", "no reader for this name"))

    def test_the_starting_list_names_every_reader_once(self):
        names, problems = usage_sources.parse_providers(usage_sources.STARTING)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(entry[0] for entry in names), sorted(["claude", "codex", *usage_sources.READERS,
                                                *usage_sources.NO_SOURCE, *usage_sources.PASSED]))

    def test_the_providers_grammar(self):
        names, problems = usage_sources.parse_providers(
            "# comment\nclaude\n\n codex  # trailing\nclaude\nBad Name\nx/y\n")
        self.assertEqual(names, [("claude", None, None), ("codex", None, None)])
        self.assertEqual(problems, ["line 6: not a provider name", "line 7: not a provider name"])

    def test_a_providers_line_may_carry_a_positive_cap(self):
        # sd:9701: Baseten's budget has no read API; the list holds it.
        entries, problems = usage_sources.parse_providers(
            "baseten cap=250\nopenai  cap=12.5 # month\nbaseten cap=9\ndeepinfra cap=0\n"
            "groq cap=-1\nmoonshot cap=1e3\nyoucom cap=\nheygen cap=5 cap=6\nrunway limit=5\n"
            "firecrawl cap=inf\nlangsmith cap=1" + "0" * 20 + "\nhf cap 5\n")
        # sd:9749: a malformed cap keeps the row, with no cap; another
        # trailer is still no provider line.
        self.assertEqual(entries, [("baseten", 250.0, None), ("openai", 12.5, None), *[
            (name, None, None) for name in ("deepinfra", "groq", "moonshot", "youcom", "heygen",
                                      "firecrawl", "langsmith", "hf")]])
        self.assertEqual(problems, ["line %d: %s" % (n, "not a provider name" if n == 9 else
                                                     "cap=N wants N a positive decimal")
                                    for n in range(4, 13)])

    def test_a_month_spend_resets_at_the_next_utc_month(self):
        # sd:9701: the cycle end, also from a late local day of the month.
        late = datetime.datetime(2026, 12, 31, 23, 30, tzinfo=datetime.timezone(
            datetime.timedelta(hours=-7)))
        var, answer, _ = ANSWERS["openai"]
        got = usage_sources.read_provider("openai", {var: SECRET}, answering(answer), late)
        self.assertEqual(got["spend"]["resets_at"], "2027-02-01T00:00:00Z")

    def test_openwhispr_spend_resets_at_its_period_end_or_not_at_all(self):
        var, answer, _ = ANSWERS["openwhispr"]
        for end, expected in ((None, None), ("2026-12-05T00:00:00Z", "2026-12-05T00:00:00Z")):
            with self.subTest(end=end):
                bent = put(answer, ("data", "current_period_end"), end)
                got = usage_sources.read_provider("openwhispr", {var: SECRET}, answering(bent), NOW)
                self.assertEqual(got["spend"].get("resets_at"), expected, got)

    def test_an_ended_openwhispr_period_is_no_reading(self):
        # sd:9749: the limit and resets_at of an ended period read ok; on
        # an unlimited plan the list's cap set a current-looking budget.
        var, answer, _ = ANSWERS["openwhispr"]
        for limit in (10000, -1):
            for end in ("2026-11-17T20:00:00Z", "2026-11-01T00:00:00Z"):
                with self.subTest(limit=limit, end=end):
                    ended = put(put(answer, ("data", "current_period_end"), end),
                                ("data", "limit"), limit)
                    got = usage_sources.read_provider("openwhispr", {var: SECRET},
                                                      answering(ended), NOW, cap=5000.0)
                    self.assertEqual((got["status"], got["spend"]), ("unknown", None), got)

    def test_an_unlimited_openwhispr_plan_reads_past_a_bad_period_end(self):
        # sd:9749: a bad end fails only a capped plan's reading.
        var, answer, _ = ANSWERS["openwhispr"]
        unlimited = put(answer, ("data", "limit"), -1)
        for end in ("not a time", 5):
            with self.subTest(end=end):
                bent = put(unlimited, ("data", "current_period_end"), end)
                got = usage_sources.read_provider("openwhispr", {var: SECRET},
                                                  answering(bent), NOW)
                self.assertEqual(got["status"], "ok", got)
                self.assertEqual(got["spend"], {"amount": 2500.0, "unit": "words",
                                                "period": "billing period"})
        capped = put(answer, ("data", "current_period_end"), "not a time")
        got = usage_sources.read_provider("openwhispr", {var: SECRET}, answering(capped), NOW)
        self.assertEqual(got["status"], "unknown", got)

    def test_openrouters_monthly_limit_counts_byok_when_the_key_says_so(self):
        # sd:9749: BYOK use counts toward the cap with include_byok_in_limit.
        var, answer, _ = ANSWERS["openrouter"]
        byok = {"data": {**answer["data"], "byok_usage_monthly": 7.5,
                         "include_byok_in_limit": True}}
        got = usage_sources.read_provider("openrouter", {var: SECRET}, answering(byok), NOW)
        self.assertEqual((got["spend"]["amount"], got["spend"]["limit"]), (20.0, 50.0), got)
        apart = put(byok, ("data", "include_byok_in_limit"), False)
        got = usage_sources.read_provider("openrouter", {var: SECRET}, answering(apart), NOW)
        self.assertEqual(got["spend"]["amount"], 12.5, got)
        unknown = put(byok, ("data", "byok_usage_monthly"), None)
        got = usage_sources.read_provider("openrouter", {var: SECRET}, answering(unknown), NOW)
        self.assertEqual(got["status"], "unknown", got)

    def test_openwhisprs_own_word_limit_wins_over_the_lists_cap(self):
        # Review on sd:9701: cap=5000 overrode a 10,000-word plan.
        var, answer, _ = ANSWERS["openwhispr"]
        got = usage_sources.read_provider("openwhispr", {var: SECRET}, answering(answer), NOW, cap=5000.0)
        self.assertEqual(got["spend"]["limit"], 10000.0, got)
        unlimited = put(answer, ("data", "limit"), -1)
        got = usage_sources.read_provider("openwhispr", {var: SECRET}, answering(unlimited), NOW, cap=5000.0)
        self.assertEqual(got["spend"]["limit"], 5000.0, got)

    def test_a_spend_limit_is_openrouters_own_then_the_lists_cap(self):
        var, answer, _ = ANSWERS["openrouter"]
        got = usage_sources.read_provider("openrouter", {var: SECRET}, answering(answer), NOW, cap=9.0)
        self.assertEqual(got["spend"]["limit"], 50.0)
        unset = put(answer, ("data", "limit"), None)
        got = usage_sources.read_provider("openrouter", {var: SECRET}, answering(unset), NOW, cap=9.0)
        self.assertEqual(got["spend"]["limit"], 9.0)
        got = usage_sources.read_provider("openrouter", {var: SECRET}, answering(unset), NOW)
        self.assertNotIn("limit", got["spend"])
        # Review on sd:9701: a key limit that resets daily, weekly or
        # never is no cap on the month's spend.
        for reset in ("daily", "weekly", None, "yearly", 5):
            with self.subTest(reset=reset):
                other = put(answer, ("data", "limit_reset"), reset)
                got = usage_sources.read_provider("openrouter", {var: SECRET},
                                                  answering(other), NOW)
                self.assertEqual(got["status"], "ok", got)
                self.assertNotIn("limit", got["spend"])
                got = usage_sources.read_provider("openrouter", {var: SECRET},
                                                  answering(other), NOW, cap=9.0)
                self.assertEqual(got["spend"]["limit"], 9.0)
        var, answer, _ = ANSWERS["baseten"]
        got = usage_sources.read_provider("baseten", {var: SECRET}, answering(answer), NOW, cap=250.0)
        self.assertEqual(got["spend"], {"amount": 244.0, "unit": "usd", "period": "month",
                                        "resets_at": NEXT, "limit": 250.0})
        # A cap on a provider with no spend changes nothing.
        var, answer, _ = ANSWERS["moonshot"]
        got = usage_sources.read_provider("moonshot", {var: SECRET}, answering(answer), NOW, cap=5.0)
        self.assertEqual((got["status"], got["spend"]), ("ok", None))

    def test_claude_reads_every_account_and_the_active_ones_week(self):
        accounts = [{"number": 1, "active": False, "pct": 10.0, "resetsAt": "a"},
                    {"number": 2, "active": True, "pct": 67.0, "resetsAt": "2026-11-20T00:00:00Z",
                     "fetchedAt": "2026-11-17T12:00:00Z"}]
        got = usage_sources.claude_reading(accounts, None, NOW)
        self.assertEqual(got["status"], "ok")
        # sd:9256 review: cswap's fetch time, not the collector's.
        self.assertEqual(got["read_at"], "2026-11-17T12:00:00Z")
        self.assertEqual(got["accounts"], accounts)
        self.assertEqual(got["windows"], [{"name": "seven_day", "used_percent": 67.0,
                                           "window_minutes": 10080,
                                           "resets_at": "2026-11-20T00:00:00Z"}])
        failed = usage_sources.claude_reading(None, "exit 1", NOW)
        self.assertEqual((failed["status"], failed["error"]), ("unknown", "exit 1"))
        for fetched in (None, "", "yesterday", "2026-11-17T12:00:00"):
            with self.subTest(fetched=fetched):
                accounts[1]["fetchedAt"] = fetched
                got = usage_sources.claude_reading(accounts, None, NOW)
                self.assertEqual((got["status"], got["read_at"], got["windows"]),
                                 ("unknown", None, []))


class Server:
    """A server on 127.0.0.1 answering each path from `routes`: (code, body)."""

    def __init__(self, routes):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen.append((self.path, dict(self.headers)))
                code, body, *extra = outer.routes.get(self.path, (404, b"{}"))
                if callable(body):
                    # A raw answer: the route writes the whole response.
                    try:
                        body(self.wfile)
                    except OSError:
                        pass
                    return
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for name, value in (extra[0] if extra else {}).items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())
                except OSError:
                    pass  # the client stopped reading: a cap or a deadline

            def log_message(self, *args):
                pass

        self.routes, self.seen = routes, []
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Fetch(unittest.TestCase):
    def setUp(self):
        local_only(self)
        self.server = Server({"/ok": (200, {"a": 1}), "/broken": (500, b"oops"),
                              "/garbage": (200, b"<html>")})
        self.addCleanup(self.server.close)

    def test_a_failed_read_is_read_failed_with_no_body(self):
        self.assertEqual(usage_sources.fetch_json(self.server.url + "/ok"), {"a": 1})
        for path, message in (("/broken", "HTTP 500"), ("/garbage", "unparseable answer"),
                              ("/missing", "HTTP 404")):
            with self.subTest(path=path):
                with self.assertRaises(ReadFailed) as caught:
                    usage_sources.fetch_json(self.server.url + path, {"Authorization": SECRET})
                self.assertEqual(str(caught.exception), message)
        with self.assertRaises(ReadFailed) as caught:
            usage_sources.fetch_json("http://127.0.0.1:%d/" % closed_port())
        self.assertNotIn(SECRET, str(caught.exception))

    def test_a_redirect_to_another_origin_never_carries_the_key(self):
        # sd:9256 review: urllib sent the key on to the redirect's target.
        other = Server({"/ok": (200, {"b": 2})})
        self.addCleanup(other.close)
        self.server.routes["/away"] = (302, b"", {"Location": other.url + "/ok"})
        self.server.routes["/here"] = (302, b"", {"Location": "/ok"})
        with self.assertRaises(ReadFailed) as caught:
            usage_sources.fetch_json(self.server.url + "/away",
                                     {"Authorization": "Bearer " + SECRET, "X-Api-Key": SECRET})
        self.assertEqual(str(caught.exception), "redirect to another origin")
        self.assertEqual(other.seen, [])
        got = usage_sources.fetch_json(self.server.url + "/here", {"X-Api-Key": SECRET})
        self.assertEqual(got, {"a": 1})
        self.assertEqual(self.server.seen[-1][1].get("X-Api-Key"), SECRET)


def drip(head, piece, tail, times=100, pause=0.1):
    """A raw answer that sends head, then piece every pause seconds."""
    def write(out):
        out.write(head)
        for _ in range(times):
            out.write(piece)
            out.flush()
            time.sleep(pause)
        out.write(tail)
    return write


class Bounded(unittest.TestCase):
    """sd:9256 round 6: a source that keeps sending held the whole run."""

    def setUp(self):
        local_only(self)
        ok = b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
        self.server = Server({
            "/body": (200, drip(ok + b"\r\n[", b"1,", b"1]")),
            "/headers": (200, drip(ok, b"X-Drip: 1\r\n", b"\r\n{}")),
            "/big": (200, b'"' + b"a" * (1 << 20) + b'"'),
            "/google": (200, [dict(GOOGLE_OPEN, end="2026-11-17T19:00:00+00:00",
                                   external_desc="x" * (2 << 20))])})
        self.addCleanup(self.server.close)

    def timed(self, call):
        started = time.monotonic()
        with self.assertRaises(ReadFailed) as caught:
            call()
        self.assertLess(time.monotonic() - started, 4)
        return str(caught.exception)

    def test_a_feed_that_keeps_sending_is_read_failed_at_the_deadline(self):
        for path in ("/body", "/headers"):
            with self.subTest(path=path):
                self.timed(lambda: usage_sources.fetch_json(self.server.url + path, timeout=1))
        self.timed(lambda: service_status.read_http(self.server.url + "/headers", timeout=1))

    def test_a_google_history_over_the_default_cap_reads(self):
        # sd:9256 round 7: the google-incidents feeds are full histories.
        got = service_status.read_service(
            ("g", "google-incidents", self.server.url + "/google", []), NOW)
        self.assertEqual((got["status"], got.get("error")), ("ok", None))

    def test_an_answer_over_the_cap_is_read_failed(self):
        message = self.timed(lambda: usage_sources.fetch_json(self.server.url + "/big"))
        self.assertIn("bytes", message)


class Codex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.day = self.home / "sessions/2026/11/17"
        self.day.mkdir(parents=True)

    def rollout(self, name, lines, mtime):
        path = self.day / ("rollout-%s.jsonl" % name)
        path.write_text("".join(json.dumps(line) + "\n" if not isinstance(line, str) else line + "\n"
                                for line in lines), encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    @staticmethod
    def event(used=61.0, stamp="2026-11-17T19:00:00.000Z", **limits):
        rate = {"primary": {"used_percent": used, "window_minutes": 10080,
                            "resets_at": 4102444800},
                "secondary": None,
                "credits": {"has_credits": True, "unlimited": False, "balance": "62500.0"}}
        rate.update(limits)
        return {"timestamp": stamp, "type": "event_msg",
                "payload": {"type": "token_count", "info": {"cwd": "/x"}, "rate_limits": rate}}

    def test_the_newest_rollout_with_limits_is_the_reading(self):
        self.rollout("old", [self.event(used=10.0, stamp="2026-11-16T00:00:00Z")], 1000)
        self.rollout("new", [self.event(used=40.0, stamp="2026-11-17T18:00:00Z"),
                             self.event(used=61.0), "{not json"], 2000)
        got = usage_sources.codex_reading(self.home, now=NOW)
        self.assertEqual(got["status"], "ok")
        self.assertEqual(got["read_at"], "2026-11-17T19:00:00.000Z")
        self.assertEqual(got["windows"], [{"name": "primary", "used_percent": 61.0,
                                           "window_minutes": 10080,
                                           "resets_at": "2100-01-01T00:00:00Z"}])
        self.assertEqual(got["balance"], {"amount": 62500.0, "unit": "credits"})

    def test_a_newest_rollout_without_limits_falls_back_to_the_one_before(self):
        self.rollout("old", [self.event(used=10.0, stamp="2026-11-16T00:00:00Z")], 1000)
        self.rollout("new", [{"timestamp": "2026-11-17T19:00:00Z", "type": "session_meta",
                              "payload": {"type": "session_meta"}}], 2000)
        got = usage_sources.codex_reading(self.home, now=NOW)
        self.assertEqual((got["status"], got["read_at"]), ("ok", "2026-11-16T00:00:00Z"))

    def test_the_newest_event_wins_whatever_file_was_touched_last(self):
        # sd:9256 review: a file touched later held an older event.
        at = datetime.datetime(2026, 11, 17, 19, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.rollout("touched", [self.event(used=10.0, stamp="2026-11-17T10:00:00Z")], at + 7200)
        self.rollout("newer", [self.event(used=55.0, stamp="2026-11-17T19:00:00Z")], at + 60)
        got = usage_sources.codex_reading(self.home, now=NOW)
        self.assertEqual((got["read_at"], got["windows"][0]["used_percent"]),
                         ("2026-11-17T19:00:00Z", 55.0))

    def test_event_time_decides_whatever_the_mtimes_and_line_order(self):
        # sd:9256 lane review: a copied rollout (old mtime) and a line out
        # of time order hid the newest reading.
        self.rollout("new", [self.event(30.0, "2026-11-17T19:00:00Z")], 2000)
        self.rollout("copied", [self.event(90.0, "2026-11-17T21:00:00Z"),
                                self.event(50.0, "2026-11-17T20:00:00Z")], 1000)
        got = usage_sources.codex_reading(self.home, now=NOW + datetime.timedelta(hours=2))
        self.assertEqual(got["read_at"], "2026-11-17T21:00:00Z")

    def test_a_models_own_bucket_never_replaces_the_accounts(self):
        # sd:9256 review: a newer model-specific snapshot won on time.
        self.rollout("x", [self.event(used=40.0, stamp="2026-11-17T18:00:00Z", limit_id="codex"),
                           self.event(used=90.0, stamp="2026-11-17T19:00:00Z",
                                      limit_id="codex_other")], 2000)
        got = usage_sources.codex_reading(self.home, now=NOW)
        self.assertEqual((got["read_at"], got["windows"][0]["used_percent"]),
                         ("2026-11-17T18:00:00Z", 40.0))
        self.rollout("x", [self.event(used=90.0, limit_id="codex_other")], 2000)
        self.assertEqual(usage_sources.codex_reading(self.home, now=NOW)["status"], "unknown")

    def test_an_event_stamped_after_now_never_outranks_a_real_reading(self):
        # sd:9499: event time had no upper bound, so one future-stamped
        # token_count beat every real reading for good. Five minutes of
        # clock skew still read; a later stamp is skipped and counted.
        now = datetime.datetime(2026, 11, 17, 20, 0, tzinfo=datetime.timezone.utc)
        self.rollout("x", [self.event(used=40.0, stamp="2026-11-17T19:00:00Z"),
                           self.event(used=99.0, stamp="2027-01-01T00:00:00Z")], 2000)
        got = usage_sources.codex_reading(self.home, now=now)
        self.assertEqual((got["status"], got["read_at"], got["windows"][0]["used_percent"],
                          got.get("future")), ("ok", "2026-11-17T19:00:00Z", 40.0, 1))
        self.rollout("x", [self.event(used=40.0, stamp="2026-11-17T19:00:00Z"),
                           self.event(used=50.0, stamp="2026-11-17T20:04:00Z")], 2000)
        got = usage_sources.codex_reading(self.home, now=now)
        self.assertEqual((got["read_at"], got.get("future")), ("2026-11-17T20:04:00Z", None))
        self.rollout("x", [self.event(used=99.0, stamp="2027-01-01T00:00:00Z")], 2000)
        got = usage_sources.codex_reading(self.home, now=now)
        self.assertEqual((got["status"], got.get("future")), ("unknown", 1))
        self.assertIn("stamped after now", got["error"])

    def test_a_window_that_reset_is_never_current(self):
        # sd:9256 round 8: an idle Codex's window that had reset read ok.
        self.rollout("x", [self.event(primary={"used_percent": 61.0, "window_minutes": 10080,
                                               "resets_at": 1794992575})], 2000)
        later = datetime.datetime(2026, 11, 18, 10, 0, tzinfo=datetime.timezone.utc)
        got = usage_sources.codex_reading(self.home, now=later)
        self.assertEqual((got["status"], got["windows"], got.get("stale")), ("ok", [], 1))
        self.assertEqual(got["balance"], {"amount": 62500.0, "unit": "credits"})

    def test_no_limits_anywhere_is_unknown(self):
        got = usage_sources.codex_reading(self.home, now=NOW)
        self.assertEqual(got["status"], "unknown")
        self.rollout("bad", [self.event(primary="x"), self.event(used=float("nan"))], 1000)
        got = usage_sources.codex_reading(self.home, now=NOW)
        self.assertEqual(got["status"], "unknown")

    def test_every_extreme_value_is_skipped_not_a_reading(self):
        for field in ("used_percent", "window_minutes", "resets_at"):
            for extreme in EXTREMES + (0, -5, 1e308):
                with self.subTest(field=field, extreme=extreme):
                    if field == "used_percent" and extreme in (0, -5, "12", 1e308):
                        continue  # a number; the meter row refuses one out of range
                    if field == "resets_at" and extreme in (0, -5, "12"):
                        continue
                    if field != "used_percent" and extreme is None:
                        continue  # Codex's schema allows null; see the null test
                    event = self.event()
                    event["payload"]["rate_limits"]["primary"][field] = extreme
                    self.rollout("x", [event], 1000)
                    got = usage_sources.codex_reading(self.home, now=NOW)
                    self.assertEqual(got["status"], "unknown", got)
                    json.dumps(got, allow_nan=False)


FAKE_CODEX = """#!%s
import json, sys, time
if sys.argv[1:] != ["app-server"]:
    sys.exit(2)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("id") == 1:
        print(json.dumps({"id": 1, "result": {}}), flush=True)
    elif message.get("id") == 2:
        reply = %r
        if reply == "hang":
            time.sleep(60)
        print(json.dumps({"method": "account/updated", "params": {}}), flush=True)
        print(reply, flush=True)
"""


def live_snapshot(used=9, limit_id="codex"):
    return {"limitId": limit_id, "limitName": None, "planType": "pro",
            "primary": {"usedPercent": used, "windowDurationMins": 10080,
                        "resetsAt": 4102444800},
            "secondary": None,
            "credits": {"hasCredits": True, "unlimited": False, "balance": "100.5"}}


class CodexLive(unittest.TestCase):
    """sd:9655: an idle Codex's newest rollout is hours old, so the reading
    comes live from `codex app-server` (account/rateLimits/read); the
    rollouts are read only when the app-server gives no answer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        day = self.home / "sessions/2026/11/17"
        day.mkdir(parents=True)
        (day / "rollout-x.jsonl").write_text(
            json.dumps(Codex.event(used=61.0, stamp="2026-11-17T12:00:00Z")) + "\n",
            encoding="utf-8")
        self.binary = self.home / "codex"

    def answer(self, reply):
        text = reply if isinstance(reply, str) else json.dumps(reply)
        self.binary.write_text(FAKE_CODEX % (sys.executable, text), encoding="utf-8")
        self.binary.chmod(0o755)
        return usage_sources.codex_reading(self.home, now=NOW, binary=str(self.binary))

    def test_the_live_answer_is_the_reading_read_now(self):
        got = self.answer({"id": 2, "result": {"rateLimits": live_snapshot()}})
        self.assertEqual((got["status"], got["source"], got["read_at"]),
                         ("ok", "codex-app-server", "2026-11-17T20:00:00Z"))
        self.assertEqual(got["windows"], [{"name": "primary", "used_percent": 9.0,
                                           "window_minutes": 10080,
                                           "resets_at": "2100-01-01T00:00:00Z"}])
        self.assertEqual(got["balance"], {"amount": 100.5, "unit": "credits"})

    def test_the_accounts_bucket_wins_over_a_models(self):
        got = self.answer({"id": 2, "result": {
            "rateLimits": live_snapshot(used=90, limit_id="codex_other"),
            "rateLimitsByLimitId": {"codex": live_snapshot(used=12),
                                    "codex_other": live_snapshot(90, "codex_other")}}})
        self.assertEqual((got["source"], got["windows"][0]["used_percent"]),
                         ("codex-app-server", 12.0))

    def test_no_live_answer_falls_back_to_the_rollouts(self):
        for reply in ({"id": 2, "error": {"code": -32600, "message": "x"}},
                      {"id": 2, "result": {"rateLimits": live_snapshot(limit_id="other")}},
                      {"id": 2, "result": {"rateLimits": {"primary": "x"}}},
                      {"id": 2, "result": []}, "{not json", "hang"):
            with self.subTest(reply=reply), mock.patch.object(usage_sources, "TIMEOUT", 2):
                got = self.answer(reply)
                self.assertEqual((got["status"], got["source"], got["read_at"]),
                                 ("ok", "codex-sessions", "2026-11-17T12:00:00Z"))
                self.assertTrue(got["live_error"])
                self.assertNotIn('"x"', json.dumps(got))

    def test_a_missing_binary_falls_back_to_the_rollouts(self):
        got = usage_sources.codex_reading(self.home, now=NOW,
                                          binary=str(self.home / "absent"))
        self.assertEqual((got["source"], got["live_error"]),
                         ("codex-sessions", "FileNotFoundError"))
        got = usage_sources.codex_reading(self.home, now=NOW)
        self.assertEqual((got["source"], got["live_error"]),
                         ("codex-sessions", "no codex binary"))


SUMMARY = {"page": {"url": "https://status.example.test"},
           "status": {"indicator": "minor", "description": "Minor"},
           "components": [{"name": "Copilot", "status": "degraded_performance"},
                          {"name": "Git", "status": "operational"}],
           "incidents": [{"id": "abc", "name": "Copilot **slow**", "status": "investigating",
                          "impact": "minor", "created_at": "2026-11-17T19:00:00Z"},
                         {"id": "old", "name": "Done", "status": "resolved"}]}


#: A Cloudflare-like page (sd:9722): a North America group, a Europe group,
#: an ungrouped component, an incident per region and an old one.
REGIONS = {
    "page": {"url": "https://status.example.test"},
    "status": {"indicator": "major"},
    "components": [
        {"id": "na", "name": "North America", "group": True, "group_id": None,
         "status": "partial_outage"},
        {"id": "iad", "name": "Ashburn, VA (IAD)", "group": False, "group_id": "na",
         "status": "degraded_performance"},
        {"id": "eu", "name": "Europe", "group": True, "group_id": None,
         "status": "major_outage"},
        {"id": "fra", "name": "Frankfurt (FRA)", "group": False, "group_id": "eu",
         "status": "major_outage"},
        {"id": "api", "name": "API", "group": False, "group_id": None,
         "status": "operational"}],
    "incidents": [
        {"id": "i1", "name": "IAD slow", "status": "investigating", "impact": "minor",
         "created_at": "2026-11-17T10:00:00Z", "components": [{"id": "iad"}]},
        {"id": "i2", "name": "FRA down", "status": "identified", "impact": "major",
         "created_at": "2026-11-17T10:00:00Z", "components": [{"id": "fra"}]},
        {"id": "i3", "name": "Workers errors", "status": "monitoring", "impact": "minor",
         "created_at": "2026-11-10T10:00:00Z", "updated_at": "2026-11-12T10:00:00Z",
         "components": []}]}


class ServiceFilters(unittest.TestCase):
    """groups= and stale= on a services line (sd:9722)."""

    def read(self, options, answer=REGIONS):
        return service_status.read_service(
            ("svc", "statuspage", "https://status.example.test/api", [], options), NOW,
            fetch=answering(answer))

    def test_an_empty_component_id_is_no_proof_an_incident_is_outside(self):
        # Review: an excluded component with id "" and a critical
        # incident linked only to "" read ok: the empty id counted as
        # proof the incident sat outside the groups.
        calm = [dict(c, status="operational") for c in REGIONS["components"]]
        for empty in ("", "   "):
            with self.subTest(empty=empty):
                comps = [dict(c, id=empty) if c["id"] == "fra" else c for c in calm]
                incident = dict(REGIONS["incidents"][0], impact="critical",
                                components=[{"id": empty}])
                got = self.read({"groups": ["north-america"]},
                                dict(REGIONS, components=comps, incidents=[incident]))
                self.assertEqual(got["status"], "unknown", got)
        # A well-formed page with an empty incident link keeps the incident.
        incident = dict(REGIONS["incidents"][0], impact="critical",
                        components=[{"id": ""}])
        got = self.read({"groups": ["north-america"]},
                        dict(REGIONS, components=calm, incidents=[incident]))
        self.assertEqual(got["status"], "outage", got)

    def test_a_group_with_a_malformed_name_is_unknown(self):
        # Review: a group in major_outage named [] had no key, so it
        # sat outside the chosen groups and its outage read ok.
        for name in ([], None, "", "   ", 7):
            with self.subTest(name=name):
                bad = [dict(c, name=name, status="major_outage") if c["id"] == "eu" else c
                       for c in REGIONS["components"]]
                got = self.read({"groups": ["north-america"]},
                                dict(REGIONS, components=bad, incidents=[]))
                self.assertEqual(got["status"], "unknown", got)

    def test_a_filter_on_a_page_with_no_components_is_unknown(self):
        # Review: stale=3 on a major page with neither components nor
        # incidents recomputed ok, a false all-clear.
        # stale= alone keeps the page's status (lane review).
        bare = {"status": {"indicator": "major"}}
        for options, want in (({"stale": 3}, "outage"),
                              ({"groups": ["north-america"]}, "unknown")):
            for answer in (bare, dict(bare, components=[]), dict(bare, incidents=[])):
                with self.subTest(options=options, answer=answer):
                    got = self.read(options, answer)
                    # No components key is a truncated answer: unknown.
                    self.assertEqual(got["status"],
                                     want if "components" in answer else "unknown", got)

    def test_a_filter_on_a_page_worse_than_its_listed_causes_is_unknown(self):
        # Review: a critical page with no `incidents` key (or an empty one)
        # and only operational components read ok under either filter;
        # nothing showed the outage sat outside the groups or was stale.
        calm = [dict(c, status="operational") for c in REGIONS["components"]]
        bare = {"status": {"indicator": "critical"}, "components": calm}
        for options, want in (({"stale": 3}, "outage"),
                              ({"groups": ["north-america"]}, "unknown")):
            for answer in (bare, dict(bare, incidents=[])):
                with self.subTest(options=options, incidents="incidents" in answer):
                    got = self.read(options, answer)
                    self.assertEqual(got["status"], want, got)
        # Review: an operational selected group on a critical page whose
        # only cause outside it is degraded; the drop explains no outage.
        mild = [dict(c, status="degraded_performance") if c["id"] == "fra" else c
                for c in calm]
        for incidents in ([], [dict(REGIONS["incidents"][0], components=[{"id": "fra"}])]):
            with self.subTest(mild=True, incidents=len(incidents)):
                got = self.read({"groups": ["north-america"]},
                                dict(bare, components=mild, incidents=incidents))
                self.assertEqual(got["status"], "unknown", got)
        # A listed cause the filter drops still filters as before.
        got = self.read({"groups": ["north-america"]},
                        dict(REGIONS, components=[dict(c, status="operational")
                                                  if c["id"] != "fra" else c
                                                  for c in REGIONS["components"]]))
        self.assertEqual(got["status"], "degraded", got)

    def test_the_options_grammar(self):
        services, problems = service_status.parse_services(
            "cf statuspage https://example.test/s.json groups=north-america,europe stale=3\n"
            "cg statuspage https://example.test/s.json cloudflare stale=1\n"
            "a statuspage https://example.test/s.json colour=red\n"
            "b statuspage https://example.test/s.json stale=0\n"
            "c statuspage https://example.test/s.json stale=x\n"
            "d statuspage https://example.test/s.json groups=\n"
            "e statuspage https://example.test/s.json groups=North\n"
            "f slack https://example.test/s.json stale=3\n"
            "g statuspage https://example.test/s.json stale=3 cloudflare\n"
            "h statuspage https://example.test/s.json stale=3 stale=4\n")
        self.assertEqual(services, [
            ("cf", "statuspage", "https://example.test/s.json", [],
             {"groups": ["north-america", "europe"], "stale": 3}),
            ("cg", "statuspage", "https://example.test/s.json", ["cloudflare"], {"stale": 1})])
        self.assertEqual(problems, [
            "line 3: option colour is not groups or stale",
            "line 4: stale is not a whole number of days from 1 to 365",
            "line 5: stale is not a whole number of days from 1 to 365",
            "line 6: groups is not lower-case group names",
            "line 7: groups is not lower-case group names",
            "line 8: options need kind statuspage",
            "line 9: want <name> <kind> <url> [provider] [key=value ...]",
            "line 10: option stale given twice"])

    def test_a_stale_value_out_of_range_is_refused_not_raised(self):
        # Review: 5,000 digits raised ValueError in int() and stopped every
        # service; stale=1000000000 passed and overflowed timedelta later.
        for value in ("9" * 5000, "1000000000", "366", "0", "-1", "１"):
            with self.subTest(value=value[:12]):
                services, problems = service_status.parse_services(
                    "a statuspage https://example.test/s.json stale=%s\n"
                    "b statuspage https://example.test/s.json\n" % value)
                self.assertEqual(problems,
                                 ["line 1: stale is not a whole number of days"
                                  " from 1 to 365"])
                self.assertEqual([s[0] for s in services], ["b"])
        services, problems = service_status.parse_services(
            "a statuspage https://example.test/s.json stale=365\n")
        self.assertEqual((problems, services[0][4]), ([], {"stale": 365}))

    def test_the_starting_list_filters_cloudflare(self):
        services, _ = service_status.parse_services(service_status.STARTING)
        options = {s[0]: s[4] for s in services}
        self.assertEqual(options["cloudflare"],
                         {"groups": ["north-america", "cloudflare-sites-and-services"],
                          "stale": 3})
        self.assertEqual(options["claude"], {})

    def test_the_old_default_differs_only_in_the_cloudflare_line(self):
        old = service_status.RETIRED[0].splitlines()
        new = service_status.STARTING.splitlines()
        self.assertEqual([(a, b) for a, b in zip(old, new) if a != b],
                         [("cloudflare statuspage"
                           " https://www.cloudflarestatus.com/api/v2/summary.json",
                           "cloudflare statuspage"
                           " https://www.cloudflarestatus.com/api/v2/summary.json"
                           " groups=north-america,cloudflare-sites-and-services stale=3")])
        # Suno came after that default (sd:9903).
        self.assertEqual(new[len(old):], ["suno http https://suno.com"])

    def test_a_group_filter_keeps_its_components_and_ungrouped_ones(self):
        got = self.read({"groups": ["north-america"]})
        self.assertEqual(got["components"],
                         [{"name": "North America", "status": "partial_outage"},
                          {"name": "Ashburn, VA (IAD)", "status": "degraded_performance"}])

    def test_a_group_filter_drops_an_incident_wholly_outside_it(self):
        got = self.read({"groups": ["north-america"]})
        self.assertEqual([i["title"] for i in got["incidents"]],
                         ["IAD slow", "Workers errors"])

    def test_a_stale_filter_drops_an_old_open_incident(self):
        got = self.read({"stale": 3})
        self.assertEqual([i["title"] for i in got["incidents"]], ["IAD slow", "FRA down"])

    def stale_page(self, **incident):
        base = {"id": "s", "name": "Old", "status": "monitoring", "impact": "minor",
                "created_at": "2026-11-13T10:00:00Z", "components": []}
        return dict(REGIONS, incidents=[dict(base, **incident)])

    def test_a_stale_filter_never_hides_a_major_or_critical_incident(self):
        # Lane review: a four-day-old critical incident updated today
        # disappeared, because stale= aged it by creation alone.
        for impact in ("major", "critical"):
            for updated in ("2026-11-17T09:00:00Z", "2026-11-13T10:00:00Z"):
                with self.subTest(impact=impact, updated=updated):
                    got = self.read({"stale": 3}, self.stale_page(
                        impact=impact, updated_at=updated))
                    self.assertEqual(([i["title"] for i in got["incidents"]],
                                      got["status"]), (["Old"], "outage"))

    def test_a_stale_filter_ages_an_incident_by_its_last_update(self):
        hidden = self.read({"stale": 3}, self.stale_page(
            updated_at="2026-11-14T10:00:00Z"))
        self.assertEqual(hidden["incidents"], [])
        fresh = self.read({"stale": 3}, self.stale_page(
            updated_at="2026-11-17T09:00:00Z"))
        self.assertEqual([i["title"] for i in fresh["incidents"]], ["Old"])

    def test_a_stale_filter_keeps_an_incident_with_no_last_update(self):
        # Review: with no updated_at, stale= aged an old, still
        # identified minor incident by created_at alone and hid it.
        for change in ({}, {"updated_at": None}, {"updated_at": ""}):
            with self.subTest(change=change):
                page = self.stale_page(**change)
                got = self.read({"stale": 3}, page)
                self.assertEqual([i["title"] for i in got["incidents"]], ["Old"])

    def test_a_stale_filter_keeps_an_incident_still_being_worked(self):
        # Lane review at 34ced33b: with groups= and stale=3, an open
        # in-scope incident with no update for four days stopped
        # counting, so a live outage read ok. Only monitoring ages.
        calm = [dict(c, status="operational") for c in REGIONS["components"]]
        for status in ("investigating", "identified"):
            for options in ({"groups": ["north-america"], "stale": 3}, {"stale": 3}):
                with self.subTest(status=status, options=options):
                    page = dict(self.stale_page(
                        status=status, updated_at="2026-11-13T09:00:00Z",
                        components=[{"id": "iad"}]),
                        components=calm, status={"indicator": "minor"})
                    got = self.read(options, page)
                    self.assertEqual(([i["title"] for i in got["incidents"]],
                                      got["status"]), (["Old"], "degraded"))

    def test_a_stale_filter_keeps_an_incident_it_cannot_place(self):
        # Class pass: an impact or a time of another shape keeps it.
        for change in ({"impact": None}, {"impact": "severe"}, {"impact": 3},
                       {"created_at": None}, {"created_at": "soon"},
                       {"updated_at": "soon"}, {"updated_at": 5}):
            with self.subTest(change=change):
                got = self.read({"stale": 3}, self.stale_page(**change))
                self.assertEqual(len(got["incidents"]), 1)

    def test_the_status_is_recomputed_from_what_is_kept(self):
        self.assertEqual(self.read({"groups": ["north-america"], "stale": 3})["status"],
                         "degraded")
        self.assertEqual(self.read({"groups": ["europe"], "stale": 3})["status"], "outage")
        # The page's own indicator matches its one listed cause, a minor
        # stale incident: worse would have no cause to filter (review).
        quiet = dict(REGIONS, incidents=REGIONS["incidents"][2:], status={"indicator": "minor"})
        got = self.read({"groups": ["north-america"], "stale": 3},
                        dict(quiet, components=[dict(c, status="operational")
                                                for c in REGIONS["components"]]))
        self.assertEqual((got["status"], got["incidents"], got["components"]), ("ok", [], []))

    def test_an_incident_on_a_kept_group_itself_is_kept(self):
        # Review: the group's own id was not a kept id, so an incident
        # linked only to the group was dropped and the reading was ok.
        page = dict(REGIONS, components=[dict(c, status="operational")
                                         for c in REGIONS["components"]],
                    incidents=[dict(REGIONS["incidents"][1], name="NA down",
                                    components=[{"id": "na"}])])
        got = self.read({"groups": ["north-america"]}, page)
        self.assertEqual((got["status"], [i["title"] for i in got["incidents"]]),
                         ("outage", ["NA down"]))

    def test_an_incident_with_no_proof_it_is_outside_is_kept(self):
        # Review: components=[{}] or an id the page does not list was
        # read as outside the groups, and a major incident read ok.
        for links in ([{}], [{"id": None}], [{"id": 5}], [{"id": ["fra"]}], [{"id": "gone"}],
                      [{"id": "fra"}, {}], [{"id": "fra"}, {"id": "gone"}]):
            with self.subTest(links=links):
                page = dict(REGIONS, components=[dict(c, status="operational")
                                                 for c in REGIONS["components"]],
                            incidents=[dict(REGIONS["incidents"][1], components=links)])
                got = self.read({"groups": ["north-america"]}, page)
                self.assertEqual((got["status"], len(got["incidents"])), ("outage", 1))

    def test_the_shipped_cloudflare_line_sees_a_global_outage(self):
        # Review: the page puts its core products in a group of their own
        # and has no ungrouped component (summary.json, 2026-11-20), so
        # groups=north-america alone read ok through a global outage.
        page = {"page": {"url": "https://status.example.test"},
                "status": {"indicator": "critical"},
                "components": [
                    {"id": "svc", "name": "Cloudflare Sites and Services", "group": True,
                     "group_id": None, "status": "major_outage"},
                    {"id": "cdn", "name": "CDN/Cache", "group": False, "group_id": "svc",
                     "status": "major_outage"},
                    {"id": "na", "name": "North America", "group": True, "group_id": None,
                     "status": "operational"},
                    {"id": "iad", "name": "Ashburn, VA (IAD)", "group": False,
                     "group_id": "na", "status": "operational"}],
                "incidents": [{"id": "g", "name": "CDN down", "status": "investigating",
                               "impact": "critical", "created_at": "2026-11-17T19:00:00Z",
                               "components": [{"id": "cdn"}]}]}
        services, _ = service_status.parse_services(service_status.STARTING)
        options = {s[0]: s[4] for s in services}["cloudflare"]
        got = self.read(options, page)
        self.assertEqual((got["status"], [i["title"] for i in got["incidents"]]),
                         ("outage", ["CDN down"]))
        self.assertEqual(got["components"],
                         [{"name": "Cloudflare Sites and Services", "status": "major_outage"},
                          {"name": "CDN/Cache", "status": "major_outage"}])

    def test_a_kept_group_row_counts_toward_the_status(self):
        # Review round 3: the group row was dropped before the status,
        # so a group in major_outage over operational children read ok.
        components = [dict(c, status="operational") for c in REGIONS["components"]]
        components[0]["status"] = "major_outage"
        got = self.read({"groups": ["north-america"]},
                        dict(REGIONS, components=components, incidents=[]))
        self.assertEqual((got["status"], got["components"]),
                         ("outage", [{"name": "North America", "status": "major_outage"}]))

    def test_a_component_tree_the_filter_cannot_trust_is_unknown(self):
        # Review round 2: North America's id was null and Ashburn still
        # named the old id, so Ashburn's outage was dropped and read ok.
        # Every assumption the filter makes about the tree is checked.
        def page(change):
            components = [dict(c) for c in REGIONS["components"]]
            components[1]["status"] = "major_outage"
            change(components)
            return dict(REGIONS, components=components, incidents=[])

        def setter(index, key, value):
            return lambda cs: cs[index].update({key: value})
        cases = {
            "group id null": setter(0, "id", None),
            "group id number": setter(0, "id", 5),
            "child names no group": setter(1, "group_id", "gone"),
            "child parent not a string": setter(1, "group_id", 5),
            "child parent not a group": setter(1, "group_id", "api"),
            "component id missing": lambda cs: cs[4].pop("id"),
            "component id repeated": setter(4, "id", "iad"),
            "group flag not a bool": setter(2, "group", "yes"),
            "two groups one name": setter(2, "name", "North  America"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                got = self.read({"groups": ["north-america"]}, page(change))
                self.assertEqual(got["status"], "unknown", got)
        self.assertEqual(self.read({"groups": ["north-america"]}, page(lambda cs: None))
                         ["status"], "outage")

    def test_a_group_flag_or_parent_of_another_type_is_unknown(self):
        # Lane review: `group: 1` passed as a flag (1 == True) but missed
        # `is True`, so Europe counted as ungrouped, and a list-valued
        # parent raised TypeError. Each field the filter reads is typed.
        def page(index, key, value):
            components = [dict(c, status="operational") for c in REGIONS["components"]]
            components[index][key] = value
            components[3]["status"] = "major_outage"
            return dict(REGIONS, components=components, incidents=[])
        cases = [(2, "group", 1), (2, "group", 1.0), (2, "group", 0),
                 (2, "group", "yes"), (0, "group", 1), (3, "group", 0),
                 (3, "group_id", ["eu"]), (3, "group_id", {"id": "eu"}),
                 (3, "group_id", 0), (3, "group_id", False)]
        for index, key, value in cases:
            with self.subTest(index=index, key=key, value=value):
                got = self.read({"groups": ["north-america"]}, page(index, key, value))
                self.assertEqual(got["status"], "unknown", got)
                self.assertNotIn("another shape: ", got["error"])

    def test_a_kept_outage_stands_on_a_worse_page(self):
        # Lane review: a critical page with a kept major_outage component
        # and no open incident read unknown with no components; the
        # confirmed outage was erased because critical outranks it.
        calm = [dict(c, status="operational") for c in REGIONS["components"]]
        down = [dict(c, status="major_outage") if c["id"] == "iad" else c for c in calm]
        page = dict(REGIONS, status={"indicator": "critical"}, components=down,
                    incidents=[])
        for options in ({"groups": ["north-america"]}, {"stale": 3}):
            with self.subTest(options=options):
                got = self.read(options, page)
                self.assertEqual((got["status"], got["components"]),
                                 ("outage", [{"name": "Ashburn, VA (IAD)",
                                              "status": "major_outage"}]))
        # A kept cause milder than the page still reads unknown.
        mild = [dict(c, status="degraded_performance") if c["id"] == "iad" else c
                for c in calm]
        got = self.read({"groups": ["north-america"]}, dict(page, components=mild))
        self.assertEqual(got["status"], "unknown", got)

    def test_stale_alone_keeps_the_page_status(self):
        # Lane review: stale= alone read ok for a minor page whose only
        # cause was an old incident still in monitoring, and unknown for
        # a major page with no listed cause. Age filters the list only;
        # without groups= the status is the page's.
        calm = [dict(c, status="operational") for c in REGIONS["components"]]
        old = dict(REGIONS["incidents"][2], components=[{"id": "fra"}])
        cases = [("minor", [old], "degraded"), ("major", [], "outage"),
                 ("none", [old], "ok")]
        for indicator, incidents, want in cases:
            with self.subTest(indicator=indicator):
                page = dict(REGIONS, status={"indicator": indicator},
                            components=calm, incidents=incidents)
                got = self.read({"stale": 3}, page)
                self.assertEqual((got["status"], got["incidents"]), (want, []), got)

    def test_malformed_incident_links_keep_a_confirmed_outage(self):
        # Lane review: an incident whose component links were ["na"]
        # raised, so a kept major_outage read unknown, not outage.
        # A link list of another shape is no proof to drop the incident.
        calm = [dict(c, status="operational") for c in REGIONS["components"]]
        down = [dict(c, status="major_outage") if c["id"] == "iad" else c for c in calm]
        for links in (["na"], "na", 5, [None], [{"id": "fra"}, "fra"], {"id": "fra"}):
            with self.subTest(links=links):
                incident = dict(REGIONS["incidents"][1], components=links)
                got = self.read({"groups": ["north-america"]},
                                dict(REGIONS, components=down, incidents=[incident]))
                self.assertEqual((got["status"], got["components"],
                                  [i["title"] for i in got["incidents"]]),
                                 ("outage", [{"name": "Ashburn, VA (IAD)",
                                              "status": "major_outage"}], ["FRA down"]))

    def test_kept_maintenance_explains_no_minor_page(self):
        # Lane review: a minor page whose only cause was maintenance the
        # filter kept read degraded: the incident path raised it to
        # minor, the component path matched on degraded alone.
        calm = [dict(c, status="operational") for c in REGIONS["components"]]
        held = [dict(c, status="under_maintenance") if c["id"] == "iad" else c
                for c in calm]
        work = {"id": "m", "name": "IAD work", "status": "identified",
                "impact": "maintenance", "created_at": "2026-11-17T10:00:00Z",
                "components": [{"id": "iad"}]}
        for label, components, incidents in (("component", held, []),
                                             ("incident", calm, [work])):
            with self.subTest(label):
                page = dict(REGIONS, status={"indicator": "minor"},
                            components=components, incidents=incidents)
                got = self.read({"groups": ["north-america"]}, page)
                self.assertEqual(got["status"], "unknown", got)
                got = self.read({"groups": ["north-america"]},
                                dict(page, status={"indicator": "maintenance"}))
                self.assertEqual(got["status"], "degraded", got)

    def test_a_new_incident_with_lagging_links_still_counts(self):
        # Review at f2f500f2: a new North America incident whose
        # component links lag the feed must not read ok beside an
        # excluded European cause of the same severity.
        calm = [dict(c, status="operational") for c in REGIONS["components"]]
        eu = [dict(c, status="partial_outage") if c["id"] in ("eu", "fra") else c
              for c in calm]
        new = {"id": "n1", "name": "New", "status": "investigating",
               "impact": "minor", "created_at": "2026-11-21T00:00:00Z",
               "updated_at": "2026-11-21T00:00:00Z"}
        for links in ([], None, [{"id": "iad"}]):
            with self.subTest(links=links):
                incident = dict(new) if links is None else dict(new, components=links)
                got = self.read({"groups": ["north-america"], "stale": 3},
                                dict(REGIONS, status={"indicator": "minor"},
                                     components=eu, incidents=[incident]))
                self.assertEqual((got["status"], [i["title"] for i in got["incidents"]]),
                                 ("degraded", ["New"]), got)

    def test_rulings_on_scoped_causes_and_quiet_incidents(self):
        # Rebutted lane findings at 82d35179, pinned (operator rulings).
        calm = [dict(c, status="operational") for c in REGIONS["components"]]
        # An excluded major_outage alone explains a major page: ok.
        out = [dict(c, status="major_outage") if c["id"] == "fra" else c
               for c in calm]
        got = self.read({"groups": ["north-america"]},
                        dict(REGIONS, components=out, incidents=[]))
        self.assertEqual(got["status"], "ok", got)
        # A quiet minor incident in a kept region, in monitoring with no
        # update for five days, is the long-running kind stale=3 hides.
        quiet = {"id": "q", "name": "IAD slow", "status": "monitoring",
                 "impact": "minor", "created_at": "2026-11-10T10:00:00Z",
                 "updated_at": "2026-11-12T10:00:00Z",
                 "components": [{"id": "iad"}]}
        page = dict(REGIONS, status={"indicator": "minor"},
                    components=calm, incidents=[quiet])
        got = self.read({"groups": ["north-america"], "stale": 3}, page)
        self.assertEqual((got["status"], got["incidents"]), ("ok", []))

    def test_maintenance_explains_no_minor_page(self):
        # Lane review: maintenance ranked with minor, so an excluded
        # under_maintenance component or maintenance incident explained
        # a minor page, and groups= read ok.
        calm = [dict(c, status="operational") for c in REGIONS["components"]]
        held = [dict(c, status="under_maintenance") if c["id"] == "fra" else c
                for c in calm]
        work = {"id": "m", "name": "FRA work", "status": "identified",
                "impact": "maintenance", "created_at": "2026-11-17T10:00:00Z",
                "components": [{"id": "fra"}]}
        for label, components, incidents in (("component", held, []),
                                             ("incident", calm, [work])):
            with self.subTest(label):
                page = dict(REGIONS, status={"indicator": "minor"},
                            components=components, incidents=incidents)
                got = self.read({"groups": ["north-america"]}, page)
                self.assertEqual(got["status"], "unknown", got)
                got = self.read({"groups": ["north-america"]},
                                dict(page, status={"indicator": "maintenance"}))
                self.assertEqual(got["status"], "ok", got)

    def test_only_maintenance_explains_a_maintenance_page(self):
        # Lane review at 128a9737: a maintenance page with an operational
        # North America and a European partial_outage read ok under
        # groups=: the excluded outage outranked maintenance, so it
        # counted as the cause. An outage is another kind of cause.
        calm = [dict(c, status="operational") for c in REGIONS["components"]]
        for status in ("partial_outage", "degraded_performance", "major_outage"):
            with self.subTest(status=status):
                out = [dict(c, status=status) if c["id"] in ("eu", "fra") else c
                       for c in calm]
                page = dict(REGIONS, status={"indicator": "maintenance"},
                            components=out, incidents=[])
                got = self.read({"groups": ["north-america"]}, page)
                self.assertEqual(got["status"], "unknown", got)
        # A listed maintenance cause, kept or not, still explains it.
        held = [dict(c, status="under_maintenance") if c["id"] == "fra"
                else dict(c, status="partial_outage") if c["id"] == "eu" else c
                for c in calm]
        got = self.read({"groups": ["north-america"]},
                        dict(REGIONS, status={"indicator": "maintenance"},
                             components=held, incidents=[]))
        self.assertEqual(got["status"], "ok", got)

    def test_an_impact_of_none_or_unknown_explains_no_indicator(self):
        # Lane review: a dropped incident of impact none (or one the
        # feed does not document) counted as a minor cause, so a minor
        # page with operational components read ok under either filter.
        calm = [dict(c, status="operational") for c in REGIONS["components"]]
        for impact in ("none", "severe", None, 3):
            old = {"id": "s", "name": "Old", "status": "monitoring", "impact": impact,
                   "created_at": "2026-11-10T10:00:00Z",
                   "updated_at": "2026-11-10T10:00:00Z", "components": [{"id": "fra"}]}
            page = dict(REGIONS, status={"indicator": "minor"}, components=calm,
                        incidents=[old])
            for options in ({"groups": ["north-america"]}, {"stale": 3}):
                with self.subTest(impact=impact, options=options):
                    got = self.read(options, page)
                    if "groups" in options:
                        self.assertEqual(got["status"], "unknown", got)
                    else:
                        # stale= alone keeps the page's minor status.
                        self.assertEqual(got["status"], "degraded", got)

    def test_a_line_without_groups_reads_no_component_id(self):
        # Lane review: the reader hashed every component id to build
        # the outside set, even with no groups= option, so a page in
        # major outage with a component id of [] read unknown
        # (TypeError) where the base read outage.
        components = [dict(c, id=[]) if c["id"] == "api" else c
                      for c in REGIONS["components"]]
        for options in ({}, {"stale": 3}):
            with self.subTest(options=options):
                got = self.read(options, dict(REGIONS, components=components))
                self.assertEqual((got["status"], got.get("error")), ("outage", None))

    def test_a_nested_group_is_unknown(self):
        # Lane review: in A -> B -> component, selecting A dropped the
        # component, so its major outage read ok. Statuspage groups are
        # one level deep; a group with a parent is a tree the filter
        # cannot place.
        components = [dict(c, status="operational") for c in REGIONS["components"]]
        components[2]["group_id"] = "na"       # Europe nested in North America
        components[3]["status"] = "major_outage"
        got = self.read({"groups": ["north-america"]},
                        dict(REGIONS, components=components, incidents=[]))
        self.assertEqual(got["status"], "unknown", got)
        # A group that names itself as parent is nested too.
        components[2]["group_id"] = "eu"
        got = self.read({"groups": ["north-america"]},
                        dict(REGIONS, components=components, incidents=[]))
        self.assertEqual(got["status"], "unknown", got)

    def test_a_major_cause_never_explains_a_critical_page(self):
        # Lane review: major and critical both became `outage` before
        # the comparison, so a critical page whose only listed cause
        # outside the groups was a major_outage component read ok.
        calm = [dict(c, status="operational") for c in REGIONS["components"]]
        major = [dict(c, status="major_outage") if c["id"] == "fra" else c for c in calm]
        page = dict(REGIONS, status={"indicator": "critical"}, components=major,
                    incidents=[])
        self.assertEqual(self.read({"groups": ["north-america"]}, page)["status"],
                         "unknown")
        # stale= keeps that component, so its outage stands (lane review).
        self.assertEqual(self.read({"stale": 3}, page)["status"], "outage")
        # A critical incident outside the groups explains it: ok.
        fra = dict(REGIONS["incidents"][1], impact="critical")
        got = self.read({"groups": ["north-america"]},
                        dict(REGIONS, status={"indicator": "critical"},
                             components=major, incidents=[fra]))
        self.assertEqual(got["status"], "ok", got)
        # A major_outage component explains a major page, as before.
        got = self.read({"groups": ["north-america"]},
                        dict(REGIONS, components=major, incidents=[]))
        self.assertEqual(got["status"], "ok", got)

    def test_a_group_the_page_lacks_is_unknown_never_ok(self):
        got = self.read({"groups": ["antarctica"]})
        self.assertEqual(got["status"], "unknown")
        self.assertIn("antarctica", got["error"])

    def test_an_unfiltered_line_reads_as_before(self):
        plain = service_status.read_service(
            ("svc", "statuspage", "https://status.example.test/api", []), NOW,
            fetch=answering(REGIONS))
        self.assertEqual(self.read({}), plain)
        self.assertEqual(plain["status"], "outage")
        self.assertEqual(len(plain["incidents"]), 3)
        self.assertEqual(len(plain["components"]), 4)


class Services(unittest.TestCase):
    def read(self, kind, answer, url="https://status.example.test/api"):
        return service_status.read_service(("svc", kind, url, ["claude"]), NOW,
                                           fetch=answering(answer))

    def test_the_services_grammar(self):
        services, problems = service_status.parse_services(
            "# watched\n"
            "claude statuspage https://status.example.test/api/v2/summary.json claude\n"
            "github statuspage https://example.test/s.json copilot,github  # both\n"
            "slack slack https://example.test/current\n"
            "claude http https://example.test\n"
            "x rss https://example.test\n"
            "y http ftp://example.test\n"
            "z http https://example.test Bad\n"
            "only two\n"
            "v http http://[bad\n"
            "w http http://example.test:99999\n")
        self.assertEqual([s[0] for s in services], ["claude", "github", "slack"])
        self.assertEqual(services[1][3], ["copilot", "github"])
        self.assertEqual(services[2][3], [])
        self.assertEqual(problems, [
            "line 5: name is not a new lower-case name",
            "line 6: kind is not one of statuspage, slack, google-incidents, http",
            "line 7: url is not http(s)",
            "line 8: a provider is not a lower-case name",
            "line 9: want <name> <kind> <url> [provider] [key=value ...]",
            "line 10: url is not http(s)",
            "line 11: url is not http(s)"])

    def test_the_starting_list_parses_whole(self):
        services, problems = service_status.parse_services(service_status.STARTING)
        self.assertEqual(problems, [])
        self.assertEqual(len(services), len(service_status.STARTING.splitlines()))

    def test_suno_is_an_http_reachability_check(self):
        # sd:9903: Suno has no status feed and no credits row; the
        # operator wants http reachability of suno.com only.
        services, _ = service_status.parse_services(service_status.STARTING)
        self.assertIn(("suno", "http", "https://suno.com", [], {}), services)

    def test_a_statuspage_summary(self):
        got = self.read("statuspage", SUMMARY)
        self.assertEqual((got["status"], got["indicator"]), ("degraded", "minor"))
        self.assertEqual(got["incidents"], [{"title": "Copilot slow",
                                             "url": "https://status.example.test/incidents/abc",
                                             "impact": "minor",
                                             "started_at": "2026-11-17T19:00:00Z"}])
        self.assertEqual(got["components"], [{"name": "Copilot", "status": "degraded_performance"}])
        self.assertEqual(got["providers"], ["claude"])
        for indicator, status in (("none", "ok"), ("major", "outage"), ("critical", "outage"),
                                  ("maintenance", "degraded")):
            with self.subTest(indicator=indicator):
                answer = dict(SUMMARY, status={"indicator": indicator})
                self.assertEqual(self.read("statuspage", answer)["status"], status)

    def test_a_summary_with_no_incidents_key_has_no_incidents(self):
        # 2026-11-20: OpenAI, Tailscale, Notion, Docker Hub, Groq and HeyGen
        # leave `incidents` out when none is open; they read unknown.
        answer = {k: v for k, v in SUMMARY.items() if k != "incidents"}
        for indicator, status in (("none", "ok"), ("minor", "degraded")):
            with self.subTest(indicator=indicator):
                got = self.read("statuspage", dict(answer, status={"indicator": indicator}))
                self.assertEqual((got["status"], got["incidents"]), (status, []), got)
        # Present but not a list is still another shape.
        self.assertEqual(self.read("statuspage", dict(SUMMARY, incidents=None))["status"], "unknown")

    def test_a_summary_with_no_components_key_is_unknown(self):
        # Review: unlike `incidents`, every statuspage summary lists its
        # components; a body without the key is truncated, not healthy.
        answer = {k: v for k, v in SUMMARY.items() if k != "components"}
        for indicator in ("none", "minor"):
            got = self.read("statuspage", dict(answer, status={"indicator": indicator}))
            self.assertEqual(got["status"], "unknown", got)
        self.assertEqual(self.read("statuspage", {"status": {"indicator": "none"}})["status"],
                         "unknown")
        # Present but not a list is still another shape.
        self.assertEqual(self.read("statuspage", dict(SUMMARY, components=None))["status"], "unknown")

    def test_slack_and_google(self):
        self.assertEqual(self.read("slack", {"status": "ok", "active_incidents": []})["status"], "ok")
        got = self.read("slack", {"status": "active", "active_incidents": [
            {"title": "Huddles down", "type": "outage", "status": "active",
             "url": "https://example.test/i", "date_created": "2026-11-17T19:00:00Z"}]})
        self.assertEqual((got["status"], got["incidents"][0]["title"]), ("outage", "Huddles down"))
        incidents = [{"end": "2026-11-01T00:00:00Z", "service_name": "Gmail"},
                     {"begin": "2026-11-17T18:00:00Z", "end": None, "service_name": "Drive",
                      "external_desc": "**Summary:**\nSlow uploads.\nMore.", "uri": "incidents/xyz",
                      "status_impact": "SERVICE_DISRUPTION"}]
        got = self.read("google-incidents", incidents,
                        url="https://status.example.test/dashboard/incidents.json")
        self.assertEqual(got["status"], "degraded")
        self.assertEqual(got["incidents"], [{
            "title": "Drive: Slow uploads.", "url": "https://status.example.test/dashboard/incidents/xyz",
            "impact": "SERVICE_DISRUPTION", "started_at": "2026-11-17T18:00:00Z"}])
        incidents[1]["status_impact"] = "SERVICE_OUTAGE"
        self.assertEqual(self.read("google-incidents", incidents)["status"], "outage")
        self.assertEqual(self.read("google-incidents", [])["status"], "ok")

    def test_a_finished_slack_incident_is_no_incident(self):
        # sd:9256 round 8: current can list a resolved incident, and its
        # type made a finished outage read outage. A status the API does
        # not document is unknown.
        def answer(status):
            return {"status": "active", "active_incidents": [
                {"title": "Huddles down", "type": "outage", "status": status,
                 "url": "https://example.test/i", "date_created": "2026-11-17T19:00:00Z"}]}
        for status in ("resolved", "completed", "cancelled", "scheduled"):
            with self.subTest(status=status):
                got = self.read("slack", answer(status))
                self.assertEqual((got["status"], got["incidents"]), ("ok", []))
        for status in (None, "weird", 5):
            with self.subTest(status=status):
                self.assertEqual(self.read("slack", answer(status))["status"], "unknown")

    def test_a_failed_or_strange_read_is_unknown_never_ok(self):
        for kind in ("statuspage", "slack", "google-incidents"):
            got = service_status.read_service(("svc", kind, "https://example.test", []), NOW,
                                              fetch=failing("TimeoutError"))
            self.assertEqual((got["status"], got["error"]), ("unknown", "TimeoutError"))
            for shape in ({}, [], "x", None, 5, {"status": {"indicator": "weird"}},
                          {"status": "broken", "active_incidents": []}, [5, "x"], {"status": "ok"}):
                with self.subTest(kind=kind, shape=shape):
                    got = self.read(kind, shape)
                    if kind == "google-incidents" and shape == []:
                        continue  # an empty incident list is a real ok
                    self.assertEqual(got["status"], "unknown", got)

    def test_a_collection_of_another_shape_is_unknown_never_ok(self):
        # sd:9256 review: a string or a dict was walked as no incident.
        shapes = {"statuspage": [dict(SUMMARY, incidents="abc"), dict(SUMMARY, incidents={"a": 1}),
                                 dict(SUMMARY, incidents=[5]), dict(SUMMARY, components="x"),
                                 dict(SUMMARY, components=[None])],
                  "slack": [{"status": "ok", "active_incidents": [5]}],
                  "google-incidents": [[5, "x"], [{"end": None}, "x"], [{}],
                                       [{"begin": "2026-11-17T18:00:00Z", "end": 5,
                                         "service_name": "Drive"}],
                                       [{"begin": 5, "service_name": "Drive"}],
                                       [{"begin": "2026-11-17T18:00:00Z", "service_name": 5}]]}
        for kind, answers in shapes.items():
            for answer in answers:
                with self.subTest(kind=kind, answer=answer):
                    self.assertEqual(self.read(kind, answer)["status"], "unknown")

    def test_every_stored_field_is_a_short_string_or_none(self):
        for value in ({"a": 1}, [1], 5, True, "x" * 1000):
            # The shown fields only; a field that decides a status must be
            # one the feed documents (see the schema tests).
            incident = {"id": "abc", "status": "investigating", "name": value, "shortlink": value,
                        "impact": value, "created_at": value, "title": value, "url": value,
                        "type": "outage", "date_created": value, "service_name": value,
                        "external_desc": value, "uri": value,
                        "status_impact": "SERVICE_DISRUPTION", "begin": value}
            answers = {"statuspage": dict(SUMMARY, incidents=[incident],
                                          components=[{"name": value,
                                                       "status": "partial_outage"}]),
                       "slack": {"status": "active",
                                 "active_incidents": [dict(incident, status="active")]},
                       "google-incidents": [dict(incident, begin="2026-11-17T18:00:00Z",
                                                 end=None, service_name="Drive")]}
            for kind, answer in answers.items():
                with self.subTest(kind=kind, value=value):
                    got = self.read(kind, answer)
                    self.assertNotEqual(got["status"], "unknown", got)
                    for entry in got["incidents"] + got["components"]:
                        for field, stored in entry.items():
                            most = service_status.URL if field == "url" else service_status.TITLE
                            self.assertTrue(stored is None or (isinstance(stored, str)
                                            and len(stored) <= most), (field, stored))
        got = self.read("google-incidents", [{"uri": "http://[bad",
                                              "status_impact": "SERVICE_OUTAGE",
                                              "begin": "2026-11-17T18:00:00Z",
                                              "service_name": "Drive"}])
        self.assertIsNone(got["incidents"][0]["url"])

    def test_an_incident_url_is_a_whole_web_url_or_none(self):
        # sd:9256 round 5: a URL cut at TITLE was a broken link, and any
        # scheme was kept.
        base = "https://status.example.test/"
        for value, kept in ((base + "a" * 1900, True), (base + "a" * 2100, False),
                            ("javascript:alert(1)", False), ("ftp://example.test/x", False),
                            ("http:///x", False), (base + "a b", False)):
            answers = {
                "statuspage": dict(SUMMARY, incidents=[
                    {"id": "abc", "status": "investigating", "name": "n",
                     "shortlink": value, "impact": "minor", "created_at": None}]),
                "slack": {"status": "active", "active_incidents": [
                    {"title": "t", "type": "outage", "status": "active", "url": value,
                     "date_created": None}]},
                "google-incidents": [{"uri": value, "status_impact": "SERVICE_OUTAGE",
                                      "begin": "2026-11-17T18:00:00Z", "service_name": "Drive"}]}
            for kind, answer in answers.items():
                with self.subTest(kind=kind, value=value[:40], length=len(value)):
                    got = self.read(kind, answer)
                    self.assertEqual(got["incidents"][0]["url"], value if kept else None)

    def test_the_http_kind(self):
        local_only(self)
        server = Server({"/up": (200, b"<html>"), "/down": (503, b""), "/gone": (404, b"")})
        self.addCleanup(server.close)
        for path, status in (("/up", "ok"), ("/down", "outage"), ("/gone", "unknown")):
            with self.subTest(path=path):
                got = service_status.read_service(("p", "http", server.url + path, []), NOW)
                self.assertEqual(got["status"], status, got)
        got = service_status.read_service(("p", "http", "http://127.0.0.1:%d/" % closed_port(), []),
                                          NOW)
        self.assertEqual(got["status"], "unknown")
        self.assertTrue(got["error"])


CSWAP_LIST = {"accounts": [{"number": 1, "email": "one@example.test", "active": True,
                            "usageStatus": "ok", "usage": {"sevenDay": {
                                "pct": 40.0, "resetsAt": "2100-01-01T00:00:00Z",
                                "expectedPct": 50.0}, "scoped": []},
                            "usageFetchedAt": "2026-11-17T19:59:00Z"}]}
CSWAP = "#!/bin/sh\ncat \"$CSWAP_FIXTURE\"\n"


class Live(unittest.TestCase):
    """The subcommands as the job runs them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "sd.db"
        initialise(self.db)
        self.cswap = root / "cswap"
        self.cswap.write_text(CSWAP, encoding="utf-8")
        self.cswap.chmod(self.cswap.stat().st_mode | stat.S_IXUSR)
        (root / "cswap.json").write_text(json.dumps(CSWAP_LIST), encoding="utf-8")
        day = root / "codex/sessions/2026/11/17"
        day.mkdir(parents=True)
        (day / "rollout-x.jsonl").write_text(json.dumps(Codex.event()) + "\n", encoding="utf-8")
        # TWILIO_ENV_FILE names twilio's credentials file (sd:9718 review).
        keys = {var for var, _, _ in ANSWERS.values()} | {"TWILIO_ENV_FILE"}
        self.env = {k: v for k, v in os.environ.items() if k not in keys and k not in PROXIES}
        self.env.update(LOCAL, CSWAP_BIN=str(self.cswap), CSWAP_FIXTURE=str(root / "cswap.json"),
                        CODEX_HOME=str(root / "codex"), SYSTEM_TOOLS_CONFIG=str(root / "config"))
        # Never the real codex: its app-server reads the account live.
        codex = root / "codex-bin"
        reply = json.dumps({"id": 2, "result": {"rateLimits": live_snapshot()}})
        codex.write_text(FAKE_CODEX % (sys.executable, reply), encoding="utf-8")
        codex.chmod(0o755)
        self.env["CODEX_BIN"] = str(codex)

    def run_live(self, command, *args, db=None):
        return subprocess.run([sys.executable, "-I", str(SCRIPT), command,
                               "--db", str(db or self.db), *args],
                              capture_output=True, text=True, env=self.env, timeout=120)

    def connection(self):
        connection = sd_db.connect(self.db, write=True)
        self.addCleanup(connection.close)
        return connection

    def rows(self, prefix):
        """{key: body} of the newest heartbeat row per key under prefix."""
        found = {}
        for key, body in self.connection().execute(
                "SELECT key, body FROM state WHERE kind = 'heartbeat' AND key LIKE ? ORDER BY id",
                (prefix + "%",)):
            found[key] = json.loads(body) if body is not None else None
        return found

    def setting(self, key, value):
        writes.set_setting(self.connection(), key, value)

    def test_the_runs_read_a_config_folder_the_test_owns(self):
        # sd:9256 round 8: the runs read this machine's config folder.
        config = Path(self.env.get("SYSTEM_TOOLS_CONFIG", ""))
        self.assertEqual(config.parent, Path(self.tmp.name))

    def test_usage_on_a_store_that_never_held_the_list_starts_it_and_reads_each_line(self):
        done = self.run_live("usage")
        self.assertEqual(done.returncode, 0, done.stderr)
        held = writes.read_settings(self.connection(), ["dashboard.usage_providers"])
        self.assertEqual(held, {"dashboard.usage_providers": usage_sources.STARTING})
        rows = self.rows("usage:")
        names, _ = usage_sources.parse_providers(usage_sources.STARTING)
        # A passed-in provider gets its row from its caller (sd:9586).
        self.assertEqual(sorted(rows), sorted("usage:" + entry[0] for entry in names
                                              if entry[0] not in usage_sources.PASSED))
        self.assertEqual(rows["usage:claude"]["status"], "ok")
        self.assertEqual(rows["usage:claude"]["windows"][0]["used_percent"], 40.0)
        self.assertEqual(rows["usage:codex"]["status"], "ok")
        self.assertEqual(rows["usage:codex"]["source"], "codex-app-server")
        self.assertEqual(rows["usage:openrouter"],
                         usage_sources.reading("openrouter", "unknown", "api",
                                               error="OPENROUTER_API_KEY not set"))
        self.assertEqual(rows["usage:gemini"]["status"], "no-source")
        self.assertNotIn("example.test", json.dumps(rows))
        self.assertIn("rows", done.stdout)

    def test_usage_reads_the_held_list_only_and_names_a_refused_line(self):
        self.setting("dashboard.usage_providers", "codex\nnot a name\ngroq cap=5\n")
        done = self.run_live("usage")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(self.rows("usage:")), ["usage:codex", "usage:groq"])
        self.assertIn("SD_REPORT_ATTENTION: dashboard.usage_providers line 2: not a provider name",
                      done.stdout)

    def test_a_zerogpu_error_is_the_huggingface_row_until_the_next_one(self):
        # sd:9586: the caller passes the error; a live run leaves its row.
        self.setting("dashboard.usage_providers", "huggingface\ngroq\n")
        done = self.run_live("usage", "--huggingface-error",
                             "You have exceeded your GPU quota (60s requested vs. 23s left)")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(self.rows("usage:")), ["usage:huggingface"])
        row = self.rows("usage:")["usage:huggingface"]
        self.assertEqual((row["status"], row["balance"]), ("ok", {"amount": 23.0, "unit": "seconds"}))
        done = self.run_live("usage")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.rows("usage:")["usage:huggingface"], row)
        self.assertEqual(self.rows("usage:")["usage:groq"]["status"], "no-source")

    def test_an_error_text_of_another_kind_writes_no_row(self):
        done = self.run_live("usage", "--huggingface-error", "GPU task aborted")
        self.assertEqual(done.returncode, 1)
        self.assertIn("no ZeroGPU quota", done.stderr)
        self.assertEqual(self.rows("usage:"), {})

    def test_a_cleared_list_reads_nothing_and_writes_nothing(self):
        self.setting("dashboard.usage_providers", None)
        done = self.run_live("usage")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("cleared", done.stdout)
        self.assertEqual(self.rows("usage:"), {})
        self.assertEqual(writes.read_settings(self.connection(), ["dashboard.usage_providers"]),
                         {"dashboard.usage_providers": None})

    def test_services_writes_one_row_per_line_and_a_failed_read_is_unknown(self):
        server = Server({"/summary.json": (200, SUMMARY), "/broken.json": (500, b"")})
        self.addCleanup(server.close)
        self.setting("dashboard.status_services",
                     "github statuspage %s/summary.json copilot\n"
                     "broken statuspage %s/broken.json\n"
                     "gone http http://127.0.0.1:%d/\n" % (server.url, server.url, closed_port()))
        done = self.run_live("services")
        self.assertEqual(done.returncode, 0, done.stderr)
        rows = self.rows("service-status:")
        self.assertEqual(sorted(rows), ["service-status:broken", "service-status:github",
                                        "service-status:gone"])
        self.assertEqual(rows["service-status:github"]["status"], "degraded")
        self.assertEqual(rows["service-status:github"]["providers"], ["copilot"])
        self.assertEqual((rows["service-status:broken"]["status"],
                          rows["service-status:broken"]["error"]), ("unknown", "HTTP 500"))
        self.assertEqual(rows["service-status:gone"]["status"], "unknown")
        self.assertIn("3 rows (1 degraded, 2 unknown)", done.stdout)

    def test_the_newest_row_is_the_newest_read(self):
        server = Server({"/s.json": (200, SUMMARY)})
        self.addCleanup(server.close)
        self.setting("dashboard.status_services", "svc statuspage %s/s.json\n" % server.url)
        self.assertEqual(self.run_live("services").returncode, 0)
        server.routes["/s.json"] = (500, b"")
        self.assertEqual(self.run_live("services").returncode, 0)
        self.assertEqual(self.rows("service-status:")["service-status:svc"]["status"], "unknown")

    def test_services_on_a_never_held_list_starts_it(self):
        # Every starting line points outside; an unroutable proxy keeps the
        # test off the network, and each read is then unknown.
        self.env.update({name: "http://127.0.0.1:%d" % closed_port() for name in PROXIES},
                        NO_PROXY="", no_proxy="")
        done = self.run_live("services")
        self.assertEqual(done.returncode, 0, done.stderr)
        held = writes.read_settings(self.connection(), ["dashboard.status_services"])
        self.assertEqual(held["dashboard.status_services"], service_status.STARTING)
        rows = self.rows("service-status:")
        self.assertEqual(len(rows), len(service_status.STARTING.splitlines()))
        self.assertEqual({row["status"] for row in rows.values()}, {"unknown"})

    def test_an_unedited_old_default_list_moves_to_the_new_one(self):
        # Review: live() seeded STARTING only on a never-held row, so a
        # store holding the old default kept reading Cloudflare unfiltered.
        self.env.update({name: "http://127.0.0.1:%d" % closed_port() for name in PROXIES},
                        NO_PROXY="", no_proxy="")
        self.setting("dashboard.status_services", service_status.RETIRED[0])
        done = self.run_live("services")
        self.assertEqual(done.returncode, 0, done.stderr)
        held = writes.read_settings(self.connection(), ["dashboard.status_services"])
        self.assertEqual(held["dashboard.status_services"], service_status.STARTING)
        self.assertIn("moved to the new default", done.stdout)

    def test_an_unedited_seed_from_before_suno_moves_to_the_new_one(self):
        # sd:9903 review: a list seeded before Suno was added never held
        # it, since only a never-held list got STARTING.
        self.env.update({name: "http://127.0.0.1:%d" % closed_port() for name in PROXIES},
                        NO_PROXY="", no_proxy="")
        earlier = service_status.STARTING.replace("suno http https://suno.com\n", "")
        self.setting("dashboard.status_services", earlier)
        done = self.run_live("services")
        self.assertEqual(done.returncode, 0, done.stderr)
        held = writes.read_settings(self.connection(), ["dashboard.status_services"])
        self.assertEqual(held["dashboard.status_services"], service_status.STARTING)
        self.assertIn("service-status:suno", self.rows("service-status:"))

    def test_an_edited_old_default_list_stays_as_the_operator_left_it(self):
        self.env.update({name: "http://127.0.0.1:%d" % closed_port() for name in PROXIES},
                        NO_PROXY="", no_proxy="")
        edited = "".join(line for line in service_status.RETIRED[0].splitlines(True)
                         if not line.startswith("strongdm "))
        self.setting("dashboard.status_services", edited)
        done = self.run_live("services")
        self.assertEqual(done.returncode, 0, done.stderr)
        held = writes.read_settings(self.connection(), ["dashboard.status_services"])
        self.assertEqual(held["dashboard.status_services"], edited)

    def test_an_entry_dropped_from_its_list_is_retired_once(self):
        # sd:9256 round 5: a dropped entry's last row stood as its newest
        # row forever, an old outage flag with it.
        server = Server({"/s.json": (200, SUMMARY)})
        self.addCleanup(server.close)
        line = "%s statuspage " + server.url + "/s.json\n"
        cases = (("usage", "dashboard.usage_providers", "usage:", "codex\n", "groq\n"),
                 ("services", "dashboard.status_services", "service-status:",
                  line % "kept", line % "dropped"))
        for command, list_key, prefix, kept, dropped in cases:
            with self.subTest(command=command):
                self.setting(list_key, kept + dropped)
                self.assertEqual(self.run_live(command).returncode, 0)
                first = self.rows(prefix)
                self.assertEqual(len(first), 2)
                self.assertNotIn("removed", {row["status"] for row in first.values()})
                self.setting(list_key, kept)
                done = self.run_live(command)
                self.assertEqual(done.returncode, 0, done.stderr)
                rows = self.rows(prefix)
                gone = prefix + dropped.split()[0]
                self.assertEqual(rows[gone], {"status": "removed"})
                self.assertNotEqual(rows[prefix + kept.split()[0]]["status"], "removed")
                self.assertIn("1 removed", done.stdout)
                self.assertEqual(self.run_live(command).returncode, 0)
                again = self.connection().execute(
                    "SELECT COUNT(*) FROM state WHERE kind = 'heartbeat' AND key = ?",
                    (gone,)).fetchone()[0]
                self.assertEqual(again, 2, "a second removed row")
                self.setting(list_key, None)
                done = self.run_live(command)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual({row["status"] for row in self.rows(prefix).values()},
                                 {"removed"})
        # One list's retirement never touches the other's keys.
        self.assertEqual(sorted(self.rows("")), ["service-status:dropped", "service-status:kept",
                                                 "usage:codex", "usage:groq"])

    def test_a_null_heartbeat_body_is_retired_not_a_crash(self):
        # sd:9256 round 7: json.loads(None) raised TypeError out of the run.
        connection = self.connection()
        with sd_db.transaction(connection):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES "
                               "('heartbeat', 'usage:old', '2026-11-17T00:00:00Z', NULL)")
        self.setting("dashboard.usage_providers", "codex\n")
        done = self.run_live("usage")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.rows("usage:")["usage:old"], {"status": "removed"})

    def test_a_row_write_that_fails_partway_lands_no_row_of_the_run(self):
        """The rows are one step: the second insert fails after the first
        row is in, and the transaction takes the first back. The run exits
        1; the rows of the run before stand (sd:9256 round 7: the failure
        used to come before the transaction began)."""
        import argparse
        meter = load_meter()
        args = argparse.Namespace(db=str(self.db), command="services")
        listed = "a http https://example.test/a\nb http https://example.test/b\n"
        meter.live(args, "dashboard.status_services", listed, service_status.parse_services,
                   lambda entry, now: (entry[0], {"status": "ok", "run": 1}), "service-status:")
        real, calls = writes.record_state, []

        def second_fails(*args, **kwargs):
            calls.append(kwargs["key"])
            if len(calls) == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real(*args, **kwargs)
        said = io.StringIO()
        with mock.patch.object(writes, "record_state", second_fails), \
                contextlib.redirect_stderr(said), self.assertRaises(SystemExit) as stopped:
            meter.live(args, "dashboard.status_services", listed, service_status.parse_services,
                       lambda entry, now: (entry[0], {"status": "degraded", "run": 2}),
                       "service-status:")
        self.assertEqual(stopped.exception.code, 1)
        # sd:9256 round 8: the message named no stage.
        self.assertIn("no rows written: writing the rows failed: OperationalError",
                      said.getvalue())
        self.assertEqual(calls, ["service-status:a", "service-status:b"])
        rows = self.rows("service-status:")
        self.assertEqual({key: row["run"] for key, row in rows.items()},
                         {"service-status:a": 1, "service-status:b": 1})

    def test_no_db_is_refused_where_rows_are_all_a_run_writes(self):
        # sd:9256 review: --no-db was ignored and the rows were written.
        for command in ("usage", "services"):
            with self.subTest(command=command):
                done = subprocess.run([sys.executable, "-I", str(SCRIPT), command, "--no-db"],
                                      capture_output=True, text=True, timeout=60,
                                      env=dict(self.env, HOME=self.tmp.name))
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertIn("--no-db", done.stderr)
                self.assertEqual(self.rows("usage:"), {})
                self.assertEqual(self.rows("service-status:"), {})

    def test_a_store_that_does_not_open_exits_1(self):
        for command in ("usage", "services"):
            with self.subTest(command=command):
                done = self.run_live(command, db=Path(self.tmp.name) / "missing" / "sd.db")
                self.assertEqual(done.returncode, 1)
                self.assertIn("did not open", done.stderr)
                self.assertNotIn("Traceback", done.stderr)

    def test_a_pack_with_no_sd_db_names_the_folder_it_read(self):
        # sd:9292: the guard said the store did not open, not where sd_db was
        # sought. The gate has an installed sd_db too, so the pack's lib
        # holds one that does not import; it is found first.
        # Round 7: a pack that raises another error says so too.
        missing = Path(self.tmp.name) / "no-pack"
        (missing / "lib/sd_db").mkdir(parents=True)
        self.env["SD_PACK_ROOT"] = str(missing)
        for error in ("ImportError", "RuntimeError"):
            (missing / "lib/sd_db/__init__.py").write_text('raise %s("no sd_db here")\n' % error)
            for command in ("usage", "services"):
                with self.subTest(command=command, error=error):
                    done = self.run_live(command)
                    self.assertEqual(done.returncode, 1)
                    self.assertIn("no sd_db in %s" % (missing / "lib"), done.stderr)
                    self.assertNotIn("Traceback", done.stderr)


#: A body nested past json's depth: json.loads raises RecursionError.
#: Python 3.15 parses 100000 levels and raises by 200000; older ones
#: raise near 1000. DEEP, 10000 levels, is a field's value.
LEVELS = 200000
DEEP_TEXT = "[" * LEVELS + "]" * LEVELS
DEEP = []
for _ in range(10000):
    DEEP = [DEEP]
#: Magnitudes a number field may hold, as numbers and as strings.
MAGNITUDES = (1e308, -1e308, "inf", "-inf", "nan", "Infinity", "1e400", 5e-324, -5e-324,
              0, -1, 10**400)
#: Values of the wrong type for any field, empty containers among them.
WRONG = (None, "x", "", True, [], {}, [1], {"a": 1})


def paths(value, here=()):
    """Every path into value, the root and every container among them."""
    yield here
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from paths(inner, here + (key,))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from paths(inner, here + (index,))


def put(value, path, new):
    """value with the node at path replaced by new, copying the path only."""
    if not path:
        return new
    out = dict(value) if isinstance(value, dict) else list(value)
    out[path[0]] = put(value[path[0]], path[1:], new)
    return out


def hostile(answer, deep=True):
    """(label, answer) for every field set to every hostile value, then
    every number in the answer set to each magnitude at once."""
    for path in list(paths(answer)):
        for new in MAGNITUDES + WRONG + ((DEEP,) if deep else ()):
            yield "%s=%s" % ("/".join(map(str, path)), repr(new)[:20]), put(answer, path, new)
    for magnitude in MAGNITUDES:
        yield "every number=%r" % (magnitude,), replace_numbers(answer, magnitude)


def load_meter():
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_meter", SCRIPT)
    meter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(meter)
    return meter


#: One open Google incident in the live shape.
GOOGLE_OPEN = {"begin": "2026-11-17T18:00:00+00:00", "end": None, "service_name": "Drive",
               "external_desc": "Slow.", "uri": "incidents/x", "status_impact": "SERVICE_OUTAGE"}


class Hostile(unittest.TestCase):
    """Every reader on hostile input (sd:9256 review, rounds 1 to 3): each
    reading is ok with finite data, or unknown. None raises, and none
    serialises a value that is not finite JSON."""

    def check(self, label, read, states):
        try:
            got = read()
            json.dumps(got, allow_nan=False)
        except Exception as e:  # the property: nothing escapes
            return ["%s: raised %s" % (label, type(e).__name__)]
        if got["status"] not in states + ("unknown",):
            return ["%s: status %s" % (label, got["status"])]
        if "provider" in got and got["status"] == "ok" and not (
                got["windows"] or got["balance"] or got["spend"]):
            return ["%s: ok without data" % label]
        return []

    def deep_fetch(self):
        """fetch_json against a server whose body is DEEP_TEXT."""
        local_only(self)
        server = Server({"/deep": (200, DEEP_TEXT.encode())})
        self.addCleanup(server.close)
        return lambda url, headers=None, **cap: usage_sources.fetch_json(server.url + "/deep", headers, **cap)

    def test_every_usage_reader(self):
        deep = self.deep_fetch()
        failures = []
        for name, (var, answer, _) in ANSWERS.items():
            for label, bent in hostile(answer):
                failures += self.check("%s %s" % (name, label), lambda: usage_sources.read_provider(
                    name, {var: SECRET}, lambda url, headers=None: bent, NOW), ("ok",))
            failures += self.check("%s deep text" % name, lambda: usage_sources.read_provider(
                name, {var: SECRET}, deep, NOW), ())
        self.assertEqual(failures, [])

    def test_the_overflows_the_review_named(self):
        two = {"data": [{"results": [{"amount": {"value": 1e308}}]},
                        {"results": [{"amount": {"value": 1e308}}]}]}
        tiny = {"data": {"remainingCredits": -1, "planCredits": 5e-324}}
        for name, var, answer in (("openai", "OPENAI_ADMIN_KEY", two),
                                  ("firecrawl", "FIRECRAWL_API_KEY", tiny)):
            with self.subTest(provider=name):
                got = usage_sources.read_provider(name, {var: SECRET}, answering(answer), NOW)
                self.assertEqual((got["status"], got["error"]),
                                 ("unknown", "a value that is not finite JSON"))

    def test_claude_from_every_cswap_reply(self):
        meter = load_meter()
        failures = []
        with mock.patch.dict(os.environ, CSWAP_BIN=sys.executable):
            for label, bent in hostile(CSWAP_LIST):
                with mock.patch.object(meter, "run_json", return_value=(bent, None)):
                    failures += self.check("cswap " + label, lambda: usage_sources.claude_reading(
                        *meter.read_accounts(5), NOW), ("ok",))
        self.assertEqual(failures, [])
        for empty in ({"accounts": []}, put(CSWAP_LIST, ("accounts", 0, "usage"), {})):
            with self.subTest(empty=empty):
                with mock.patch.dict(os.environ, CSWAP_BIN=sys.executable), \
                        mock.patch.object(meter, "run_json", return_value=(empty, None)):
                    got = usage_sources.claude_reading(*meter.read_accounts(5), NOW)
                self.assertEqual((got["status"], got["error"]),
                                 ("unknown", "no active account with a seven-day share"))

    def test_a_flag_or_code_of_another_type_is_unknown(self):
        # sd:9256 round 7: the string "false" counted as the active
        # account, and False or 0.0 as a success code of 0.
        meter = load_meter()
        bent = [("active=%r" % v, put(CSWAP_LIST, ("accounts", 0, "active"), v))
                for v in ("false", "true", 1, 0, "", [], {})]
        bent += [("scoped=%r" % v, put(CSWAP_LIST, ("accounts", 0, "usage", "scoped"), v))
                 for v in ("x", False, {"a": 1})]
        bent += [("usage=%r" % v, put(CSWAP_LIST, ("accounts", 0, "usage"), v))
                 for v in (False, "x", [1])]
        for label, reply in bent:
            with self.subTest(label), mock.patch.dict(os.environ, CSWAP_BIN=sys.executable), \
                    mock.patch.object(meter, "run_json", return_value=(reply, None)):
                got = usage_sources.claude_reading(*meter.read_accounts(5), NOW)
                self.assertEqual(got["status"], "unknown", got)
        entry = {"number": 1, "active": "false", "status": "ok", "pct": 40.0,
                 "resetsAt": None, "expectedPct": None, "scoped": {},
                 "fetchedAt": "2026-11-17T19:59:00Z"}
        with self.subTest("an entry"):
            self.assertEqual(usage_sources.claude_reading([entry], None, NOW)["status"],
                             "unknown")
        for name, path in (("moonshot", ("code",)), ("minimax", ("base_resp", "status_code"))):
            var, answer, _ = ANSWERS[name]
            for value in (False, 0.0, "0"):
                with self.subTest(provider=name, code=value):
                    got = usage_sources.read_provider(name, {var: SECRET},
                                                      answering(put(answer, path, value)), NOW)
                    self.assertEqual(got["status"], "unknown", got)

    def test_a_reply_that_is_not_utf8_is_an_error_not_a_raise(self):
        # sd:9256 round 6: text=True raised UnicodeDecodeError out of the run.
        for echo in (False, True):
            with self.subTest(echo=echo):
                data, error = load_meter().run_json(
                    [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'{\\xff}')"],
                    30, echo=echo)
                self.assertIsNone(data)
                self.assertTrue(error.startswith("unparseable output"), error)
        data, error = load_meter().run_json(
            [sys.executable, "-c", "import sys; sys.stderr.buffer.write(b'\\xff'); sys.exit(3)"],
            30)
        self.assertEqual((data, error[:6]), (None, "exit 3"))

    def test_a_deep_reply_from_a_command_is_an_error_not_a_raise(self):
        data, error = load_meter().run_json(
            [sys.executable, "-c", "print('[' * %d + ']' * %d)" % (LEVELS, LEVELS)], 30,
            echo=False)
        self.assertEqual((data, error), (None, "unparseable output"))

    def test_codex_from_every_event(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        day = Path(tmp.name) / "sessions/2026/11/17"
        day.mkdir(parents=True)
        failures = []
        for label, bent in hostile(Codex.event(), deep=False):
            (day / "rollout-x.jsonl").write_text(json.dumps(bent) + "\n", encoding="utf-8")
            failures += self.check("codex " + label,
                                   lambda: usage_sources.codex_reading(tmp.name, now=NOW), ("ok",))
        (day / "rollout-x.jsonl").write_text(
            '{"type": "token_count", "rate_limits": %s}\n' % DEEP_TEXT, encoding="utf-8")
        got = usage_sources.codex_reading(tmp.name, now=NOW)
        self.assertEqual(got["status"], "unknown")
        self.assertEqual(failures, [])

    def test_every_status_reader(self):
        answers = {"statuspage": SUMMARY,
                   "slack": {"status": "active", "active_incidents": [
                       {"title": "Huddles", "type": "outage", "status": "active",
                        "url": "https://example.test/i", "date_created": "2026-11-17T19:00:00Z"}]},
                   "google-incidents": [{"begin": "2026-11-17T18:00:00Z", "end": None,
                                         "service_name": "Drive", "external_desc": "Slow.",
                                         "uri": "incidents/x", "status_impact": "SERVICE_OUTAGE"}]}
        deep = self.deep_fetch()
        failures = []
        for kind, answer in answers.items():
            for label, bent in hostile(answer):
                failures += self.check("%s %s" % (kind, label), lambda: service_status.read_service(
                    ("svc", kind, "https://status.example.test/a", []), NOW,
                    fetch=lambda url, **_: bent), ("ok", "degraded", "outage"))
            failures += self.check("%s deep text" % kind, lambda: service_status.read_service(
                ("svc", kind, "https://status.example.test/a", []), NOW, fetch=deep), ())
        self.assertEqual(failures, [])

    def nullable(self):
        """(source, label, read, still) per field each source's schema lets
        be null: read() with that field null, and still(reading) what the
        rest must keep. Codex: codex-rs protocol.rs RateLimitSnapshot;
        cswap: claude_swap json_output.py; the rest: each API's docs."""
        both = put(Codex.event(limit_id="codex"), ("payload", "rate_limits", "secondary"),
                   {"used_percent": 23.0, "window_minutes": 300, "resets_at": 4102444800})
        rate = ("payload", "rate_limits")
        for path in (("primary", "window_minutes"), ("primary", "resets_at"), ("primary",),
                     ("secondary",), ("credits",), ("credits", "balance"), ("limit_id",)):
            event = put(both, rate + path, None)
            yield ("codex", "/".join(path), lambda event=event: self.codex(event),
                   lambda got, path=path: len(got["windows"]) == 2 - (len(path) == 1 and path[0]
                                                                     in ("primary", "secondary")))
        active = ("accounts", 0, "usage", "sevenDay", "resetsAt")
        for label, reply in (("resetsAt", put(CSWAP_LIST, active, None)),
                             ("another account's usage", put(
                                 put(CSWAP_LIST, ("accounts",), CSWAP_LIST["accounts"] + [
                                     {"number": 2, "active": False, "usageStatus": "error",
                                      "usage": None, "usageFetchedAt": None}]),
                                 ("accounts", 1, "usage"), None))):
            yield ("cswap", label, lambda reply=reply: self.claude(reply),
                   lambda got: got["windows"][0]["used_percent"] == 40.0)
        openrouter = put(ANSWERS["openrouter"][1], ("data", "limit_remaining"), None)
        yield ("openrouter", "limit_remaining", lambda: usage_sources.read_provider(
            "openrouter", {"OPENROUTER_API_KEY": SECRET}, answering(openrouter), NOW),
            lambda got: got["spend"]["amount"] == 12.5 and got["balance"] is None)
        firecrawl = put(ANSWERS["firecrawl"][1], ("data", "billingPeriodEnd"), None)
        yield ("firecrawl", "billingPeriodEnd", lambda: usage_sources.read_provider(
            "firecrawl", {"FIRECRAWL_API_KEY": SECRET}, answering(firecrawl), NOW),
            lambda got: got["windows"][0]["resets_at"] is None)
        for kind, answer, still in (
                ("statuspage", put(SUMMARY, ("incidents", 0), dict(SUMMARY["incidents"][0],
                                                                  shortlink=None)),
                 lambda got: got["incidents"][0]["url"].endswith("/incidents/abc")),
                ("slack", {"status": "active", "active_incidents": [
                    {"title": "t", "type": "outage", "status": "active", "url": None,
                     "date_created": None}]},
                 lambda got: got["status"] == "outage"),
                ("google-incidents", [dict(GOOGLE_OPEN, end=None, uri=None)],
                 lambda got: got["status"] == "outage")):
            yield (kind, "null", lambda kind=kind, answer=answer: service_status.read_service(
                ("svc", kind, "https://status.example.test/a", []), NOW,
                fetch=lambda url, **_: answer), still)

    def codex(self, event):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        day = Path(tmp.name) / "sessions/2026/11/17"
        day.mkdir(parents=True)
        (day / "rollout-x.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
        return usage_sources.codex_reading(tmp.name, now=NOW)

    def claude(self, reply):
        meter = load_meter()
        with mock.patch.dict(os.environ, CSWAP_BIN=sys.executable), \
                mock.patch.object(meter, "run_json", return_value=(reply, None)):
            return usage_sources.claude_reading(*meter.read_accounts(5), NOW)

    def test_null_where_the_schema_allows_null_keeps_the_rest(self):
        # sd:9256 review: one null Codex field dropped the whole snapshot,
        # and an older one was published as ok.
        failures = []
        for source, label, read, still in self.nullable():
            got = read()
            if got["status"] == "unknown" or not still(got):
                failures.append("%s %s: %s %s" % (source, label, got["status"],
                                                   got.get("error")))
        self.assertEqual(failures, [])

    def test_an_invalid_timestamp_never_closes_or_resets(self):
        # sd:9256 review: any non-empty Google end closed the incident.
        failures = []
        for bad in ("garbage", "", "2026-13-01T00:00:00Z", "2026-11-17T18:00:00", 5, [], True):
            cases = [("google %s" % url, lambda url=url: service_status.read_service(
                ("svc", "google-incidents", url, []), NOW,
                fetch=lambda u, **_: [dict(GOOGLE_OPEN, end=bad)]))
                for url in ("https://status.cloud.google.com/incidents.json",
                            "https://www.google.com/appsstatus/dashboard/incidents.json")]
            cases.append(("google begin", lambda: service_status.read_service(
                ("svc", "google-incidents", "https://example.test/i.json", []), NOW,
                fetch=lambda u: [dict(GOOGLE_OPEN, begin=bad)])))
            cases.append(("claude resetsAt", lambda: self.claude(
                put(CSWAP_LIST, ("accounts", 0, "usage", "sevenDay", "resetsAt"), bad))))
            cases.append(("firecrawl", lambda: usage_sources.read_provider(
                "firecrawl", {"FIRECRAWL_API_KEY": SECRET}, answering(
                    put(ANSWERS["firecrawl"][1], ("data", "billingPeriodEnd"), bad)), NOW)))
            if not isinstance(bad, int) or isinstance(bad, bool):
                # Codex sends epoch seconds: 5 is a time, 1970's.
                cases.append(("codex resets_at", lambda: self.codex(put(
                    Codex.event(), ("payload", "rate_limits", "primary", "resets_at"), bad))))
            for label, read in cases:
                got = read()
                if got["status"] != "unknown":
                    failures.append("%s=%r: %s" % (label, bad, got["status"]))
        self.assertEqual(failures, [])

    def test_a_status_field_the_feed_does_not_document_is_unknown(self):
        failures = []
        for bad in ([], None, "weird", 5):
            for kind, answer in (
                    ("statuspage", put(SUMMARY, ("components", 0, "status"), bad)),
                    ("statuspage", put(SUMMARY, ("incidents", 0, "status"), bad)),
                    ("slack", {"status": "active", "active_incidents": [
                        {"title": "t", "type": bad, "status": "active"}]}),
                    ("slack", {"status": "active", "active_incidents": [
                        {"title": "t", "type": "outage", "status": bad}]}),
                    ("google-incidents", [dict(GOOGLE_OPEN, status_impact=bad)])):
                got = service_status.read_service(("svc", kind, "https://example.test/a", []),
                                                  NOW, fetch=lambda url, **_: answer)
                if got["status"] != "unknown":
                    failures.append("%s %r: %s" % (kind, bad, got["status"]))
        self.assertEqual(failures, [])

    def test_the_newest_codex_snapshot_decides(self):
        # A newer snapshot that does not read is unknown, not the older one.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        day = Path(tmp.name) / "sessions/2026/11/17"
        day.mkdir(parents=True)
        bad = put(Codex.event(stamp="2026-11-17T19:30:00Z"),
                  ("payload", "rate_limits", "primary", "used_percent"), "x")
        (day / "rollout-x.jsonl").write_text(
            json.dumps(Codex.event(stamp="2026-11-17T19:00:00Z")) + "\n" + json.dumps(bad) + "\n",
            encoding="utf-8")
        got = usage_sources.codex_reading(tmp.name, now=NOW)
        self.assertEqual((got["status"], got["error"]), ("unknown", "primary.used_percent is not a number"))

    def test_a_deep_body_over_http_is_read_failed(self):
        local_only(self)
        server = Server({"/deep": (200, DEEP_TEXT.encode())})
        self.addCleanup(server.close)
        with self.assertRaises(ReadFailed) as caught:
            usage_sources.fetch_json(server.url + "/deep")
        self.assertEqual(str(caught.exception), "unparseable answer")


if __name__ == "__main__":
    unittest.main()


#: A synthetic Twilio account SID and the two answers in the live shape
#: (Twilio's API reference: Usage Records, Balance); amounts are strings.
TWILIO_SID = "AC" + "0" * 32
TWILIO_USAGE = {"usage_records": [{"category": "totalprice", "description": "Total Price",
                                   "price": "12.3456", "price_unit": "usd",
                                   "usage": "12.3456", "usage_unit": "usd"}]}
TWILIO_BALANCE = {"account_sid": TWILIO_SID, "balance": "20.50", "currency": "USD"}


class Twilio(unittest.TestCase):
    """sd:9718: the month's spend and the prepaid balance, with an API key
    read from the env file TWILIO_ENV_FILE names."""

    def env_file(self, text=None):
        folder = Path(tempfile.mkdtemp(prefix="twilio-test."))
        self.addCleanup(lambda: [p.unlink() for p in folder.iterdir()] and None or folder.rmdir())
        path = folder / "twilio.env"
        if text is None:
            text = ("# synthetic\nexport TWILIO_ACCOUNT_SID=%s\nTWILIO_API_KEY='SK0123'\n"
                    'TWILIO_API_SECRET="%s"\n' % (TWILIO_SID, SECRET))
        path.write_text(text)
        return {"TWILIO_ENV_FILE": str(path)}

    def fetch(self, usage=TWILIO_USAGE, balance=TWILIO_BALANCE, calls=None):
        def fetch(url, headers=None, **_):
            if calls is not None:
                calls.append((url, headers))
            return json.loads(json.dumps(usage if "/Usage/" in url else balance))
        return fetch

    def test_spend_and_balance_parse(self):
        calls = []
        got = usage_sources.read_provider("twilio", self.env_file(), self.fetch(calls=calls), NOW, cap=50.0)
        self.assertEqual(got["status"], "ok", got)
        self.assertEqual(got["spend"], {"amount": 12.3456, "unit": "usd", "period": "month",
                                        "limit": 50.0, "resets_at": NEXT})
        self.assertEqual(got["balance"], {"amount": 20.5, "unit": "usd"})
        base = "https://api.twilio.com/2010-04-01/Accounts/%s/" % TWILIO_SID
        self.assertEqual([url for url, _ in calls],
                         [base + "Usage/Records/ThisMonth.json?Category=totalprice",
                          base + "Balance.json"])
        import base64
        auth = "Basic " + base64.b64encode(("SK0123:" + SECRET).encode()).decode()
        self.assertTrue(all(headers == {"Authorization": auth} for _, headers in calls))
        self.assertNotIn(SECRET, json.dumps(got))

    def test_a_missing_file_reads_as_not_configured(self):
        for env in ({}, {"TWILIO_ENV_FILE": ""}, {"TWILIO_ENV_FILE": "/nonexistent/twilio.env"}):
            with self.subTest(env=env):
                got = usage_sources.read_provider("twilio", env, failing("unreached"), NOW)
                self.assertEqual((got["status"], got["error"]),
                                 ("unknown", "not configured: TWILIO_ENV_FILE names no file"))

    def test_a_path_that_does_not_resolve_is_not_configured(self):
        # Review on sd:9718: ~missing-user raised RuntimeError past the
        # reader's handler and stopped the whole usage run.
        for path in ("~nosuch-operator-example/twilio.env", "bad\x00path", "/dev/null/x"):
            with self.subTest(path=path):
                got = usage_sources.read_provider("twilio", {"TWILIO_ENV_FILE": path},
                                                  failing("unreached"), NOW)
                self.assertEqual((got["status"], got["error"]),
                                 ("unknown", "not configured: TWILIO_ENV_FILE names no file"))

    def test_a_file_missing_a_value_names_it_and_calls_nothing(self):
        calls = []
        got = usage_sources.read_provider("twilio", self.env_file("TWILIO_ACCOUNT_SID=%s\n" % TWILIO_SID),
                                          self.fetch(calls=calls), NOW)
        self.assertEqual((got["status"], got["error"]), ("unknown", "TWILIO_API_KEY not set"))
        got = usage_sources.read_provider("twilio", self.env_file(
            "TWILIO_ACCOUNT_SID=../x\nTWILIO_API_KEY=k\nTWILIO_API_SECRET=s\n"), self.fetch(calls=calls), NOW)
        self.assertEqual((got["status"], got["error"]), ("unknown", "TWILIO_ACCOUNT_SID is not an account SID"))
        self.assertEqual(calls, [])

    def test_a_401_reads_as_an_error_row(self):
        got = usage_sources.read_provider("twilio", self.env_file(), failing("HTTP 401"), NOW)
        self.assertEqual((got["status"], got["error"], got["read_at"]), ("unknown", "HTTP 401", None))
        self.assertNotIn(SECRET, json.dumps(got))

    def test_another_currency_or_no_totalprice_row_is_unknown(self):
        env = self.env_file()
        for usage, balance in ((put(TWILIO_USAGE, ("usage_records", 0, "price_unit"), "eur"), TWILIO_BALANCE),
                               ({"usage_records": []}, TWILIO_BALANCE),
                               (TWILIO_USAGE, put(TWILIO_BALANCE, ("currency",), "EUR"))):
            with self.subTest(usage=usage, balance=balance):
                got = usage_sources.read_provider("twilio", env, self.fetch(usage, balance), NOW)
                self.assertEqual(got["status"], "unknown", got)

    def test_every_hostile_answer_is_ok_or_unknown(self):
        env, failures = self.env_file(), []
        for which, answer in (("usage", TWILIO_USAGE), ("balance", TWILIO_BALANCE)):
            for label, bent in hostile(answer):
                fetch = self.fetch(**{which: bent})
                failures += Hostile.check(self, "%s %s" % (which, label), lambda: usage_sources.read_provider(
                    "twilio", env, fetch, NOW), ("ok",))
        for amount in ("1e999", "nan", "-inf", "1" * 40, "0x10", " 1", "1.5.5"):
            got = usage_sources.read_provider("twilio", env, self.fetch(
                usage=put(TWILIO_USAGE, ("usage_records", 0, "price"), amount)), NOW)
            if got["status"] != "unknown":
                failures.append("price %r: %s" % (amount, got["status"]))
        self.assertEqual(failures, [])

    def test_the_env_file_goes_through_the_shared_parser(self):
        # sd:9864: one .env parser, lib/system_tools_config's, not a copy.
        self.assertFalse(hasattr(usage_sources, "env_file"))
        env = self.env_file()
        shared = usage_sources.system_tools_config.read_env_file
        with mock.patch.object(usage_sources.system_tools_config, "read_env_file",
                               side_effect=shared) as parse:
            got = usage_sources.read_provider("twilio", env, self.fetch(), NOW)
        self.assertEqual(got["status"], "ok", got)
        self.assertEqual(parse.call_args.args[0], Path(env["TWILIO_ENV_FILE"]))

    def test_agent_meter_reads_twilio_env_file_from_its_own_env(self):
        meter = load_meter()
        config = Path(tempfile.mkdtemp(prefix="meter-config."))
        self.addCleanup(lambda: __import__("shutil").rmtree(config))
        (config / "agent-meter").mkdir()
        (config / "agent-meter/.env").write_text("TWILIO_ENV_FILE=/elsewhere/twilio.env\n")
        seen = {}
        with mock.patch.dict(os.environ, SYSTEM_TOOLS_CONFIG=str(config)), \
                mock.patch.object(meter.usage_sources, "read_provider",
                                  side_effect=lambda name, env, *rest: seen.update(env) or {}):
            os.environ.pop("TWILIO_ENV_FILE", None)
            meter.read_one("twilio", None, None, NOW)
        self.assertEqual(seen.get("TWILIO_ENV_FILE"), "/elsewhere/twilio.env")


class MeterEnv(unittest.TestCase):
    """Review on sd:9718: read_one read the tool's .env for every keyed
    provider outside the per-provider handler, so a .env that does not
    read stopped the whole usage run."""

    def test_an_unreadable_env_costs_twilio_its_row_only(self):
        meter = load_meter()
        config = Path(tempfile.mkdtemp(prefix="meter-config."))
        self.addCleanup(lambda: __import__("shutil").rmtree(config))
        (config / "agent-meter").mkdir()
        (config / "agent-meter/.env").write_bytes(b"TWILIO_ENV_FILE=\xff\xfe\n")
        answer = ANSWERS["openrouter"][1]
        with mock.patch.dict(os.environ, SYSTEM_TOOLS_CONFIG=str(config), OPENROUTER_API_KEY=SECRET), \
                mock.patch.object(meter.usage_sources, "fetch_json", answering(answer)):
            keyed = meter.read_one("openrouter", None, None, NOW)
            twilio = meter.read_one("twilio", None, None, NOW)
        self.assertEqual(keyed["status"], "ok")
        self.assertEqual((twilio["status"], twilio["error"]),
                         ("unknown", "agent-meter .env does not read: UnicodeDecodeError"))

    def test_an_unreadable_env_costs_the_usage_run_only_twilios_row(self):
        # Review on sd:9718: the parser's --out default read the same
        # .env before dispatch, so the run wrote no row at all.
        live = Live("test_the_runs_read_a_config_folder_the_test_owns")
        live.setUp()
        self.addCleanup(live.doCleanups)
        config = Path(live.env["SYSTEM_TOOLS_CONFIG"]) / "agent-meter"
        config.mkdir(parents=True, exist_ok=True)
        (config / ".env").write_bytes(b"TWILIO_ENV_FILE=\xff\xfe\n")
        live.setting("dashboard.usage_providers", "nabucasa fixed=6.50\ntwilio\n")
        done = live.run_live("usage")
        self.assertEqual(done.returncode, 0, done.stderr)
        rows = live.rows("usage:")
        self.assertEqual(rows["usage:nabucasa"]["status"], "ok")
        self.assertEqual((rows["usage:twilio"]["status"], rows["usage:twilio"]["error"]),
                         ("unknown", "agent-meter .env does not read: UnicodeDecodeError"))

    def test_the_live_runs_never_read_the_operators_twilio_file(self):
        # Review on sd:9718: twilio is on the starting list and its file
        # is named by TWILIO_ENV_FILE, not a key ANSWERS holds.
        with mock.patch.dict(os.environ, TWILIO_ENV_FILE="/nowhere/twilio.env"):
            live = Live("test_the_runs_read_a_config_folder_the_test_owns")
            live.setUp()
        self.addCleanup(live.doCleanups)
        self.assertNotIn("TWILIO_ENV_FILE", live.env)


class FixedFees(unittest.TestCase):
    """sd:9719: a providers line 'name fixed=N' is that monthly cost and
    no usage; nothing is fetched. Socket has no plan usage API."""

    def test_a_fixed_line_parses_beside_cap_lines(self):
        entries, problems = usage_sources.parse_providers(
            "nabucasa fixed=6.50\nvoicedub  fixed=12 # month\nzeroed fixed=0\nbaseten cap=250\n"
            "resend fixed=-1\nsocket fixed=x\ntwilio cap=5 fixed=3\nnabucasa fixed=9\n")
        # sd:9749: a malformed value keeps the row, with no value.
        self.assertEqual(entries, [("nabucasa", None, 6.5), ("voicedub", None, 12.0),
                                   ("zeroed", None, None), ("baseten", 250.0, None),
                                   *[(name, None, None) for name in ("resend", "socket", "twilio")]])
        self.assertEqual(problems, ["line 3: fixed=N wants N a positive decimal",
                                    "line 5: fixed=N wants N a positive decimal",
                                    "line 6: fixed=N wants N a positive decimal",
                                    "line 7: cap=N wants N a positive decimal"])

    def test_a_fixed_fee_on_a_provider_read_elsewhere_is_refused(self):
        # sd:9719 triage: claude, codex and huggingface never reach
        # read_provider, so their fee would parse and never show.
        entries, problems = usage_sources.parse_providers(
            "claude fixed=20\ncodex fixed=5\nhuggingface fixed=9\nnabucasa fixed=6.50\n")
        self.assertEqual(entries, [("claude", None, None), ("codex", None, None),
                                   ("huggingface", None, None), ("nabucasa", None, 6.5)])
        self.assertEqual(problems, ["line %d: fixed=N is not for %s: its usage is read" % (n, name)
                                    for n, name in ((1, "claude"), (2, "codex"), (3, "huggingface"))])

    def test_a_fixed_fee_is_the_months_cost_with_no_call(self):
        got = usage_sources.read_provider("nabucasa", {}, failing("unreached"), NOW, fixed=6.5)
        self.assertEqual(got, usage_sources.reading(
            "nabucasa", "ok", "fixed", read_at="2026-11-17T20:00:00Z",
            spend={"amount": 6.5, "unit": "usd", "period": "month", "fixed": True,
                   "resets_at": NEXT}))

    def test_socket_has_no_usage_source(self):
        got = usage_sources.read_provider("socket", {"SOCKET_SECURITY_API_KEY": SECRET},
                                          failing("unreached"), NOW)
        self.assertEqual((got["status"], got["source"]), ("no-source", "none"))

    def test_resend_without_a_monthly_number_is_unknown(self):
        var, answer, _ = ANSWERS["resend"]
        for path, value in ((("emails", "monthly", "limit"), None), (("emails", "monthly", "limit"), 0),
                            (("emails", "monthly", "used"), -1), (("emails", "monthly"), None),
                            (("emails",), [])):
            with self.subTest(path=path, value=value):
                got = usage_sources.read_provider("resend", {var: SECRET}, answering(put(answer, path, value)), NOW)
                self.assertEqual(got["status"], "unknown", got)

    def test_resend_s_month_that_has_reset_is_no_reading(self):
        # sd:9719 review: at the reset boundary the old month's record read
        # ok with zero emails left; its spend and balance outlived its
        # window. A record whose resets_at is not after now is refused.
        var, answer, _ = ANSWERS["resend"]
        for end in ("2026-11-17T20:00:00Z", "2026-11-01T00:00:00.000Z"):
            with self.subTest(end=end):
                old = put(put(answer, ("emails", "monthly", "resets_at"), end),
                          ("emails", "monthly", "used"), 3000)
                got = usage_sources.read_provider("resend", {var: SECRET}, answering(old), NOW)
                self.assertEqual(got["status"], "unknown", got)
                self.assertEqual((got["balance"], got["spend"]), (None, None))

    def test_the_live_run_writes_a_fixed_row(self):
        live = Live("test_the_runs_read_a_config_folder_the_test_owns")
        live.setUp()
        self.addCleanup(live.doCleanups)
        live.setting("dashboard.usage_providers", "nabucasa fixed=6.50\n")
        done = live.run_live("usage")
        self.assertEqual(done.returncode, 0, done.stderr)
        row = live.rows("usage:")["usage:nabucasa"]
        self.assertEqual((row["status"], row["source"], row["spend"]["amount"], row["spend"]["fixed"]),
                         ("ok", "fixed", 6.5, True))
