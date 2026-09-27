"""opentelemetry-demo.sh takes its export destination and paths from config.

The script runs from a copy in a temporary folder with stub kind, kubectl and
helm on PATH, so nothing reaches a cluster and no pid or log file lands in
this checkout.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "opentelemetry-demo.sh"
CONFIG_VARS = ("OTLP_EXPORT_URL", "OTLP_EXPORT_AUTH", "OTLP_EXPORT_AUTH_HEADER", "AURA_CHART_DIR",
               "AURA_VALUES_FILE", "OTEL_DEMO_REPO")

HEADER_SET = "--set-string opentelemetry-collector.config.exporters.otlphttp/export.headers."

STUB = """\
#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB_LOG"
case "$(basename "$0") $1 $2" in
  "kind get clusters") echo otel-demo ;;
esac
exit 0
"""


class DemoConfig(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / "local-opentelemetry-demo"
        self.folder.mkdir()
        shutil.copy(ENTRYPOINT, self.folder / ENTRYPOINT.name)
        shutil.copy(FOLDER / "collector-values.yaml", self.folder / "collector-values.yaml")
        shutil.copytree(FOLDER.parent / "lib", self.root / "lib")
        stubs = self.root / "bin"
        stubs.mkdir()
        for tool in ("kind", "kubectl", "helm"):
            (stubs / tool).write_text(STUB, encoding="utf-8")
            (stubs / tool).chmod(0o755)
        self.log = self.root / "calls.log"
        self.env = {k: v for k, v in os.environ.items() if k not in CONFIG_VARS}
        self.env.update(
            PATH=f"{stubs}:{os.environ['PATH']}",
            STUB_LOG=str(self.log),
            SYSTEM_TOOLS_CONFIG=str(self.root / "config"),
        )

    def run_verb(self, *args, expect):
        done = subprocess.run(
            ["/bin/sh", str(self.folder / ENTRYPOINT.name), *args],
            capture_output=True, text=True, input="", env=self.env, timeout=60,
        )
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done

    def test_help_exits_zero(self):
        done = self.run_verb("help", expect=0)
        self.assertIn("OTLP_EXPORT_URL", done.stdout)

    def test_no_args_prints_help_and_fails(self):
        done = self.run_verb(expect=1)
        self.assertIn("opentelemetry-demo.sh", done.stderr)

    def test_upgrade_builds_the_export_secret_from_the_config_env(self):
        env_dir = self.root / "config" / "opentelemetry-demo"
        env_dir.mkdir(parents=True)
        (env_dir / ".env").write_text(
            "OTLP_EXPORT_URL=https://otlp.example.invalid/otel\n"
            "OTLP_EXPORT_AUTH='Bearer token-one'\n", encoding="utf-8")
        self.run_verb("upgrade", expect=0)
        calls = self.log.read_text(encoding="utf-8")
        self.assertIn("create secret generic otlp-export", calls)
        self.assertIn("--from-literal=url=https://otlp.example.invalid/otel", calls)
        self.assertIn("--from-literal=auth=Bearer token-one", calls)
        self.assertIn(f"-f {self.folder / 'collector-values.yaml'}", calls)
        self.assertIn("--from-literal=auth-header=Authorization", calls)
        self.assertIn(HEADER_SET + "Authorization=${env:OTLP_EXPORT_AUTH}", calls)

    def test_header_name_reaches_the_secret_and_the_helm_values(self):
        self.env.update(OTLP_EXPORT_URL="https://otlp.example.invalid/otel",
                        OTLP_EXPORT_AUTH="key-one", OTLP_EXPORT_AUTH_HEADER="apikey")
        self.run_verb("upgrade", expect=0)
        calls = self.log.read_text(encoding="utf-8")
        self.assertIn("--from-literal=auth-header=apikey", calls)
        helm = [c for c in calls.splitlines() if c.startswith("helm ") and " upgrade " in c]
        self.assertEqual(len(helm), 1, calls)
        self.assertIn(HEADER_SET + "apikey=${env:OTLP_EXPORT_AUTH}", helm[0])
        self.assertNotIn("headers.Authorization", helm[0])

    def test_values_file_hardcodes_no_auth_header(self):
        values = (FOLDER / "collector-values.yaml").read_text(encoding="utf-8")
        self.assertIn("key: auth-header", values)
        self.assertNotIn('Authorization: "${env:', values)

    def test_a_header_name_that_is_not_a_token_is_refused(self):
        self.env.update(OTLP_EXPORT_URL="https://otlp.example.invalid/otel",
                        OTLP_EXPORT_AUTH="k", OTLP_EXPORT_AUTH_HEADER="api.key")
        done = self.run_verb("upgrade", expect=1)
        self.assertIn("OTLP_EXPORT_AUTH_HEADER", done.stderr)
        self.assertNotIn("create secret", self.log.read_text(encoding="utf-8"))

    def test_missing_export_url_names_variable_and_remedies(self):
        self.env["OTLP_EXPORT_AUTH"] = "k"
        done = self.run_verb("upgrade", expect=1)
        self.assertIn("OTLP_EXPORT_URL is not set", done.stderr)
        self.assertIn("local-opentelemetry-demo/.env.example", done.stderr)
        self.assertNotIn("create secret", self.log.read_text(encoding="utf-8"))

    def test_placeholder_export_values_are_rejected(self):
        self.env.update(OTLP_EXPORT_URL="https://otlp.example.test/change-me", OTLP_EXPORT_AUTH="k")
        done = self.run_verb("upgrade", expect=1)
        self.assertIn("placeholder", done.stderr)

    def test_mcp_install_needs_the_aura_paths(self):
        self.env["OPENAI_API_KEY"] = "k"
        done = self.run_verb("mcp-install", expect=1)
        self.assertIn("AURA_CHART_DIR is not set", done.stderr)

    def test_repo_start_needs_the_demo_checkout(self):
        done = self.run_verb("repo-start", expect=1)
        self.assertIn("OTEL_DEMO_REPO is not set", done.stderr)


if __name__ == "__main__":
    unittest.main()
