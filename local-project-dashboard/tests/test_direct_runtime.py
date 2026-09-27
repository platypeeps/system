"""Direct IP access requires a current local Tailscale identity and owned address."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sd_dashboard import runtime


class DirectRuntime(unittest.TestCase):
    def setUp(self):
        self.config = {
            "origin": "https://fixture.example.ts.net:8443",
            "operator_login": "owner@example.test", "port": 8767,
            "ip_origin": "http://100.64.0.10:8768",
        }
        self.node = {
            "BackendState": "Running",
            "Self": {"DNSName": "fixture.example.ts.net.", "UserID": 42,
                     "TailscaleIPs": ["100.64.0.10"]},
            "User": {"42": {"LoginName": "owner@example.test"}},
        }
        self.serve = {
            "TCP": {"8443": {"HTTPS": True}, "443": {"HTTPS": True}},
            "Web": {"fixture.example.ts.net:8443": {
                "Handlers": {"/": {"Proxy": "http://127.0.0.1:8767"}}}},
            "AllowFunnel": {"fixture.example.ts.net:443": True},
        }

    def response(self, value):
        return subprocess.CompletedProcess([], 0, json.dumps(value), "")

    def test_direct_address_must_belong_to_current_running_node(self):
        with patch.object(runtime, "_run", return_value=self.response(self.node)):
            runtime._check_node(self.config)
            with self.assertRaisesRegex(runtime.RuntimeRefused, "IP is not this"):
                runtime._check_node(dict(self.config, ip_origin="http://100.64.0.11:8768"))
        for change in ({"BackendState": "Stopped"}, {"Self": dict(self.node["Self"], TailscaleIPs=[])}):
            with patch.object(runtime, "_run", return_value=self.response(dict(self.node, **change))):
                with self.assertRaises(runtime.RuntimeRefused):
                    runtime._check_node(self.config)

    def test_direct_port_cannot_overlap_any_serve_configuration(self):
        before = deepcopy(self.serve)
        runtime._check_direct_serve(self.config, self.serve)
        self.assertEqual(self.serve, before)
        for section, key, value in (
            ("TCP", "8768", {"TCPForward": "127.0.0.1:9999"}),
            ("Web", "other.example.ts.net:8768", {"Handlers": {}}),
            ("AllowFunnel", "other.example.ts.net:8768", True),
        ):
            status = deepcopy(self.serve)
            status[section][key] = value
            with self.assertRaisesRegex(runtime.RuntimeRefused, "already configured"):
                runtime._check_direct_serve(self.config, status)
        for section in ("TCP", "Web", "AllowFunnel"):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "invalid"):
                runtime._check_direct_serve(self.config, dict(self.serve, **{section: []}))

    def test_current_ip_and_serve_evidence_are_reloaded(self):
        def run(arguments, **options):
            return self.response(self.node if arguments == ["tailscale", "status", "--json"] else self.serve)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "dashboard.json"
            config.write_text(json.dumps(self.config))
            with patch.object(runtime, "_run", side_effect=run):
                frontdoor = runtime.load_frontdoor(config, 8767)
                self.assertEqual(frontdoor.direct.address, "100.64.0.10")
                self.serve["TCP"]["8768"] = {"HTTP": True}
                with self.assertRaisesRegex(runtime.RuntimeRefused, "already configured"):
                    runtime.load_frontdoor(config, 8767)

    def test_whois_uses_tcp_peer_and_rejects_untrusted_identity_records(self):
        peer = ("100.64.0.20", 45678)
        identity = {"Node": {"Addresses": ["100.64.0.20/32"], "Tags": []},
                    "UserProfile": {"LoginName": "owner@example.test"}}
        with patch.object(runtime, "_run", return_value=self.response(identity)) as run:
            self.assertEqual(runtime.peer_login(peer), "owner@example.test")
            run.assert_called_once_with(["tailscale", "whois", "--json", "--proto=tcp", "100.64.0.20:45678"])
        for node in ({"Tags": ["tag:server"]}, {"Expired": True}, {"Addresses": ["100.64.0.21/32"]}):
            changed = dict(identity, Node=dict(identity["Node"], **node))
            with patch.object(runtime, "_run", return_value=self.response(changed)):
                self.assertIsNone(runtime.peer_login(peer))
        for invalid in ({}, {"Node": [], "UserProfile": {}}, {"Node": {"Addresses": ["bad"]}, "UserProfile": {}}):
            with patch.object(runtime, "_run", return_value=self.response(invalid)):
                with self.assertRaises(runtime.RuntimeRefused):
                    runtime.peer_login(peer)
        with patch.object(runtime, "_run", side_effect=runtime.RuntimeRefused("offline")):
            with self.assertRaises(runtime.RuntimeRefused):
                runtime.peer_login(peer)

    def test_non_tailnet_and_malformed_peers_never_query_identity(self):
        with patch.object(runtime, "_run") as run:
            for peer in (("127.0.0.1", 45678), ("192.0.2.2", 45678), ("8.8.8.8", 45678),
                         ("100.64.0.20", True), ("100.64.0.20", 0), ("bad", 45678), None):
                self.assertIsNone(runtime.peer_login(peer), peer)
            run.assert_not_called()
