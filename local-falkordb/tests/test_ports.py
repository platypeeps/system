"""Ports follow the same precedence as the credentials (sd:3648).

An exported value wins over `<config>/falkordb/.env`, which wins over the
baked-in default. The ports were once resolved before the file was read, so a
port set only in `.env` never reached `docker run -p`.

The script runs from a copy in a temporary folder, with `docker` a stub that
records its arguments; nothing starts a container.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "falkordb.sh"

DOCKER = """\
#!/bin/sh
echo "$*" >> "$STUB_LOG"
exit 0
"""


class PortsCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        folder = self.root / "local-falkordb"
        folder.mkdir()
        shutil.copy(ENTRYPOINT, folder / ENTRYPOINT.name)
        shutil.copytree(FOLDER.parent / "lib", self.root / "lib")
        stubs = self.root / "bin"
        stubs.mkdir()
        (stubs / "docker").write_text(DOCKER, encoding="utf-8")
        (stubs / "docker").chmod(0o755)
        self.config = self.root / "config"
        (self.config / "falkordb").mkdir(parents=True)
        self.log = self.root / "docker.log"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("FALKORDB_")}
        self.env.update(
            PATH=f"{stubs}:{os.environ['PATH']}",
            STUB_LOG=str(self.log),
            SYSTEM_TOOLS_CONFIG=str(self.config),
        )
        self.script = folder / ENTRYPOINT.name

    def start(self, dotenv=None, **exported):
        if dotenv is not None:
            (self.config / "falkordb" / ".env").write_text(dotenv, encoding="utf-8")
        env = dict(self.env, **exported)
        result = subprocess.run(["sh", str(self.script), "start"], env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.log.read_text(encoding="utf-8")

    def test_defaults(self):
        log = self.start()
        self.assertIn("-p 127.0.0.1:6380:6379", log)
        self.assertIn("-p 127.0.0.1:3003:3000", log)

    def test_env_file_ports_win_over_defaults(self):
        log = self.start(dotenv="FALKORDB_PORT=16380\nFALKORDB_UI_PORT=13003\n")
        self.assertIn("-p 127.0.0.1:16380:6379", log)
        self.assertIn("-p 127.0.0.1:13003:3000", log)

    def test_exported_ports_win_over_env_file(self):
        log = self.start(dotenv="FALKORDB_PORT=16380\nFALKORDB_UI_PORT=13003\n",
                         FALKORDB_PORT="26380", FALKORDB_UI_PORT="23003")
        self.assertIn("-p 127.0.0.1:26380:6379", log)
        self.assertIn("-p 127.0.0.1:23003:3000", log)


if __name__ == "__main__":
    unittest.main()
