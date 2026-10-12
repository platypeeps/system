"""An exported value beats the config .env, and the .env beats a script default (sd:3553).

`st_source_env` holds the one rule. The docker wrappers below lost their own
snapshots to it, so each runs from a scratch copy of its folder beside a copy
of lib/config.sh, with a stub `docker` that logs its arguments. Nothing real
is started and nothing is written into the checkout. Stdlib only, so it runs
in the `tests/ci-native.sh` preflight.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: folder, entrypoint, tool config folder, the variable the .env and the shell both set
WRAPPERS = (
    ("local-postgres", "postgres.sh", "postgres", "POSTGRES_PASSWORD"),
    ("local-clickhouse", "clickhouse.sh", "clickhouse", "CLICKHOUSE_PASSWORD"),
    ("local-falkordb", "falkordb.sh", "falkordb", "FALKORDB_PASSWORD"),
)


class WrapperPrecedence(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "lib").mkdir()
        shutil.copy(REPO_ROOT / "lib" / "config.sh", self.root / "lib" / "config.sh")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "docker.log"
        stub = self.bin / "docker"
        stub.write_text(f'#!/bin/sh\necho "$*" >> "{self.log}"\n')
        stub.chmod(0o755)
        self.config = self.root / "config"

    def start(self, folder, script, tool, variable, exported):
        shutil.copytree(REPO_ROOT / folder, self.root / folder, ignore=shutil.ignore_patterns("tests", "storage", "logs"))
        (self.config / tool).mkdir(parents=True, exist_ok=True)
        (self.config / tool / ".env").write_text(f"{variable}=from-file\n")
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.root),
               "SYSTEM_TOOLS_CONFIG": str(self.config)}
        if exported is not None:
            env[variable] = exported
        done = subprocess.run(["sh", str(self.root / folder / script), "start"], env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return self.log.read_text()

    def test_the_file_fills_what_the_environment_leaves_unset(self):
        for folder, script, tool, variable in WRAPPERS:
            with self.subTest(folder=folder):
                self.assertIn("from-file", self.start(folder, script, tool, variable, None))
                self.log.unlink()

    def test_an_exported_value_beats_the_file(self):
        for folder, script, tool, variable in WRAPPERS:
            with self.subTest(folder=folder):
                log = self.start(folder, script, tool, variable, "from-shell")
                self.assertIn("from-shell", log)
                self.assertNotIn("from-file", log)
                self.log.unlink()


if __name__ == "__main__":
    unittest.main()
