"""The hub's Claude and codex sessions export OTel to genai-traces (sd:6944).

On the hub the tooling stage adds the telemetry env block to settings.json and
an [otel] table to codex's config.toml, both at the one endpoint
`genai-traces.sh endpoint` prints; the source wins over another value. A
satellite gets neither. doctor warns when the block is on and genai-traces is
down. `genai-traces.sh` and `codex` are stubs.
"""

import json
import socket
import subprocess
import unittest

from tests.test_claude_settings import SyntheticHome, write_exec
from tests.test_profile_prefs import Case

# `endpoint` prints what the real one prints by default; `status` exits $TRACES_STATUS.
TRACES_STUB = """#!/bin/sh
case "$1" in
  endpoint) echo OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4337
            echo OTEL_EXPORTER_OTLP_PROTOCOL=grpc
            echo PHOENIX_UI=http://127.0.0.1:6016 ;;
  status) exit "${TRACES_STATUS:-0}" ;;
esac
"""
HOST = subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip().lower() \
    or socket.gethostname().split(".")[0].lower()
OTEL_ENV = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4337",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_RESOURCE_ATTRIBUTES": f"service.namespace=sd-workflow,host.name={HOST}",
    "OTEL_LOG_USER_PROMPTS": "0",
    "OTEL_LOG_TOOL_DETAILS": "0",
}
CODEX_OTEL = ('[otel]\nenvironment = "sd-workflow"\n'
              'exporter = { otlp-grpc = { endpoint = "http://127.0.0.1:4337" } }\n'
              'log_user_prompt = false\n')


class HubTelemetry(SyntheticHome):
    """The fixture's personal profile lists the sd agents: it is the hub."""

    def setUp(self):
        super().setUp()
        write_exec(self.repo / "local-genai-traces/genai-traces.sh", TRACES_STUB)
        write_exec(self.stubs / "codex", "#!/bin/sh\nexit 0\n")
        self.codex = self.home / ".codex/config.toml"
        self.codex.parent.mkdir(parents=True)
        self.codex.write_text('model = "gpt-5"\n\n[otel]\nexporter = "none"\n\n[tui]\nnotifications = true\n')

    def test_a_dry_run_names_the_block_and_writes_nothing(self):
        out = self.run_tooling()
        self.assertIn("MISSING settings.json env.CLAUDE_CODE_ENABLE_TELEMETRY=1", out)
        self.assertIn("STALE   codex config.toml [otel]", out)
        self.assertNotIn("CLAUDE_CODE_ENABLE_TELEMETRY", self.settings.read_text())
        self.assertIn('exporter = "none"', self.codex.read_text())

    def test_apply_sets_the_block_at_the_genai_traces_endpoint(self):
        self.run_tooling("--apply")
        env = json.loads(self.settings.read_text())["env"]
        self.assertEqual({key: env.get(key) for key in OTEL_ENV}, OTEL_ENV)
        self.assertEqual(self.codex.read_text(),
                         'model = "gpt-5"\n\n' + CODEX_OTEL + '\n[tui]\nnotifications = true\n')

    def test_the_source_wins_over_prompt_logging_and_an_old_endpoint(self):
        self.settings.write_text(json.dumps({"env": {"OTEL_LOG_USER_PROMPTS": "1",
                                                     "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1"}}))
        out = self.run_tooling("--apply")
        self.assertIn("STALE   settings.json env.OTEL_LOG_USER_PROMPTS=0", out)
        env = json.loads(self.settings.read_text())["env"]
        self.assertEqual(env["OTEL_LOG_USER_PROMPTS"], "0")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://127.0.0.1:4337")

    def test_a_second_apply_changes_nothing(self):
        self.run_tooling("--apply")
        before = (self.settings.read_bytes(), self.codex.read_bytes())
        out = self.run_tooling("--apply")
        self.assertIn("ok      settings.json baseline", out)
        self.assertIn("ok      codex config.toml [otel]", out)
        self.assertEqual((self.settings.read_bytes(), self.codex.read_bytes()), before)

    def test_a_satellite_gets_no_telemetry(self):
        hub_json = self.home / ".config/sd/hub.json"
        hub_json.parent.mkdir(parents=True)
        hub_json.write_text("{}\n")
        self.run_tooling("--apply")
        self.assertNotIn("CLAUDE_CODE_ENABLE_TELEMETRY", self.settings.read_text())
        self.assertIn('exporter = "none"', self.codex.read_text())


class DoctorTelemetry(Case):
    def doctor(self, status):
        write_exec(self.folder.parent / "local-genai-traces/genai-traces.sh", TRACES_STUB)
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1"}}, indent=2))
        return self.run_script("doctor", TRACES_STATUS=str(status)).stdout

    def test_telemetry_on_and_genai_traces_down_warns(self):
        self.assertIn("  WARN    agent telemetry is on but genai-traces status is not 0", self.doctor(3))

    def test_telemetry_on_and_genai_traces_up_is_ok(self):
        out = self.doctor(0)
        self.assertIn("  ok      agent telemetry: genai-traces answers", out)
        self.assertNotIn("WARN    agent telemetry", out)


if __name__ == "__main__":
    unittest.main()
