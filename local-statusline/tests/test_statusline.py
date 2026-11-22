"""Tests for statusline.sh render, without bun, claude-hud or a real sysctl.

Each case builds a throwaway config folder holding a claude-hud version
folder, a stub `bun` (named by BUN_BIN) that ignores its arguments and prints
two fixed HUD lines, and stubs `sysctl` and `ps` first on PATH that answer
`sysctl -n hw.logicalcpu` and `ps -A -o %cpu=` from the environment. HOME points into
the same temporary folder, so the `~/.claude.json` fallback finds nothing.
The suite therefore runs the same on Linux as on a Mac.
"""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

FOLDER = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = FOLDER / "statusline.sh"

BAR = "│"
HUD_LINE_1 = "[Model] %s git:(main)" % BAR
HUD_LINE_2 = "Context 10%% %s Cache 5m" % BAR
SEP = " %s " % BAR
YELLOW = "\033[38;5;214m"
RED = "\033[1;38;5;196m"

BUN_STUB = """#!/bin/sh
cat > /dev/null
printf '%%s\\n' '%s'
printf '%%s\\n' '%s'
[ -z "${FAKE_HUD_LINE3:-}" ] || printf '%%s\\n' "$FAKE_HUD_LINE3"
""" % (HUD_LINE_1, HUD_LINE_2)

# hw.logicalcpu prints a count; ps prints one %cpu value per process.
SYSCTL_STUB = """#!/bin/sh
[ "${FAKE_SYSCTL_FAIL:-0}" = 1 ] && exit 1
printf '%s\\n' "$FAKE_NCPU"
"""
PS_STUB = """#!/bin/sh
for v in $FAKE_PCPU; do printf '%s\\n' "$v"; done
"""


def write_exec(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class StatuslineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = self.root / "claude"
        hud = self.config / "plugins/cache/x/claude-hud/1.0.0/src"
        hud.mkdir(parents=True)
        (hud / "index.ts").write_text("// unused by the stub bun\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.bun = self.bin / "bun"
        write_exec(self.bun, BUN_STUB)
        write_exec(self.bin / "sysctl", SYSCTL_STUB)
        write_exec(self.bin / "ps", PS_STUB)

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, **extra):
        env = {
            "HOME": str(self.home),
            "PATH": "%s:%s" % (self.bin, os.environ.get("PATH", "/usr/bin:/bin")),
            "CLAUDE_CONFIG_DIR": str(self.config),
            "BUN_BIN": str(self.bun),
            "COLUMNS": "100",
            "FAKE_PCPU": "100.0 60.0",
            "FAKE_NCPU": "8",
        }
        env.update(extra)
        return env

    def run_script(self, *args, stdin="{}", **extra):
        return subprocess.run(
            ["sh", str(SCRIPT), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env(**extra),
            timeout=30,
        )

    def render(self, **extra):
        proc = self.run_script("render", **extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 2, proc.stdout)
        return lines

    def test_cpu_below_70_is_uncoloured(self):
        line1, line2 = self.render(FAKE_PCPU="100.0 60.0", FAKE_NCPU="8")
        self.assertEqual(line1, HUD_LINE_1 + SEP + "cpu 20%")
        self.assertEqual(line2, HUD_LINE_2)

    def test_cpu_at_70_is_yellow(self):
        line1, _ = self.render(FAKE_PCPU="400 160", FAKE_NCPU="8")
        self.assertTrue(line1.endswith(SEP + YELLOW + "cpu 70%[0m"), repr(line1))

    def test_cpu_at_90_is_red(self):
        line1, _ = self.render(FAKE_PCPU="700 20", FAKE_NCPU="8")
        self.assertTrue(line1.endswith(SEP + RED + "cpu 90%[0m"), repr(line1))

    def test_cpu_caps_at_100(self):
        # ps %cpu is a decaying average and can sum past the core count.
        line1, _ = self.render(FAKE_PCPU="900", FAKE_NCPU="8")
        self.assertTrue(line1.endswith(SEP + RED + "cpu 100%[0m"), repr(line1))

    def test_failing_sysctl_leaves_cpu_out(self):
        proc = self.run_script("render", FAKE_SYSCTL_FAIL="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("cpu", proc.stdout)
        self.assertEqual(proc.stdout.splitlines(), [HUD_LINE_1, HUD_LINE_2])

    def test_email_joins_line_one_before_cpu(self):
        (self.root / "claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "someone@example.test"}})
        )
        line1, line2 = self.render()
        self.assertEqual(
            line1,
            HUD_LINE_1 + SEP + "someone@example.test" + SEP + "cpu 20%",
        )
        self.assertEqual(line2, HUD_LINE_2)

    def test_mapped_email_shows_its_label(self):
        (self.root / "claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "b@example.test"}})
        )
        conf = self.home / ".config/system/statusline"
        conf.mkdir(parents=True)
        (conf / ".env").write_text(
            'STATUSLINE_ACCOUNT_LABELS="a@example.test=one b@example.test=two"\n'
        )
        line1, _ = self.render()
        self.assertEqual(line1, HUD_LINE_1 + SEP + "two" + SEP + "cpu 20%")
        # An unmapped login keeps its full email.
        line1, _ = self.render(STATUSLINE_ACCOUNT_LABELS="a@example.test=one")
        self.assertIn(SEP + "b@example.test" + SEP, line1)

    def test_login_ends_the_tokens_line(self):
        (self.root / "claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "b@example.test"}})
        )
        tokens = "\033[2mTokens 1.2M (in: 1k, out: 2k)\033[0m"
        proc = self.run_script(
            "render",
            FAKE_HUD_LINE3=tokens,
            STATUSLINE_ACCOUNT_LABELS="b@example.test=two",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[0], HUD_LINE_1 + SEP + "cpu 20%")
        self.assertEqual(lines[2], tokens + SEP + "two")

    def test_missing_claude_hud_exits_1(self):
        proc = self.run_script(
            "render", CLAUDE_CONFIG_DIR=str(self.root / "empty")
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claude-hud not found", proc.stderr)

    def test_help_exits_0(self):
        proc = self.run_script("help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage: statusline.sh", proc.stdout)

    def test_no_args_exits_1(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("usage:", proc.stderr)


if __name__ == "__main__":
    unittest.main()
