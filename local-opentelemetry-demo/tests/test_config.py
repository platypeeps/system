"""opentelemetry-demo.sh takes its export destination and paths from config.

The script runs from a copy in a temporary folder with stub kind, kubectl and
helm on PATH, so nothing reaches a cluster and no pid or log file lands in
this checkout.
"""

import os
import shutil
import subprocess
import tempfile
import time
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
  "kind get clusters") [ -n "$STUB_NO_CLUSTER" ] || echo otel-demo ;;
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
        shutil.copy(FOLDER / "llm_varied_wrong.py", self.folder / "llm_varied_wrong.py")
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

    def test_status_without_the_cluster_is_nothing_to_check(self):
        # sd:3552. Convention 6: 3 when the cluster is absent, not 0.
        self.env["STUB_NO_CLUSTER"] = "1"
        done = self.run_verb("status", expect=3)
        self.assertIn("not found", done.stdout)

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

    def spawn(self, *argv):
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def test_stop_spares_a_process_that_reused_the_recorded_pid(self):
        # sd:10434. A pid file outlives its forward; the pid can belong to
        # another process by the time stop runs.
        other = self.spawn("sleep", "30")
        forward = ("--context kind-otel-demo -n otel-demo port-forward "
                   "svc/frontend-proxy 8080:8080")
        (self.folder / ".port-forward.pid").write_text(f"{other.pid}\n{forward}\n")
        (self.folder / ".mcp-port-forward.pid").write_text(f"{other.pid}\n")
        done = self.run_verb("stop", expect=0)
        self.assertIn("frontend-proxy port-forward not running", done.stdout)
        self.assertIsNone(other.poll(), "stop signaled an unrelated process")
        self.assertFalse((self.folder / ".port-forward.pid").exists())

    def sleeping_kubectl(self):
        stub = self.root / "bin" / "kubectl"
        stub.write_text(STUB.replace("exit 0", """\
case "$5" in port-forward) trap 'kill $!; exit 0' TERM; sleep 30 & wait ;; esac
exit 0"""), encoding="utf-8")
        return stub

    def assert_ends(self, pid):
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail("the forward is still running")

    def test_stop_ends_a_forward_from_a_one_line_pid_file(self):
        # Review of sd:10434: pid files written before the change hold only
        # the pid; a live forward named by one must still end.
        stub = self.sleeping_kubectl()
        forward = self.spawn("/bin/sh", str(stub), "--context", "kind-otel-demo",
                             "-n", "otel-demo", "port-forward",
                             "svc/frontend-proxy", "8080:8080")
        time.sleep(0.3)
        (self.folder / ".port-forward.pid").write_text(f"{forward.pid}\n")
        done = self.run_verb("stop", expect=0)
        self.assertIn("frontend-proxy port-forward stopped", done.stdout)
        forward.wait(timeout=5)  # the test is its parent: reap, then it is gone

    def test_start_replaces_a_forward_from_a_one_line_pid_file(self):
        # A missed old forward would keep the port, and its replacement
        # could not bind.
        stub = self.sleeping_kubectl()
        old = self.spawn("/bin/sh", str(stub), "--context", "kind-otel-demo",
                         "-n", "default", "port-forward",
                         "svc/prometheus-mcp-server", "18081:8080")
        time.sleep(0.3)
        (self.folder / ".mcp-port-forward.pid").write_text(f"{old.pid}\n")
        self.env.update(OTLP_EXPORT_URL="https://otlp.example.invalid/otel",
                        OTLP_EXPORT_AUTH="k")
        self.run_verb("start", expect=0)
        old.wait(timeout=5)
        self.run_verb("stop", expect=0)

    def test_stop_ends_the_forward_it_started(self):
        self.sleeping_kubectl()
        self.env.update(OTLP_EXPORT_URL="https://otlp.example.invalid/otel",
                        OTLP_EXPORT_AUTH="k")
        self.run_verb("start", expect=0)
        pid = int((self.folder / ".port-forward.pid").read_text().split()[0])
        os.kill(pid, 0)
        done = self.run_verb("stop", expect=0)
        self.assertIn("frontend-proxy port-forward stopped", done.stdout)
        self.assert_ends(pid)

    def test_repo_start_needs_the_demo_checkout(self):
        done = self.run_verb("repo-start", expect=1)
        self.assertIn("OTEL_DEMO_REPO is not set", done.stderr)

    def test_repo_start_keeps_a_failed_start_and_its_output(self):
        # sd:10435: the tee pipe returned 0 when make failed.
        repo = self.root / "demo"
        repo.mkdir()
        make = self.root / "bin" / "make"
        make.write_text('#!/bin/sh\necho "make-output $*"\n'
                        'exit "${STUB_RC:-0}"\n', encoding="utf-8")
        make.chmod(0o755)
        self.env["OTEL_DEMO_REPO"] = str(repo)
        for rc in (17, 0):
            with self.subTest(rc=rc):
                self.env["STUB_RC"] = str(rc)
                done = self.run_verb("repo-start", expect=rc)
                self.assertIn("make-output start", done.stdout)
                out = self.folder / "opentelemetry-demo-output.txt"
                self.assertIn("make-output start",
                              out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
