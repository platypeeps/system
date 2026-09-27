"""mock-mcp.sh takes its checkout and default scenario from config."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "mock-mcp.sh"
CONFIG_VARS = ("MOCK_MCP_SRC", "MOCK_SCENARIO", "MOCK_MCP_NAME", "MOCK_MCP_IMAGE", "MOCK_MCP_PORT")


class MockMcpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config"
        # Stub docker: images exist, nothing is running, run succeeds.
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "docker.log"
        stub = self.bin / "docker"
        stub.write_text(f'#!/bin/sh\necho "$*" >> "{self.log}"\nexit 0\n')
        stub.chmod(0o755)

    def run_script(self, *args, env=None):
        base = {k: v for k, v in os.environ.items() if k not in CONFIG_VARS}
        base["SYSTEM_TOOLS_CONFIG"] = str(self.config)
        base["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        base.update(env or {})
        return subprocess.run(["sh", str(SCRIPT), *args], env=base,
                              capture_output=True, text=True)

    def checkout(self, *scenarios):
        src = self.root / "src"
        scen = src / "mock-scenarios" / "scenarios"
        scen.mkdir(parents=True)
        for name in scenarios:
            (scen / f"{name}.yaml").write_text("{}\n")
        return src

    def test_help_exits_zero(self):
        result = self.run_script("help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("MOCK_MCP_SRC", result.stdout)

    def test_no_args_prints_usage_and_fails(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage:", result.stderr)

    def test_missing_src_names_variable_and_remedies(self):
        result = self.run_script("build")
        self.assertEqual(result.returncode, 1)
        self.assertIn("MOCK_MCP_SRC is not set", result.stderr)
        self.assertIn("local-mock-mcp/.env.example", result.stderr)

    def test_scenarios_listed_from_config_env(self):
        src = self.checkout("alpha", "beta")
        (self.config / "mock-mcp").mkdir(parents=True)
        (self.config / "mock-mcp" / ".env").write_text(f"MOCK_MCP_SRC={src}\n")
        result = self.run_script("scenarios")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["alpha", "beta"])

    def test_start_without_scenario_needs_mock_scenario(self):
        src = self.checkout("alpha")
        result = self.run_script("start", env={"MOCK_MCP_SRC": str(src)})
        self.assertEqual(result.returncode, 1)
        self.assertIn("MOCK_SCENARIO is not set", result.stderr)

    def test_name_and_image_from_config(self):
        src = self.checkout("alpha")
        result = self.run_script("build", env={"MOCK_MCP_SRC": str(src),
                                               "MOCK_MCP_IMAGE": "bench-image"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"build -t bench-image {src}", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
