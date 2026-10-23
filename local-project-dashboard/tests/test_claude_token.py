"""Every dashboard `claude` line maps env.sh's CUSTOM_CLAUDE_CODE_OAUTH_TOKEN (sd:5791).

env.sh exports the setup token as CUSTOM_CLAUDE_CODE_OAUTH_TOKEN, so a day-to-day claude keeps its own login (sd:5786).
Each line starts with `claude_env.SHELL_ENV`, which exports it as CLAUDE_CODE_OAUTH_TOKEN when that is unset, and
names no path outside the package, so an installed copy maps it too.
`claude` is a double named by `CLAUDE_BIN` that prints only whether it saw the token; `HOME` is a disposable folder,
so the operator's own env.sh is never sourced.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sd_dashboard
from sd_dashboard import chat, email_draft, publish_drain, realm_sweep

PACKAGE = Path(sd_dashboard.__file__).resolve().parent

DOUBLE = """#!/bin/sh
case "${CLAUDE_CODE_OAUTH_TOKEN-unset}" in unset) echo unset ;; tok-custom) echo same ;; *) echo other ;; esac
"""


class TheToken(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.home = root / "home"
        (self.home / ".config" / "shell").mkdir(parents=True)
        self.claude = root / "claude"
        self.claude.write_text(DOUBLE)
        self.claude.chmod(0o755)

    def lines(self):
        """Each caller's `sh -c` argv, after the line itself."""
        return {"email_draft": (email_draft.CLAUDE, []), "chat": (chat.line(), []),
                "publish_drain": (publish_drain.CLAUDE, ["sh", "q", "c", "r", "a"]),
                "realm_sweep": (realm_sweep.WITH_TOKEN, ["claude-run", str(self.claude)])}

    def seen(self, **extra):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "CLAUDE_BIN": str(self.claude), **extra}
        out = {}
        for name, (line, argv) in self.lines().items():
            done = subprocess.run(["sh", "-c", line, *argv], env=env, input="", capture_output=True, text=True,
                                  timeout=20, check=False)
            out[name] = done.stdout.strip() or done.stderr.strip()
        return out

    def test_env_sh_custom_token_reaches_claude_as_claude_code_oauth_token(self):
        (self.home / ".config" / "shell" / "env.sh").write_text("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\n")
        self.assertEqual(self.seen(), dict.fromkeys(self.lines(), "same"))

    def test_a_token_already_set_wins(self):
        (self.home / ".config" / "shell" / "env.sh").write_text("export CUSTOM_CLAUDE_CODE_OAUTH_TOKEN=tok-custom\n")
        self.assertEqual(self.seen(CLAUDE_CODE_OAUTH_TOKEN="tok-own"), dict.fromkeys(self.lines(), "other"))

    def test_no_token_anywhere_leaves_it_unset(self):
        self.assertEqual(self.seen(), dict.fromkeys(self.lines(), "unset"))


class NoPathOutsideThePackage(unittest.TestCase):
    """The mapping is a constant inside sd_dashboard, so a copy outside the checkout still maps the token."""

    def test_no_claude_line_names_a_path_outside_the_package(self):
        lines = {"email_draft": email_draft.CLAUDE, "chat": chat.line(), "publish_drain": publish_drain.CLAUDE,
                 "realm_sweep": realm_sweep.WITH_TOKEN}
        for name, line in lines.items():
            with self.subTest(caller=name):
                self.assertNotIn(str(PACKAGE.parent.parent), line)
                self.assertNotIn("st_shell_env", line)

    def test_publish_drain_imports_the_constant_when_run_as_its_own_script(self):
        # Its runner starts it by path, outside the package; a failed import would end every drain unseen.
        env = {"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir()}
        done = subprocess.run([sys.executable, str(PACKAGE / "publish_drain.py")], env=env, capture_output=True,
                              text=True, timeout=20, check=False)
        self.assertEqual(done.stderr.strip(), "usage: publish_drain.py run <state file> <lock descriptor>")

    def test_no_module_reaches_lib_config_sh(self):
        found = [path.name for path in sorted(PACKAGE.rglob("*.py"))
                 if re.search(r"\bconfig\.sh\b|\bst_shell_env\b", path.read_text(encoding="utf-8"))]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
