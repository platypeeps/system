"""Direct-access policy over loopback fixtures; no Tailscale listener is opened.

The child server substitutes the socket peer address and binds an ephemeral
loopback port. Pure auth tests separately reject non-Tailscale peer addresses.
"""

from copy import deepcopy
import http.client
from http.server import ThreadingHTTPServer
import json
import re
import threading
from unittest.mock import Mock, patch


from sd_dashboard import auth, runtime, server

from support import ScreenCase
from tests import test_remote_access


class PeerFixtureServer(ThreadingHTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        return connection, ("100.64.0.20", address[1])


class DirectRemoteAccess(test_remote_access.RemoteAccess):
    """Retain every HTTPS boundary test while the additional listener runs."""

    def setUp(self):
        ScreenCase.setUp(self)
        self.origin = "https://fixture.tail-example.ts.net:8443"
        self.authority = "fixture.tail-example.ts.net:8443"
        self.operator = "operator@example.test"
        self.ip_origin = "http://100.64.0.10:8768"
        self.config = {"origin": self.origin, "operator_login": self.operator,
                       "ip_origin": self.ip_origin, "port": 8767}
        self.serve = {"TCP": {"8443": {"HTTPS": True}, "443": {"HTTPS": True}},
            "Web": {self.authority: {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8767"}}},
                    "fixture.tail-example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8766"}}}},
            "AllowFunnel": {"fixture.tail-example.ts.net:443": True}}
        self.frontdoor = auth.validate_frontdoor(self.config, self.serve, 8767)
        self.checks = 0
        self.unavailable = False
        self.lookup_unavailable = False
        self.lookup_login = self.operator
        self.peer_calls = []
        with patch.object(server, "Listener", side_effect=self.child_server):
            self.listening = server.build(self.path, port=0, frontdoor=self.frontdoor,
                                          frontdoor_check=self.check_frontdoor,
                                          peer_lookup=self.peer_lookup)
        self.port = self.listening.server_address[1]
        self.local = f"http://127.0.0.1:{self.port}"
        self.config["port"] = self.port
        self.serve["Web"][self.authority]["Handlers"]["/"]["Proxy"] = self.local
        self.thread = threading.Thread(target=self.listening.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.listening.server_close)
        self.addCleanup(self.listening.shutdown)

    def child_server(self, address, handler, **options):
        self.assertEqual(address, ("100.64.0.10", 8768))
        return PeerFixtureServer(("127.0.0.1", 0), handler, **options)

    def peer_lookup(self, peer):
        self.peer_calls.append(peer)
        if self.lookup_unavailable:
            raise runtime.RuntimeRefused("fixture Tailscale identity unavailable")
        return self.lookup_login

    def direct_request(self, path="/", *, headers=None, method="GET", payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        port = self.listening.direct_server.server_address[1]
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            client.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers or [("Host", self.frontdoor.direct.authority)]:
                client.putheader(name, value)
            if body is not None:
                client.putheader("Content-Type", "application/json")
                client.putheader("Content-Length", str(len(body)))
            client.endheaders(body)
            result = client.getresponse()
            return result.status, dict(result.getheaders()), result.read().decode()
        finally:
            client.close()

    def direct_session(self):
        status, received, body = self.direct_request()
        self.assertEqual(status, 200)
        cookie = received["Set-Cookie"].split(";", 1)[0]
        csrf = re.search(r'name="sd-csrf" content="([a-f0-9]+)"', body).group(1)
        return cookie, csrf, received

    def direct_headers(self, cookie, csrf):
        return [("Host", self.frontdoor.direct.authority), ("Origin", self.ip_origin),
                ("Cookie", cookie), ("X-SD-CSRF", csrf), ("Sec-Fetch-Site", "same-origin")]

    def direct_post(self, headers):
        return self.direct_request("/api/items", method="POST", payload={"title": "IP fixture task"},
                                   headers=headers)

    def test_ip_session_preserves_guards_and_https_stays_available(self):
        before_serve = deepcopy(self.serve)
        cookie, csrf, received = self.direct_session()
        self.assertNotIn("Secure", received["Set-Cookie"])
        for attribute in ("HttpOnly", "SameSite=Strict", "Max-Age=3600"):
            self.assertIn(attribute, received["Set-Cookie"])
        self.assertEqual(received["Content-Security-Policy"], server.CSP)
        self.assertEqual(self.direct_post(self.direct_headers(cookie, csrf))[0], 201)
        self.assertEqual(len(self.peer_calls), 2)
        self.assertTrue(all(peer[0] == "100.64.0.20" and type(peer[1]) is int for peer in self.peer_calls))
        _, _, secure_received = self.session()
        self.assertIn("Secure", secure_received["Set-Cookie"])
        self.assertEqual(self.serve, before_serve)

    def test_ip_identity_and_runtime_are_revalidated_for_every_route(self):
        before = self.snapshot()
        for failure in ("identity", "lookup", "runtime"):
            self.lookup_login = "other@example.test" if failure == "identity" else self.operator
            self.lookup_unavailable = failure == "lookup"
            self.unavailable = failure == "runtime"
            for path in ("/", "/health", "/static/dashboard.js", "/api/items/1/capture-context"):
                self.assertEqual(self.direct_request(path)[0], 403, (failure, path))
        self.assertEqual(self.snapshot(), before)

    def test_ip_listener_refuses_forged_proxy_identity_before_lookup(self):
        before = self.snapshot()
        for name in ("Tailscale-User-Login", "X-Forwarded-For", "X-Forwarded-User", "Forwarded", "X-Auth-User"):
            self.peer_calls.clear()
            headers = [("Host", self.frontdoor.direct.authority), (name, self.operator)]
            self.assertEqual(self.direct_request(headers=headers)[0], 403)
            self.assertEqual(self.peer_calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_ip_writes_require_exact_origin_session_csrf_and_unique_headers(self):
        cookie, csrf, _ = self.direct_session()
        valid = self.direct_headers(cookie, csrf)
        before = self.snapshot()
        for name, value in (("Origin", self.origin), ("Origin", self.local),
                            ("Origin", "http://100.64.0.10"), ("Host", self.authority),
                            ("Cookie", "sd_session=invalid"), ("X-SD-CSRF", "0" * 64),
                            ("Sec-Fetch-Site", "cross-site")):
            changed = [(key, value if key == name else old) for key, old in valid]
            self.assertEqual(self.direct_post(changed)[0], 403, name)
        for name in ("Host", "Origin", "Cookie", "X-SD-CSRF", "Sec-Fetch-Site"):
            duplicate = next(pair for pair in valid if pair[0] == name)
            self.assertEqual(self.direct_post(valid + [duplicate])[0], 403, name)
        for name in ("Origin", "Cookie", "X-SD-CSRF"):
            self.assertEqual(self.direct_post([p for p in valid if p[0] != name])[0], 403, name)
        self.assertEqual(self.snapshot(), before)

    def test_sessions_cannot_cross_all_three_origins(self):
        ip_cookie, ip_csrf, _ = self.direct_session()
        https_cookie, https_csrf, _ = self.session()
        local_cookie, local_csrf, _ = self.session(local=True)
        before = self.snapshot()
        for cookie, csrf in ((https_cookie, https_csrf), (local_cookie, local_csrf)):
            self.assertEqual(self.direct_post(self.direct_headers(cookie, csrf))[0], 403)
        self.assertEqual(self.post(self.write_headers(ip_cookie, ip_csrf))[0], 403)
        self.assertEqual(self.post(self.write_headers(ip_cookie, ip_csrf, local=True))[0], 403)
        self.assertEqual(self.snapshot(), before)

    def test_ip_session_expires_and_operator_change_invalidates_it(self):
        with patch("sd_dashboard.server.time.time", return_value=0):
            expired, csrf, _ = self.direct_session()
        before = self.snapshot()
        self.assertEqual(self.direct_post(self.direct_headers(expired, csrf))[0], 403)
        cookie, csrf, _ = self.direct_session()
        self.lookup_login = "new@example.test"
        self.assertEqual(self.direct_post(self.direct_headers(cookie, csrf))[0], 403)
        self.assertEqual(self.snapshot(), before)

    def test_loopback_cannot_select_direct_identity_with_an_ip_host(self):
        self.peer_calls.clear()
        self.assertEqual(self.request(headers=[("Host", self.frontdoor.direct.authority)])[0], 403)
        self.assertEqual(self.peer_calls, [])

    def test_health_requires_the_direct_listener_thread(self):
        status, _, body = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["ip_origin"], self.ip_origin)
        self.assertTrue(json.loads(body)["direct_listener_ok"])
        self.listening.direct_server.shutdown()
        self.listening.direct_thread.join(timeout=2)
        status, _, body = self.request("/health")
        self.assertEqual(status, 503)
        self.assertFalse(json.loads(body)["ok"])
        self.assertFalse(json.loads(body)["direct_listener_ok"])
        self.assertNotIn("ip_origin", json.loads(body))

    def test_direct_bind_failure_closes_the_primary_socket(self):
        created = []
        original = server.DashboardServer
        def primary(*args, **kwargs):
            instance = original(*args, **kwargs)
            created.append(instance)
            return instance
        with patch.object(server, "DashboardServer", side_effect=primary), \
                patch.object(server, "Listener", side_effect=OSError("occupied")):
            with self.assertRaisesRegex(OSError, "occupied"):
                server.build(self.path, port=0, frontdoor=self.frontdoor,
                             frontdoor_check=self.check_frontdoor, peer_lookup=self.peer_lookup)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].socket.fileno(), -1)

    def test_thread_start_failure_closes_both_sockets(self):
        with patch.object(server, "Listener", side_effect=self.child_server):
            candidate = server.build(self.path, port=0, frontdoor=self.frontdoor,
                                     frontdoor_check=self.check_frontdoor, peer_lookup=self.peer_lookup)
        self.addCleanup(candidate.server_close)
        with patch.object(server.threading.Thread, "start", side_effect=RuntimeError("thread unavailable")):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                candidate.serve_forever()
        self.assertEqual(candidate.socket.fileno(), -1)
        self.assertEqual(candidate.direct_server.socket.fileno(), -1)

    def test_shutdown_closes_direct_socket_and_thread(self):
        self.assertEqual(self.direct_request("/health")[0], 200)
        self.listening.shutdown()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.listening.direct_thread.is_alive())
        self.assertEqual(self.listening.direct_server.socket.fileno(), -1)
