"""`status` answers convention 6: 0 healthy, 3 nothing to check, 1 up and broken.

The script runs from a copy in a temporary folder, so `state/started` and
`storage/` land there and never in this checkout. `docker` is a stub: one
file per running container, which `compose up` creates and `compose down`
removes. Deleting one file is that container crashing. `curl` is the real
one, pointed at local HTTP servers that answer with the code a test asks for.
"""

import http.server
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "genai-traces.sh"
COLLECTOR = "local-genai-collector"
PHOENIX = "local-genai-phoenix"

DOCKER = """\
#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$1" in
  compose)
    case "$*" in
      *" up "*|*" up") : > "$STUB_DIR/%(c)s"; : > "$STUB_DIR/%(p)s" ;;
      *" down"*) [ -f "$STUB_DIR/docker-down" ] && exit 1
                 rm -f "$STUB_DIR/%(c)s" "$STUB_DIR/%(p)s" ;;
      *" pull"*) [ -f "$STUB_DIR/pull-fails" ] && exit 1 ;;
    esac ;;
  ps)
    # docker ps -q -f name=^NAME$
    name=$(echo "$*" | sed -n 's/.*name=\\^\\([^$]*\\)\\$.*/\\1/p')
    [ -n "$name" ] && [ -f "$STUB_DIR/$name" ] && echo 0123456789ab ;;
esac
exit 0
""" % {"c": COLLECTOR, "p": PHOENIX}


class Answer(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(self.server.code)
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, *args):
        pass


class StatusCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / "local-genai-traces"
        self.folder.mkdir()
        shutil.copy(ENTRYPOINT, self.folder / ENTRYPOINT.name)
        stubs = self.root / "bin"
        stubs.mkdir()
        (stubs / "docker").write_text(DOCKER, encoding="utf-8")
        (stubs / "docker").chmod(0o755)
        self.containers = self.root / "containers"
        self.containers.mkdir()
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GENAI_TRACES_")
        }
        self.env.update(
            PATH=f"{stubs}:{os.environ['PATH']}",
            STUB_LOG=str(self.root / "docker.log"),
            STUB_DIR=str(self.containers),
        )

    def serve(self, variable, code):
        server = http.server.HTTPServer(("127.0.0.1", 0), Answer)
        server.code = code
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.env[variable] = str(server.server_address[1])

    def closed(self, variable):
        server = http.server.HTTPServer(("127.0.0.1", 0), Answer)
        self.env[variable] = str(server.server_address[1])
        server.server_close()

    def run_verb(self, *args, expect):
        done = subprocess.run(
            ["/bin/sh", str(self.folder / ENTRYPOINT.name), *args],
            capture_output=True, text=True, input="", env=self.env, timeout=60,
        )
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done

    def marker(self):
        return self.folder / "state" / "started"


class TheUsage(StatusCase):
    def test_no_argument_prints_usage_and_fails(self):
        done = self.run_verb(expect=1)
        self.assertIn("usage:", done.stderr)

    def test_help_exits_zero_and_declares_the_codes(self):
        done = self.run_verb("help", expect=0)
        self.assertIn("local-health-check reads those codes", done.stdout)

    def test_endpoint_prints_the_default_ports(self):
        done = self.run_verb("endpoint", expect=0)
        self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4337", done.stdout)
        self.assertIn("PHOENIX_UI=http://127.0.0.1:6016", done.stdout)

    def otel_lines(self, *args):
        done = self.run_verb("endpoint", *args, expect=0)
        return [line for line in done.stdout.splitlines() if line.startswith("OTEL_")]

    def test_endpoint_prints_only_standard_sdk_variables(self):
        # PR #61 review: OTEL_EXPORTER_OTLP_HTTP_ENDPOINT is no SDK variable;
        # an endpoint goes with the protocol it speaks.
        self.assertEqual(self.otel_lines(), [
            "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4337",
            "OTEL_EXPORTER_OTLP_PROTOCOL=grpc",
        ])

    def test_endpoint_http_pairs_the_http_port_with_its_protocol(self):
        self.assertEqual(self.otel_lines("http"), [
            "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4338",
            "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf",
        ])

    def test_endpoint_docker_reaches_the_host(self):
        self.assertEqual(self.otel_lines("docker"), [
            "OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4337",
            "OTEL_EXPORTER_OTLP_PROTOCOL=grpc",
        ])

    def test_an_unknown_endpoint_kind_fails(self):
        self.run_verb("endpoint", "smoke-signal", expect=1)

    def test_phoenix_gets_a_retention_limit(self):
        # PR #61 review: Phoenix keeps traces forever unless told otherwise.
        compose = (FOLDER / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn(
            "PHOENIX_DEFAULT_RETENTION_POLICY_DAYS: ${GENAI_TRACES_RETENTION_DAYS:-30}",
            compose)


class TheProbes(StatusCase):
    def setUp(self):
        super().setUp()
        self.run_verb("start", expect=0)

    def test_both_answering_is_healthy(self):
        self.serve("GENAI_TRACES_HEALTH_PORT", 200)
        self.serve("GENAI_TRACES_UI_PORT", 200)
        done = self.run_verb("status", expect=0)
        self.assertIn("OK", done.stdout)

    def test_a_collector_that_is_not_answering_is_broken(self):
        self.closed("GENAI_TRACES_HEALTH_PORT")
        self.serve("GENAI_TRACES_UI_PORT", 200)
        done = self.run_verb("status", expect=1)
        self.assertIn("collector", done.stdout)

    def test_a_phoenix_500_is_broken(self):
        self.serve("GENAI_TRACES_HEALTH_PORT", 200)
        self.serve("GENAI_TRACES_UI_PORT", 500)
        done = self.run_verb("status", expect=1)
        self.assertIn("HTTP 500", done.stdout)

    def test_a_redirect_is_broken(self):
        self.serve("GENAI_TRACES_HEALTH_PORT", 302)
        self.serve("GENAI_TRACES_UI_PORT", 200)
        self.run_verb("status", expect=1)


class AContainerThatIsGone(StatusCase):
    def test_never_started_is_nothing_to_check(self):
        done = self.run_verb("status", expect=3)
        self.assertIn("SKIP", done.stdout)

    def test_started_and_one_container_gone_is_broken(self):
        self.run_verb("start", expect=0)
        self.assertTrue(self.marker().is_file())
        (self.containers / PHOENIX).unlink()
        done = self.run_verb("status", expect=1)
        self.assertIn(PHOENIX, done.stdout)

    def test_started_and_both_gone_is_broken(self):
        self.run_verb("start", expect=0)
        for name in (COLLECTOR, PHOENIX):
            (self.containers / name).unlink()
        self.run_verb("status", expect=1)

    def test_stop_clears_the_record(self):
        self.run_verb("start", expect=0)
        self.run_verb("stop", expect=0)
        self.assertFalse(self.marker().exists())
        self.run_verb("status", expect=3)

    def test_a_failed_stop_keeps_the_record(self):
        # PR #61 review: with Docker down, `compose down` fails and the
        # containers come back with Docker; clearing the marker would make
        # status call them never started.
        self.run_verb("start", expect=0)
        (self.containers / "docker-down").write_text("", encoding="utf-8")
        self.run_verb("stop", expect=1)
        self.assertTrue(self.marker().is_file())

    def docker_calls(self):
        log = self.root / "docker.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def test_update_pulls_and_recreates_without_removing_images(self):
        # PR #61 review: `docker rmi` ran with no IDs, and the collector
        # image is shared with local-opentelemetry-collector.
        self.run_verb("start", expect=0)
        before = len(self.docker_calls())
        self.run_verb("update", expect=0)
        calls = self.docker_calls()[before:]
        self.assertTrue(any(c.endswith(" pull") for c in calls), calls)
        self.assertTrue(any(" up -d" in c for c in calls), calls)
        self.assertFalse(any(c.startswith(("rmi", "images")) or " down" in c
                             for c in calls), calls)
        self.assertTrue(self.marker().is_file())
        self.assertTrue((self.containers / COLLECTOR).exists())

    def test_update_of_a_service_never_started_only_pulls(self):
        self.run_verb("update", expect=0)
        calls = self.docker_calls()
        self.assertFalse(any(" up" in c for c in calls), calls)
        self.assertFalse(self.marker().exists())

    def test_a_failed_update_fails_and_keeps_the_record(self):
        # PR #61 review: update cleared the record after a failed down.
        self.run_verb("start", expect=0)
        (self.containers / "pull-fails").write_text("", encoding="utf-8")
        self.run_verb("update", expect=1)
        self.assertTrue(self.marker().is_file())

    def test_partly_running_without_a_record_is_broken(self):
        (self.containers / COLLECTOR).write_text("", encoding="utf-8")
        self.run_verb("status", expect=1)


if __name__ == "__main__":
    unittest.main()
