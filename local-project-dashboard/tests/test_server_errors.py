"""Every failure in a route answers; none closes the socket with no body (sd:3529).

Review 2026-10-12 A1 F2, F3: an exception the handler's catch lists did not
name (a `PermissionError` from a launcher, a `RuntimeError` from a screen) left
the browser a failed fetch with no body and the service log a traceback, and
a page route whose item vanished answered JSON. The last-resort handler maps
an `OSError` to 503 and anything else to 500, each naming the route, JSON on
`/api/` and the error page elsewhere. A socket that fails once the answer has
started gets one log line, never a second answer.
"""

from __future__ import annotations

import http.client
import io
import json
import socketserver
from unittest.mock import patch

from sd_db import repos, workflow
from sd_dashboard import server
from test_workflow_actions import BrowserSession


class LastResort(BrowserSession):
    def test_a_page_route_that_raises_answers_a_500_page_naming_the_route(self):
        with patch.object(server, "route", side_effect=RuntimeError("the screen broke")), \
                patch("sys.stderr", new_callable=io.StringIO):
            status, headers, body = self.request("/tasks")
        self.assertEqual(status, 500)
        self.assertTrue(headers["Content-Type"].startswith("text/html"), headers["Content-Type"])
        self.assertIn("/tasks", body)
        self.assertIn("RuntimeError: the screen broke", body)

    def test_an_api_read_that_raises_answers_500_json_naming_the_route(self):
        with patch.object(repos, "registered", side_effect=RuntimeError("the read broke")), \
                patch("sys.stderr", new_callable=io.StringIO) as log:
            status, headers, body = self.request("/api/capture-repos", headers={"Cookie": self.cookie})
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "GET /api/capture-repos failed: RuntimeError: the read broke"})
        # The unexpected failure keeps its traceback in the service log, for whoever fixes it.
        self.assertIn("Traceback", log.getvalue())

    def test_a_vanished_item_on_a_page_route_is_the_404_page_not_json(self):
        with patch.object(server, "route", side_effect=workflow.MissingItem("no item 9")):
            status, headers, body = self.request("/tasks")
        self.assertEqual(status, 404)
        self.assertTrue(headers["Content-Type"].startswith("text/html"), headers["Content-Type"])
        self.assertIn("The related item no longer exists", body)

    def test_an_action_that_cannot_reach_a_file_answers_503_json(self):
        refused = PermissionError(13, "Permission denied", "/example/herdr.sh")
        with patch.object(workflow, "capture_task", side_effect=refused), \
                patch("sys.stderr", new_callable=io.StringIO) as log:
            status, _, answer = self.post("/api/items", {"title": "Book the review"})
        self.assertEqual(status, 503)
        self.assertIn("Permission denied", answer["error"])
        self.assertNotIn("Traceback", log.getvalue())

    def test_an_action_that_raises_answers_500_json_naming_the_route(self):
        with patch.object(workflow, "capture_task", side_effect=RuntimeError("the writer broke")), \
                patch("sys.stderr", new_callable=io.StringIO):
            status, _, answer = self.post("/api/items", {"title": "Book the review"})
        self.assertEqual(status, 500)
        self.assertEqual(answer, {"error": "POST /api/items failed: RuntimeError: the writer broke"})

    def test_a_socket_that_fails_mid_answer_is_one_line_and_no_second_answer(self):
        aborted = ConnectionAbortedError(53, "Software caused connection abort")
        host, port = self.listening.server_address[:2]
        with patch.object(socketserver._SocketWriter, "write", side_effect=aborted), \
                patch("sys.stderr", new_callable=io.StringIO) as log:
            connection = http.client.HTTPConnection(host, port, timeout=5)
            self.addCleanup(connection.close)
            connection.request("GET", "/tasks")
            with self.assertRaises((http.client.RemoteDisconnected, ConnectionError)):
                connection.getresponse()
        self.assertNotIn("Traceback", log.getvalue())
        self.assertEqual(log.getvalue().count("left before the response"), 1, log.getvalue())
        # The listener still serves the next request.
        self.assertEqual(self.request("/tasks")[0], 200)
