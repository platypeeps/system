"""Pure boundary checks; remote request handling is not enabled by these tests."""

from copy import deepcopy
from email.message import Message
import unittest

from sd_dashboard.auth import access_context, direct_context, parse_direct_origin, validate_frontdoor


class AuthenticationPolicy(unittest.TestCase):
    def setUp(self):
        self.config = {"origin": "https://fixture.tail-example.ts.net:8443", "operator_login": "operator@example.test", "port": 8767}
        self.authority = "fixture.tail-example.ts.net:8443"
        self.status = {"TCP": {"8443": {"HTTPS": True}, "443": {"HTTPS": True}},
            "Web": {self.authority: {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8767"}}},
                    "fixture.tail-example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8766"}}}},
            "AllowFunnel": {"fixture.tail-example.ts.net:443": True}}
        self.frontdoor = validate_frontdoor(self.config, self.status, 8767)

    def context(self, headers, peer="127.0.0.1", frontdoor=True):
        message = Message()
        for name, value in headers:
            message[name] = value
        return access_context(message, peer, 8767, self.frontdoor if frontdoor else None)

    def test_private_origin_checks_do_not_modify_unrelated_public_route(self):
        before = deepcopy(self.status)
        self.assertEqual(self.frontdoor.origin, self.config["origin"])
        self.assertEqual(self.status, before)
        self.assertIsNone(validate_frontdoor({}, {}, 8767))

    def test_invalid_public_or_misdirected_configuration_refuses(self):
        for mutate in (
            lambda value: value["AllowFunnel"].update({self.authority: True}),
            lambda value: value["Web"][self.authority]["Handlers"]["/"].update(Proxy="http://127.0.0.1:8766"),
            lambda value: value["Web"][self.authority]["Handlers"].update({"/other": {"Proxy": "http://127.0.0.1:8767"}}),
            lambda value: value.update(TCP=[]),
            lambda value: value["Web"].update({self.authority: []}),
        ):
            state = deepcopy(self.status)
            mutate(state)
            with self.assertRaises(ValueError):
                validate_frontdoor(self.config, state, 8767)
        for origin in ("http://fixture.tail-example.ts.net:8443", "https://other.invalid:8443",
                       "https://fixture.tail-example.ts.net:0", self.config["origin"] + "/extra"):
            with self.assertRaises(ValueError):
                validate_frontdoor(dict(self.config, origin=origin), self.status, 8767)

    def test_exact_operator_and_authority_required_with_no_forwarded_fallback(self):
        valid = [("Host", self.authority), ("Tailscale-User-Login", self.config["operator_login"])]
        self.assertTrue(self.context(valid).secure)
        self.assertTrue(self.context(valid).remote)
        for headers in (valid[:1], valid + [("Tailscale-User-Login", "other@example.test")],
                        [("Host", self.authority), ("X-Forwarded-User", self.config["operator_login"])],
                        [("Host", self.authority), ("Tailscale-User-Login", "other@example.test")],
                        valid + [("Host", "127.0.0.1:8767")]):
            self.assertIsNone(self.context(headers))
        self.assertIsNone(self.context(valid, peer="100.64.0.10"))
        self.assertIsNone(self.context(valid, frontdoor=False))

    def test_local_scope_rejects_forwarded_identity_and_remote_headers(self):
        local = [("Host", "127.0.0.1:8767")]
        context = self.context(local)
        self.assertFalse(context.secure)
        self.assertFalse(context.remote)
        self.assertEqual(context.scope, "http://127.0.0.1:8767\nlocal")
        for header in ("Tailscale-User-Login", "Forwarded", "X-Forwarded-Host", "X-Auth-User"):
            self.assertIsNone(self.context(local + [(header, self.config["operator_login"])]))
        self.assertNotEqual(context.scope, self.context([
            ("Host", self.authority), ("Tailscale-User-Login", self.config["operator_login"])]).scope)

    def test_direct_origin_is_explicit_canonical_and_keeps_https_authority(self):
        self.assertIsNone(parse_direct_origin(self.config))
        config = dict(self.config, ip_origin="http://100.64.0.10:8768/")
        door = validate_frontdoor(config, self.status, 8767)
        self.assertEqual(door.origin, self.frontdoor.origin)
        self.assertEqual(door.direct.origin, "http://100.64.0.10:8768")
        self.assertEqual((door.direct.address, door.direct.port), ("100.64.0.10", 8768))
        for value in (None, "", "http://100.64.0.10", "https://100.64.0.10:8768",
                      "http://100.64.0.10:8443", "http://100.64.0.10:08768",
                      "http://100.64.0.010:8768", "http://127.0.0.1:8768",
                      "http://192.0.2.2:8768", "http://100.128.0.1:8768",
                      "http://[fd7a:115c:a1e0::1]:8768", "http://fixture.ts.net:8768",
                      "http://user@100.64.0.10:8768", "http://100.64.0.10:8768/path",
                      "http://100.64.0.10:8768?x=1", "http://100.64.0.10:8768#x",
                      " http://100.64.0.10:8768", "HTTP://100.64.0.10:8768"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_direct_origin(dict(self.config, ip_origin=value))
        for changes in ({"origin": None}, {"operator_login": None}, {"port": 8768},
                        {"origin": "http://fixture.ts.net:8443"},
                        {"origin": "https://fixture.ts.net:0"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_direct_origin(dict(config, **changes))

    def test_direct_access_authenticates_socket_peer_without_forwarded_headers(self):
        door = validate_frontdoor(dict(self.config, ip_origin="http://100.64.0.10:8768"), self.status, 8767)
        message = Message()
        message["Host"] = door.direct.authority
        calls = []
        def lookup(peer):
            calls.append(peer)
            return self.config["operator_login"]
        peer = ("100.64.0.20", 51900)
        context = direct_context(message, peer, door, lookup)
        self.assertEqual(calls, [peer])
        self.assertTrue(context.remote)
        self.assertFalse(context.secure)
        self.assertEqual(context.scope, "http://100.64.0.10:8768\noperator@example.test")
        self.assertIsNone(direct_context(message, peer, door, lambda peer: "other@example.test"))
        self.assertIsNone(direct_context(message, peer, door, lambda peer: None))
        self.assertIsNone(direct_context(message, peer, self.frontdoor, lookup))
        for invalid in (("127.0.0.1", 51900), ("192.0.2.2", 51900), ("100.128.0.1", 51900),
                        ("fd7a:115c:a1e0::1", 51900), ("100.64.0.20", 0), ("100.64.0.20", True),
                        ("100.64.0.20", "51900"), ("100.64.0.20",), "100.64.0.20:51900"):
            calls.clear()
            self.assertIsNone(direct_context(message, invalid, door, lookup))
            self.assertEqual(calls, [])
        for name in ("Host", "Tailscale-User-Login", "Forwarded", "X-Forwarded-For",
                     "X-Auth-User", "X-Real-IP", "Remote-User"):
            forged = Message()
            forged["Host"] = door.direct.authority
            forged[name] = door.direct.authority if name == "Host" else self.config["operator_login"]
            calls.clear()
            self.assertIsNone(direct_context(forged, peer, door, lookup))
            self.assertEqual(calls, [])
        for host in ("127.0.0.1:8767", self.authority, "100.64.0.10", "100.64.0.10:8769"):
            forged = Message()
            forged["Host"] = host
            self.assertIsNone(direct_context(forged, peer, door, lookup))
