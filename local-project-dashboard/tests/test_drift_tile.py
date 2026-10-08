"""The toolbox's drift tile reads the nightly update job's log.

The operator removed `machine-setup-drift`; `machine-setup-update-nightly`
runs `update --apply` and then `status --fail-on-drift` as its last step. The
tile reads that log's last run block, and only its status part, because the
update part prints MISSING for what it then fixes.
"""

import datetime
import pathlib
import tempfile
import unittest
from unittest import mock

import collectors

NOW = datetime.datetime.now(datetime.timezone.utc).astimezone()
STAMP = NOW.strftime("%Y-%m-%dT%H:%M:%S%z")
BLOCK = f"""[machine-setup-update-nightly] 2026-10-01T03:15:05-0600 starting (cwd: /tmp)
profile : personal (recorded in /tmp/profile)
drift   : 9 item(s)
[machine-setup-update-nightly] 2026-10-01T03:18:22-0600 done
[machine-setup-update-nightly] {STAMP} starting (cwd: /tmp)
profile : personal (recorded in /tmp/profile)

== dotfiles
  MISSING .zshrc
  linked  .zshrc
profile : personal
repo    : /tmp/repo

== dotfiles
  ok      .zshrc
  DIFFERS .gitconfig

drift   : 1 item(s)
[machine-setup-update-nightly] {STAMP} FAILED rc=1
"""


class TheDriftTile(unittest.TestCase):
    def test_the_tile_reads_the_update_logs_last_status_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = pathlib.Path(tmp) / "local-cron-jobs" / "logs"
            logs.mkdir(parents=True)
            (logs / "machine-setup-update-nightly.log").write_text(BLOCK)
            with mock.patch.object(collectors, "SYSTEM", pathlib.Path(tmp)), \
                    mock.patch.object(collectors, "run", return_value=""), \
                    mock.patch.object(collectors, "cron_job_files", return_value=[]):
                tile = collectors.collect_toolbox()
        self.assertEqual(tile["drift"], 1)
        self.assertEqual(tile["drift_items"], ["DIFFERS .gitconfig"])
        self.assertEqual(tile["drift_at"], STAMP)
        self.assertEqual(tile["drift_unknown"], "")


if __name__ == "__main__":
    unittest.main()
