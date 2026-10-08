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
import stat
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
    """`mask --apply` writes a temp file and renames it over the original (sd:3042).

    One test per row of the failure table in
    docs/work/2026-10-08-mask-rewrite/design.md. Every file here is a
    synthetic fixture in a temporary folder.
    """

    #: Joined here, so this file is not a finding of the repository scan.
    TOKEN = "ghp_" + "Zq7" * 12
    PATTERN = "ghp_[0-9A-Za-z]{36}"

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

    def leftovers(self):
        return sorted(p.name for p in self.root.iterdir() if ".mask-" in p.name)

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

    def held_open(self, path: Path):
        """A child process holding `path` open, as a live session does."""
        child = subprocess.Popen([sys.executable, "-c", "import sys, time; f = open(sys.argv[1], 'ab'); "
                                  "print('open', flush=True); time.sleep(60)", str(path)],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "open")
        return child

    # Row 1: another process holds the file open.
    def test_a_file_another_process_holds_open_is_busy_and_untouched(self):
        path = self.log("session.jsonl")
        before = path.read_bytes()
        self.held_open(path)
        result = self.mod.mask_file(str(path), [], self.pats, True)
        self.assertTrue(result.busy)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.leftovers(), [])

    def test_the_summary_counts_a_held_file_as_busy(self):
        free, held = self.log("old.jsonl"), self.log("live.jsonl")
        self.held_open(held)
        result = self.run_main([free, held], apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  busy %s" % held, result.stdout)
        self.assertIn("in 1 file(s); 1 busy", result.stdout)
        self.assertIn(self.TOKEN.encode(), held.read_bytes())
        self.assertNotIn(self.TOKEN.encode(), free.read_bytes())

    # Row 2: nothing can tell whether a process holds it.
    def test_no_way_to_ask_for_holders_fails_and_leaves_the_file(self):
        path = self.log("old.jsonl")
        before = path.read_bytes()
        with mock.patch.object(self.mod, "holders", lambda _path: None):
            code, err = self.main_in_process(path)
        self.assertEqual(code, 1, err)
        self.assertIn("cannot tell whether another process holds it open", err)
        self.assertEqual(path.read_bytes(), before)

    # Row 3: a file the rewrite must not replace.
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

    def test_a_hard_link_is_not_split(self):
        path = self.log("old.jsonl")
        twin = self.root / "twin.jsonl"
        os.link(path, twin)
        code, err = self.main_in_process(path)
        self.assertEqual(code, 1, err)
        self.assertIn("hard links", err)
        self.assertEqual(os.stat(path).st_ino, os.stat(twin).st_ino)
        self.assertIn(self.TOKEN.encode(), twin.read_bytes())

    # Row 4: the temp file cannot be made (read-only volume, full folder).
    def test_a_temp_file_that_cannot_be_made_fails_only_when_it_holds_a_match(self):
        held = self.log("held.jsonl")
        clean = self.root / "clean.jsonl"
        clean.write_bytes(b'{"line": 1}\n')

        def read_only(*_a, **_k):
            raise OSError(errno.EROFS, "Read-only file system")

        with mock.patch.object(self.mod.tempfile, "mkstemp", read_only):
            code, err = self.main_in_process(held)
            self.assertEqual(self.main_in_process(clean)[0], 0)
        self.assertEqual(code, 1, err)
        self.assertIn("Read-only file system", err)
        self.assertIn(self.TOKEN.encode(), held.read_bytes())

    # Row 5: ENOSPC or EIO partway through the temp write or its fsync,
    # including an expanding write (sd:3005 blockers).
    def test_a_failed_temp_write_or_sync_leaves_the_original_whole(self):
        real_write = os.write

        def half_then_full(fd, data):
            real_write(fd, bytes(data[: max(1, len(data) // 2)]))
            raise OSError(errno.ENOSPC, "No space left on device")

        def eio(_fd):
            raise OSError(errno.EIO, "Input/output error")

        expanding = "A" * 40
        cases = {"ENOSPC mid-write": ("write", half_then_full, ""),
                 "EIO on fsync": ("fsync", eio, ""),
                 "ENOSPC on an expanding write": ("write", half_then_full, "K=" + expanding)}
        for label, (name, fake, pairs) in cases.items():
            with self.subTest(label):
                path = self.log("old.jsonl")
                if pairs:
                    path.write_bytes(("x %s %s\n" % (self.TOKEN, expanding)).encode())
                before = path.read_bytes()
                with mock.patch.object(self.mod.os, name, fake):
                    code, err = self.main_in_process(path, pairs)
                self.assertEqual(code, 1, err)
                self.assertIn("FAILED", err)
                self.assertIn("the original is unchanged", err)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(self.leftovers(), [])

    # Row 6: a writer gets in after the read and before the rename.
    def test_an_append_before_the_rename_makes_the_file_busy_and_is_kept(self):
        path = self.log("session.jsonl")
        appended = b'{"line": 2, "text": "written by the live session"}\n'

        def session_appends(target):
            with open(target, "ab") as f:
                f.write(appended)

        result = self.mod.mask_file(str(path), [], self.pats, True, before_rename=session_appends)
        self.assertTrue(result.busy)
        self.assertTrue(path.read_bytes().endswith(appended))
        self.assertEqual(self.leftovers(), [])

    def test_a_session_that_opens_the_file_before_the_rename_makes_it_busy(self):
        path = self.log("session.jsonl")
        before = path.read_bytes()
        result = self.mod.mask_file(str(path), [], self.pats, True, before_rename=self.held_open)
        self.assertTrue(result.busy)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.leftovers(), [])

    # Row 7: the rename fails.
    def test_a_failed_rename_leaves_the_original_whole(self):
        path = self.log("old.jsonl")
        before = path.read_bytes()

        def eio(*_a):
            raise OSError(errno.EIO, "Input/output error")

        with mock.patch.object(self.mod.os, "replace", eio):
            code, err = self.main_in_process(path)
        self.assertEqual(code, 1, err)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.leftovers(), [])

    # Row 8: bytes reach the old inode during the rename.
    def test_bytes_written_to_the_old_inode_during_the_rename_are_masked_and_kept(self):
        path = self.log("session.jsonl")
        late = ('{"line": 2, "text": "late %s"}\n' % self.TOKEN).encode()
        real_replace = os.replace

        def append_then_replace(src, dst):
            with open(dst, "ab") as f:
                f.write(late)
            real_replace(src, dst)

        with mock.patch.object(self.mod.os, "replace", append_then_replace):
            result = self.mod.mask_file(str(path), [], self.pats, True)
        self.assertEqual(result.pcount, 2)
        on_disk = path.read_bytes()
        self.assertTrue(on_disk.endswith(b'{"line": 2, "text": "late <masked:pattern>"}\n'), on_disk)
        self.assertNotIn(self.TOKEN.encode(), on_disk)

    # Row 9: the folder sync after the rename fails.
    def test_a_failed_folder_sync_fails_the_run_with_the_masked_bytes_in_place(self):
        path = self.log("old.jsonl")
        real_fsync = os.fsync

        def folder_fails(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "Input/output error")
            return real_fsync(fd)

        with mock.patch.object(self.mod.os, "fsync", folder_fails):
            code, err = self.main_in_process(path)
        self.assertEqual(code, 1, err)
        self.assertIn("masked, then", err)
        self.assertNotIn(self.TOKEN.encode(), path.read_bytes())

    # Row 10: a sound rewrite keeps the mode and the path shape.
    def test_a_masked_file_keeps_its_mode_and_leaves_no_temp_file(self):
        path = self.log("session.jsonl")
        result = self.mod.mask_file(str(path), [], self.pats, True)
        self.assertEqual((result.busy, result.pcount), (False, 1))
        self.assertIn(b"<masked:pattern>", path.read_bytes())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.leftovers(), [])

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
        result = self.run_main([path], apply=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("would mask 0 your-key value(s) + 1 pattern match(es) in 1 file(s); 0 busy (dry run", result.stdout)
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

if __name__ == "__main__":
    unittest.main()
