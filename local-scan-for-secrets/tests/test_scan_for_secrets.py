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
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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
            # Listed and switched on: a local-only stage still reaches no arm.
            JEV_COMPARE_STAGES="JEV_SECRET_SCAN",
            JEV_COMPARE_KEV="1", JEV_COMPARE_HAIKU_VIA="anthropic",
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
    """`mask --apply` never loses a line a live session appends (sd:1254).

    The rewrite reads a file, then writes the masked bytes back and truncates.
    A session log can grow in between. Two guards stop that: a file modified
    inside the settle window is skipped, and a file whose size or mtime moved
    since the read is skipped. A skipped file counts as busy; the next run
    masks it. Every file here is a synthetic fixture in a temporary folder.
    """

    #: Joined here, so this file is not a finding of the repository scan.
    TOKEN = "ghp_" + "Zq7" * 12
    PATTERN = "ghp_[0-9A-Za-z]{36}"
    SETTLED = 20 * 60

    def setUp(self):
        self.mod = load_mask_files()
        self.pats = self.mod.parse_patterns(self.PATTERN)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def log(self, name: str, age_seconds: float) -> Path:
        path = self.root / name
        path.write_bytes(('{"line": 1, "text": "token %s"}\n' % self.TOKEN).encode())
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_a_concurrent_append_survives_the_rewrite(self):
        path = self.log("session.jsonl", self.SETTLED)
        appended = b'{"line": 2, "text": "written by the live session"}\n'

        def session_appends(target):
            with open(target, "ab") as f:
                f.write(appended)

        result = self.mod.mask_file(str(path), [], self.pats, True, before_write=session_appends)
        self.assertIn(appended, path.read_bytes())
        self.assertTrue(result.busy)

    def test_a_file_inside_the_settle_window_is_left_alone(self):
        path = self.log("session.jsonl", 60)
        before = path.read_bytes()
        result = self.mod.mask_file(str(path), [], self.pats, True, settle_s=600)
        self.assertTrue(result.busy)
        self.assertEqual(path.read_bytes(), before)

    def test_a_settled_file_is_masked(self):
        path = self.log("session.jsonl", self.SETTLED)
        result = self.mod.mask_file(str(path), [], self.pats, True, settle_s=600)
        self.assertFalse(result.busy)
        self.assertEqual(result.pcount, 1)
        self.assertNotIn(self.TOKEN.encode(), path.read_bytes())
        self.assertIn(b"<masked:pattern>", path.read_bytes())

    def run_main(self, paths, apply):
        env = dict(os.environ, S4S_PATTERNS=self.PATTERN, S4S_PAIRS="", S4S_MASK_SETTLE_MIN="10",
                   S4S_APPLY="1" if apply else "0")
        return subprocess.run([sys.executable, str(FOLDER / "mask_files.py")],
                              input="\n".join(str(p) for p in paths) + "\n",
                              env=env, capture_output=True, text=True, timeout=60)

    def test_the_summary_counts_busy_files(self):
        settled = self.log("old.jsonl", self.SETTLED)
        recent = self.log("new.jsonl", 60)
        result = self.run_main([settled, recent], apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  busy %s" % recent, result.stdout)
        self.assertIn("in 1 file(s); 1 busy", result.stdout)
        self.assertIn(self.TOKEN.encode(), recent.read_bytes())
        self.assertNotIn(self.TOKEN.encode(), settled.read_bytes())

    def test_a_failure_after_the_rewrite_starts_exits_nonzero(self):
        """Every step after the first byte moves: seek, write, truncate, flush
        and close, alone and with close failing again during cleanup."""
        real_open = open
        for fail_on in ({"seek"}, {"write"}, {"truncate"}, {"flush"}, {"close"}, {"write", "close"}):
            with self.subTest(fail_on=sorted(fail_on)):
                path = self.log("old.jsonl", self.SETTLED)

                class Full:
                    """A file whose disk fills at the named steps."""

                    def __init__(self, handle):
                        self.handle = handle

                    def __enter__(self):
                        return self

                    def __exit__(self, *_exc):
                        self.handle.close()
                        self.fail("close")

                    def __getattr__(self, name):
                        return getattr(self.handle, name)

                    def fail(self, step):
                        if step in fail_on and "+" in self.handle.mode:
                            raise OSError(errno.ENOSPC, "No space left on device")

                    def seek(self, *a):
                        self.fail("seek")
                        return self.handle.seek(*a)

                    def write(self, data):
                        self.fail("write")
                        return self.handle.write(data)

                    def truncate(self, *a):
                        self.fail("truncate")
                        return self.handle.truncate(*a)

                    def flush(self):
                        self.fail("flush")
                        return self.handle.flush()

                env = {"S4S_PATTERNS": self.PATTERN, "S4S_PAIRS": "", "S4S_MASK_SETTLE_MIN": "10",
                       "S4S_APPLY": "1"}
                err = io.StringIO()
                with mock.patch.dict(os.environ, env), \
                        mock.patch.object(sys, "stdin", io.StringIO("%s\n" % path)), \
                        mock.patch.object(self.mod, "open", lambda *a, **k: Full(real_open(*a, **k)), create=True), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    code = self.mod.main()
                self.assertEqual(code, 1, err.getvalue())
                self.assertIn("FAILED", err.getvalue())

    def test_an_unwritable_file_fails_only_when_it_holds_a_match(self):
        held = self.log("held.jsonl", self.SETTLED)
        clean = self.root / "clean.jsonl"
        clean.write_bytes(b'{"line": 1}\n')
        for path in (held, clean):
            path.chmod(0o444)
        result = self.run_main([held], apply=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAILED %s" % held, result.stderr)
        self.assertIn(self.TOKEN.encode(), held.read_bytes())
        result = self.run_main([clean], apply=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def apply_through(self, path: Path, wrap) -> tuple[int, str]:
        """Run `main` in apply mode on `path`, each open handle passed through `wrap`."""
        real_open = open
        env = {"S4S_PATTERNS": self.PATTERN, "S4S_PAIRS": "", "S4S_MASK_SETTLE_MIN": "10", "S4S_APPLY": "1"}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "stdin", io.StringIO("%s\n" % path)), \
                mock.patch.object(self.mod, "open", lambda *a, **k: wrap(real_open(*a, **k)), create=True), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = self.mod.main()
        return code, err.getvalue()

    def test_a_half_written_file_gets_its_original_bytes_back(self):
        path = self.log("old.jsonl", self.SETTLED)
        # The token first, so half the masked bytes differ from the original.
        original = ("%s and the rest of the line\n" % self.TOKEN).encode()
        path.write_bytes(original)
        os.utime(path, (time.time() - self.SETTLED,) * 2)

        class HalfWay:
            """The disk fills after half the masked bytes land."""

            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                self.handle.close()

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def write(self, data):
                if "+" in self.handle.mode:
                    self.handle.write(data[: len(data) // 2])
                    self.handle.flush()
                    raise OSError(errno.ENOSPC, "No space left on device")
                return self.handle.write(data)

        code, err = self.apply_through(path, HalfWay)
        self.assertEqual(code, 1, err)
        self.assertEqual(path.read_bytes(), original, "a half-masked file was left in place")
        self.assertIn("original restored", err)

    def half_way_then(self, after_close):
        """A handle that fills the disk halfway through the masked write,
        then runs `after_close` once it is closed, before recovery."""

        class HalfWay:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                self.handle.close()
                if "+" in self.handle.mode:
                    after_close(Path(self.handle.name))

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def write(self, data):
                if "+" in self.handle.mode:
                    self.handle.write(data[: len(data) // 2])
                    self.handle.flush()
                    raise OSError(errno.ENOSPC, "No space left on device")
                return self.handle.write(data)

        return HalfWay

    def token_first(self) -> Path:
        path = self.log("old.jsonl", self.SETTLED)
        path.write_bytes(("%s and the rest of the line\n" % self.TOKEN).encode())
        os.utime(path, (time.time() - self.SETTLED,) * 2)
        return path

    def test_recovery_keeps_an_append_that_landed_after_the_failed_write(self):
        path = self.token_first()
        event = b'{"line": 2, "text": "written by the live session"}\n'

        def session_appends(target):
            with open(target, "ab") as f:
                f.write(event)

        code, err = self.apply_through(path, self.half_way_then(session_appends))
        self.assertEqual(code, 1, err)
        self.assertTrue(path.read_bytes().endswith(event), "recovery dropped the appended event")
        self.assertIn("changed after the failed write", err)

    def test_recovery_leaves_a_replacement_file_alone(self):
        path = self.token_first()
        rotated = b'{"line": 1, "text": "a new log after rotation"}\n'

        def rotate(target):
            fresh = target.with_suffix(".new")
            fresh.write_bytes(rotated)
            fresh.replace(target)

        code, err = self.apply_through(path, self.half_way_then(rotate))
        self.assertEqual(code, 1, err)
        self.assertEqual(path.read_bytes(), rotated, "recovery wrote into a file it never read")
        self.assertIn("replaced after the failed write", err)

    def test_recovery_keeps_an_append_that_lands_while_it_restores(self):
        path = self.token_first()
        event = b'{"line": 2, "text": "written by the live session"}\n'
        real_fdopen = os.fdopen

        class AppendOnSeek:
            """The session appends just as recovery starts its write."""

            def __init__(self, raw):
                self.raw = raw

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self.raw.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(self.raw, name)

            def seek(self, *a):
                with open(path, "ab") as f:
                    f.write(event)
                return self.raw.seek(*a)

        with mock.patch.object(self.mod.os, "fdopen", lambda *a, **k: AppendOnSeek(real_fdopen(*a, **k))):
            code, err = self.apply_through(path, self.half_way_then(lambda _target: None))
        self.assertEqual(code, 1, err)
        self.assertTrue(path.read_bytes().endswith(event), "recovery cut the appended event")

    def test_the_cut_short_shape(self):
        cut = self.mod.cut_short
        data, new = b"SECRETSECRET tail", b"<m> tail"
        self.assertTrue(cut(b"<m>RETSECRET tail", data, new), "a prefix of the masked bytes, then the original")
        self.assertTrue(cut(b"<m> tailCRET tail", data, new), "all masked bytes, no truncate")
        self.assertFalse(cut(b"<m>RETSECRET tail+event", data, new), "an append since")
        self.assertFalse(cut(b"<m>RETSECRXT tail", data, new), "a change past the written prefix")
        longer = b"<masked:$NAME> tail"
        self.assertFalse(cut(b"<masked:$NAME> ta", b"abc tail", longer),
                         "a longer write grew the file; restoring it needs a truncate")

    def test_a_rewrite_that_landed_is_not_undone(self):
        path = self.log("old.jsonl", self.SETTLED)

        class CloseFails:
            """Every masked byte lands; the close then reports an error."""

            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                self.handle.close()
                if "+" in self.handle.mode:
                    raise OSError(errno.EIO, "Input/output error")

            def __getattr__(self, name):
                return getattr(self.handle, name)

        code, err = self.apply_through(path, CloseFails)
        self.assertEqual(code, 1, err)
        self.assertNotIn(self.TOKEN.encode(), path.read_bytes(), "the restore put the secret back")
        self.assertIn("the masked bytes landed", err)

    def test_a_read_only_volume_fails_only_when_it_holds_a_match(self):
        held = self.log("held.jsonl", self.SETTLED)
        clean = self.root / "clean.jsonl"
        clean.write_bytes(b'{"line": 1}\n')

        def read_only_volume(handle):
            if "+" in handle.mode:
                handle.close()
                raise OSError(errno.EROFS, "Read-only file system")
            return handle

        code, err = self.apply_through(held, read_only_volume)
        self.assertEqual(code, 1, err)
        self.assertIn("Read-only file system", err)
        self.assertIn(self.TOKEN.encode(), held.read_bytes())
        self.assertEqual(self.apply_through(clean, read_only_volume)[0], 0)

    def test_a_symlink_masks_its_target_and_stays_a_link(self):
        target = self.log("target.jsonl", self.SETTLED)
        link = self.root / "link.jsonl"
        link.symlink_to(target)
        result = self.run_main([link], apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(link.is_symlink())
        self.assertNotIn(self.TOKEN.encode(), target.read_bytes())

    def test_a_dry_run_reports_busy_files_and_writes_nothing(self):
        settled = self.log("old.jsonl", self.SETTLED)
        recent = self.log("new.jsonl", 60)
        result = self.run_main([settled, recent], apply=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("  busy %s" % recent, result.stdout)
        self.assertIn("in 1 file(s); 1 busy (dry run", result.stdout)
        self.assertIn(self.TOKEN.encode(), settled.read_bytes())


if __name__ == "__main__":
    unittest.main()
