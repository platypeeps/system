"""update --apply pulls no repo when none is missing (sd:8278).

repo-sync-nightly pulls the fleet at 02:45. The nightly update ran
`repo-sync.sh sync` again at 03:15: it echoed every pull's diffstat into its
log and paged a second time for each pull repo-sync-nightly had already
reported. The stage now runs `check` and runs `sync` only to clone a missing
checkout or fix an origin.
"""

import subprocess

from tests import fixture_config
from tests import test_claude_settings as base

# Logs each call; `check` exits $CHECK_RC, `sync` exits $SYNC_RC.
REPO_SYNC = r"""#!/bin/sh
echo "$1" >> "$REPO_SYNC_LOG"
case "$1" in
  check) echo "  MISSING example.test/widget"; exit "${CHECK_RC:-0}" ;;
  sync) echo "  synced"; exit "${SYNC_RC:-0}" ;;
esac
exit 9
"""


class ReposStage(base.SyntheticHome):
    def setUp(self):
        super().setUp()
        base.write_exec(self.repo / "local-repo-sync/repo-sync.sh", REPO_SYNC)
        self.log = self.home / "repo-sync.log"

    def run_repos(self, check_rc, sync_rc=0):
        env = {"HOME": str(self.home), "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
               "LANG": "en_US.UTF-8", "MACHINE_SETUP_STATE": str(self.state),
               **fixture_config.env(self.config_root),
               "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
               "SD_PACK_ROOT": str(self.home / "no-pack"), "REPO_SYNC_LOG": str(self.log),
               "CHECK_RC": str(check_rc), "SYNC_RC": str(sync_rc)}
        result = subprocess.run([str(self.folder / "machine-setup.sh"), "update", "repos", "--apply"],
                                env=env, capture_output=True, text=True, cwd=self.cwd,
                                stdin=subprocess.DEVNULL, timeout=120)
        calls = self.log.read_text().split() if self.log.exists() else []
        return result, calls

    def test_nothing_missing_runs_check_and_no_sync(self):
        result, calls = self.run_repos(check_rc=0)

        self.assertEqual(calls, ["check"], result.stdout + result.stderr)
        self.assertNotIn("repo-sync.sh sync", result.stdout)

    def test_a_missing_checkout_runs_sync(self):
        result, calls = self.run_repos(check_rc=1)

        self.assertEqual(calls, ["check", "sync"], result.stdout + result.stderr)
        self.assertIn("  synced", result.stdout)

    def test_a_failed_sync_is_still_a_failure(self):
        result, calls = self.run_repos(check_rc=1, sync_rc=1)

        self.assertEqual(calls, ["check", "sync"])
        self.assertIn("FAILED  repo-sync.sh sync (exit 1)", result.stdout)
        self.assertNotEqual(result.returncode, 0)
