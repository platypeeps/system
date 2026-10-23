"""The second OTLP destination (system sd:5771): off unless configured.

With TRACES_FORWARD_OTLP_ENDPOINT unset, `start` runs the compose file alone,
as before. With it set, `start` writes state/forward.yaml, a collector
overlay with one more exporter, and adds docker-compose.forward.yml. The
header values stay in that owner-only file: never in argv, output or a
tracked file. Same stubs as test_status.py.
"""

import re
import stat
import unittest

from .test_status import FOLDER, StatusCase

ENDPOINT = "https://otlp.example.test"
SECRET = "synthetic-token-5771"
OVERRIDE = "docker-compose.forward.yml"


def base_exporters():
    text = (FOLDER / "collector.yaml").read_text(encoding="utf-8")
    pipeline = text.split("pipelines:", 1)[1]
    return re.search(r"exporters: \[([^\]]*)\]", pipeline).group(1).split(", ")


class Forwarding(StatusCase):
    def overlay(self):
        return self.folder / "state" / "forward.yaml"

    def compose_calls(self):
        log = self.root / "docker.log"
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return [line for line in lines if line.startswith("compose ")]

    def configure(self, endpoint=ENDPOINT, headers=f"apikey={SECRET}"):
        folder = self.config / "genai-traces"
        folder.mkdir(exist_ok=True)
        (folder / ".env").write_text(
            f"TRACES_FORWARD_OTLP_ENDPOINT={endpoint}\n"
            f"TRACES_FORWARD_OTLP_HEADERS={headers}\n", encoding="utf-8")

    def assert_no_secret(self, done):
        self.assertNotIn(SECRET, done.stdout + done.stderr)
        self.assertNotIn(SECRET, "\n".join(self.compose_calls()))


class Unset(Forwarding):
    def test_unset_values_start_without_a_second_exporter(self):
        self.run_verb("start", expect=0)
        self.assertFalse(self.overlay().exists())
        calls = self.compose_calls()
        self.assertTrue(calls, "start ran no compose")
        self.assertFalse(any(OVERRIDE in c for c in calls), calls)


class Set(Forwarding):
    def test_values_from_the_config_folder_add_the_exporter(self):
        self.configure()
        done = self.run_verb("start", expect=0)
        text = self.overlay().read_text(encoding="utf-8")
        self.assertIn("otlphttp/forward:", text)
        self.assertIn(f'endpoint: "{ENDPOINT}"', text)
        self.assertIn(f'"apikey": "{SECRET}"', text)
        self.assertIn(
            "exporters: [" + ", ".join(base_exporters() + ["otlphttp/forward"]) + "]",
            text)
        self.assertEqual(stat.S_IMODE(self.overlay().stat().st_mode), 0o600)
        calls = self.compose_calls()
        self.assertTrue(any(OVERRIDE in c and " up -d" in c for c in calls), calls)
        self.assertTrue(any("--force-recreate --no-deps collector" in c for c in calls),
                        calls)
        self.assert_no_secret(done)

    def test_the_destination_has_its_own_queue_and_retries(self):
        # A failing second destination must not hold up Phoenix: its exporter
        # queues on its own and the base exporters are untouched.
        self.configure()
        self.run_verb("start", expect=0)
        text = self.overlay().read_text(encoding="utf-8")
        self.assertIn("sending_queue:\n      enabled: true", text)
        self.assertIn("retry_on_failure:\n      enabled: true", text)
        self.assertNotIn("otlp/phoenix:", text)

    def test_several_headers_and_no_headers(self):
        self.configure(headers=f"apikey={SECRET},x-scope=lab")
        self.run_verb("start", expect=0)
        text = self.overlay().read_text(encoding="utf-8")
        self.assertIn(f'"apikey": "{SECRET}"', text)
        self.assertIn('"x-scope": "lab"', text)
        self.configure(headers="")
        self.run_verb("start", expect=0)
        self.assertIn("headers: {}", self.overlay().read_text(encoding="utf-8"))

    def test_an_exported_value_wins_over_the_file(self):
        self.configure()
        self.env["TRACES_FORWARD_OTLP_ENDPOINT"] = ""
        self.run_verb("start", expect=0)
        self.assertFalse(self.overlay().exists())

    def test_unsetting_turns_it_off_and_removes_the_file(self):
        self.configure()
        self.run_verb("start", expect=0)
        (self.config / "genai-traces" / ".env").unlink()
        before = len(self.compose_calls())
        self.run_verb("start", expect=0)
        self.assertFalse(self.overlay().exists())
        calls = self.compose_calls()[before:]
        self.assertTrue(any(" up -d" in c for c in calls), calls)
        self.assertFalse(any(OVERRIDE in c for c in calls), calls)

    def test_update_of_a_started_service_renders_again(self):
        self.run_verb("start", expect=0)
        self.configure()
        self.run_verb("update", expect=0)
        self.assertIn(SECRET, self.overlay().read_text(encoding="utf-8"))

    def test_a_directory_in_the_overlay_place_is_replaced(self):
        # Docker creates a directory for a bind source that is missing.
        self.overlay().parent.mkdir(parents=True, exist_ok=True)
        self.overlay().mkdir()
        self.configure()
        self.run_verb("start", expect=0)
        self.assertTrue(self.overlay().is_file())


class Refused(Forwarding):
    def refuse(self, **values):
        # Each subtest starts clean, so one accepted value fails only itself.
        for path in (self.overlay(), self.marker(), self.root / "docker.log"):
            path.unlink(missing_ok=True)
        self.configure(**values)
        done = self.run_verb("start", expect=1)
        self.assertFalse(self.overlay().exists())
        self.assertFalse(self.marker().exists())
        self.assertFalse(any(" up" in c for c in self.compose_calls()))
        self.assert_no_secret(done)
        return done

    def test_plain_http_is_refused(self):
        done = self.refuse(endpoint="http://otlp.example.test")
        self.assertIn("TRACES_FORWARD_OTLP_ENDPOINT", done.stderr)

    def test_a_url_the_exporter_cannot_parse_is_refused(self):
        # Codex review: the collector does not start with a malformed
        # endpoint, and then Phoenix and the raw file get nothing either.
        for endpoint in ("https://otlp.example.test:bad", "https://otlp.example.test:99999",
                         "https://otlp.example.test:", "https://user@otlp.example.test",
                         "https://[::1]:4318", "https://otlp.example.test/v1?x=1",
                         "https://", "https:///v1"):
            with self.subTest(endpoint=endpoint):
                self.refuse(endpoint=endpoint)

    def test_a_host_port_and_path_are_accepted(self):
        self.configure(endpoint="https://203.0.113.7:4318/otlp")
        self.run_verb("start", expect=0)
        self.assertIn('endpoint: "https://203.0.113.7:4318/otlp"',
                      self.overlay().read_text(encoding="utf-8"))

    def test_a_placeholder_is_refused(self):
        self.refuse(headers="apikey=change-me")

    def test_a_header_without_a_value_is_refused(self):
        done = self.refuse(headers=f"apikey{SECRET}")
        self.assertIn("TRACES_FORWARD_OTLP_HEADERS", done.stderr)

    def test_characters_the_collector_would_expand_are_refused(self):
        for value in (f"{SECRET}$HOME", f'{SECRET}"', f"{SECRET}\\x"):
            with self.subTest(value=value):
                self.refuse(headers=f"apikey='{value}'")

    def test_a_bad_header_name_is_refused(self):
        self.refuse(headers=f"api key={SECRET}")


class TrackedFiles(unittest.TestCase):
    def test_the_example_holds_only_placeholders(self):
        lines = (FOLDER / ".env.example").read_text(encoding="utf-8").splitlines()
        values = [line.split("=", 1)[1] for line in lines
                  if line.lstrip("#").startswith("TRACES_FORWARD_")]
        self.assertEqual(len(values), 2, lines)
        for value in values:
            self.assertRegex(value, r"change-me|example\.test")
        # Commented out, so a copied example forwards nothing.
        self.assertFalse(any(line.startswith("TRACES_FORWARD_") for line in lines))

    def test_the_override_passes_no_value_in_the_environment(self):
        text = (FOLDER / OVERRIDE).read_text(encoding="utf-8")
        self.assertIn("./state/forward.yaml:", text)
        self.assertNotIn("environment:", text)
        self.assertNotIn("${TRACES_FORWARD", text)
        for name in ("collector.yaml", "docker-compose.yml"):
            self.assertNotIn("forward", (FOLDER / name).read_text(encoding="utf-8"))

    def test_help_names_the_config(self):
        text = (FOLDER / "genai-traces.sh").read_text(encoding="utf-8")
        help_text = text.split("HELPEOF", 2)[1]
        self.assertIn("TRACES_FORWARD_OTLP_ENDPOINT", help_text)
        self.assertIn("TRACES_FORWARD_OTLP_HEADERS", help_text)


if __name__ == "__main__":
    unittest.main()
