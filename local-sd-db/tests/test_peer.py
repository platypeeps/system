"""Step 7 of the second-machine plan: peer identity on the tailnet listener.

`docs/work/2026-09-22-run-the-framework-from-a-second-machine/implement.md`.
Without `--loopback`, `sd-db.sh serve` binds this node's Tailscale address,
carries no token, and admits a session by its TCP peer through
`sd_db.tailnet`, the rules the dashboard's direct listener reads too.
Criterion 6: a tagged node, another login and the hub's own address are
refused, the serve log records it, and no SQL runs.

`tailscale` is the harness stub on PATH; no test reaches the real daemon.
The sessions run the real handler over a socket pair whose far end carries
a tailnet address, since no test can own one.
"""

from __future__ import annotations

import io
import ipaddress
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sd_db import database, initialise, remote, serve, tailnet
from sd_db.testing import Stubs

HERE = Path(__file__).resolve().parents[1]
SD_DB = HERE / "sd-db.sh"

OPERATOR = "operator@example.test"
HUB = "100.64.0.10"
SATELLITE = "100.64.0.20"


def record(address: str, login: str = OPERATOR, **node) -> dict:
    """What `tailscale whois --json` prints for a node at `address`."""
    return {"Node": {"Addresses": [f"{address}/32"], "Tags": [], **node},
            "UserProfile": {"LoginName": login}}


class StubCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stubs = Stubs(self.root / "stubs", names=("tailscale",))
        patcher = mock.patch.dict(os.environ, self.stubs.environment())
        patcher.start()
        self.addCleanup(patcher.stop)

    def whois(self, **records) -> None:
        self.stubs.state("tailscale", {"ips": [HUB], "login": OPERATOR, "whois": records})

    def asked(self) -> list[list[str]]:
        return [call.argv for call in self.stubs.calls("tailscale") if call.argv[:1] == ["whois"]]


class TheRules(StubCase):
    """`sd_db.tailnet`, against the stub."""

    def test_only_a_canonical_tailnet_ipv4_peer_with_a_port_counts(self):
        self.assertEqual(tailnet.peer_address((SATELLITE, 45678)), (ipaddress.IPv4Address(SATELLITE), 45678))
        for peer in (("127.0.0.1", 45678), ("192.0.2.5", 45678), ("::1", 45678), (SATELLITE, 0),
                     (SATELLITE, True), (SATELLITE, "45678"), [SATELLITE, 45678], ("bad", 1), None):
            with self.subTest(peer=peer):
                self.assertIsNone(tailnet.peer_address(peer))
                self.assertIsNone(tailnet.login(peer))
        self.assertEqual(self.asked(), [], "a non-tailnet peer reached the daemon")

    def test_whois_names_the_operator_for_the_exact_tcp_peer(self):
        self.whois(**{SATELLITE: record(SATELLITE)})
        self.assertEqual(tailnet.login((SATELLITE, 45678)), OPERATOR)
        self.assertEqual(self.asked(), [["whois", "--json", "--proto=tcp", f"{SATELLITE}:45678"]])

    def test_a_tagged_expired_or_mismatched_node_has_no_login(self):
        cases = {
            "tagged": (record(SATELLITE, Tags=["tag:server"]), "tagged node (tag:server)"),
            "expired": (record(SATELLITE, Expired=True), "key expired"),
            "mismatched": (record("100.64.0.21"), f"does not own {SATELLITE}"),
            "nameless": (record(SATELLITE, login=" "), "no owner login"),
        }
        for name, (answer, reason) in cases.items():
            with self.subTest(name):
                self.whois(**{SATELLITE: answer})
                identity = tailnet.whois((SATELLITE, 45678))
                self.assertIn(reason, identity.refusal)
                self.assertIsNone(tailnet.login((SATELLITE, 45678)))

    def test_an_unknown_or_malformed_answer_raises_and_never_admits(self):
        for answer in ({}, {"Node": [], "UserProfile": {}}, {"Node": {"Addresses": ["bad"]}, "UserProfile": {}}):
            with self.subTest(answer=answer):
                self.whois(**{SATELLITE: answer})
                with self.assertRaises(tailnet.TailnetError):
                    tailnet.login((SATELLITE, 45678))
        self.whois()
        with self.assertRaisesRegex(tailnet.TailnetError, "peer not found"):
            tailnet.login((SATELLITE, 45678))

    def test_this_node_names_its_addresses_and_owner(self):
        self.stubs.state("tailscale", {"ips": [HUB, "fd7a:115c:a1e0::a"], "login": OPERATOR})
        node = tailnet.this_node()
        self.assertEqual(node.login, OPERATOR)
        self.assertEqual(node.address, ipaddress.IPv4Address(HUB))
        self.assertEqual(node.addresses, {ipaddress.ip_address(HUB), ipaddress.ip_address("fd7a:115c:a1e0::a")})

    def test_a_node_with_no_operator_refuses_to_serve(self):
        for state, reason in (({"tags": ["tag:hub"]}, "tagged"), ({"backend": "Stopped"}, "stopped"),
                              ({"ips": ["fd7a:115c:a1e0::a"]}, "no Tailscale IPv4")):
            with self.subTest(reason):
                self.stubs.state("tailscale", state)
                with self.assertRaisesRegex(tailnet.TailnetError, reason):
                    tailnet.this_node()


class TheListener(StubCase):
    """The tailnet listener's accept path, with the real session handler."""

    def setUp(self):
        super().setUp()
        self.database = self.root / "sd.db"
        initialise(self.database)
        local = database.connect(self.database)
        local.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        local.close()
        self.watch = database.connect(self.database, write=False)
        self.addCleanup(self.watch.close)
        self.stream = io.StringIO()
        # This node owns HUB. The socket binds loopback, the one address a
        # test can own; the sessions below never use it.
        self.node = tailnet.Node(frozenset({ipaddress.ip_address(HUB)}), OPERATOR, ipaddress.IPv4Address("127.0.0.1"))
        self.server = serve.Server(0, self.database, serve.Log(self.stream), "", self.root / "unused.token", self.node)
        self.addCleanup(self.server.server_close)

    def version(self) -> int:
        return self.watch.execute("PRAGMA data_version").fetchone()[0]

    def handler(self, peer) -> tuple[socket.socket, threading.Thread]:
        """Our end of a socket pair, and the session handler on the other end
        under `peer`. The hub's end closes when the handler ends, as the
        server's own `shutdown_request` closes it."""
        ours, theirs = socket.socketpair()
        ours.settimeout(30)

        def handle():
            try:
                serve.Session(theirs, peer, self.server)
            finally:
                theirs.close()

        thread = threading.Thread(target=handle, daemon=True)
        thread.start()
        return ours, thread

    def session(self, peer, *, path=None) -> list:
        """Open from `peer`, then insert a row; the answers, `None` once the hub hangs up."""
        ours, handler = self.handler(peer)
        answers = []
        try:
            for frame in (
                remote.request("open", path=path, write=True, create=False, busy_timeout=5000,
                               token="", **remote.handshake()),
                remote.request("execute", sql="INSERT INTO probe (name) VALUES ('wire')", params=None),
                remote.request("close"),
            ):
                try:
                    remote.send_frame(ours, frame)
                    answers.append(remote.read_frame(ours))
                except (EOFError, OSError):
                    answers.append(None)
                    break
        finally:
            ours.close()
            handler.join(30)
        self.assertFalse(handler.is_alive(), "the session handler did not end")
        return answers

    def assertRefused(self, peer, reason: str, *, path=None, whois: bool = True) -> None:
        before = self.version()
        answers = self.session(peer, path=path)
        self.assertFalse(answers[0]["ok"], answers)
        self.assertEqual(answers[0]["error"]["name"], "PeerRefused")
        self.assertIn(reason, answers[0]["error"]["text"])
        self.assertEqual(answers[1:], [None], "a refused peer got a second frame answered")
        # Criterion 6: no SQL ran, and the serve log says why.
        self.assertEqual(self.version(), before)
        self.assertEqual(self.watch.execute("SELECT count(*) FROM probe").fetchone()[0], 0)
        self.assertRegex(self.stream.getvalue(), rf"refused before open: refused the peer .*{re.escape(reason)}")
        if not whois:
            self.assertEqual(self.asked(), [], "refused only after asking the daemon")

    def test_the_operators_node_is_admitted_with_no_token(self):
        self.whois(**{SATELLITE: record(SATELLITE)})
        answers = self.session((SATELLITE, 45678))
        self.assertTrue(all(answer["ok"] for answer in answers), answers)
        self.assertEqual(self.watch.execute("SELECT count(*) FROM probe").fetchone()[0], 1)
        self.assertIn(f"admitted {OPERATOR} from {SATELLITE}:45678", self.stream.getvalue())

    def test_a_tagged_node_is_refused_and_no_sql_runs(self):
        self.whois(**{SATELLITE: record(SATELLITE, Tags=["tag:ci"])})
        self.assertRefused((SATELLITE, 45678), "tagged node")

    def test_another_login_is_refused_and_no_sql_runs(self):
        self.whois(**{SATELLITE: record(SATELLITE, login="other@example.test")})
        self.assertRefused((SATELLITE, 45678), "other@example.test is not this hub's operator")

    def test_the_hubs_own_address_is_refused_before_whois(self):
        """`whois` names the operator for any account on the hub; the address is the tell."""
        self.whois(**{HUB: record(HUB)})
        self.assertRefused((HUB, 45678), "this hub's own Tailscale address", whois=False)

    def test_an_expired_or_unknown_node_is_refused(self):
        self.whois(**{SATELLITE: record(SATELLITE, Expired=True)})
        self.assertRefused((SATELLITE, 45678), "key expired")
        self.whois()
        self.assertRefused((SATELLITE, 45679), "tailscale whois failed: peer not found")

    def test_a_peer_off_the_tailnet_is_refused_without_asking(self):
        self.assertRefused(("192.0.2.5", 45678), "not a Tailscale IPv4 address", whois=False)

    def test_a_tailnet_session_cannot_name_another_file(self):
        self.whois(**{SATELLITE: record(SATELLITE)})
        other = self.root / "other.db"
        initialise(other)
        self.assertRefused((SATELLITE, 45678), "names no path", path=str(other))

    def test_a_first_frame_too_large_for_an_open_is_dropped_unread(self):
        ours, handler = self.handler((SATELLITE, 45678))
        try:
            ours.sendall((serve.OPEN_LIMIT + 1).to_bytes(4, "big"))
            self.assertEqual(ours.recv(1), b"", "the hub waited for an oversized first frame")
        finally:
            ours.close()
            handler.join(30)
        self.assertIn(f"exceeds {serve.OPEN_LIMIT}", self.stream.getvalue())
        self.assertEqual(self.asked(), [])

    def test_the_listener_binds_the_nodes_address_and_writes_no_token(self):
        """A real socket: the peer is loopback, which the tailnet listener refuses."""
        listener = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        listener.start()
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address[:2]
        self.assertEqual(host, str(self.node.address))
        with self.assertRaisesRegex(remote.RemoteError, "not a Tailscale IPv4 address"):
            remote.connect(host, port, None, token="")
        self.assertFalse((self.root / "unused.token").exists())


class TheCommand(StubCase):
    """`sd-db.sh serve` without `--loopback`, against the stub."""

    def run_serve(self, *arguments) -> subprocess.CompletedProcess:
        home = self.root / "home"
        environment = {**os.environ, "PYTHONPATH": str(HERE), "PYTHON": sys.executable, "HOME": str(home),
                       "XDG_DATA_HOME": str(home / ".local" / "share")}
        self.database = self.root / "sd.db"
        if not self.database.exists():
            initialise(self.database)
        return subprocess.run(["sh", str(SD_DB), "serve", *arguments, "--database", str(self.database)],
                              capture_output=True, text=True, env=environment, cwd=HERE, timeout=60)

    def test_it_binds_this_nodes_tailscale_address(self):
        # Tailscale keeps 100.100.100.100 for its own resolver and never gives
        # it to a node, so no machine running this suite can bind it.
        self.stubs.state("tailscale", {"ips": ["100.100.100.100"], "login": OPERATOR})
        done = self.run_serve("--port", "0")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("cannot listen on 100.100.100.100:0", done.stderr)
        self.assertEqual([call.argv for call in self.stubs.calls("tailscale")], [["status", "--json"]])
        self.assertEqual(sorted(p.name for p in self.root.glob("sd.db.serve.*")), ["sd.db.serve.lock"])

    def test_a_node_with_no_operator_does_not_listen(self):
        for state, reason in (({"tags": ["tag:hub"]}, "tagged"), ({"backend": "Stopped"}, "stopped")):
            with self.subTest(reason):
                self.stubs.state("tailscale", state)
                done = self.run_serve("--port", "0")
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIn("cannot serve on the tailnet", done.stderr)
                self.assertIn(reason, done.stderr)
                self.assertNotIn("Traceback", done.stderr)


if __name__ == "__main__":
    unittest.main()
