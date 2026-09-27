"""Real loopback HTTP proofs of the configured private proxy boundary.

Identity headers and Serve observations are synthetic. These tests establish
the server's policy, not physical iPad access or actual Tailscale forwarding.
"""

from copy import deepcopy
from dataclasses import replace
import http.client
import json
from pathlib import Path
import re
import threading
from unittest.mock import Mock, patch

from sd_db import workflow
from sd_dashboard import auth, runtime, server

from support import ScreenCase


class RemoteAccess(ScreenCase):
    def setUp(self):
        super().setUp()
        self.origin = "https://fixture.tail-example.ts.net:8443"
        self.authority = "fixture.tail-example.ts.net:8443"
        self.operator = "operator@example.test"
        self.config = {"origin": self.origin, "operator_login": self.operator, "port": 8767}
        self.serve = {"TCP": {"8443": {"HTTPS": True}, "443": {"HTTPS": True}},
            "Web": {self.authority: {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8767"}}},
                    "fixture.tail-example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8766"}}}},
            "AllowFunnel": {"fixture.tail-example.ts.net:443": True}}
        self.frontdoor = auth.validate_frontdoor(self.config, self.serve, 8767)
        self.checks = 0
        self.unavailable = False
        self.listening = server.build(self.path, port=0, frontdoor=self.frontdoor,
                                      frontdoor_check=self.check_frontdoor)
        self.port = self.listening.server_address[1]
        self.local = f"http://127.0.0.1:{self.port}"
        self.config["port"] = self.port
        self.serve["Web"][self.authority]["Handlers"]["/"]["Proxy"] = self.local
        thread = threading.Thread(target=self.listening.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.listening.server_close)
        self.addCleanup(self.listening.shutdown)

    def check_frontdoor(self):
        self.checks += 1
        if self.unavailable:
            raise runtime.RuntimeRefused("fixture Tailscale inspection unavailable")
        return auth.validate_frontdoor(self.config, self.serve, self.port) == self.frontdoor

    def request(self, path="/", *, headers=None, method="GET", payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            client.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers or [("Host", self.authority), ("Tailscale-User-Login", self.operator)]:
                client.putheader(name, value)
            if body is not None:
                client.putheader("Content-Type", "application/json")
                client.putheader("Content-Length", str(len(body)))
            client.endheaders(body)
            result = client.getresponse()
            return result.status, dict(result.getheaders()), result.read().decode()
        finally:
            client.close()

    def session(self, *, local=False):
        headers = [("Host", f"127.0.0.1:{self.port}")] if local else None
        status, received, body = self.request(headers=headers)
        self.assertEqual(status, 200)
        cookie = received["Set-Cookie"].split(";", 1)[0]
        csrf = re.search(r'name="sd-csrf" content="([a-f0-9]+)"', body).group(1)
        return cookie, csrf, received

    def write_headers(self, cookie, csrf, *, local=False):
        return [("Host", f"127.0.0.1:{self.port}" if local else self.authority),
                *([] if local else [("Tailscale-User-Login", self.operator)]),
                ("Origin", self.local if local else self.origin), ("Cookie", cookie),
                ("X-SD-CSRF", csrf), ("Sec-Fetch-Site", "same-origin")]

    def post(self, headers):
        return self.request("/api/items", method="POST", payload={"title": "Remote fixture task"}, headers=headers)

    def snapshot(self):
        return tuple(self.connection.iterdump())

    def test_authorized_remote_get_and_write_use_secure_bound_session(self):
        before_serve = deepcopy(self.serve)
        cookie, csrf, received = self.session()
        for attribute in ("Secure", "HttpOnly", "SameSite=Strict"):
            self.assertIn(attribute, received["Set-Cookie"])
        self.assertEqual(received["Content-Security-Policy"], server.CSP)
        status, _, body = self.post(self.write_headers(cookie, csrf))
        self.assertEqual(status, 201)
        item = json.loads(body)["item"]
        self.assertEqual(item["title"], "Remote fixture task")
        self.assertIn("Remote fixture task", self.request(f"/item/{item['id']}")[2])
        self.assertEqual(self.checks, 3)
        self.assertEqual(self.serve, before_serve)

    def test_missing_wrong_or_forwarded_identity_cannot_read_even_health(self):
        before = self.snapshot()
        for path in ("/", "/health", "/static/dashboard.js", "/api/items/1/capture-context"):
            for headers in (
                [("Host", self.authority)],
                [("Host", self.authority), ("Tailscale-User-Login", "other@example.test")],
                [("Host", self.authority), ("X-Forwarded-User", self.operator)],
                [("Host", "wrong.tail-example.ts.net:8443"), ("Tailscale-User-Login", self.operator)],
            ):
                self.assertEqual(self.request(path, headers=headers)[0], 403)
        self.assertEqual(self.checks, 0)
        self.assertEqual(self.snapshot(), before)

    def test_capture_context_revalidates_private_access_and_does_not_write(self):
        state = workflow.capture_task(self.connection, title="Related item", who="operator")
        path = f"/api/items/{state['item']['id']}/capture-context"
        before = self.snapshot()
        status, headers, body = self.request(path)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["revision"], state["revision"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.checks, 1)
        self.unavailable = True
        self.assertEqual(self.request(path)[0], 403)
        self.assertEqual(self.checks, 2)
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_security_headers_are_refused_without_a_write(self):
        cookie, csrf, _ = self.session()
        valid = self.write_headers(cookie, csrf)
        before = self.snapshot()
        for name in ("Host", "Tailscale-User-Login", "Origin", "Cookie", "X-SD-CSRF", "Sec-Fetch-Site"):
            duplicate = next(pair for pair in valid if pair[0] == name)
            self.assertEqual(self.post(valid + [duplicate])[0], 403, name)
        self.assertEqual(self.snapshot(), before)

    def test_origin_identity_and_csrf_must_all_match_for_a_write(self):
        cookie, csrf, _ = self.session()
        valid = self.write_headers(cookie, csrf)
        before = self.snapshot()
        for name, value in (("Origin", "http://" + self.authority),
                            ("Origin", "https://fixture.tail-example.ts.net"),
                            ("Origin", "https://other.invalid"),
                            ("Tailscale-User-Login", "other@example.test"),
                            ("Cookie", ""), ("X-SD-CSRF", "0" * 64),
                            ("Sec-Fetch-Site", "cross-site")):
            changed = [(key, value if key == name else old) for key, old in valid]
            self.assertEqual(self.post(changed)[0], 403, (name, value))
        self.assertEqual(self.post([pair for pair in valid if pair[0] != "Origin"])[0], 403)
        self.assertEqual(self.snapshot(), before)

    def test_session_cannot_cross_remote_and_local_origins(self):
        local_cookie, local_csrf, received = self.session(local=True)
        self.assertNotIn("Secure", received["Set-Cookie"])
        remote_cookie, remote_csrf, _ = self.session()
        before = self.snapshot()
        self.assertEqual(self.post(self.write_headers(local_cookie, local_csrf))[0], 403)
        self.assertEqual(self.post(self.write_headers(remote_cookie, remote_csrf, local=True))[0], 403)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.post(self.write_headers(local_cookie, local_csrf, local=True))[0], 201)

    def test_expired_or_malformed_remote_session_is_refused_without_a_write(self):
        with patch("sd_dashboard.server.time.time", return_value=0):
            expired, csrf, _ = self.session()
        before = self.snapshot()
        self.assertEqual(self.post(self.write_headers(expired, csrf))[0], 403)
        huge_stamp = "sd_session=" + "9" * 500 + "." + "a" * 48 + "." + "f" * 64
        self.assertEqual(self.post(self.write_headers(huge_stamp, csrf))[0], 403)
        self.assertEqual(self.snapshot(), before)

    def test_session_is_bound_to_operator_even_when_the_server_secret_is_unchanged(self):
        cookie, csrf, _ = self.session()
        # Test-only boundary replacement isolates principal binding from the
        # production callback's additional refusal of configuration drift.
        self.operator = "replacement@example.test"
        self.config["operator_login"] = self.operator
        self.frontdoor = replace(self.frontdoor, operator_login=self.operator)
        self.listening.RequestHandlerClass.frontdoor = self.frontdoor
        before = self.snapshot()
        self.assertEqual(self.post(self.write_headers(cookie, csrf))[0], 403)
        self.assertEqual(self.snapshot(), before)
        cookie, csrf, _ = self.session()
        self.assertEqual(self.post(self.write_headers(cookie, csrf))[0], 201)

    def test_each_remote_get_head_static_health_and_post_revalidates_private_serve(self):
        cookie, csrf, _ = self.session()
        for path, method in (("/", "GET"), ("/health", "GET"), ("/", "HEAD"), ("/static/dashboard.css", "GET")):
            previous = self.checks
            self.assertEqual(self.request(path, method=method)[0], 200)
            self.assertEqual(self.checks, previous + 1)
        previous = self.checks
        self.assertEqual(self.post(self.write_headers(cookie, csrf))[0], 201)
        self.assertEqual(self.checks, previous + 1)

    def test_public_proxy_or_unavailable_observation_fails_closed_and_local_still_works(self):
        cookie, csrf, _ = self.session()
        clean = deepcopy(self.serve)
        before = self.snapshot()
        for drift in ("funnel", "proxy", "handlers", "unavailable", "operator"):
            self.serve = deepcopy(clean)
            self.unavailable = drift == "unavailable"
            self.config["operator_login"] = "changed@example.test" if drift == "operator" else self.operator
            if drift == "funnel":
                self.serve["AllowFunnel"][self.authority] = True
            elif drift == "proxy":
                self.serve["Web"][self.authority]["Handlers"]["/"]["Proxy"] = "http://127.0.0.1:9999"
            elif drift == "handlers":
                self.serve["Web"][self.authority]["Handlers"]["/extra"] = {"Proxy": self.local}
            self.assertEqual(self.request("/health")[0], 403, drift)
            self.assertEqual(self.post(self.write_headers(cookie, csrf))[0], 403, drift)
            checks = self.checks
            self.assertEqual(self.request(headers=[("Host", f"127.0.0.1:{self.port}")])[0], 200)
            self.assertEqual(self.checks, checks)
        self.assertEqual(self.snapshot(), before)

    def test_local_identity_headers_cannot_downgrade_remote_authentication(self):
        cookie, csrf, _ = self.session(local=True)
        before = self.snapshot()
        for name in ("Tailscale-User-Login", "Forwarded", "X-Forwarded-Host", "X-Auth-User"):
            headers = self.write_headers(cookie, csrf, local=True) + [(name, self.operator)]
            self.assertEqual(self.post(headers)[0], 403)
            self.assertEqual(self.request("/health", headers=headers)[0], 403)
        self.assertEqual(self.snapshot(), before)

    def test_default_local_build_does_not_enable_a_remote_host_from_headers(self):
        self.listening.RequestHandlerClass.frontdoor = None
        self.assertEqual(self.request()[0], 403)
        self.assertEqual(self.checks, 0)
        with self.assertRaises(ValueError):
            server.build(self.path, port=0, frontdoor=self.frontdoor)

    def test_production_callback_reloads_the_explicit_boundary_on_every_invocation(self):
        config_path = Path(self.tmp.name) / "dashboard.json"
        config_path.write_text(json.dumps(dict(self.config, port=8767)))
        listening = Mock(server_address=("127.0.0.1", 8767))
        with patch.object(runtime, "installed_library"), patch.object(runtime, "load_frontdoor", return_value=self.frontdoor) as load, patch.object(server, "build", return_value=listening) as build:
            self.assertEqual(server.main(["--config", str(config_path)]), 0)
            check = build.call_args.kwargs["frontdoor_check"]
            self.assertTrue(check())
            self.assertTrue(check())
            self.assertEqual(load.call_count, 3)
            load.return_value = replace(self.frontdoor, operator_login="changed@example.test")
            self.assertFalse(check())
            load.side_effect = runtime.RuntimeRefused("inspection unavailable")
            with self.assertRaises(runtime.RuntimeRefused):
                check()  # Dashboard._context catches this and rejects the request.
