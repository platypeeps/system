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

import contextlib
import errno
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

FOLDER = Path(__file__).resolve().parent.parent
REPO = FOLDER.parent
SCRIPT = FOLDER / "scan-for-secrets.sh"


def isolate_jev(env: dict) -> None:
    """Keep a `jev` on the inherited PATH out of the operator's ledger,
    collector and trace corpus: `enabled --record` writes a row even with the
    stage off."""
    env["JEV_METER"] = "0"
    env.pop("JEV_METER_DB", None)
    env.pop("JEV_TRACES_URL", None)
    env["JEV_CORPUS"] = "0"
    env.pop("JEV_CORPUS_DIR", None)


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
        isolate_jev(self.env)

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
            isolate_jev(env)
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
        for name in ("home", "tree", "bin", "config", "corpus"):
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
            # Pinned on: an operator's own JEV_ENABLED=0 would switch every call here off.
            JEV_ENABLED="1",
            JEV_FLAG_FILE=str(self.root / "config" / "enabled"), JEV_SHADOW="0", JEV_METER="0",
            TYPESAFE_API_KEY="test-key", JEV_URL=remote + "/v1/systemone",
            # Listed, with both arms on: a local-only stage still reaches no arm.
            JEV_COMPARE_STAGES="JEV_SECRET_SCAN", JEV_COMPARE_HAIKU_VIA="anthropic",
            JEV_COMPARE_ANTHROPIC_KEY="test-key", JEV_COMPARE_ANTHROPIC_URL=remote + "/v1/messages",
            JEV_CORPUS="1", JEV_CORPUS_DIR=str(self.root / "corpus"))
        self.env.pop("S4S_CONF", None)
        self.env.pop("JEV_SECRET_SCAN", None)
        # An inherited endpoint would send this suite's spans to the operator's collector.
        self.env.pop("JEV_TRACES_URL", None)

    def tearDown(self):
        self._tmp.cleanup()

    def scan(self, **extra):
        return subprocess.run(["sh", str(SCRIPT)], cwd=self.root / "tree", env=dict(self.env, **extra),
                              capture_output=True, text=True, timeout=120)

    def seen(self, result):
        return (result.returncode, result.stdout, result.stderr)

    def test_the_scan_is_the_same_with_the_stage_on_off_and_kev_down(self):
        # Without this case's `jev`. The inherited PATH may hold an installed
        # one, so the stage is switched off as well.
        without = self.seen(self.scan(PATH=self.env["PATH"].split(":", 1)[1],
                                      JEV_SECRET_SCAN="0"))
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

    def test_the_corpus_keeps_each_hit_as_a_hash_and_never_as_text(self):
        self.scan()
        files = list((self.root / "corpus").glob("*.jsonl"))
        lines = [json.loads(line) for path in files for line in path.read_text().splitlines()]
        # Each hit is a Kev call and its shadow: the scanner's own `yes`.
        self.assertEqual(sorted((r["stage"], r["arm"]) for r in lines),
                         [("JEV_SECRET_SCAN", "baseline")] * 2 + [("JEV_SECRET_SCAN", "kev")] * 2)
        self.assertTrue(all(r["request"]["state_sha256"] for r in lines if r["arm"] == "kev"))
        self.assertNotIn(self.TOKEN, "".join(path.read_text() for path in files))

    def test_each_hit_is_named_by_its_location_and_the_run_is_one_id(self):
        """`--subject` hashes `path:line`, never the token; one `JEV_RUN` per run (sd:2953)."""
        stub = self.root / "stub"
        stub.mkdir()
        (stub / "jev").write_text(
            '#!/bin/sh\nprintf \'%s run=%s\\n\' "$*" "${JEV_RUN:-}" >> "$JEV_STUB_LOG"\n'
            'cat >/dev/null\n')
        (stub / "jev").chmod(0o755)
        log = self.root / "calls"
        result = self.scan(PATH=f"{stub}:{self.env['PATH']}", JEV_STUB_LOG=str(log), JEV_RUN="")
        calls = log.read_text().splitlines()
        self.assertEqual([line.split()[0] for line in calls], ["enabled", "noul", "noul"])
        subjects = sorted(re.search(r"--subject (\S+)", line).group(1) for line in calls[1:])
        # The report names each hit's location; ripgrep prints `one.txt`, `grep -E` `./one.txt`.
        locations = re.findall(r"^\s+(\S*one\.txt:\d+):", result.stdout, re.M)
        self.assertEqual(len(locations), 2, result.stdout)
        expected = sorted("secret-scan:" + hashlib.sha256(f"{where}\n".encode()).hexdigest()[:16]
                          for where in locations)
        self.assertEqual(subjects, expected)
        self.assertNotIn(self.TOKEN[4:], " ".join(subjects))
        runs = {line.rsplit("run=", 1)[1] for line in calls}
        self.assertEqual(len(runs), 1, runs)
        self.assertRegex(runs.pop(), r"^secret-scan-\d{8}T\d{6}-[0-9a-f]{4}$")

    def test_a_colon_in_the_path_keeps_each_line_apart(self):
        """`a:b.txt:1` and `a:b.txt:2` are two subjects: the path keeps its colon."""
        (self.root / "tree" / "one.txt").unlink()
        (self.root / "tree" / "a:b.txt").write_text(f"token = {self.TOKEN}\nother = {self.TOKEN}\n",
                                                    encoding="utf-8")
        stub = self.root / "stub"
        stub.mkdir()
        (stub / "jev").write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$JEV_STUB_LOG"\ncat >/dev/null\n')
        (stub / "jev").chmod(0o755)
        log = self.root / "calls"
        result = self.scan(PATH=f"{stub}:{self.env['PATH']}", JEV_STUB_LOG=str(log))
        subjects = sorted(re.search(r"--subject (\S+)", line).group(1)
                          for line in log.read_text().splitlines()[1:])
        # ripgrep names the file `a:b.txt`, `grep -E` `./a:b.txt`.
        where = "./a:b.txt" if "./a:b.txt" in result.stdout else "a:b.txt"
        self.assertEqual(subjects, sorted("secret-scan:" + hashlib.sha256(f"{where}:{n}\n".encode()).hexdigest()[:16]
                                          for n in (1, 2)))

    def test_a_colon_digits_colon_in_the_path_keeps_each_line_apart(self):
        """`a:12:b.txt` holds two hits, so two subjects: the path is its own field (sd:2977)."""
        (self.root / "tree" / "one.txt").unlink()
        (self.root / "tree" / "a:12:b.txt").write_text(f"token = {self.TOKEN}\nother = {self.TOKEN}\n",
                                                       encoding="utf-8")
        stub = self.root / "stub"
        stub.mkdir()
        (stub / "jev").write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$JEV_STUB_LOG"\ncat >> "$JEV_STUB_LOG.in"\n')
        (stub / "jev").chmod(0o755)
        log = self.root / "calls"
        for searcher, path in UrlCredentials.SEARCHERS.items():
            with self.subTest(searcher=searcher):
                log.unlink(missing_ok=True)
                Path(f"{log}.in").unlink(missing_ok=True)
                result = self.scan(PATH=f"{stub}:{path}", JEV_STUB_LOG=str(log))
                subjects = sorted(re.search(r"--subject (\S+)", line).group(1)
                                  for line in log.read_text().splitlines()[1:])
                where = "./a:12:b.txt" if "./a:12:b.txt" in result.stdout else "a:12:b.txt"
                self.assertEqual(subjects, sorted("secret-scan:" + hashlib.sha256(f"{where}:{n}\n".encode())
                                                  .hexdigest()[:16] for n in (1, 2)))
                # Kev still reads the hit as `path:line:match`.
                self.assertEqual(sorted(Path(f"{log}.in").read_text().splitlines()),
                                 [f"{where}:{n}:{self.TOKEN}" for n in (1, 2)])

    def test_the_report_prints_a_colon_path_with_its_line_and_kind(self):
        """`a:b.txt` reports as `a:b.txt:1: ghp_…`, the provider named (sd:2977)."""
        (self.root / "tree" / "one.txt").unlink()
        (self.root / "tree" / "a:b.txt").write_text(f"token = {self.TOKEN}\n", encoding="utf-8")
        for searcher, path in UrlCredentials.SEARCHERS.items():
            with self.subTest(searcher=searcher):
                result = self.scan(PATH=path, JEV_SECRET_SCAN="0")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertRegex(result.stdout, r"(?m)^  (\./)?a:b\.txt:1: ghp_Zq7Z… \[GitHub classic PAT\]$")

    def test_the_stage_switched_off_asks_nobody(self):
        self.scan(JEV_SECRET_SCAN="0")
        self.assertEqual((self.kev.seen, self.remote.seen), ([], []))

    def test_the_hits_asked_per_run_are_capped(self):
        self.scan(S4S_JEV_MAX_HITS="1")
        self.assertEqual(len(self.kev.seen), 1)


def load_mask_files():
    spec = importlib.util.spec_from_file_location("mask_files", FOLDER / "mask_files.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MaskRewrite(unittest.TestCase):
    """`mask --apply` masks in place, same length, same inode (sd:3042).

    One test per row of the failure table in
    docs/work/2026-10-08-mask-rewrite/design.md. Every file here is a
    synthetic fixture in a temporary folder.
    """

    #: Joined here, so this file is not a finding of the repository scan.
    TOKEN = "ghp_" + "Zq7" * 12
    PATTERN = "ghp_[0-9A-Za-z]{36}"
    MASKED = "<masked:pattern>" + "*" * 24

    def setUp(self):
        self.mod = load_mask_files()
        self.pats = self.mod.parse_patterns(self.PATTERN)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def log(self, name: str) -> Path:
        path = self.root / name
        path.write_bytes(('{"line": 1, "text": "token %s"}\n' % self.TOKEN).encode())
        path.chmod(0o600)
        return path

    def run_main(self, paths, apply):
        env = dict(os.environ, S4S_PATTERNS=self.PATTERN, S4S_PAIRS="", S4S_APPLY="1" if apply else "0")
        return subprocess.run([sys.executable, str(FOLDER / "mask_files.py")],
                              input="\n".join(str(p) for p in paths) + "\n",
                              env=env, capture_output=True, text=True, timeout=60)

    def main_in_process(self, path: Path, pairs: str = "") -> tuple[int, str]:
        env = {"S4S_PATTERNS": self.PATTERN, "S4S_PAIRS": pairs, "S4S_APPLY": "1"}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdin", io.StringIO("%s\n" % path)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = self.mod.main()
        return code, err.getvalue()

    # Row 1: a sound mask keeps the length, the inode and the mode.
    def test_a_mask_keeps_the_length_the_inode_and_the_mode(self):
        path = self.log("session.jsonl")
        before = os.stat(path)
        result = self.mod.mask_file(str(path), [], self.pats, True)
        after = os.stat(path)
        self.assertEqual(result, (0, 1))
        self.assertEqual(path.read_text(), '{"line": 1, "text": "token %s"}\n' % self.MASKED)
        self.assertEqual((after.st_size, after.st_ino, after.st_mode), (before.st_size, before.st_ino, before.st_mode))

    def test_a_value_shorter_than_its_label_masks_to_stars(self):
        path = self.root / "history"
        path.write_bytes(b"export K=s3cr3t; echo s3cr3t\n")
        code, err = self.main_in_process(path, "K=s3cr3t")
        self.assertEqual(code, 0, err)
        self.assertEqual(path.read_bytes(), b"export K=******; echo ******\n")

    def test_a_value_longer_than_its_label_keeps_the_label(self):
        path = self.root / "history"
        value = "v" * 20
        path.write_bytes(("x %s y\n" % value).encode())
        code, err = self.main_in_process(path, "K=" + value)
        self.assertEqual(code, 0, err)
        self.assertEqual(path.read_bytes(), b"x <masked:$K>" + b"*" * 9 + b" y\n")

    # Row 2: a writer appends while mask runs (the review's reproduction).
    def test_a_session_that_opens_the_file_mid_mask_keeps_every_record(self):
        path = self.log("session.jsonl")
        records = [b'{"line": %d}\n' % n for n in (2, 3, 4)]
        real_fsync, session = os.fsync, []

        def session_appends(fd):
            # The session opens the log after mask read it, writes, and keeps
            # its descriptor for later writes.
            if not session:
                session.append(os.open(path, os.O_WRONLY | os.O_APPEND))
                os.write(session[0], records[0])
            return real_fsync(fd)

        with mock.patch.object(self.mod.os, "fsync", session_appends):
            code, err = self.main_in_process(path)
        self.addCleanup(os.close, session[0])
        for record in records[1:]:
            os.write(session[0], record)
        self.assertEqual(code, 0, err)
        self.assertEqual(path.read_bytes(),
                         ('{"line": 1, "text": "token %s"}\n' % self.MASKED).encode() + b"".join(records))

    def test_a_live_appender_process_keeps_every_record(self):
        # Each record holds a token, so every pass while the child runs writes.
        path = self.log("session.jsonl")
        record = '{"line": %d, "text": "token %s"}'
        child = subprocess.Popen([sys.executable, "-c",
                                  "import os, sys\n"
                                  "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND)\n"
                                  "for n in range(2, 2002):\n"
                                  "    os.write(fd, (sys.argv[2] % (n, sys.argv[3]) + '\\n').encode())\n",
                                  str(path), record, self.TOKEN])
        self.addCleanup(child.wait)
        passes = 0
        while child.poll() is None:
            passes += self.mod.mask_file(str(path), [], self.pats, True).pcount > 0
        self.mod.mask_file(str(path), [], self.pats, True)
        self.assertEqual(child.returncode, 0)
        self.assertGreater(passes, 1)
        self.assertEqual(path.read_text().splitlines(), [record % (n, self.MASKED) for n in range(1, 2002)])

    # Write rows: one pwrite per match span; on any failure, list every span
    # not yet fully written, because a second run cannot find a half-masked value.
    def three_tokens(self) -> tuple[Path, list[str]]:
        path = self.root / "three.jsonl"
        lines = ['{"line": %d, "text": "token %s"}' % (n, self.TOKEN) for n in (1, 2, 3)]
        path.write_bytes(("\n".join(lines) + "\n").encode())
        data = path.read_bytes()
        offsets, at = [], 0
        for _ in lines:
            at = data.index(self.TOKEN.encode(), at)
            offsets.append("    offset %d length %d" % (at, len(self.TOKEN)))
            at += 1
        return path, offsets

    def fail_on_second(self, name, action):
        real, calls = getattr(os, name), []

        def wrapped(*args):
            calls.append(args)
            if len(calls) == 2:
                return action(real, *args)
            return real(*args)

        return mock.patch.object(self.mod.os, name, wrapped), calls

    def test_a_token_across_a_4k_boundary_is_written_by_one_pwrite(self):
        path = self.root / "big.jsonl"
        head = b"x" * (4096 - 10)
        path.write_bytes(head + self.TOKEN.encode() + b"\n")
        patch, calls = self.fail_on_second("pwrite", lambda real, *a: real(*a))
        with patch:
            self.mod.mask_file(str(path), [], self.pats, True)
        self.assertEqual([(len(bytes(data)), offset) for _fd, data, offset in calls], [(len(self.TOKEN), len(head))])
        self.assertEqual(path.read_bytes(), head + self.MASKED.encode() + b"\n")

    def test_overlapping_matches_are_one_span_and_one_pwrite(self):
        path = self.root / "history"
        path.write_bytes(b"secret=abcdef12 x\n")
        pats = self.mod.parse_patterns("secret=[^ ]+")
        patch, calls = self.fail_on_second("pwrite", lambda real, *a: real(*a))
        with patch:
            self.mod.mask_file(str(path), [("K", b"abcdef12")], pats, True)
        self.assertEqual([(len(bytes(data)), offset) for _fd, data, offset in calls], [(15, 0)])
        self.assertEqual(path.read_bytes(), b"*" * 15 + b" x\n")

    def test_a_failed_pwrite_lists_every_span_not_yet_written(self):
        path, offsets = self.three_tokens()

        def eio(_real, *_a):
            raise OSError(errno.EIO, "Input/output error")

        patch, _calls = self.fail_on_second("pwrite", eio)
        with patch:
            code, err = self.main_in_process(path)
        self.assertEqual(code, 1, err)
        self.assertIn("2 match span(s) not fully masked", err)
        self.assertEqual([line for line in err.splitlines() if line.startswith("    offset")], offsets[1:])
        self.assertEqual(path.read_bytes().count(self.MASKED.encode()), 1)

    def test_a_short_pwrite_lists_the_half_written_span(self):
        path, offsets = self.three_tokens()

        def half(real, fd, data, offset):
            return real(fd, bytes(data)[: len(data) // 2], offset)

        patch, _calls = self.fail_on_second("pwrite", half)
        with patch:
            code, err = self.main_in_process(path)
        self.assertEqual(code, 1, err)
        self.assertIn("short write, 20 of 40 bytes", err)
        self.assertEqual([line for line in err.splitlines() if line.startswith("    offset")], offsets[1:])

    def test_a_span_changed_or_unreadable_before_its_write_stops_the_file(self):
        def rewrite(real, fd, n, offset):
            os.pwrite(fd, b"y" * n, offset)
            return real(fd, n, offset)

        def eio(_real, *_a):
            raise OSError(errno.EIO, "Input/output error")

        for label, action, reason in (("rewritten", rewrite, "changed while it was masked"),
                                      ("unreadable", eio, "Input/output error")):
            with self.subTest(label):
                path, offsets = self.three_tokens()
                patch, _calls = self.fail_on_second("pread", action)
                with patch:
                    code, err = self.main_in_process(path)
                self.assertEqual(code, 1, err)
                self.assertIn(reason, err)
                self.assertEqual([line for line in err.splitlines() if line.startswith("    offset")], offsets[1:])

    def test_a_failed_fsync_lists_every_span(self):
        path, offsets = self.three_tokens()
        size = path.stat().st_size

        def eio(_fd):
            raise OSError(errno.EIO, "Input/output error")

        with mock.patch.object(self.mod.os, "fsync", eio):
            code, err = self.main_in_process(path)
        self.assertEqual(code, 1, err)
        self.assertIn("nothing is known to be on disk", err)
        self.assertEqual([line for line in err.splitlines() if line.startswith("    offset")], offsets)
        self.assertEqual(path.stat().st_size, size)

    # Row 5: the file will not open to write.
    def test_an_unwritable_file_fails_only_when_it_holds_a_match(self):
        held = self.log("held.jsonl")
        clean = self.root / "clean.jsonl"
        clean.write_bytes(b'{"line": 1}\n')
        for path in (held, clean):
            path.chmod(0o444)
        result = self.run_main([held], apply=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAILED %s: not writable" % held, result.stderr)
        self.assertIn(self.TOKEN.encode(), held.read_bytes())
        self.assertEqual(self.run_main([clean], apply=True).returncode, 0)

    # Row 6: links and dry runs.
    def test_a_hard_link_sees_the_mask(self):
        path = self.log("old.jsonl")
        twin = self.root / "twin.jsonl"
        os.link(path, twin)
        code, err = self.main_in_process(path)
        self.assertEqual(code, 0, err)
        self.assertNotIn(self.TOKEN.encode(), twin.read_bytes())

    def test_a_symlink_masks_its_target_and_stays_a_link(self):
        target = self.log("target.jsonl")
        link = self.root / "link.jsonl"
        link.symlink_to(target)
        result = self.run_main([link], apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(link.is_symlink())
        self.assertNotIn(self.TOKEN.encode(), target.read_bytes())

    def test_a_dry_run_writes_nothing(self):
        path = self.log("old.jsonl")
        path.chmod(0o444)
        result = self.run_main([path], apply=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("would mask 0 your-key value(s) + 1 pattern match(es) in 1 file(s) (dry run", result.stdout)
        self.assertIn(self.TOKEN.encode(), path.read_bytes())


class ManualMask(unittest.TestCase):
    """`mask`, which the operator runs by hand, on homes with less in them
    (sd:1254). A fixture $HOME and fixture scratch roots only: no live file
    is read or rewritten."""

    #: Joined here, so this file is not a finding of the repository scan.
    TOKEN = "ghp_" + "Zq7" * 12

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        (self.root / "scratch").mkdir()
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), SYSTEM_TOOLS_CONFIG=str(self.root / "config"),
                        S4S_SCRATCH_ROOTS=str(self.root / "scratch"), JEV_SECRET_SCAN="0")
        self.env.pop("S4S_CONF", None)
        isolate_jev(self.env)

    def tearDown(self):
        self._tmp.cleanup()

    def plant(self, relative: str) -> None:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"text": "token %s"}\n' % self.TOKEN, encoding="utf-8")

    def test_mask_without_exports_still_masks_known_patterns(self):
        # No env.sh and no .bash_profile: the weekly job's mask pass must not
        # fail, and a pattern hit is still masked (review, sd:1254).
        self.plant(".codex/sessions/rollout.jsonl")
        dry = subprocess.run(["sh", str(SCRIPT), "mask", "--no-prune"], cwd=self.root, env=self.env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(dry.returncode, 2, dry.stdout + dry.stderr)
        self.assertIn("would mask 0 your-key value(s) + 1 pattern match(es) in 1 file(s)", dry.stdout)
        applied = subprocess.run(["sh", str(SCRIPT), "mask", "--apply", "--no-prune"], cwd=self.root,
                                 env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertNotIn(self.TOKEN, (self.home / ".codex/sessions/rollout.jsonl").read_text())

    def test_mask_with_no_targets_touches_nothing(self):
        # A home with no history, no AI store and no scratchpad: mask must not
        # fall back to searching the current directory, $HOME (review, sd:1254).
        self.plant("repos/project/app.py")
        for flags in (["--no-prune"], ["--apply", "--no-prune"]):
            with self.subTest(flags=flags):
                result = subprocess.run(["sh", str(SCRIPT), "mask", *flags], cwd=self.root, env=self.env,
                                        capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(self.TOKEN, (self.home / "repos/project/app.py").read_text())
                self.assertNotIn("repos/project/app.py", result.stdout)

    def test_mask_leaves_the_gemini_jetski_token_store_alone(self):
        # Antigravity keeps its live login in this file; masking it logs the
        # tool out (sd:3208). A session log beside it is still masked.
        self.plant(".gemini/jetski-standalone-oauth-token")
        self.plant(".gemini/tmp/session.jsonl")
        applied = subprocess.run(["sh", str(SCRIPT), "mask", "--apply", "--no-prune"], cwd=self.root,
                                 env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn(self.TOKEN, (self.home / ".gemini/jetski-standalone-oauth-token").read_text())
        self.assertNotIn(self.TOKEN, (self.home / ".gemini/tmp/session.jsonl").read_text())

if __name__ == "__main__":
    unittest.main()
