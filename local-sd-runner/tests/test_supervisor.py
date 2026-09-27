"""Provider supervision keeps power assertions best-effort."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_runner import supervisor


class Process:
    def __init__(self, *, pid: int = 101, returncode: int = 0, timeout: bool = False):
        self.pid = pid
        self.returncode = returncode
        self.timeout = timeout
        self.terminated = False

    def communicate(self, _input: bytes) -> None:
        return None

    def wait(self, timeout: int) -> int:
        if self.timeout:
            raise subprocess.TimeoutExpired("caffeinate", timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True


class ProviderPowerAssertion(unittest.TestCase):
    def test_caffeinate_timeout_does_not_replace_provider_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").mkdir()
            command = {
                "action": "provider",
                "argv": ["provider"],
                "environment": {},
                "prompt": "review",
                "request": {"run": {"work_path": str(root)}},
            }
            provider = Process()
            caffeinate = Process(timeout=True)
            output = io.StringIO()
            with patch.object(supervisor.sys, "platform", "darwin"), \
                    patch.object(supervisor.sys, "stdin", [json.dumps(command)]), \
                    patch.object(supervisor.subprocess, "Popen", side_effect=[provider, caffeinate]), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(supervisor.main(), 0)
            self.assertEqual(json.loads(output.getvalue())["ok"], True)
            self.assertTrue(caffeinate.terminated)


if __name__ == "__main__":
    unittest.main()
