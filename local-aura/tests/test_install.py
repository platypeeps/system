"""`aura.sh install` and `server-repo` build from the $AURA_REPO checkout.

The checkout is a temporary git repository with a tag; `cargo` is a stub
that logs its arguments and writes a fake `target/release/aura`. `brew` is
a stub that fails, so a call to it fails the test. HOME is a temporary
folder, so the operator's own install is never touched. The script runs
from a copy, so `server-repo` writes its output log there.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "aura.sh"

CARGO = """#!/bin/sh
echo "$PWD | $*" >> "$STUB_LOG"
if [ "$1" = build ]; then
  mkdir -p target/release
  printf '#!/bin/sh\\necho built-aura\\n' > target/release/aura
fi
exit 0
"""

BREW = """#!/bin/sh
echo "brew must not run: $*" >&2
exit 97
"""


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


class FromCheckout(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "local-aura").mkdir()
        self.entrypoint = root / "local-aura" / ENTRYPOINT.name
        shutil.copy(ENTRYPOINT, self.entrypoint)
        shutil.copytree(FOLDER.parent / "lib", root / "lib")
        stubs = root / "bin"
        stubs.mkdir()
        for name, body in (("cargo", CARGO), ("brew", BREW)):
            (stubs / name).write_text(body, encoding="utf-8")
            (stubs / name).chmod(0o755)
        self.repo = root / "aura"
        self.repo.mkdir()
        (self.repo / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("target/\n", encoding="utf-8")
        git(self.repo, "init", "-q")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.test",
            "commit", "-q", "-m", "init")
        git(self.repo, "tag", "v9.9.9-test.1")
        self.commit = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        self.home = root / "home"
        self.home.mkdir()
        self.log = root / "cargo.log"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("OTEL_", "AURA_", "GENAI_TRACES_"))}
        self.env.update(
            PATH=f"{stubs}:{os.environ['PATH']}",
            HOME=str(self.home),
            SYSTEM_TOOLS_CONFIG=str(root / "config"),
            AURA_REPO=str(self.repo), STUB_LOG=str(self.log),
            OPENAI_API_KEY="k", MEZMO_API_KEY="k",
        )

    def run_aura(self, *args):
        return subprocess.run(["/bin/sh", str(self.entrypoint), *args],
                              capture_output=True, text=True, env=self.env,
                              timeout=30)

    def test_install_builds_the_checkout_and_links_the_version(self):
        done = self.run_aura("install")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.log.read_text(encoding="utf-8").splitlines(),
                         [f"{self.repo} | build --release --bin aura"])
        dest = self.home / ".local/opt/aura/v9.9.9-test.1"
        self.assertEqual(os.readlink(self.home / ".local/bin/aura"),
                         str(dest / "aura"))
        self.assertTrue(os.access(dest / "aura", os.X_OK))
        self.assertEqual((dest / "SOURCE").read_text(encoding="utf-8"),
                         f"v9.9.9-test.1 {self.commit}\n")

    def test_install_marks_a_dirty_checkout(self):
        # A dirty build must not overwrite the clean tag's folder.
        (self.repo / "Cargo.toml").write_text("[workspace]\n# x\n", encoding="utf-8")
        done = self.run_aura("install")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.home / ".local/opt/aura/v9.9.9-test.1-dirty/aura").exists())
        self.assertFalse((self.home / ".local/opt/aura/v9.9.9-test.1").exists())

    def test_install_needs_a_checkout(self):
        del self.env["AURA_REPO"]
        done = self.run_aura("install")
        self.assertEqual(done.returncode, 1)
        self.assertIn("AURA_REPO", done.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.home / ".local").exists())

    def test_server_repo_runs_aura_webserver(self):
        # AURA v0.2.18-nightly.16 deprecated the aura-web-server binary.
        done = self.run_aura("server-repo")
        self.assertEqual(done.returncode, 0, done.stderr)
        line = self.log.read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(
            line.startswith(f"{self.repo} | run --bin aura -- webserver --config "),
            line)


if __name__ == "__main__":
    unittest.main()
