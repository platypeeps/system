#!/usr/bin/env python3
"""scan-for-secrets.sh finds its own folder however it is invoked (sd:1915).

`critical`, `mask` and `prune` change to `$HOME` before they scan. A relative
`$0` resolved after that change points nowhere, and the script then failed to
source `lib/config.sh`. Cron runs it by an absolute path, so only a hand-typed
relative invocation, or a relative symlink, showed the fault.

Every case runs `prune` without `--apply` against an empty temporary `$HOME`,
so nothing is deleted and the operator's config is never read.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
REPO = FOLDER.parent
SCRIPT = FOLDER / "scan-for-secrets.sh"


class RelativeInvocation(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = dict(os.environ)
        self.env["HOME"] = str(self.home)
        self.env["SYSTEM_TOOLS_CONFIG"] = str(self.tmp / "config")
        self.env.pop("S4S_CONF", None)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_prune(self, script: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", script, "prune"],
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def assert_clean_run(self, result: subprocess.CompletedProcess) -> None:
        self.assertNotIn("config.sh", result.stderr)
        self.assertNotIn("No such file or directory", result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no prune dirs present", result.stdout)

    def test_relative_path_from_repository_root(self) -> None:
        result = self.run_prune("local-scan-for-secrets/scan-for-secrets.sh", REPO)
        self.assert_clean_run(result)

    def test_relative_path_to_a_relative_symlink(self) -> None:
        # local-bin-links links the script into a bin folder; the link target
        # here is relative too, so both hops must resolve before the cd.
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        link = bin_dir / "scan-for-secrets.sh"
        link.symlink_to(os.path.relpath(SCRIPT, bin_dir))
        result = self.run_prune("bin/scan-for-secrets.sh", self.tmp)
        self.assert_clean_run(result)


class UrlCredentials(unittest.TestCase):
    """A URL's userinfo cannot hold `[` or `]` (RFC 3986, 3.2.1), so the
    bracketed grammar line `scheme://[user[:password]@]host` is not a
    credential (sd:2591); a real `user:password@` still is."""

    def scan(self, text: str, path: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "home").mkdir()
            (root / "tree").mkdir()
            (root / "tree" / "remote.py").write_text(text, encoding="utf-8")
            # The stage is off, so a `jev` on the inherited PATH asks no Kev.
            env = dict(os.environ, HOME=str(root / "home"), SYSTEM_TOOLS_CONFIG=str(root / "config"), PATH=path,
                       JEV_SECRET_SCAN="0")
            env.pop("S4S_CONF", None)
            return subprocess.run(["sh", str(SCRIPT)], cwd=root / "tree", env=env,
                                  capture_output=True, text=True, timeout=120)

    #: The scan uses ripgrep when PATH has it and `grep -E` otherwise; the
    #: gate's PATH has no ripgrep. Both are checked where both exist.
    SEARCHERS = {"inherited PATH": os.environ.get("PATH", "/usr/bin:/bin"), "grep -E": "/usr/bin:/bin"}

    def test_a_bracketed_grammar_line_is_not_a_finding(self) -> None:
        for searcher, path in self.SEARCHERS.items():
            with self.subTest(searcher=searcher):
                result = self.scan("#: `scheme://[user[:password]@]host[:port]/path`, the URL form of a remote.\n", path)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("embedded credentials", result.stdout)

    def test_a_url_with_a_password_is_still_a_finding(self) -> None:
        for searcher, path in self.SEARCHERS.items():
            with self.subTest(searcher=searcher):
                # Joined here, so this file is not a finding of the repository scan.
                result = self.scan("REMOTE = 'https://alice:" + "correct-horse-battery@example.test/repo.git'\n", path)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("URL with embedded credentials", result.stdout)


class Recorder(BaseHTTPRequestHandler):
    """A stub endpoint that answers a System One request and keeps each body."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self.server.seen.append(body)
        answers = {qid: {"type": "noul", "noul": 0.1} for qid in body.get("questions", {})}
        data = json.dumps({"model": "stub", "answers": answers}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class TheLocalJudgment(unittest.TestCase):
    """Each hit is asked of the local Kev alone, and the scan does not change
    (sd:2761). `jev` is the real `local-jev/jev.sh` on a PATH of this case's
    own; Kev, Jev and the Haiku arm are stubs on loopback, all switched on, so
    a hit that reached anything but Kev would show in a stub's count."""

    #: Joined here, so this file is not a finding of the repository scan.
    TOKEN = "ghp_" + "Zq7" * 12

    @classmethod
    def setUpClass(cls):
        cls.kev, cls.remote = serve(), serve()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.kev, cls.remote):
            server.shutdown()
            server.server_close()

    def setUp(self):
        self.kev.seen.clear()
        self.remote.seen.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        for name in ("home", "tree", "bin", "config"):
            (self.root / name).mkdir()
        # Two hits in one file: ripgrep orders files differently run to run.
        (self.root / "tree" / "one.txt").write_text(f"token = {self.TOKEN}\nother = {self.TOKEN}\n",
                                                    encoding="utf-8")
        (self.root / "bin" / "jev").symlink_to(REPO / "local-jev" / "jev.sh")
        remote = "http://127.0.0.1:%d" % self.remote.server_address[1]
        self.env = dict(
            os.environ, HOME=str(self.root / "home"), SYSTEM_TOOLS_CONFIG=str(self.root / "config"),
            PATH=f"{self.root / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            JEV_COMPARE_KEV_URL="http://127.0.0.1:%d/v1/systemone" % self.kev.server_address[1],
            JEV_FLAG_FILE=str(self.root / "config" / "enabled"), JEV_SHADOW="0", JEV_METER="0",
            TYPESAFE_API_KEY="test-key", JEV_URL=remote + "/v1/systemone",
            JEV_COMPARE_KEV="1", JEV_COMPARE_HAIKU_VIA="anthropic",
            JEV_COMPARE_ANTHROPIC_KEY="test-key", JEV_COMPARE_ANTHROPIC_URL=remote + "/v1/messages")
        self.env.pop("S4S_CONF", None)
        self.env.pop("JEV_SECRET_SCAN", None)

    def tearDown(self):
        self._tmp.cleanup()

    def scan(self, **extra):
        return subprocess.run(["sh", str(SCRIPT)], cwd=self.root / "tree", env=dict(self.env, **extra),
                              capture_output=True, text=True, timeout=120)

    def seen(self, result):
        return (result.returncode, result.stdout, result.stderr)

    def test_the_scan_is_the_same_with_the_stage_on_off_and_kev_down(self):
        without = self.seen(self.scan(PATH=self.env["PATH"].split(":", 1)[1]))
        self.assertEqual(without[0], 2, without)
        self.assertEqual(self.kev.seen, [])
        cases = {"on": {}, "off": {"JEV_SECRET_SCAN": "0"},
                 "kev down": {"JEV_COMPARE_KEV_URL": "http://127.0.0.1:9/v1/systemone"}}
        for name, extra in cases.items():
            with self.subTest(stage=name):
                self.assertEqual(self.seen(self.scan(**extra)), without)

    def test_each_hit_reaches_kev_and_nothing_else(self):
        self.scan()
        self.assertEqual(len(self.kev.seen), 2)
        self.assertTrue(all(self.TOKEN in body["state"] for body in self.kev.seen))
        self.assertEqual(self.remote.seen, [])

    def test_the_stage_switched_off_asks_nobody(self):
        self.scan(JEV_SECRET_SCAN="0")
        self.assertEqual((self.kev.seen, self.remote.seen), ([], []))

    def test_the_hits_asked_per_run_are_capped(self):
        self.scan(S4S_JEV_MAX_HITS="1")
        self.assertEqual(len(self.kev.seen), 1)


if __name__ == "__main__":
    unittest.main()
