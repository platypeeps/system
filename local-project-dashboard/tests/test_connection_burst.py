"""A burst of connections that arrive at once is queued, not reset (sd:2131).

Tailscale serve fetches a page's assets over one HTTP/2 connection and dials
one backend connection per asset at the same instant. socketserver's default
listen backlog is 5, and macOS resets a connect that finds the accept queue
full; Go's reverse proxy then answers 502, and the page loads without two or
three of its scripts. These tests open the burst before the server accepts
anything, so the queue alone must hold it, then read one response on each
socket. The direct IP listener gets its own case, since `build` makes it too.
"""

from __future__ import annotations

import socket
import threading
from unittest.mock import Mock

from sd_dashboard import server
from support import ScreenCase
from tests import test_client_disconnect

#: More connections than the stdlib backlog, fewer than any kernel's SOMAXCONN.
BURST = 32


class Burst(ScreenCase):
    def setUp(self) -> None:
        super().setUp()
        self.listening = self.build()
        self.addCleanup(self.listening.server_close)

    def build(self):
        return server.build(self.path, port=0, operations_backend=Mock(names=lambda: []))

    def address(self):
        return self.listening.server_address[:2]

    def serve(self):
        thread = threading.Thread(target=self.listening.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.listening.shutdown)

    def status_lines(self, path):
        """Connect `BURST` sockets before serving starts, then one GET on each; the first line of each answer."""
        host, port = self.address()
        clients = []
        for _ in range(BURST):
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.addCleanup(client.close)
            client.setblocking(False)
            client.connect_ex((host, port))
            clients.append(client)
        self.serve()
        lines = []
        for client in clients:
            client.settimeout(10)
            try:
                client.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n"
                               .encode("ascii"))
                lines.append(client.makefile("rb").readline().decode("latin-1").strip() or "EOF")
            except OSError as problem:
                lines.append(f"{type(problem).__name__}: {problem}")
        return lines

    def test_every_connection_in_a_burst_is_answered(self):
        lines = self.status_lines("/ui/shell.js")
        self.assertEqual(lines, ["HTTP/1.1 200 OK"] * BURST)


class DirectBurst(Burst):
    """The same on the optional direct IP listener."""

    direct_class = server.Listener
    build = test_client_disconnect.DirectClientLeaves.build

    def address(self):
        return self.listening.direct_server.server_address[:2]

    def test_every_connection_in_a_burst_is_answered(self):
        # A bare loopback GET is refused on the authenticated listener; the refusal is still an answer.
        lines = self.status_lines("/ui/shell.js")
        self.assertEqual(lines, ["HTTP/1.1 403 Forbidden"] * BURST)
