"""The Jira collector, lifted from the pack and returning `Collected`.

Three rules are carried over from the pack's module and each is asserted here
because each was learned the hard way: `myself` is the availability check and
not the search, account ids are compared when both sides have one, and the JQL
window is relative minutes and never a timestamp. The fourth assertion is the
one thing the lift changed on purpose -- `ok` folds truncation in, so a caller
that guards its watermark on `ok` alone cannot step past a page it never read.

Nothing here imports `dashboard`: there is no such package in this repository,
and an import of it would fail at collection rather than in a test.

Every test runs with `JIRA_*` removed from the process environment. Each call
here passes its own environment, but a developer shell carries a real
`JIRA_API_TOKEN`, and one call that forgot would otherwise send it.
"""

import http.client
import os
import re
import socketserver
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from sd_db import shadow_jira
from sd_db.shadow_jira import (MAX_PAGES, MAX_REQUESTS, PAGE_SIZE, _http, collect, missing, search, settings,
                               window_minutes)
from sd_db.shadow_sync import iso

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)

JIRA_ENV = {
    "JIRA_BASE_URL": "https://example.invalid",
    "JIRA_EMAIL": "someone@example.invalid",
    "JIRA_API_TOKEN": "not-a-real-token",
}


def jira_issue(key: str, *, category: str = "To Do", me: str = "acct-1", **over):
    fields = {
        "summary": f"issue {key}",
        "status": {"name": "Open", "statusCategory": {"name": category}},
        "updated": "2026-08-30T00:00:00.000+0000",
        "assignee": {"accountId": me, "displayName": "Alex"},
        "reporter": {"accountId": "acct-other", "displayName": "Someone"},
        "project": {"key": key.split("-")[0]},
        "watches": {"isWatching": False},
    }
    fields.update(over)
    return {"key": key, "fields": fields}


def jira_transport(pages: list[dict], *, me: str = "acct-1", myself_error=None):
    """A Jira that answers from a script. Records the JQL it was asked."""
    seen: dict = {"jql": [], "calls": 0}

    def transport(url, headers, body):
        seen["calls"] += 1
        if url.endswith("/myself"):
            if myself_error is not None:
                return None, myself_error
            return {"accountId": me}, ""
        seen["jql"].append(body["jql"])
        index = len([j for j in seen["jql"]]) - 1
        return (pages[index] if index < len(pages) else {"issues": [], "isLast": True}), ""

    transport.seen = seen
    return transport


class WindowedJira:
    """A Jira that applies the JQL window and pages by token, on a clock the test moves.

    `updated >= -Nm` resolves against Jira's own now when the request arrives,
    so a split window asked later covers a later span. `ages` are minutes
    before the collect began; `step` is how far Jira's clock and the
    collector's move per search request.
    """

    def __init__(self, ages, *, step=0.0):
        self.ages = sorted(ages)
        self.step = step
        self.elapsed = 0.0
        self.jql: list[str] = []

    def clock(self) -> float:
        return self.elapsed

    def __call__(self, url, headers, body):
        if url.endswith("/myself"):
            return {"accountId": "acct-1"}, ""
        jql = body["jql"]
        self.jql.append(jql)
        at = self.elapsed / 60
        older = int(re.search(r"updated >= -(\d+)m", jql).group(1))
        newer = re.search(r"updated <= -(\d+)m", jql)
        matched = [age for age in self.ages
                   if -age >= at - older and (newer is None or -age <= at - int(newer.group(1)))]
        start = int(body.get("nextPageToken") or 0)
        page = matched[start:start + body["maxResults"]]
        self.elapsed += self.step
        last = start + len(page) >= len(matched)
        answer = {"issues": [jira_issue(f"ABC-{self.ages.index(age)}") for age in page], "isLast": last}
        if not last:
            answer["nextPageToken"] = str(start + len(page))
        return answer, ""


def without_jira_environment():
    """The process environment minus `JIRA_*` and any proxy, for one test."""
    kept = {name: value for name, value in os.environ.items()
            if not name.startswith("JIRA_") and not name.lower().endswith("_proxy")}
    return patch.dict(os.environ, {**kept, "no_proxy": "*", "NO_PROXY": "*"}, clear=True)


class Recorder(HTTPServer):
    """A loopback server that records each request's path and headers."""

    def __init__(self, answer):
        self.seen: list[tuple[str, dict]] = []
        self.answer = answer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(inner):
                self.seen.append((inner.path, dict(inner.headers)))
                code, headers, body = self.answer()
                inner.send_response(code)
                for name, value in headers.items():
                    inner.send_header(name, value)
                inner.send_header("Content-Length", str(len(body)))
                inner.end_headers()
                inner.wfile.write(body)

            def log_message(inner, *args):
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def close(self):
        self.shutdown()
        self.server_close()


class RawServer(socketserver.TCPServer):
    """A loopback server that answers every request with the same raw bytes.

    For the answers `BaseHTTPRequestHandler` will not write: a status line that
    is not HTTP, or a body shorter than its `Content-Length`.
    """

    allow_reuse_address = True

    def __init__(self, reply: bytes):
        self.connections = 0

        class Handler(socketserver.StreamRequestHandler):
            def handle(inner):
                self.connections += 1
                while inner.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                inner.wfile.write(reply)

        super().__init__(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def close(self):
        self.shutdown()
        self.server_close()


# A secret an operator pasted into the base URL. It must reach no reason, no
# heartbeat and no exception.
SECRET = "pa55word"

# Answers `http.client` raises on, each quoting something in its message.
BROKEN_ANSWERS = {
    "BadStatusLine": f"NOT-HTTP {SECRET}\r\n\r\n".encode(),
    "IncompleteRead": (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                       b"Content-Length: 100\r\n\r\n{\"accountId\": \"" + SECRET.encode()),
    # Both a `BadStatusLine` and a `ConnectionResetError`, so an `OSError`.
    "RemoteDisconnected": b"",
}

# Base URLs `urllib.parse` refuses with a `ValueError` that quotes the
# authority: look-alike punctuation that NFKC turns into `:` and `@`, and a
# bracketed host that is not an address. `[%3A%3A1]` is one only until it is
# decoded, so the value itself must be split as well as its decoded authority.
UNSPLITTABLE = (
    f"https://user\uff1a{SECRET}\uff20jira.example.invalid",
    f"https://jira.example.invalid\uff1a{SECRET}",
    f"https://jira.example.invalid\ufe6b{SECRET}",
    f"https://user%EF%BC%9A{SECRET}%EF%BC%A0jira.example.invalid",
    f"https://[::1:{SECRET}]",
    "https://[%3A%3A1]",
)

# Base URLs `urllib.parse` splits whose host is no name or address, so the
# resolver, a proxy's CONNECT line and a certificate-mismatch message all get
# the host text: an IPvFuture, a zone, a second colon, a twice-encoded `@`,
# an empty label, which the `idna` codec refuses, and names longer than DNS
# allows, which it does not: one far over, one a character over 253.
NOT_A_HOST = (
    f"https://[v1.user:{SECRET}]",
    f"https://[v1.user%3A{SECRET}]",
    f"https://[fe80::1%25user:{SECRET}]",
    f"https://user%253A{SECRET}%2540jira.example.invalid",
    f"https://user:{SECRET}:443",
    "https://[fe80::1%25en0]:443",
    "https://[v1.x]",
    "https://:443",
    f"https://{SECRET}..jira.example.invalid",
    f"https://{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 63}.{SECRET}.invalid",
    f"https://{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 62}",
    # A plain host and port once decoded, but `config["base"]` would keep the
    # encoded text, so the value checked is not the value used.
    f"https://{SECRET}%3A443",
    "https://jira.example.invalid%3A8443/jira",
)
NOT_A_HOST_REASON = "JIRA_BASE_URL must have a host that is a DNS name, an IPv4 address or a bracketed IPv6 address"

# Hosts that are not ASCII once decoded, refused before any other host rule.
# `http.client` writes a Latin-1 host raw into `Host` (`jüra`), and the stdlib
# `idna` codec is IDNA 2003, which names these differently from IDNA 2008:
# `ß`, `ẞ` (raw and encoded) and `ｊｉｒａ` become `fass` and `jira`, the
# Cherokee capitals are lowercased, `ς` becomes `σ` and the joiners are
# dropped. `例え` and a 226-character IDN whose A-label is 254 complete it.
NON_ASCII_HOST = (
    "https://j\u00fcra.example.invalid",
    "https://\u4f8b\u3048.example.invalid",
    "https://\uff4a\uff49\uff52\uff41.example.invalid",
    "https://fa\u1e9e.example.invalid",
    "https://fa%E1%BA%9E.example.invalid",
    "https://\u13a0\u13a1.example.invalid",
    "https://fa\u00df.example.invalid",
    "https://fa%C3%9F.example.invalid",
    "https://\u03c2igma.example.invalid",
    "https://ji\u200cra.example.invalid",
    "https://ji\u200dra.example.invalid",
    f"https://{'a' * 55}\u00fc.{'b' * 55}\u00fc.{'c' * 55}\u00fc.{'d' * 54}\u00fc",
)
NON_ASCII_HOST_REASON = "JIRA_BASE_URL must have an ASCII host; write an international name in its xn-- form"


def no_socket():
    """Any connection attempt fails in the fixture, before DNS or connect."""
    return patch("http.client.HTTPConnection.connect",
                 side_effect=OSError("fixture: no network in tests"))


class JiraTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = without_jira_environment()
        environment.start()
        self.addCleanup(environment.stop)

    def test_a_redirect_is_refused_and_the_credentials_stay_put(self) -> None:
        """urllib copies `Authorization` into a followed redirect, to any host.

        The loopback servers are http because a test cannot mint a trusted
        certificate; `settings` is replaced so `collect` runs its real transport
        against them. The second server is a different host:port, which is where
        the copied header would land.
        """
        elsewhere = Recorder(lambda: (200, {"Content-Type": "application/json"}, b'{"accountId": "x"}'))
        self.addCleanup(elsewhere.close)
        jira = Recorder(lambda: (302, {"Location": elsewhere.url + "/rest/api/3/myself"}, b""))
        self.addCleanup(jira.close)
        config = {**settings(JIRA_ENV), "base": jira.url}
        with patch("sd_db.shadow_jira.settings", return_value=config):
            result = collect(None, NOW, None, {})
        self.assertEqual(elsewhere.seen, [], "a redirect was followed, credentials and all")
        self.assertFalse(result.ok)
        self.assertIn("Jira redirected the request (302 Found)", result.reason)
        self.assertEqual(result.requests, 1)
        self.assertEqual(len(jira.seen), 1)
        self.assertIn("Authorization", jira.seen[0][1], "the fixture did not exercise an authenticated request")

    def test_absent_variables_are_named_never_valued(self) -> None:
        """Names and presence only -- the report must not become a leak."""
        result = collect(None, NOW, None, {"JIRA_API_TOKEN": "secret-value"})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "JIRA_BASE_URL and JIRA_EMAIL not set")
        self.assertNotIn("secret-value", result.reason)
        self.assertNotIn("JIRA_API_TOKEN", result.reason, "a set variable was reported missing")

    def test_no_base_url_is_never_guessed(self) -> None:
        """The backbone carries no employer host, not even as a fallback."""
        env = dict(JIRA_ENV)
        del env["JIRA_BASE_URL"]
        result = collect(None, NOW, jira_transport([]), env)
        self.assertFalse(result.ok)
        self.assertIn("JIRA_BASE_URL", result.reason)

    def test_a_base_url_that_is_not_https_is_not_configured(self) -> None:
        """The token rides in every request's header; http would send it clear."""
        cases = {
            "http://example.invalid": "JIRA_BASE_URL must be an https URL, not http",
            "HTTP://example.invalid": "JIRA_BASE_URL must be an https URL, not http",
            "http://localhost:8080": "JIRA_BASE_URL must be an https URL, not http",
            "ftp://example.invalid": "JIRA_BASE_URL must be an https URL, not ftp",
            "example.invalid:8080": "JIRA_BASE_URL must be an https URL",
        }
        for base, reason in cases.items():
            with self.subTest(base=base):
                env = {**JIRA_ENV, "JIRA_BASE_URL": base}
                transport = jira_transport([{"issues": [jira_issue("ABC-1")], "isLast": True}])
                result = collect(None, NOW, transport, env)
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, reason)
                self.assertEqual(transport.seen["calls"], 0, "a request was sent over a non-https base")
                self.assertEqual(result.requests, 0)
                self.assertEqual(missing(settings(env)), ["JIRA_BASE_URL"],
                                 "a refused base must read as unconfigured to the caller")
                self.assertNotIn("example.invalid", result.reason)
        env = {**JIRA_ENV, "JIRA_BASE_URL": "HTTPS://example.invalid/"}
        self.assertEqual(missing(settings(env)), [])
        env = {"JIRA_BASE_URL": "http://example.invalid", "JIRA_API_TOKEN": "t"}
        self.assertEqual(collect(None, NOW, jira_transport([]), env).reason,
                         "JIRA_BASE_URL must be an https URL, not http; JIRA_EMAIL not set")

    def test_a_base_url_with_a_user_or_a_word_for_a_port_is_not_configured(self) -> None:
        """`http.client` raises `InvalidURL` quoting the text after the last colon.

        The seam is the real transport, with every socket refused, so a base URL
        that got past `settings` would reach `http.client` and not the network.
        """
        cases = {
            f"https://user:{SECRET}@jira.example.invalid": "JIRA_BASE_URL must not carry a user name or password",
            f"https://user%3A{SECRET}%40jira.example.invalid": "JIRA_BASE_URL must not carry a user name or password",
            "https://someone@jira.example.invalid/": "JIRA_BASE_URL must not carry a user name or password",
            f"https://jira.example.invalid:{SECRET}": "JIRA_BASE_URL must have a port from 1 to 65535",
            f"https://jira.example.invalid:{SECRET}/wiki?x=1": "JIRA_BASE_URL must have a port from 1 to 65535",
            f"https://[::1]:{SECRET}": "JIRA_BASE_URL must have a port from 1 to 65535",
            "https://jira.example.invalid:0": "JIRA_BASE_URL must have a port from 1 to 65535",
            "https://jira.example.invalid:65536": "JIRA_BASE_URL must have a port from 1 to 65535",
            **{base: "JIRA_BASE_URL is not a URL urllib can split" for base in UNSPLITTABLE},
            **{base: NOT_A_HOST_REASON for base in NOT_A_HOST},
            **{base: NON_ASCII_HOST_REASON for base in NON_ASCII_HOST},
        }
        for base, reason in cases.items():
            with self.subTest(base=base), no_socket() as connect:
                env = {**JIRA_ENV, "JIRA_BASE_URL": base}
                result = collect(None, NOW, None, env)
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, reason)
                self.assertEqual((result.requests, connect.call_count), (0, 0))
                self.assertEqual(missing(settings(env)), ["JIRA_BASE_URL"],
                                 "a refused base must read as unconfigured to the caller")
                self.assertNotIn(SECRET, result.reason)
                self.assertNotIn("example.invalid", result.reason)
        # `int` refuses 4300 digits or more; the value stays out of the subtest label.
        long_port = {**JIRA_ENV, "JIRA_BASE_URL": "https://jira.example.invalid:" + "4" * 5000}
        self.assertEqual((settings(long_port)["refused"], missing(settings(long_port))),
                         ("JIRA_BASE_URL must have a port from 1 to 65535", ["JIRA_BASE_URL"]))
        for base in ("https://jira.example.invalid:8443", "https://jira.example.invalid:/",
                     "https://jira.example.invalid:1", "https://jira.example.invalid:65535",
                     "https://[::1]:8443/jira", "https://[::1]", "https://jira.example.invalid/a@b:c",
                     "https://jira.example.invalid", "https://jira.example.invalid:443",
                     "https://jira.example.invalid.", "https://[2001:db8::1]:8443", "https://10.0.0.1:8443",
                     "https://xn--bcher-kva.example.invalid", "https://jira_host.example.invalid", "https://jira_host",
                     f"https://{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 61}.", "https://[::ffff:10.0.0.1]"):
            with self.subTest(admitted=base):
                config = settings({**JIRA_ENV, "JIRA_BASE_URL": base})
                self.assertEqual((missing(config), config["base"]), ([], base.rstrip("/")))

    def test_a_transport_failure_is_a_failed_collect_that_names_only_its_type(self) -> None:
        """`http.client.HTTPException` is neither `OSError` nor `ValueError`.

        Uncaught, it left `collect` and `sync` with no heartbeat and no count.
        """
        for name, reply in BROKEN_ANSWERS.items():
            with self.subTest(answer=name):
                server = RawServer(reply)
                self.addCleanup(server.close)
                config = {**settings(JIRA_ENV), "base": server.url}
                with patch("sd_db.shadow_jira.settings", return_value=config):
                    result = collect(None, NOW, None, {})
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, f"the request to Jira failed: {name}")
                self.assertEqual((result.requests, server.connections), (1, 1))
        # Past `settings`, authorities `http.client` and `urllib.parse` refuse,
        # quoting them whole.
        cases = {f"https://jira.example.invalid {SECRET}": "InvalidURL",
                 **{base: "ValueError" for base in UNSPLITTABLE if "%" not in base}}
        for base, name in cases.items():
            with self.subTest(base=base), no_socket() as connect:
                payload, reason = _http(base + "/rest/api/3/myself", {}, None)
                self.assertEqual((payload, reason, connect.call_count),
                                 (None, f"the request to Jira failed: {name}", 0))

    def test_only_a_refused_follow_is_called_a_redirect(self) -> None:
        """`urllib` follows 301, 302, 303, 307 and 308, and only with a `Location`.

        A 300, 304 or 305, or a 302 with nowhere to go, was never going to be
        followed, so it is reported as the status it is.
        """
        elsewhere = Recorder(lambda: (200, {"Content-Type": "application/json"}, b'{"accountId": "x"}'))
        self.addCleanup(elsewhere.close)
        cases = {
            (300, True): "Jira returned 300 Multiple Choices",
            (304, False): "Jira returned 304 Not Modified",
            (305, True): "Jira returned 305 Use Proxy",
            (302, False): "Jira returned 302 Found",
            (308, True): "Jira redirected the request (308 Permanent Redirect)",
        }
        for (code, located), reason in cases.items():
            with self.subTest(code=code, location=located):
                headers = {"Location": elsewhere.url + "/rest/api/3/myself"} if located else {}
                jira = Recorder(lambda: (code, headers, b""))
                self.addCleanup(jira.close)
                config = {**settings(JIRA_ENV), "base": jira.url}
                with patch("sd_db.shadow_jira.settings", return_value=config):
                    result = collect(None, NOW, None, {})
                self.assertFalse(result.ok)
                self.assertTrue(result.reason.startswith(reason), result.reason)
                self.assertEqual((result.requests, len(jira.seen)), (1, 1))
        self.assertEqual(elsewhere.seen, [])

    def test_every_request_is_counted_including_failed_ones(self) -> None:
        pages = [
            {"issues": [jira_issue("ABC-1")], "isLast": False, "nextPageToken": "2"},
            {"issues": [jira_issue("ABC-2")], "isLast": True},
        ]
        self.assertEqual(collect(None, NOW, jira_transport(pages), JIRA_ENV).requests, 3)
        failed = jira_transport([], myself_error="Jira rejected the credentials (401 Unauthorized)")
        self.assertEqual(collect(None, NOW, failed, JIRA_ENV).requests, 1)
        self.assertEqual(collect(None, NOW, jira_transport([]), {}).requests, 0)

        def second_page_fails(url, headers, body):
            if url.endswith("/myself"):
                return {"accountId": "acct-1"}, ""
            if "nextPageToken" in body:
                return None, "Jira returned 500 Internal Server Error"
            return {"issues": [jira_issue("ABC-1")], "isLast": False, "nextPageToken": "2"}, ""

        result = collect(None, NOW, second_page_fails, JIRA_ENV)
        self.assertEqual((result.ok, result.requests), (False, 3))

    def test_the_real_transport_counts_a_request_that_never_connected(self) -> None:
        """No seam: the opener itself is stubbed to fail, so nothing leaves the machine."""
        refused = urllib.error.URLError("fixture: no network in tests")
        with patch("sd_db.shadow_jira.urllib.request.OpenerDirector.open", side_effect=refused) as opened:
            result = collect(None, NOW, None, JIRA_ENV)
        self.assertFalse(result.ok)
        self.assertIn("could not reach Jira", result.reason)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(result.requests, 1)

    def test_a_falsey_transport_is_still_the_transport(self) -> None:
        """An absent seam is `None`, not any value that tests false."""

        class Empty:
            def __init__(self):
                self.calls = 0

            def __len__(self):
                return 0

            def __call__(self, url, headers, body):
                self.calls += 1
                if url.endswith("/myself"):
                    return {"accountId": "acct-1"}, ""
                return {"issues": [], "isLast": True}, ""

        transport = Empty()
        with patch("sd_db.shadow_jira.urllib.request.OpenerDirector.open",
                   side_effect=AssertionError("the real transport was used")) as opened:
            result = collect(None, NOW, transport, JIRA_ENV)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(opened.call_count, 0)
        self.assertEqual((transport.calls, result.requests), (2, 2))
        # And below `collect`, where the seam reaches `_request` directly.
        with patch("sd_db.shadow_jira.urllib.request.OpenerDirector.open",
                   side_effect=AssertionError("the real transport was used")) as opened:
            search("project = ABC", settings(JIRA_ENV), transport)
        self.assertEqual((opened.call_count, transport.calls), (0, 3))

    def test_bad_credentials_fail_before_the_search_is_believed(self) -> None:
        """A 200-with-no-issues must not read as "you have nothing to do"."""
        transport = jira_transport([], myself_error="Jira rejected the credentials (401 Unauthorized)")
        result = collect(None, NOW, transport, JIRA_ENV)
        self.assertFalse(result.ok)
        self.assertIn("401", result.reason)
        self.assertEqual(transport.seen["calls"], 1, "the search ran despite bad credentials")

    def test_the_window_is_relative_minutes_not_a_timestamp(self) -> None:
        """JQL date literals resolve in the Jira account's timezone, not ours."""
        transport = jira_transport([{"issues": [], "isLast": True}])
        collect(iso(NOW - timedelta(hours=2)), NOW, transport, JIRA_ENV)
        jql = transport.seen["jql"][0]
        self.assertIn("updated >= -180m", jql, "the window was not the mark plus the overlap")
        self.assertNotIn("2026-", jql, "a timezone-dependent timestamp reached the JQL")

    def test_reasons_come_from_identity_and_from_jira_itself(self) -> None:
        raw = jira_issue("ABC-1")
        raw["fields"]["watches"] = {"isWatching": True}
        raw["fields"]["reporter"] = {"accountId": "acct-1", "displayName": "Alex"}
        transport = jira_transport([{"issues": [raw], "isLast": True}])
        result = collect(None, NOW, transport, JIRA_ENV)
        self.assertTrue(result.ok)
        self.assertEqual(sorted(result.issues[0]["why"]), ["assigned", "filed", "watching"])

    def test_an_empty_identity_does_not_match_everyone(self) -> None:
        """Empty-equals-empty is how every issue becomes both yours and theirs."""
        raw = jira_issue("ABC-1")
        raw["fields"]["assignee"] = {"displayName": "Nobody"}
        raw["fields"]["reporter"] = {"displayName": "Nobody"}
        transport = jira_transport([{"issues": [raw], "isLast": True}])
        result = collect(None, NOW, transport, JIRA_ENV)
        self.assertEqual(result.issues[0]["why"], ["matched"])

    def test_an_account_id_beats_a_matching_email(self) -> None:
        """The id when both sides have one -- Jira hides email on private sites.

        The assignee here carries a *different* id and the operator's own
        address. Comparing by email would call it assigned; comparing by id,
        which is the rule, calls it only watched.
        """
        raw = jira_issue("ABC-1")
        raw["fields"]["assignee"] = {
            "accountId": "acct-someone-else",
            "displayName": "Someone Else",
            "emailAddress": JIRA_ENV["JIRA_EMAIL"],
        }
        raw["fields"]["watches"] = {"isWatching": True}
        transport = jira_transport([{"issues": [raw], "isLast": True}])
        result = collect(None, NOW, transport, JIRA_ENV)
        self.assertEqual(result.issues[0]["why"], ["watching"])

    def test_the_status_category_decides_closed(self) -> None:
        transport = jira_transport(
            [{"issues": [jira_issue("ABC-1", category="Done")], "isLast": True}]
        )
        result = collect(None, NOW, transport, JIRA_ENV)
        self.assertEqual(result.issues[0]["state"], "closed")

    def test_a_missing_page_token_is_truncation_not_the_end(self) -> None:
        """`isLast: false` with no token is malformed, not "no more results".

        Reading it as the end drops the remainder while reporting a complete
        walk, which is the failure mode that looks right.
        """
        transport = jira_transport([{"issues": [jira_issue("ABC-1")], "isLast": False}])
        result = collect(None, NOW, transport, JIRA_ENV)
        self.assertEqual(result.truncated, ["jql"])
        self.assertEqual(len(result.issues), 1)
        self.assertFalse(result.ok, "the rows are real, but the window is not covered")
        self.assertEqual(result.reason, "Jira answered with malformed pagination")
        self.assertEqual(len(transport.seen["jql"]), 1, "a malformed page was split and asked again")

    def test_window_minutes_rounds_up(self) -> None:
        """Truncating would shave the oldest edge off the overlap."""
        self.assertEqual(window_minutes(NOW - timedelta(seconds=61), NOW), 2)
        self.assertEqual(window_minutes(NOW, NOW), 1)

    def test_one_open_issue_is_one_row(self) -> None:
        """The control: the shape the library's `store` is handed."""
        transport = jira_transport([{"issues": [jira_issue("ABC-1")], "isLast": True}])
        result = collect(None, NOW, transport, JIRA_ENV)
        self.assertTrue(result.ok)
        self.assertEqual(result.truncated, [])
        self.assertEqual(len(result.issues), 1)
        row = result.issues[0]
        self.assertEqual(row["tracker"], "jira")
        self.assertIsNone(row["number"])
        self.assertEqual(row["state"], "open")
        self.assertEqual(row["url"], "https://example.invalid/browse/ABC-1")

    def test_the_window_is_reported_in_the_librarys_own_spelling(self) -> None:
        """`window_start` must read back through `parse_iso`, or the caller's
        cursor comparison in `sync` silently fails on every run."""
        result = collect(None, NOW, jira_transport([{"issues": [], "isLast": True}]), JIRA_ENV)
        self.assertEqual(result.window_start, iso(NOW - timedelta(days=90)))
        self.assertEqual(result.window_end, iso(NOW))

    def test_a_first_window_past_the_ceiling_is_split_until_it_is_covered(self) -> None:
        """More issues than `MAX_PAGES * PAGE_SIZE` must not restart every run.

        Truncated, `ok` stays false and the watermark unset, so a window that
        can never fit is asked again whole, forever. Split, it is covered.
        """
        count = MAX_PAGES * PAGE_SIZE + 200
        jira = WindowedJira([n * 129000 / count + 0.5 for n in range(count)])
        result = collect(None, NOW, jira, JIRA_ENV, clock=jira.clock)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual((result.truncated, len(result.issues)), ([], count))
        self.assertEqual(jira.jql[0], shadow_jira.DEFAULT_JQL.format(window="updated >= -129600m"))
        self.assertTrue(any("updated <= -" in jql for jql in jira.jql[MAX_PAGES:]),
                        "no split window had a newer edge")

    def test_a_split_window_asked_later_is_widened_by_the_time_since(self) -> None:
        """Relative minutes move with Jira's clock; the older edge must move back as far."""
        count = MAX_PAGES * PAGE_SIZE + 200
        jira = WindowedJira([n * 129000 / count + 0.5 for n in range(count)], step=3600)
        result = collect(None, NOW, jira, JIRA_ENV, clock=jira.clock)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(result.issues), count, "a split window asked later skipped a span")

    def test_a_window_that_never_fits_stops_at_the_request_limit(self) -> None:
        def endless(url, headers, body):
            endless.searches += not url.endswith("/myself")
            if url.endswith("/myself"):
                return {"accountId": "acct-1"}, ""
            start = int(body.get("nextPageToken") or 0)
            return {"issues": [jira_issue(f"ABC-{start}")], "isLast": False,
                    "nextPageToken": str(start + 1)}, ""

        endless.searches = 0
        result = collect(None, NOW, endless, JIRA_ENV)
        self.assertFalse(result.ok)
        self.assertIn("request limit", result.reason)
        self.assertEqual(endless.searches, MAX_REQUESTS)
        self.assertEqual(result.requests, MAX_REQUESTS + 1)

    def test_a_custom_jql_is_not_split_and_still_reports_the_ceiling(self) -> None:
        """`JIRA_JQL` carries no window to split, so the ceiling stands and says so."""
        pages = [{"issues": [jira_issue(f"ABC-{n}")], "isLast": False, "nextPageToken": str(n + 1)}
                 for n in range(MAX_PAGES + 2)]
        transport = jira_transport(pages)
        result = collect(None, NOW, transport, {**JIRA_ENV, "JIRA_JQL": "project = ABC"})
        self.assertEqual((result.truncated, len(result.issues), result.ok), (["jql"], MAX_PAGES, False))
        self.assertEqual(set(transport.seen["jql"]), {"project = ABC"})

    def test_is_last_must_be_a_boolean(self) -> None:
        """A truthy string such as `"false"` must not end the walk as complete."""
        for value in ("false", "true", 1, None, [False]):
            with self.subTest(isLast=value):
                transport = jira_transport([{"issues": [jira_issue("ABC-1")], "isLast": value,
                                             "nextPageToken": "2"}])
                result = collect(None, NOW, transport, JIRA_ENV)
                self.assertFalse(result.ok, "a malformed isLast was read as a finished walk")
                self.assertEqual(result.reason, "Jira answered with malformed pagination")
                self.assertEqual(len(transport.seen["jql"]), 1, "a malformed page was split and asked again")

    def test_the_settings_contract_names_every_variable_it_reads(self) -> None:
        """`settings` reads the optional `JIRA_JQL` too; its docstring must say so."""
        for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_JQL"):
            self.assertIn(name, settings.__doc__)
        self.assertNotIn("three", settings.__doc__)
        self.assertEqual(settings({**JIRA_ENV, "JIRA_JQL": " project = ABC "})["jql"], "project = ABC")
        self.assertEqual(missing(settings(JIRA_ENV)), [], "JIRA_JQL is optional")

    def test_an_http_exception_inside_a_url_error_names_only_its_type(self) -> None:
        """`URLError`'s message quotes its reason, which can be an `HTTPException`."""
        for inner in (http.client.RemoteDisconnected(f"closed by {SECRET}"),
                      http.client.InvalidURL(f"nonnumeric port: '{SECRET}'"),
                      ValueError(f"bad authority {SECRET}")):
            with self.subTest(inner=type(inner).__name__):
                with patch("sd_db.shadow_jira.urllib.request.OpenerDirector.open",
                           side_effect=urllib.error.URLError(inner)):
                    payload, reason = _http("https://example.invalid/rest/api/3/myself", {}, None)
                self.assertEqual((payload, reason),
                                 (None, f"the request to Jira failed: {type(inner).__name__}"))
        with patch("sd_db.shadow_jira.urllib.request.OpenerDirector.open",
                   side_effect=urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))):
            _, reason = _http("https://example.invalid/rest/api/3/myself", {}, None)
        self.assertIn("Connection refused", reason, "an ordinary OSError keeps its why")


if __name__ == "__main__":
    unittest.main()
