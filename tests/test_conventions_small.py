"""Small convention gaps that each need one fact pinned (sd:3555).

Each case runs the real script in a scratch HOME and config folder, with stub
tools on PATH, so nothing real is read or started. Stdlib and git only, so it
runs in the `tests/ci-native.sh` preflight.
"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run(args, env, cwd):
    return subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60)


class Scratch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = self.root / "config"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.home),
                    "SYSTEM_TOOLS_CONFIG": str(self.config)}

    def stub(self, name, body="exit 0"):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)


class GraphitiPassword(Scratch):
    def write_env(self, password):
        folder = self.config / "graphiti-mcp"
        folder.mkdir(parents=True)
        (folder / ".env").write_text(f"FALKORDB_PASSWORD={password}\n")

    def start(self):
        self.stub("docker")
        return run(["sh", str(REPO_ROOT / "local-graphiti-mcp/graphiti-mcp.sh"), "start"],
                   self.env, self.root)

    def test_the_example_carries_no_well_known_password(self):
        text = (REPO_ROOT / "local-graphiti-mcp/.env.example").read_text()
        self.assertNotIn("verysecure", text)
        self.assertIn("FALKORDB_PASSWORD=change-me", text)

    def test_start_refuses_the_placeholder_password(self):
        self.write_env("change-me")
        done = self.start()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("FALKORDB_PASSWORD", done.stderr)

    def test_start_accepts_a_real_password(self):
        self.write_env("not-the-placeholder")
        done = self.start()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class LlamaDir(Scratch):
    def test_the_checkout_is_overridable(self):
        checkout = self.root / "llama.cpp"
        checkout.mkdir()
        self.stub("cmake", f'pwd -P >> "{self.root}/cmake.cwd"')
        env = dict(self.env, LLAMA_DIR=str(checkout))
        done = run(["sh", str(REPO_ROOT / "local-llama-cpp/llama-cpp.sh"), "build"], env, self.root)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        cwds = (self.root / "cmake.cwd").read_text().split()
        self.assertEqual(set(cwds), {os.path.realpath(checkout)})


class Entrypoints(Scratch):
    def test_the_webhook_signer_is_executable(self):
        done = subprocess.run(["git", "ls-files", "-s", "mezmo-webhook/mezmo-webhook.js"],
                              cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertTrue(done.stdout.startswith("100755 "), done.stdout)

    def test_every_tracked_file_with_a_shebang_is_executable(self):
        # A5 F12: three helpers carried a shebang at 100644, so `./<file>` exited 126.
        done = subprocess.run(["git", "ls-files", "-s", "-z"], cwd=REPO_ROOT,
                              capture_output=True, text=True, check=True)
        plain = []
        for record in filter(None, done.stdout.split("\0")):
            meta, path = record.split("\t", 1)
            if meta.startswith("100644 "):
                with open(REPO_ROOT / path, "rb") as file:
                    if file.read(2) == b"#!":
                        plain.append(path)
        self.assertEqual(plain, [], "git update-index --chmod=+x, or drop the shebang")

    def test_the_langchain_smoke_test_answers_help_before_importing_anything(self):
        for form in ("help", "-h", "--help"):
            with self.subTest(form=form):
                done = run(["python3", "-I", str(REPO_ROOT / "mezmo-langchain/main.py"), form],
                           self.env, self.root)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn("usage", done.stdout.lower())


class ConventionText(unittest.TestCase):
    def test_convention_4_names_state(self):
        text = (REPO_ROOT / "CLAUDE.md").read_text()
        line = next(l for l in text.splitlines() if l.startswith("4. **Data and log folders**"))
        self.assertTrue(re.search(r"`state/`", line), line)


if __name__ == "__main__":
    unittest.main()
