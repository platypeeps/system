"""Ports follow the same precedence as the credentials (sd:3648).

An exported value wins over `<config>/clickhouse/.env`, which wins over the
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
ENTRYPOINT = FOLDER / "clickhouse.sh"

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
        folder = self.root / "local-clickhouse"
        folder.mkdir()
        shutil.copy(ENTRYPOINT, folder / ENTRYPOINT.name)
        shutil.copytree(FOLDER.parent / "lib", self.root / "lib")
        stubs = self.root / "bin"
        stubs.mkdir()
        (stubs / "docker").write_text(DOCKER, encoding="utf-8")
        (stubs / "docker").chmod(0o755)
        self.config = self.root / "config"
        (self.config / "clickhouse").mkdir(parents=True)
        self.log = self.root / "docker.log"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("CLICKHOUSE_")}
        self.env.update(
            PATH=f"{stubs}:{os.environ['PATH']}",
            STUB_LOG=str(self.log),
            SYSTEM_TOOLS_CONFIG=str(self.config),
        )
        self.script = folder / ENTRYPOINT.name

    def start(self, dotenv=None, **exported):
        if dotenv is not None:
            (self.config / "clickhouse" / ".env").write_text(dotenv, encoding="utf-8")
        env = dict(self.env, **exported)
        result = subprocess.run(["sh", str(self.script), "start"], env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.log.read_text(encoding="utf-8")

    def test_defaults(self):
        log = self.start()
        self.assertIn("-p 127.0.0.1:8123:8123", log)
        self.assertIn("-p 127.0.0.1:9000:9000", log)
        self.assertIn("-p 127.0.0.1:8002:8000", log)

    def test_env_file_ports_win_over_defaults(self):
        log = self.start(dotenv="CLICKHOUSE_HTTP_PORT=18123\n"
                                "CLICKHOUSE_NATIVE_PORT=19000\n"
                                "CLICKHOUSE_MCP_PORT=18002\n")
        self.assertIn("-p 127.0.0.1:18123:8123", log)
        self.assertIn("-p 127.0.0.1:19000:9000", log)
        self.assertIn("-p 127.0.0.1:18002:8000", log)
        # The MCP container reaches the server on the same HTTP port.
        self.assertIn("CLICKHOUSE_PORT=18123", log)

    def test_exported_ports_win_over_env_file(self):
        log = self.start(dotenv="CLICKHOUSE_HTTP_PORT=18123\n"
                                "CLICKHOUSE_NATIVE_PORT=19000\n"
                                "CLICKHOUSE_MCP_PORT=18002\n",
                         CLICKHOUSE_HTTP_PORT="28123",
                         CLICKHOUSE_NATIVE_PORT="29000",
                         CLICKHOUSE_MCP_PORT="28002")
        self.assertIn("-p 127.0.0.1:28123:8123", log)
        self.assertIn("-p 127.0.0.1:29000:9000", log)
        self.assertIn("-p 127.0.0.1:28002:8000", log)


if __name__ == "__main__":
    unittest.main()
