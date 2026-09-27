"""The two registry shapes: a `url` endpoint and a `start` command."""

import json
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from sd_db.testing import (
    START_PROVIDERS,
    URL_PROVIDERS,
    ProviderDouble,
    Reply,
    install_start_command,
    provider_environment,
    write_registry,
)


def post(url, body):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read())


class ProviderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.double = ProviderDouble()
        self.double.__enter__()
        self.addCleanup(self.double.close)


class TheUrlEntries(ProviderCase):
    def test_every_registered_provider_has_its_own_base_url(self):
        urls = {name: self.double.base_url(name) for name in URL_PROVIDERS}
        self.assertEqual(len(set(urls.values())), len(URL_PROVIDERS))
        self.assertTrue(urls["kimi"].endswith("/kimi/v1"))

    def test_a_completion_carries_usage_a_cap_can_be_charged_against(self):
        url = self.double.base_url("baseten") + "/chat/completions"
        status, payload = post(url, {"model": "fixture-model", "messages": []})
        self.assertEqual(status, 200)
        self.assertEqual(payload["usage"]["prompt_tokens"], 100)
        self.assertEqual(payload["choices"][0]["message"]["content"], "ok")

    def test_queued_replies_are_answered_in_order(self):
        self.double.queue("kimi", Reply(text="first"), Reply(text="second"))
        url = self.double.base_url("kimi") + "/chat/completions"
        self.assertEqual(post(url, {})[1]["choices"][0]["message"]["content"], "first")
        self.assertEqual(post(url, {})[1]["choices"][0]["message"]["content"], "second")
        self.assertEqual(post(url, {})[1]["choices"][0]["message"]["content"], "ok")

    def test_a_failing_reply_is_a_failure_and_is_recorded(self):
        self.double.queue("minimax", Reply(status=429, error="rate limited"))
        url = self.double.base_url("minimax") + "/chat/completions"
        status, payload = post(url, {})
        self.assertEqual(status, 429)
        self.assertEqual(payload["error"]["message"], "rate limited")
        self.assertEqual(self.double.calls_to("minimax")[-1].status, 429)

    def test_the_request_body_is_recorded(self):
        url = self.double.base_url("exo") + "/chat/completions"
        post(url, {"model": "fixture-local", "messages": [{"role": "user", "content": "hi"}]})
        call = self.double.calls_to("exo")[-1]
        self.assertEqual(call.body["messages"][0]["content"], "hi")

    def test_an_unknown_provider_is_a_404_on_nobody_s_call_list(self):
        before = len(self.double.calls)
        status, _ = post(self.double.base_url("kimi").replace("/kimi/", "/nope/"), {})
        self.assertEqual(status, 404)
        self.assertEqual(len(self.double.calls), before)

    def test_an_unwritten_route_is_a_404_and_is_recorded(self):
        status, _ = post(self.double.base_url("kimi") + "/embeddings", {})
        self.assertEqual(status, 404)
        self.assertEqual(self.double.calls_to("kimi")[-1].status, 404)


class TheStartEntries(ProviderCase):
    def test_the_spawn_is_recorded_with_argv_stdin_and_environment(self):
        provider = install_start_command(self.root / "bin", "claude")
        environment = provider_environment(self.root / "bin", provider, base={"PATH": "/usr/bin:/bin"})
        environment["SD_FIXTURE_MARKER"] = "visible"

        completed = subprocess.run(
            [provider.command, "-p", "review this"],
            input="the diff\n", capture_output=True, text=True, env=environment,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        spawns = provider.spawns()
        self.assertEqual(len(spawns), 1)
        self.assertEqual(spawns[0]["argv"], ["-p", "review this"])
        self.assertEqual(spawns[0]["stdin"], "the diff\n")
        self.assertEqual(spawns[0]["env"]["SD_FIXTURE_MARKER"], "visible")
        self.assertEqual(json.loads(completed.stdout)["result"], "ok")

    def test_two_providers_do_not_share_a_transcript(self):
        claude = install_start_command(self.root / "bin", "claude", output="from claude\n")
        codex = install_start_command(self.root / "bin", "codex", output="from codex\n")
        environment = provider_environment(
            self.root / "bin", claude, codex, base={"PATH": "/usr/bin:/bin"}
        )

        first = subprocess.run([claude.command], capture_output=True, text=True, input="", env=environment)
        second = subprocess.run([codex.command], capture_output=True, text=True, input="", env=environment)

        self.assertEqual(first.stdout, "from claude\n")
        self.assertEqual(second.stdout, "from codex\n")
        self.assertEqual(len(claude.spawns()), 1)
        self.assertEqual(len(codex.spawns()), 1)
        self.assertEqual(claude.spawns()[0]["provider"], "claude")
        self.assertEqual(codex.spawns()[0]["provider"], "codex")

    def test_a_failing_provider_exits_non_zero_and_still_records(self):
        provider = install_start_command(self.root / "bin", "codex", status=3, output="refused\n")
        environment = provider_environment(self.root / "bin", provider, base={"PATH": "/usr/bin:/bin"})
        completed = subprocess.run(
            [provider.command], capture_output=True, text=True, input="", env=environment
        )
        self.assertEqual(completed.returncode, 3)
        self.assertEqual(completed.stderr, "refused\n")
        self.assertEqual(len(provider.spawns()), 1)


class TheRegistryFile(ProviderCase):
    def test_it_names_every_provider_and_points_them_at_the_doubles(self):
        claude = install_start_command(self.root / "bin", "claude")
        path = write_registry(self.root / "providers.yaml", double=self.double, start={"claude": claude})
        text = path.read_text(encoding="utf-8")

        for name in (*URL_PROVIDERS, *START_PROVIDERS):
            self.assertIn(f"  {name}:", text)
        for name in URL_PROVIDERS:
            self.assertIn(self.double.base_url(name), text)
        self.assertIn(str(claude.command), text)
        self.assertNotIn("https://api.moonshot.ai", text)
        self.assertNotIn("inference.baseten.co", text)


if __name__ == "__main__":
    unittest.main()
