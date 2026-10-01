"""Which OTLP settings `aura.sh server` hands the server.

`aura-web-server` is a stub on PATH that prints its OTEL_ environment, so the
suite runs no Aura and needs no key. The local default exports spans
without content; recording is the caller's choice; a caller's endpoint wins;
AURA_TRACES=0 sets nothing.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "aura.sh"

STUB = """#!/bin/sh
env | grep -E '^OTEL_' | sort
"""


class ServerEnvironment(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "bin").mkdir()
        stub = root / "bin" / "aura-web-server"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("OTEL_", "AURA_", "GENAI_TRACES_"))}
        self.env.update(
            PATH=f"{root / 'bin'}:{os.environ['PATH']}",
            # Never read the operator's real config directory.
            SYSTEM_TOOLS_CONFIG=str(root / "config"),
            OPENAI_API_KEY="k", MEZMO_API_KEY="k",
        )

    def otel(self, **extra):
        env = dict(self.env, **extra)
        done = subprocess.run(["/bin/sh", str(ENTRYPOINT), "server"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return dict(line.split("=", 1) for line in done.stdout.splitlines())

    def test_the_local_default_does_not_record_content(self):
        # Ship-lane review: a server may carry real work and the collector
        # may forward, so prompts and answers stay opt-in.
        self.assertEqual(self.otel(), {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4337",
            "OTEL_SERVICE_NAME": "aura",
        })

    def test_a_callers_endpoint_leaves_recording_off(self):
        got = self.otel(OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp.example.test")
        self.assertEqual(got["OTEL_EXPORTER_OTLP_ENDPOINT"], "https://otlp.example.test")
        self.assertNotIn("OTEL_RECORD_CONTENT", got)

    def test_a_callers_own_recording_choice_is_kept(self):
        got = self.otel(OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp.example.test",
                        OTEL_RECORD_CONTENT="true")
        self.assertEqual(got["OTEL_RECORD_CONTENT"], "true")

    def test_aura_traces_off_sets_nothing(self):
        self.assertEqual(self.otel(AURA_TRACES="0"), {})

    def env_file(self, text):
        conf = Path(self.env["SYSTEM_TOOLS_CONFIG"]) / "aura"
        conf.mkdir(parents=True)
        (conf / ".env").write_text(text, encoding="utf-8")

    def test_an_exported_endpoint_wins_over_the_env_file(self):
        # PR #61 review: .env overwrote the caller's exported trace settings.
        self.env_file("OTEL_EXPORTER_OTLP_ENDPOINT=http://from-file.test\n")
        got = self.otel(OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp.example.test")
        self.assertEqual(got["OTEL_EXPORTER_OTLP_ENDPOINT"], "https://otlp.example.test")

    def test_an_exported_traces_off_wins_over_the_env_file(self):
        self.env_file("AURA_TRACES=1\n")
        self.assertEqual(self.otel(AURA_TRACES="0"), {})


if __name__ == "__main__":
    unittest.main()
