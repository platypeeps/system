"""`status` answers convention 6: 0 healthy, 3 nothing to check, 1 up and broken.

sd:1387 found two ways it passed a collector that was not working:

- the zpages probe accepted any HTTP answer, so a 404 or a 500 read as
  "zpages answering" and exited 0;
- a crashed container reads as "not running", exit 3, because `docker run
  --rm` removes it, and nothing recorded that it had ever been started.

The script runs from a copy in a temporary folder, so the `state/started`
marker it writes lands there and never in this checkout. `docker` is a stub
whose one piece of state is a file standing for the running container;
deleting that file is a crash. `curl` is the real one, pointed at a local
HTTP server that answers with whatever code the test asks for.
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
ENTRYPOINT = FOLDER / "opentelemetry-collector.sh"

DOCKER = """\
#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$1" in
  ps) [ -f "$STUB_RUNNING" ] && echo 0123456789ab ;;
  run) : > "$STUB_RUNNING"; echo 0123456789ab ;;
  stop) rm -f "$STUB_RUNNING" ;;
esac
exit 0
"""


class Zpages(http.server.BaseHTTPRequestHandler):
    code = 200

    def do_GET(self):
        self.send_response(self.server.code)
        if getattr(self.server, "truncate", False):
            # Promise more body than is sent, then close: curl reports the
            # 200 and exits 18 (partial transfer).
            self.send_header("Content-Length", "1000")
        self.end_headers()
        self.wfile.write(b"servicez\n")

    def log_message(self, *args):
        pass


class StatusCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / "local-opentelemetry-collector"
        self.folder.mkdir()
        shutil.copy(ENTRYPOINT, self.folder / ENTRYPOINT.name)
        # The entrypoint sources ../lib/config.sh beside its folder.
        shutil.copytree(ENTRYPOINT.parents[1] / "lib", self.root / "lib")
        stubs = self.root / "bin"
        stubs.mkdir()
        (stubs / "docker").write_text(DOCKER, encoding="utf-8")
        (stubs / "docker").chmod(0o755)
        self.running = self.root / "running"
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("OTLP_EXPORT_", "OTELCOL_"))
        }
        self.env.update(
            PATH=f"{stubs}:{os.environ['PATH']}",
            STUB_LOG=str(self.root / "docker.log"),
            STUB_RUNNING=str(self.running),
            OTLP_EXPORT_URL="https://otlp.example.invalid/v1/abc",
            OTLP_EXPORT_AUTH="k",
            # Never read the operator's real config directory.
            SYSTEM_TOOLS_CONFIG=str(self.root / "config"),
        )

    def zpages(self, code, truncate=False):
        server = http.server.HTTPServer(("127.0.0.1", 0), Zpages)
        server.code = code
        server.truncate = truncate
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.env["OTELCOL_ZPAGES_PORT"] = str(server.server_address[1])

    def closed_port(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), Zpages)
        port = server.server_address[1]
        server.server_close()
        self.env["OTELCOL_ZPAGES_PORT"] = str(port)

    def run_verb(self, *args, expect):
        done = subprocess.run(
            ["/bin/sh", str(self.folder / ENTRYPOINT.name), *args],
            capture_output=True, text=True, input="", env=self.env, timeout=60,
        )
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done

    def marker(self):
        return self.folder / "state" / "started"


class TheZpagesProbe(StatusCase):
    def setUp(self):
        super().setUp()
        self.run_verb("start", expect=0)

    def test_a_2xx_is_healthy(self):
        self.zpages(200)
        done = self.run_verb("status", expect=0)
        self.assertIn("OK", done.stdout)

    def test_a_404_is_broken_not_answering(self):
        # sd:1387 item 1. zpages off, or its debug path moved: curl without
        # -f exits 0 on a 404 and status said "OK — zpages answering".
        self.zpages(404)
        done = self.run_verb("status", expect=1)
        self.assertTrue(done.stdout.startswith("local-opentelemetry-collector: FAIL"), done.stdout)

    def test_a_500_is_broken(self):
        self.zpages(500)
        self.run_verb("status", expect=1)

    def test_a_redirect_is_broken(self):
        # PR #564 review. `curl -f` still exits 0 on a 3xx, so a moved debug
        # path that redirects read as healthy. Only a 2xx is.
        self.zpages(302)
        done = self.run_verb("status", expect=1)
        self.assertIn("HTTP 302", done.stdout)

    def test_a_200_whose_body_is_cut_short_is_broken(self):
        # PR #564 local review. Reading only the HTTP code passed a 200 whose
        # transfer failed; curl's own exit has to be clean as well.
        self.zpages(200, truncate=True)
        done = self.run_verb("status", expect=1)
        self.assertIn("did not complete", done.stdout)

    def test_nothing_listening_is_broken(self):
        self.closed_port()
        self.run_verb("status", expect=1)


class TheExportSettings(StatusCase):
    def test_a_missing_export_url_is_nothing_to_check(self):
        del self.env["OTLP_EXPORT_URL"]
        done = self.run_verb("status", expect=3)
        self.assertIn("not configured", done.stdout)

    def test_start_names_a_missing_export_setting(self):
        del self.env["OTLP_EXPORT_AUTH"]
        done = self.run_verb("start", expect=1)
        self.assertIn("OTLP_EXPORT_AUTH", done.stderr)

    def test_start_names_the_config_path_for_a_missing_setting(self):
        del self.env["OTLP_EXPORT_AUTH"]
        done = self.run_verb("start", expect=1)
        self.assertIn(
            str(self.root / "config" / "opentelemetry-collector" / ".env"),
            done.stderr,
        )

    def test_start_reads_the_setting_from_the_config_dir(self):
        del self.env["OTLP_EXPORT_AUTH"]
        conf = self.root / "config" / "opentelemetry-collector"
        conf.mkdir(parents=True)
        (conf / ".env").write_text("OTLP_EXPORT_AUTH=k\n", encoding="utf-8")
        done = self.run_verb("start", expect=0)
        self.assertNotIn("OTLP_EXPORT_AUTH", done.stderr)

    def test_start_rejects_a_placeholder_url(self):
        self.env["OTLP_EXPORT_URL"] = "https://undefined/v1/abc"
        done = self.run_verb("start", expect=1)
        self.assertIn("placeholder", done.stderr)


class AContainerThatIsGone(StatusCase):
    def test_never_started_is_nothing_to_check(self):
        done = self.run_verb("status", expect=3)
        self.assertIn("SKIP", done.stdout)

    def test_started_and_crashed_is_broken(self):
        # sd:1387 item 2. --rm removes a crashed container, so `docker ps`
        # finds nothing and the old answer was 3, which the sweep never shows.
        self.run_verb("start", expect=0)
        self.assertTrue(self.marker().is_file())
        self.running.unlink()
        done = self.run_verb("status", expect=1)
        first = done.stdout.splitlines()[0]
        self.assertTrue(first.startswith("local-opentelemetry-collector: FAIL"), first)
        self.assertIn("gone", first)

    def test_stopped_on_purpose_is_nothing_to_check(self):
        self.run_verb("start", expect=0)
        self.run_verb("stop", expect=0)
        self.assertFalse(self.marker().exists())
        self.run_verb("status", expect=3)

    def test_update_clears_the_record_as_stop_does(self):
        self.run_verb("start", expect=0)
        self.run_verb("update", expect=0)
        self.assertFalse(self.marker().exists())
        self.run_verb("status", expect=3)

    def test_a_start_that_docker_refused_records_nothing(self):
        failing = self.root / "bin" / "docker"
        failing.write_text("#!/bin/sh\nexit 125\n", encoding="utf-8")
        self.run_verb("start", expect=125)
        self.assertFalse(self.marker().exists())


if __name__ == "__main__":
    unittest.main()
