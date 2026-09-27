"""The exit contract of `claude.sh mem-pro-watchdog`.

Every run of this job goes through `cron-jobs.sh exec`, which reads any
non-zero exit as FAILED, calls `notify_failure`, and records a run report
that `sd reports ingest` turns into a report item. The job runs every
fifteen minutes, so an exit status is not a detail here: it decides whether
one deliberate setting becomes ninety-six items a day (sd:880).

These tests drive the real script with `CLAUDE_MEM_DATA_DIR` pointed at a
scratch directory. Nothing touches the operator's ~/.claude-mem, nothing is
written (no `--apply`), and every case exits before the first network call,
so the suite needs no gateway stub.
"""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "claude.sh"
GATEWAY = "https://cmem.ai/api/inference/v1"
TOKEN = "cm_pro_" + "x" * 24


def settings(**overrides):
    """A settings.json the watchdog accepts, before `overrides` are applied."""
    base = {"CLAUDE_MEM_CLOUD_SYNC_TOKEN": TOKEN,
            "CLAUDE_MEM_OPENROUTER_BASE_URL": GATEWAY,
            "CLAUDE_MEM_OPENROUTER_API_KEY": TOKEN,
            "CLAUDE_MEM_PROVIDER": "claude"}
    base.update(overrides)
    return base


class WatchdogCase(unittest.TestCase):
    DANGLING = object()

    def run_watchdog(self, content):
        """Run the watchdog against a scratch data directory.

        `content` is a dict written as settings.json, a string written
        verbatim, None for no file at all, or DANGLING for a settings.json
        that is a symlink to nothing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            data = pathlib.Path(tmp)
            if content is self.DANGLING:
                (data / "settings.json").symlink_to(data / "gone.json")
            elif isinstance(content, dict):
                (data / "settings.json").write_text(json.dumps(content))
            elif isinstance(content, str):
                (data / "settings.json").write_text(content)
            # CLAUDE_MEM_PRO_GATEWAY is passed rather than left to its
            # default: the script reads it from the environment, so a
            # developer who has it exported would otherwise see the fixture's
            # base URL treated as the wrong one and four cases change answer.
            # CI runs under `env -i` and would never have caught that.
            environment = dict(os.environ,
                               CLAUDE_MEM_DATA_DIR=str(data),
                               CLAUDE_MEM_PRO_GATEWAY=GATEWAY,
                               CLAUDE_MEM_PRO_WATCHDOG_NOTIFY="0")
            done = subprocess.run(["sh", str(SCRIPT), "mem-pro-watchdog"],
                                  capture_output=True, text=True,
                                  env=environment, timeout=120)
            return done.returncode, done.stdout + done.stderr


class ConfigurationsTheWatchdogDoesNotManage(WatchdogCase):
    """Each of these is a state somebody chose. None of them is a failure."""

    def assert_unmanaged(self, content, *, saying):
        code, output = self.run_watchdog(content)
        self.assertEqual(code, 0,
            "a configuration the watchdog does not manage must not exit "
            "non-zero: cron-jobs.sh reports that as a failed job every "
            "fifteen minutes. Output: " + output)
        self.assertIn("not managing claude-mem here", output)
        self.assertIn(saying, output)

    def test_a_third_provider_is_the_operators_to_manage(self):
        # The live case that filed sd:880: settings.json said "gemini" and
        # the job reported a failure every quarter hour for hours.
        self.assert_unmanaged(settings(CLAUDE_MEM_PROVIDER="gemini"),
                              saying="CLAUDE_MEM_PROVIDER is 'gemini'")

    def test_no_settings_file_means_claude_mem_is_not_installed_here(self):
        self.assert_unmanaged(None, saying="does not exist")

    def test_a_token_that_is_not_a_pro_key_means_there_is_nothing_to_watch(self):
        self.assert_unmanaged(settings(CLAUDE_MEM_CLOUD_SYNC_TOKEN="sk-not-pro"),
                              saying="nothing to watch")

    def test_a_base_url_that_is_not_the_gateway_is_not_this_watchdogs_config(self):
        self.assert_unmanaged(
            settings(CLAUDE_MEM_OPENROUTER_BASE_URL="https://example.invalid/v1"),
            saying="is not the cmem gateway")


class ConfigurationsThatAreStillFaults(WatchdogCase):
    """Two states nobody chooses. These keep their non-zero exit."""

    def test_a_key_that_contradicts_the_pro_token_still_fails(self):
        # A cm_pro_ token beside a different OPENROUTER key is not a
        # decision, it is a half-finished edit. It is worth a page.
        code, output = self.run_watchdog(
            settings(CLAUDE_MEM_OPENROUTER_API_KEY="cm_pro_something_else"))
        self.assertEqual(code, 1, output)
        self.assertIn("differs from the Pro token", output)
        self.assertNotIn("not managing claude-mem here", output)

    def test_a_dangling_settings_symlink_is_broken_and_not_absent(self):
        # `Path.exists()` follows the link, so a settings.json pointing at
        # nothing answers False and would be filed as "not installed here".
        # This folder makes that a live shape: local-claude/settings.json is
        # itself a symlink, so a link that stops resolving is a way the
        # configuration breaks, not a way it goes away.
        code, output = self.run_watchdog(self.DANGLING)
        self.assertEqual(code, 1, output)
        self.assertIn("cannot read", output)
        self.assertNotIn("not managing claude-mem here", output)

    def test_settings_that_exist_but_do_not_parse_still_fail(self):
        # Distinct from the missing file above: the file is there and broken.
        code, output = self.run_watchdog("{ this is not json")
        self.assertEqual(code, 1, output)
        self.assertIn("cannot read", output)
        self.assertNotIn("not managing claude-mem here", output)


class TheJobRunnerReadsTheExitStatus(unittest.TestCase):
    """Why exit 0 and exit 1 mean what they mean here.

    This pins the contract these tests rest on, so that a change to
    cron-jobs.sh that stopped reading the status this way fails here rather
    than silently making the choice above pointless.
    """

    def test_cron_jobs_treats_any_non_zero_exit_as_a_failure(self):
        runner = (pathlib.Path(__file__).resolve().parent.parent.parent
                  / "local-cron-jobs" / "cron-jobs.sh")
        text = runner.read_text()
        self.assertIn('if [ "$rc" -ne 0 ]; then', text)
        self.assertIn('notify_failure "$job" "$rc"', text)


if __name__ == "__main__":
    unittest.main()
