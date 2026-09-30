"""A client that leaves before the response is one log line, not a traceback.

The owner's err log held 109 `Exception occurred during processing of request`
blocks ending in `BrokenPipeError` or `ConnectionResetError` (sd:970): a browser
tab navigating away, a curl with `--max-time`, a health probe. The write into
the closed socket is ordinary; the stdlib's `handle_error` prints a traceback
for it. These tests hold the response until the client has reset the
connection, so the write fails every time rather than when the scheduler
happens to lose the race, then read the socket and stderr. The guard sits on
the handler both listeners share, so the direct IP socket gets its own case.
"""

from __future__ import annotations

import http.client
import io
import socket
import struct
import threading
import time
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

from sd_dashboard import auth, server
from support import ScreenCase

LEFT = "left before the response"


class ClientLeaves(ScreenCase):
    def setUp(self) -> None:
        super().setUp()
        self.item("a live item")
        self.listening = self.build()
        thread = threading.Thread(target=self.listening.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.listening.server_close)
        self.addCleanup(self.listening.shutdown)
        self.host, self.port = self.listening.server_address[:2]

    def build(self):
        return server.build(self.path, port=0, operations_backend=Mock(names=lambda: []))

    def leave_then_return(self, host, port):
        """A reset mid-response on `host:port`, then a normal request to it; the captured stderr."""
        reset = threading.Event()
        original = server.Dashboard._send

        def held_send(handler, *args, **kwargs):
            # The handler writes only once the client is gone.
            reset.wait(5)
            return original(handler, *args, **kwargs)

        captured = io.StringIO()
        with patch.object(server.Dashboard, "_send", held_send), patch("sys.stderr", captured):
            client = socket.create_connection((host, port), timeout=5)
            # Linger 0: the close is a reset, and the server's write fails at once.
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.sendall(f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode("ascii"))
            client.close()
            time.sleep(0.05)
            reset.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and LEFT not in captured.getvalue() and "Traceback" not in captured.getvalue():
                time.sleep(0.01)
            with patch.object(server.Dashboard, "_send", original):
                connection = http.client.HTTPConnection(host, port, timeout=5)
                self.addCleanup(connection.close)
                connection.request("GET", "/")
                answer = connection.getresponse()
                answer.read()
        return answer.status, captured.getvalue()

    def assert_one_line(self, log, host):
        self.assertNotIn("Traceback", log, log)
        self.assertIn(f"client {host}:", log, log)
        self.assertEqual(log.count(LEFT), 1, log)

    def test_a_disconnect_is_one_line_and_the_next_request_is_served(self):
        status, log = self.leave_then_return(self.host, self.port)
        self.assertEqual(status, 200)
        self.assert_one_line(log, self.host)


class DirectClientLeaves(ClientLeaves):
    """The same on the optional direct IP listener, which `build` makes from `server.Listener`."""

    #: What the fixture binds in place of the direct listener; the burst test needs the real class.
    direct_class = ThreadingHTTPServer

    def build(self):
        config = {"origin": "https://fixture.tail-example.ts.net:8443", "operator_login": "operator@example.test",
                  "ip_origin": "http://100.64.0.10:8768", "port": 8767}
        serve = {"TCP": {"8443": {"HTTPS": True}},
                 "Web": {"fixture.tail-example.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8767"}}}},
                 "AllowFunnel": {}}
        frontdoor = auth.validate_frontdoor(config, serve, 8767)
        # The fixture IP is not on this host; bind the direct socket to loopback instead.
        with patch.object(server, "Listener",
                          side_effect=lambda address, handler: self.direct_class(("127.0.0.1", 0), handler)):
            return server.build(self.path, port=0, frontdoor=frontdoor, frontdoor_check=lambda: True,
                                peer_lookup=lambda peer: "operator@example.test",
                                operations_backend=Mock(names=lambda: []))

    def test_a_disconnect_is_one_line_and_the_next_request_is_served(self):
        host, port = self.listening.direct_server.server_address[:2]
        status, log = self.leave_then_return(host, port)
        # A bare loopback GET is refused on the authenticated listener; the refusal is still a response.
        self.assertEqual(status, 403, log)
        self.assert_one_line(log, host)
