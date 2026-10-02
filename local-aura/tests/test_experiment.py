"""`aura.sh experiment start` refuses what it cannot trust, and passes the rest.

The script runs from a copy beside a stub `local-genai-traces` whose
`status` exits with a code the test sets, so the experiment's own guard is
what is under test. `docker` is a stub that logs its arguments and the LLM
variables Compose would see, and answers `image inspect` from a file. The
Aura checkout is a folder holding only the example config.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "aura.sh"
EXAMPLE = "examples/quickstart-orchestration-math/config.toml"

DOCKER = """#!/bin/sh
echo "$* | LLM_MODEL=${LLM_MODEL:-} LLM_API_KEY_SET=${LLM_API_KEY:+yes}" >> "$STUB_LOG"
case "$1 $2" in
  "image inspect") [ -f "$STUB_IMAGE" ] || exit 1; echo "${STUB_LABEL-local/test@abc}" ;;
esac
exit 0
"""

CONFIG = """[agent.llm]
provider = "openai"
api_key = "{{ env.OPENAI_API_KEY }}"
model = "gpt-test"

[orchestration]
enabled = true
"""


class ExperimentCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / "local-aura"
        (self.folder / "experiment").mkdir(parents=True)
        shutil.copy(ENTRYPOINT, self.folder / ENTRYPOINT.name)
        shutil.copytree(FOLDER.parent / "lib", self.root / "lib")
        traces = self.root / "local-genai-traces"
        traces.mkdir()
        (traces / "genai-traces.sh").write_text(
            '#!/bin/sh\nexit "${STUB_TRACES:-0}"\n', encoding="utf-8")
        stubs = self.root / "bin"
        stubs.mkdir()
        (stubs / "docker").write_text(DOCKER, encoding="utf-8")
        (stubs / "docker").chmod(0o755)
        self.repo = self.root / "aura"
        (self.repo / EXAMPLE).parent.mkdir(parents=True)
        (self.repo / EXAMPLE).write_text(CONFIG, encoding="utf-8")
        self.image = self.root / "image-present"
        self.image.write_text("", encoding="utf-8")
        self.log = self.root / "docker.log"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("LLM_", "AURA_", "OTEL_", "GENAI_TRACES_"))}
        self.env.update(
            PATH=f"{stubs}:{os.environ['PATH']}",
            SYSTEM_TOOLS_CONFIG=str(self.root / "config"),
            STUB_LOG=str(self.log), STUB_IMAGE=str(self.image),
            AURA_REPO=str(self.repo),
            LLM_PROVIDER="openrouter", LLM_MODEL="model-exported", LLM_API_KEY="k",
        )

    def start(self, expect, **extra):
        env = dict(self.env, **extra)
        env = {k: v for k, v in env.items() if v is not None}
        done = subprocess.run(["/bin/sh", str(self.folder / "aura.sh"), "experiment", "start"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done

    def docker_calls(self):
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def state(self, name):
        return (self.folder / "experiment" / "state" / name).read_text(encoding="utf-8")


class TheRefusals(ExperimentCase):
    def test_a_missing_model_setting_is_named(self):
        done = self.start(1, LLM_API_KEY=None)
        self.assertIn("LLM_API_KEY", done.stderr)
        self.assertNotIn("compose", self.docker_calls())

    def test_no_local_image_refuses(self):
        self.image.unlink()
        done = self.start(1)
        self.assertIn("aura.sh image", done.stderr)
        self.assertNotIn("compose", self.docker_calls())

    def test_an_unhealthy_collector_refuses(self):
        done = self.start(1, STUB_TRACES="3")
        self.assertIn("local-genai-traces", done.stderr)
        self.assertNotIn("compose", self.docker_calls())

    def test_an_example_whose_model_line_moved_refuses(self):
        (self.repo / EXAMPLE).write_text(
            CONFIG.replace('model = "gpt-test"\n', ""), encoding="utf-8")
        done = self.start(1)
        self.assertIn("changed shape", done.stderr)
        self.assertNotIn("compose", self.docker_calls())


class TheImageLabel(ExperimentCase):
    # PR #61 review: the tag alone may name any image; only `aura.sh image`
    # writes the aura.source label.
    def test_an_image_without_the_label_refuses(self):
        done = self.start(1, STUB_LABEL="<no value>")
        self.assertIn("aura.source", done.stderr)
        self.assertNotIn("compose", self.docker_calls())

    def test_an_empty_label_refuses(self):
        done = self.start(1, STUB_LABEL="")
        self.assertIn("aura.source", done.stderr)
        self.assertNotIn("compose", self.docker_calls())

    def test_the_label_is_printed(self):
        done = self.start(0)
        self.assertIn("(local/test@abc)", done.stdout)


CURL = """#!/bin/sh
echo "$*" >> "$STUB_CURL_LOG"
case "${STUB_CURL:-ok}" in
  fail) exit 7 ;;
  error) echo '{"error":{"message":"upstream failed"}}' ;;
  *) echo '{"choices":[{"message":{"content":"42"}}]}' ;;
esac
"""


class ARun(ExperimentCase):
    # PR #61 review: a failed curl was swallowed and `run` exited 0.
    def setUp(self):
        super().setUp()
        shutil.copy(FOLDER / "experiment" / "scenarios.sh",
                    self.folder / "experiment" / "scenarios.sh")
        (self.folder / "experiment" / "state").mkdir(exist_ok=True)
        curl = self.root / "bin" / "curl"
        curl.write_text(CURL, encoding="utf-8")
        curl.chmod(0o755)
        self.curl_log = self.root / "curl.log"
        self.env["STUB_CURL_LOG"] = str(self.curl_log)

    def run_scenarios(self, mode):
        env = dict(self.env, STUB_CURL=mode)
        return subprocess.run(["/bin/sh", str(self.folder / "aura.sh"), "experiment", "run"],
                              capture_output=True, text=True, env=env, timeout=60)

    def requests_sent(self):
        return len(self.curl_log.read_text(encoding="utf-8").splitlines())

    def test_answers_everywhere_exit_zero(self):
        done = self.run_scenarios("ok")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.requests_sent(), 6)

    def test_a_failed_request_fails_the_run_after_sending_all(self):
        done = self.run_scenarios("fail")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(self.requests_sent(), 6)
        self.assertIn("curl exit 7", done.stderr)

    def test_an_error_answer_fails_the_run(self):
        done = self.run_scenarios("error")
        self.assertEqual(done.returncode, 1, done.stderr)
        responses = self.state("responses.jsonl")
        self.assertIn("upstream failed", responses)


class AStart(ExperimentCase):
    def test_renders_both_configs_and_brings_up_compose(self):
        self.start(0)
        orch, single = self.state("config-orch.toml"), self.state("config-single.toml")
        self.assertIn('model = "{{ env.LLM_MODEL }}"', orch)
        self.assertIn("enabled = true", orch)
        self.assertIn("enabled = false", single)
        self.assertIn("compose -f", self.docker_calls())
        self.assertIn("up -d", self.docker_calls())

    def test_needs_no_env_file_and_passes_the_model_by_name(self):
        # PR #61 review: an exported-only setup failed on a required env_file.
        self.start(0)
        self.assertFalse((self.root / "config" / "aura" / ".env").exists())
        self.assertIn("LLM_MODEL=model-exported LLM_API_KEY_SET=yes", self.docker_calls())

    def test_an_exported_value_wins_over_the_env_file(self):
        # PR #61 review: .env overwrote the exported LLM_* values.
        conf = self.root / "config" / "aura"
        conf.mkdir(parents=True)
        (conf / ".env").write_text("LLM_MODEL=model-from-file\n", encoding="utf-8")
        self.start(0)
        self.assertIn("LLM_MODEL=model-exported", self.docker_calls())
        self.assertNotIn("model-from-file", self.docker_calls())

    def test_the_env_file_fills_what_is_not_exported(self):
        conf = self.root / "config" / "aura"
        conf.mkdir(parents=True)
        (conf / ".env").write_text("LLM_MODEL=model-from-file\n", encoding="utf-8")
        self.start(0, LLM_MODEL=None)
        self.assertIn("LLM_MODEL=model-from-file", self.docker_calls())


if __name__ == "__main__":
    unittest.main()
