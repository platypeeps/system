"""The email channel against a stub workspace-mcp that fails on purpose.

workspace-mcp sometimes accepts `initialize` and then never answers the
`notifications/initialized` call that follows (two lost mails on 2026-09-27,
eight `failed channels: email` lines in the cron logs before that). notify.sh
opens a fresh session and tries again when the session setup fails, and never
repeats the send itself, which might already have gone out.

Real curl against a local HTTP stub; the stub answers 500 where the server
hung, so the tests are fast.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NOTIFY = Path(__file__).resolve().parent.parent / "notify.sh"


class Stub:
    """Counts each MCP method and fails the first `fail_initialized` confirms."""

    def __init__(self, fail_initialized=0, fail_send=False):
        self.fail_initialized = fail_initialized
        self.fail_send = fail_send
        self.calls = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method = body.get("method")
                name = body.get("params", {}).get("name", "")
                stub.calls.append(name or method)
                if method == "notifications/initialized" and stub.fail_initialized:
                    stub.fail_initialized -= 1
                    self.send_response(500)
                    self.end_headers()
                    return
                if name == "send_gmail_message" and stub.fail_send:
                    self.send_response(500)
                    self.end_headers()
                    return
                self.send_response(200)
                if method == "initialize":
                    self.send_header("mcp-session-id", f"s{len(stub.calls)}")
                self.end_headers()
                if method == "tools/call":
                    self.wfile.write(b'{"result":{"isError":false,"content":'
                                     b'[{"text":"Email sent! Message ID: abc123"}]}}')

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class EmailRetryTest(unittest.TestCase):
    def send(self, stub, recipient="operator@example.test"):
        env = {
            "HOME": os.environ.get("HOME", "/tmp"),
            "PATH": os.environ["PATH"],
            "JEV_NOTIFY": "0",
            "EMAIL_FROM": "sender@example.com",
            "WORKSPACE_MCP_URL": stub.url,
            "NOTIFY_MCP_RETRY_WAIT": "0",
        }
        # An empty config directory, so a real <config>/notify/.env cannot
        # supply the recipient (or anything else) behind the test's back.
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        env["SYSTEM_TOOLS_CONFIG"] = str(folder / "config")
        script = NOTIFY
        if recipient:
            env["NOTIFY_EMAIL_TO"] = recipient
        return subprocess.run(["sh", str(script), "-t", "t", "-c", "email", "body"],
                              env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def stub(self, **kw):
        stub = Stub(**kw)
        self.addCleanup(stub.close)
        return stub

    def test_a_clean_session_sends_once(self):
        stub = self.stub()
        done = self.send(stub)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(stub.calls, ["initialize", "notifications/initialized",
                                      "send_gmail_message"])

    def test_a_failed_session_setup_is_retried_in_a_fresh_session(self):
        stub = self.stub(fail_initialized=2)
        done = self.send(stub)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(stub.calls.count("initialize"), 3)
        self.assertEqual(stub.calls.count("send_gmail_message"), 1)
        self.assertIn("retrying", done.stderr)

    def test_three_failed_setups_fail_the_channel_without_sending(self):
        stub = self.stub(fail_initialized=3)
        done = self.send(stub)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(stub.calls.count("initialize"), 3)
        self.assertNotIn("send_gmail_message", stub.calls)
        self.assertIn("failed channels: email", done.stderr)

    def test_a_failed_send_is_never_repeated(self):
        # The send may have reached Gmail before it failed; a retry could
        # deliver the same mail twice.
        stub = self.stub(fail_send=True)
        done = self.send(stub)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(stub.calls.count("send_gmail_message"), 1)
        self.assertEqual(stub.calls.count("initialize"), 1)

    def test_an_unset_recipient_fails_the_channel_by_name_without_sending(self):
        stub = self.stub()
        done = self.send(stub, recipient=None)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("NOTIFY_EMAIL_TO not set", done.stderr)
        self.assertEqual(stub.calls, [])


if __name__ == "__main__":
    unittest.main()
