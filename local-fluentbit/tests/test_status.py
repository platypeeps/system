"""`status` answers convention 6: 0 healthy, 3 nothing to check, 1 up and broken.

sd:1387: `start` runs the container with `docker run --rm`, so a shipper that
crashed is removed and `docker ps` finds nothing. `status` read that as "not
running", exit 3, which local-health-check never shows, because nothing
recorded that it had been started. `start` now leaves `state/started` and
`stop` clears it; gone with the marker present is 1.

The script runs from a copy in a temporary folder, so the marker and the
rendered `config.yaml` land there and never in this checkout. `docker` is a
stub whose one piece of state is a file standing for the running container;
deleting that file is a crash. Nothing here passes `--probe`, so nothing is
sent anywhere.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "fluentbit.sh"

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


class StatusCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / "local-fluentbit"
        self.folder.mkdir()
        shutil.copy(ENTRYPOINT, self.folder / ENTRYPOINT.name)
        shutil.copy(FOLDER / "config.yaml.template", self.folder / "config.yaml.template")
        shutil.copytree(FOLDER.parent / "lib", self.root / "lib")
        stubs = self.root / "bin"
        stubs.mkdir()
        (stubs / "docker").write_text(DOCKER, encoding="utf-8")
        (stubs / "docker").chmod(0o755)
        self.running = self.root / "running"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("FLUENTBIT_EXPORT_")}
        self.env.update(
            PATH=f"{stubs}:{os.environ['PATH']}",
            STUB_LOG=str(self.root / "docker.log"),
            STUB_RUNNING=str(self.running),
            # An empty config root, so a real .env on this machine cannot leak in.
            SYSTEM_TOOLS_CONFIG=str(self.root / "config"),
            FLUENTBIT_EXPORT_URL_1="https://ingest.example.invalid/v1/11111111-2222-3333-4444-555555555555",
            FLUENTBIT_EXPORT_URL_2="http://collector.example.invalid:8080/logs",
            FLUENTBIT_EXPORT_AUTH_1="key-one",
            FLUENTBIT_EXPORT_AUTH_2="Bearer key-two",
        )

    def run_verb(self, *args, expect):
        done = subprocess.run(
            ["/bin/sh", str(self.folder / ENTRYPOINT.name), *args],
            capture_output=True, text=True, input="", env=self.env, timeout=60,
        )
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done

    def marker(self):
        return self.folder / "state" / "started"


class Status(StatusCase):
    def test_not_configured_is_nothing_to_check(self):
        del self.env["FLUENTBIT_EXPORT_AUTH_1"]
        done = self.run_verb("status", expect=3)
        self.assertIn("not configured", done.stdout)

    def test_never_started_is_nothing_to_check(self):
        done = self.run_verb("status", expect=3)
        self.assertIn("SKIP", done.stdout)

    def test_started_and_up_is_healthy(self):
        self.run_verb("start", expect=0)
        self.assertTrue(self.marker().is_file())
        self.run_verb("status", expect=0)

    def test_started_and_crashed_is_broken(self):
        # sd:1387 item 2. The old answer was 3 and the nightly sweep printed
        # nothing about a shipper an OOM kill had removed.
        self.run_verb("start", expect=0)
        self.running.unlink()
        done = self.run_verb("status", expect=1)
        first = done.stdout.splitlines()[0]
        self.assertTrue(first.startswith("local-fluentbit: FAIL"), first)
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
        (self.root / "bin" / "docker").write_text("#!/bin/sh\nexit 125\n", encoding="utf-8")
        self.run_verb("start", expect=125)
        self.assertFalse(self.marker().exists())


class Config(StatusCase):
    def test_start_renders_both_destinations_from_their_urls(self):
        self.run_verb("start", expect=0)
        rendered = (self.folder / "config.yaml").read_text(encoding="utf-8")
        self.assertNotIn("${", rendered)
        for line in ("host: 'ingest.example.invalid'",
                     "uri: '/v1/11111111-2222-3333-4444-555555555555'",
                     "header: 'Authorization: key-one'", "tls: 'on'", "port: 443",
                     "host: 'collector.example.invalid'", "uri: '/logs'",
                     "header: 'Authorization: Bearer key-two'", "tls: 'off'", "port: 8080"):
            self.assertIn(line, rendered)

    def test_values_come_from_the_config_env_file(self):
        env_dir = self.root / "config" / "fluentbit"
        env_dir.mkdir(parents=True)
        lines = [f"{key}='{self.env.pop(key)}'" for key in sorted(self.env)
                 if key.startswith("FLUENTBIT_EXPORT_")]
        (env_dir / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.run_verb("start", expect=0)
        self.run_verb("status", expect=0)

    def test_missing_value_names_variable_and_both_remedies(self):
        del self.env["FLUENTBIT_EXPORT_URL_2"]
        done = self.run_verb("start", expect=1)
        self.assertIn("FLUENTBIT_EXPORT_URL_2", done.stderr)
        self.assertIn("export them", done.stderr)
        self.assertIn("local-fluentbit/.env.example", done.stderr)

    def test_placeholder_value_is_rejected(self):
        self.env["FLUENTBIT_EXPORT_URL_1"] = "https://ingest.example.test/change-me-1"
        done = self.run_verb("start", expect=1)
        self.assertIn("placeholder", done.stderr)


if __name__ == "__main__":
    unittest.main()
