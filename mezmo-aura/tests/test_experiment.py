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
import tomllib
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "aura.sh"
EXAMPLE = "examples/quickstart-orchestration-math/config.toml"

DOCKER = """#!/bin/sh
echo "$* | LLM_MODEL=${LLM_MODEL:-} LLM_API_KEY_SET=${LLM_API_KEY:+yes} MEZMO_SERVICE_KEY_SET=${MEZMO_SERVICE_KEY:+yes} MEZMO_TARGET=${MEZMO_TARGET:-} MEZMO_KEY_KIND=${MEZMO_SERVICE_KEY%%-*} MEZMO_MCP_URL=${MEZMO_MCP_URL:-} MEZMO_ACCOUNT_ID=${MEZMO_ACCOUNT_ID:-}" >> "$STUB_LOG"
case "$1 $2" in
  "image inspect") [ -f "$STUB_IMAGE" ] || exit 1; echo "${STUB_LABEL-local/test@abc}" ;;
esac
case " $* " in
  *" compose "*" up "*) [ -z "${STUB_COMPOSE_UP_FAIL:-}" ] || exit 1 ;;
  *" compose "*" down "*) [ -z "${STUB_COMPOSE_DOWN_FAIL:-}" ] || exit 1 ;;
esac
exit 0
"""

CONFIG = """[agent.llm]
provider = "openai"
api_key = "{{ env.OPENAI_API_KEY }}"
model = "gpt-test"

[mcp]
sanitize_schemas = true

[mcp.servers.math]
transport = "http_streamable"
url = "http://math-mcp:8081/mcp"

[mcp.servers.math.headers]
X-Example = "math"

[orchestration]
enabled = true

[orchestration.worker.arithmetic]
description = "Basic arithmetic"
mcp_filter = ["add", "multiply"]

[orchestration.worker.arithmetic.extra]
note = "a sub-table"
"""


class ExperimentCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / "mezmo-aura"
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
                    if not k.startswith(("LLM_", "AURA_", "OTEL_", "GENAI_TRACES_", "MEZMO_"))}
        self.env.update(
            PATH=f"{stubs}:{os.environ['PATH']}",
            SYSTEM_TOOLS_CONFIG=str(self.root / "config"),
            STUB_LOG=str(self.log), STUB_IMAGE=str(self.image),
            AURA_REPO=str(self.repo),
            LLM_PROVIDER="openrouter", LLM_MODEL="model-exported", LLM_API_KEY="k",
        )

    def start(self, expect, *args, **extra):
        env = dict(self.env, **extra)
        env = {k: v for k, v in env.items() if v is not None}
        done = subprocess.run(["/bin/sh", str(self.folder / "aura.sh"), "experiment", "start", *args],
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
  provider) case "$*" in
      *3102*) echo '{"choices":[{"message":{"content":"The upstream model provider (openrouter/example/model-a) returned an error: No endpoints found for example/model-a."}}]}' ;;
      *) echo '{"choices":[{"message":{"content":"42"}}]}' ;;
    esac ;;
  *) echo '{"choices":[{"message":{"content":"42"}}]}' ;;
esac
"""


class RunCase(ExperimentCase):
    def setUp(self):
        super().setUp()
        shutil.copy(FOLDER / "experiment" / "scenarios.sh",
                    self.folder / "experiment" / "scenarios.sh")
        (self.folder / "experiment" / "state").mkdir(exist_ok=True)
        (self.folder / "experiment" / "state" / "target").write_text("dev\n", encoding="utf-8")
        curl = self.root / "bin" / "curl"
        curl.write_text(CURL, encoding="utf-8")
        curl.chmod(0o755)
        self.curl_log = self.root / "curl.log"
        self.env["STUB_CURL_LOG"] = str(self.curl_log)

    def run_scenarios(self, mode, *args, **extra):
        env = dict(self.env, STUB_CURL=mode, **extra)
        return subprocess.run(["/bin/sh", str(self.folder / "aura.sh"), "experiment", "run", *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def requests_sent(self):
        return len(self.curl_log.read_text(encoding="utf-8").splitlines())


class ARun(RunCase):
    # PR #61 review: a failed curl was swallowed and `run` exited 0.

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


class AProviderError(RunCase):
    """sd:5073: Aura answers a provider failure with HTTP 200 and the error as
    the reply text, so the run recorded it as an answer and exited 0."""

    def test_a_provider_error_answer_fails_the_run_naming_case_and_error(self):
        done = self.run_scenarios("provider")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertEqual(self.requests_sent(), 6)
        self.assertIn("scenarios.sh: single-arith: The upstream model provider "
                      "(openrouter/example/model-a) returned an error: No endpoints found", done.stderr)
        self.assertNotIn("orch-mixed:", done.stderr)

    def test_the_servers_surface_the_provider_detail(self):
        compose = (FOLDER / "experiment" / "docker-compose.yml").read_text(encoding="utf-8")
        env = compose.split("environment: &aura-env", 1)[1].split("volumes:", 1)[0]
        self.assertRegex(env, r'\n\s+AURA_DEBUG_PROVIDER_ERRORS: "true"\n')


class APromptFile(RunCase):
    """sd:5061: `run --prompts <file>` sends each prompt line to both servers."""

    def prompts(self, text):
        path = self.root / "prompts.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_each_prompt_line_goes_to_both_servers(self):
        path = self.prompts("# a comment\nList the pipelines.\n\n   \nShow the last error\n")
        done = self.run_scenarios("ok", "--prompts", path)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.requests_sent(), 4)
        sent = self.curl_log.read_text(encoding="utf-8")
        self.assertEqual(sent.count("List the pipelines."), 2)
        self.assertEqual(sent.count("Show the last error"), 2)
        self.assertNotIn("multiplied", sent)
        self.assertIn("127.0.0.1:3101/", sent)
        self.assertIn("127.0.0.1:3102/", sent)
        cases = self.state("responses.jsonl")
        for case in ("single-p1", "orch-p1", "single-p2", "orch-p2"):
            self.assertIn(f'"case": "{case}"', cases)

    def test_a_file_without_prompts_refuses(self):
        done = self.run_scenarios("ok", "--prompts", self.prompts("# only a comment\n\n"))
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("no prompt", done.stderr)
        self.assertFalse(self.curl_log.exists())

    def test_a_missing_file_refuses(self):
        done = self.run_scenarios("ok", "--prompts", str(self.root / "absent.txt"))
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("absent.txt", done.stderr)
        self.assertFalse(self.curl_log.exists())


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
        self.assertFalse((self.root / "config" / "mezmo-aura" / ".env").exists())
        self.assertIn("LLM_MODEL=model-exported LLM_API_KEY_SET=yes", self.docker_calls())

    def test_an_exported_value_wins_over_the_env_file(self):
        # PR #61 review: .env overwrote the exported LLM_* values.
        conf = self.root / "config" / "mezmo-aura"
        conf.mkdir(parents=True)
        (conf / ".env").write_text("LLM_MODEL=model-from-file\n", encoding="utf-8")
        self.start(0)
        self.assertIn("LLM_MODEL=model-exported", self.docker_calls())
        self.assertNotIn("model-from-file", self.docker_calls())

    def test_the_env_file_fills_what_is_not_exported(self):
        conf = self.root / "config" / "mezmo-aura"
        conf.mkdir(parents=True)
        (conf / ".env").write_text("LLM_MODEL=model-from-file\n", encoding="utf-8")
        self.start(0, LLM_MODEL=None)
        self.assertIn("LLM_MODEL=model-from-file", self.docker_calls())


OVERLAY = """[mcp.servers.dev]
transport = "http_streamable"
url = "https://mcp.example.test/mcp"

[mcp.servers.dev.headers]
Authorization = "Bearer {{ env.MEZMO_SERVICE_KEY }}"
"""

KEY = "svc-key-value-for-tests-7f3a"


class OverlayCase(ExperimentCase):
    def setUp(self):
        super().setUp()
        self.env["AURA_MEZMO_DEV_SERVICE_KEY"] = "dev-key-for-tests"

    def overlay(self, text):
        path = self.root / "overlay.toml"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def servers(self, name):
        return tomllib.loads(self.state(name))["mcp"]["servers"]


class AnOverlay(OverlayCase):
    """sd:5061: `start --overlay <file>` replaces or adds MCP servers by name."""

    def test_without_an_overlay_the_render_keeps_the_example(self):
        self.start(0)
        servers = self.servers("config-orch.toml")
        self.assertEqual(list(servers), ["math"])
        self.assertEqual(servers["math"]["url"], "http://math-mcp:8081/mcp")

    def test_a_new_server_is_added_to_both_configs(self):
        self.start(0, "--overlay", self.overlay(OVERLAY))
        for name in ("config-orch.toml", "config-single.toml"):
            servers = self.servers(name)
            self.assertEqual(sorted(servers), ["dev", "math"])
            self.assertEqual(servers["dev"]["url"], "https://mcp.example.test/mcp")
            self.assertEqual(servers["math"]["headers"], {"X-Example": "math"})
        self.assertTrue(tomllib.loads(self.state("config-orch.toml"))["orchestration"]["enabled"])
        self.assertFalse(tomllib.loads(self.state("config-single.toml"))["orchestration"]["enabled"])

    def test_a_start_without_the_overlay_drops_the_last_one(self):
        self.start(0, "--overlay", self.overlay(OVERLAY))
        self.start(0)
        for name in ("config-orch.toml", "config-single.toml"):
            self.assertEqual(list(self.servers(name)), ["math"])

    def test_a_server_of_the_same_name_is_replaced_whole(self):
        self.start(0, "--overlay", self.overlay(
            '[mcp.servers.math]\ntransport = "http_streamable"\nurl = "https://math.example.test/mcp"\n'))
        math = self.servers("config-orch.toml")["math"]
        self.assertEqual(math, {"transport": "http_streamable", "url": "https://math.example.test/mcp"})
        self.assertNotIn("math-mcp:8081", self.state("config-orch.toml"))

    def test_an_overlay_with_other_tables_refuses(self):
        done = self.start(1, "--overlay", self.overlay('[agent]\nname = "other"\n'))
        self.assertIn("[mcp.servers.<name>]", done.stderr)
        self.assertNotIn("compose", self.docker_calls())

    def test_a_missing_overlay_refuses(self):
        done = self.start(1, "--overlay", str(self.root / "absent.toml"))
        self.assertIn("absent.toml", done.stderr)
        self.assertNotIn("compose", self.docker_calls())

    def test_an_unknown_option_refuses(self):
        done = self.start(1, "--overlays", self.overlay(OVERLAY))
        self.assertIn("--overlays", done.stderr)
        self.assertNotIn("compose", self.docker_calls())


WORKER = """[orchestration.worker.traces]
description = "Reads Mezmo traces"
turn_depth = 5
mcp_filter = ["list_failed_traces", "get_trace_spans"]
preamble = "You read traces."
"""


class AWorkerOverlay(OverlayCase):
    """sd:5077: the orchestration server's workers carry their own mcp_filter,
    so overlay servers reached the single agent and never the workers. An
    overlay may add or replace [orchestration.worker.<name>] tables too."""

    def workers(self, name):
        return tomllib.loads(self.state(name))["orchestration"]["worker"]

    def test_a_new_worker_is_added_beside_the_example_ones(self):
        self.start(0, "--overlay", self.overlay(OVERLAY + "\n" + WORKER))
        workers = self.workers("config-orch.toml")
        self.assertEqual(sorted(workers), ["arithmetic", "traces"])
        self.assertEqual(workers["traces"]["mcp_filter"], ["list_failed_traces", "get_trace_spans"])
        self.assertEqual(workers["arithmetic"]["mcp_filter"], ["add", "multiply"])
        self.assertEqual(sorted(self.servers("config-orch.toml")), ["dev", "math"])

    def test_a_worker_of_the_same_name_is_replaced_whole(self):
        self.start(0, "--overlay", self.overlay(
            '[orchestration.worker.arithmetic]\ndescription = "Math and traces"\n'
            'mcp_filter = ["add", "list_failed_traces"]\n'))
        self.assertEqual(self.workers("config-orch.toml")["arithmetic"],
                         {"description": "Math and traces", "mcp_filter": ["add", "list_failed_traces"]})

    def test_other_orchestration_keys_still_refuse(self):
        done = self.start(1, "--overlay", self.overlay("[orchestration]\nenabled = false\n"))
        self.assertIn("[orchestration.worker.<name>]", done.stderr)
        self.assertNotIn("compose", self.docker_calls())


class TheServiceKey(OverlayCase):
    """sd:5061: MEZMO_SERVICE_KEY reaches Compose by name; its value lands in no file."""

    def written(self):
        state = self.folder / "experiment" / "state"
        files = [p for p in self.root.rglob("*") if p.is_file()]
        self.assertTrue(any(p.parent == state for p in files))
        return {str(p): p.read_bytes() for p in files}

    def test_the_value_is_in_no_rendered_file(self):
        done = self.start(0, "--overlay", self.overlay(OVERLAY), AURA_MEZMO_DEV_SERVICE_KEY=KEY)
        self.assertIn("{{ env.MEZMO_SERVICE_KEY }}", self.state("config-orch.toml"))
        self.assertIn("MEZMO_SERVICE_KEY_SET=yes", self.docker_calls())
        for path, body in self.written().items():
            self.assertNotIn(KEY.encode(), body, path)
        self.assertNotIn(KEY, done.stdout + done.stderr)

    def test_an_overlay_holding_the_value_refuses(self):
        done = self.start(1, "--overlay", self.overlay(OVERLAY.replace("{{ env.MEZMO_SERVICE_KEY }}", KEY)),
                          AURA_MEZMO_DEV_SERVICE_KEY=KEY)
        self.assertIn("{{ env.MEZMO_SERVICE_KEY }}", done.stderr)
        self.assertNotIn(KEY, done.stdout + done.stderr)
        self.assertNotIn("compose", self.docker_calls())
        state = self.folder / "experiment" / "state"
        for path in state.rglob("*"):
            self.assertNotIn(KEY.encode(), path.read_bytes(), path)


class TheTarget(OverlayCase):
    """sd:5073: the experiment took MEZMO_* from the operator's shell, whose
    default target is production. It picks its own target: --target, else
    AURA_MEZMO_TARGET, else dev; a name holding "prod" needs --allow-prod."""

    SHELL = {"MEZMO_TARGET": "example-prod", "MEZMO_SERVICE_KEY": "prod-key-from-shell",
             "MEZMO_MCP_URL": "https://mcp.prod.example.test/mcp", "MEZMO_ACCOUNT_ID": "prod-account"}

    def test_the_shell_target_never_reaches_compose(self):
        self.start(0, "--overlay", self.overlay(OVERLAY), **self.SHELL)
        calls = self.docker_calls()
        self.assertIn("MEZMO_TARGET=dev MEZMO_KEY_KIND=dev MEZMO_MCP_URL= MEZMO_ACCOUNT_ID=", calls)
        self.assertNotIn("prod", calls)
        self.assertEqual(self.state("target"), "dev\n")

    def test_the_named_target_supplies_its_values(self):
        self.start(0, "--target", "example-dev", "--overlay", self.overlay(OVERLAY), **self.SHELL,
                   AURA_MEZMO_EXAMPLE_DEV_SERVICE_KEY="dev2-key",
                   AURA_MEZMO_EXAMPLE_DEV_MCP_URL="https://mcp.dev.example.test/mcp")
        self.assertIn("MEZMO_TARGET=example-dev MEZMO_KEY_KIND=dev2 "
                      "MEZMO_MCP_URL=https://mcp.dev.example.test/mcp", self.docker_calls())

    def test_the_env_file_names_the_target_and_the_flag_wins(self):
        conf = self.root / "config" / "mezmo-aura"
        conf.mkdir(parents=True)
        (conf / ".env").write_text("AURA_MEZMO_TARGET=example-dev\n", encoding="utf-8")
        self.start(0)
        self.assertIn("MEZMO_TARGET=example-dev ", self.docker_calls())
        self.start(0, "--target", "other-dev")
        self.assertIn("MEZMO_TARGET=other-dev ", self.docker_calls())

    def test_a_prod_target_needs_allow_prod(self):
        for target in ("example-prod", "PROD"):
            done = self.start(1, "--target", target)
            self.assertIn("--allow-prod", done.stderr)
        self.assertNotIn("compose", self.docker_calls())
        self.start(0, "--target", "example-prod", "--allow-prod")
        self.assertIn("MEZMO_TARGET=example-prod ", self.docker_calls())

    def test_an_overlay_needing_a_key_the_target_lacks_refuses(self):
        done = self.start(1, "--target", "example-dev", "--overlay", self.overlay(OVERLAY), **self.SHELL)
        self.assertIn("AURA_MEZMO_EXAMPLE_DEV_SERVICE_KEY", done.stderr)
        self.assertNotIn("compose", self.docker_calls())

    def test_a_target_name_that_is_no_name_refuses(self):
        for target in ("", "../dev", "dev env"):
            done = self.start(1, "--target", target)
            self.assertIn("--target", done.stderr)
        self.assertNotIn("compose", self.docker_calls())


class TheRunTarget(RunCase):
    """sd:5073: a run goes to the servers start brought up, against its target."""

    def test_each_answer_names_the_target(self):
        done = self.run_scenarios("ok")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('"target": "dev"', self.state("responses.jsonl"))

    def test_another_target_than_the_started_one_refuses(self):
        done = self.run_scenarios("ok", "--target", "example-dev")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("dev", done.stderr)
        self.assertFalse(self.curl_log.exists())

    def test_no_recorded_target_refuses(self):
        (self.folder / "experiment" / "state" / "target").unlink()
        done = self.run_scenarios("ok")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("experiment start", done.stderr)
        self.assertFalse(self.curl_log.exists())

    def test_a_prod_target_needs_allow_prod_on_run_too(self):
        (self.folder / "experiment" / "state" / "target").write_text("example-prod\n", encoding="utf-8")
        done = self.run_scenarios("ok")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("--allow-prod", done.stderr)
        self.assertFalse(self.curl_log.exists())
        self.assertEqual(self.run_scenarios("ok", "--allow-prod").returncode, 0)


class TheTargetMarker(RunCase):
    """sd:5064 review: state/target is the target the running servers carry.
    start wrote it before Compose ran, so a dev start that failed while
    production servers ran left `dev` on them, and `run` sent requests to
    production without --allow-prod, labelled dev. Every step that changes
    the servers drops the marker first; only a start Compose confirmed
    writes it."""

    def marker(self):
        return self.folder / "experiment" / "state" / "target"

    def running_prod(self):
        self.marker().write_text("example-prod\n", encoding="utf-8")

    def test_a_failed_compose_up_leaves_run_refusing(self):
        self.running_prod()
        self.start(1, "--target", "dev", STUB_COMPOSE_UP_FAIL="1")
        self.assertFalse(self.marker().exists())
        done = self.run_scenarios("ok")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("experiment start", done.stderr)
        self.assertFalse(self.curl_log.exists())

    def test_a_refused_overlay_drops_the_marker(self):
        overlay = self.root / "bad.toml"
        overlay.write_text('[agent]\nname = "other"\n', encoding="utf-8")
        self.running_prod()
        self.start(1, "--overlay", str(overlay))
        self.assertFalse(self.marker().exists())

    def test_a_refusal_before_any_change_keeps_the_marker(self):
        """Nothing moved: the servers still run with the target recorded."""
        self.running_prod()
        self.image.unlink()
        self.start(1)
        self.assertEqual(self.marker().read_text(encoding="utf-8"), "example-prod\n")

    def test_a_confirmed_start_writes_the_marker(self):
        self.running_prod()
        self.start(0, "--target", "dev")
        self.assertEqual(self.marker().read_text(encoding="utf-8"), "dev\n")

    def test_stop_drops_the_marker_even_when_down_fails(self):
        for fail in ("", "1"):
            self.running_prod()
            subprocess.run(["/bin/sh", str(self.folder / "aura.sh"), "experiment", "stop"],
                           capture_output=True, text=True, env=dict(self.env, STUB_COMPOSE_DOWN_FAIL=fail),
                           timeout=30)
            self.assertFalse(self.marker().exists(), fail)

    def test_start_recreates_the_servers(self):
        """Compose keeps a container whose definition did not change, and a
        rendered config is read only at startup: a new overlay on the same
        target would leave the servers on the old one."""
        self.start(0)
        up = [line for line in self.docker_calls().splitlines() if " up " in line]
        self.assertTrue(up and all("--force-recreate" in line for line in up), up)


class TheComposeFile(unittest.TestCase):
    """Both Aura servers run the inspected local image, never a pulled one."""

    def test_the_shared_aura_service_never_pulls(self):
        compose = (Path(__file__).resolve().parent.parent / "experiment" / "docker-compose.yml").read_text()
        anchor = compose.split("aura-orch: &aura", 1)[1].split("environment:", 1)[0]
        self.assertIn("pull_policy: never", anchor)
        self.assertIn("<<: *aura\n", compose.split("aura-single:", 1)[1])

    def test_the_service_key_is_passed_by_name_only(self):
        """sd:5061: a bare name hands the caller's value through; ${...} would interpolate it."""
        compose = (Path(__file__).resolve().parent.parent / "experiment" / "docker-compose.yml").read_text()
        env = compose.split("environment: &aura-env", 1)[1].split("volumes:", 1)[0]
        self.assertRegex(env, r"\n\s+MEZMO_SERVICE_KEY:\s*\n")
        self.assertNotIn("${MEZMO_SERVICE_KEY", compose)


if __name__ == "__main__":
    unittest.main()
