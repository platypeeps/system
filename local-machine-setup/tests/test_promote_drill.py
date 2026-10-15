"""`machine-setup.sh promote --drill`: a standby rehearses promote's steps 1, 5, 6 and 9 (sd:4013).

docs/work/2026-10-10-hub-failover/design.md, "Drill" and the failure table's
drill row. The restore is the stub's; the loopback server and the session are
the pinned pack's own, so the drill's one session is real.
"""

import sys

from tests.test_backbone import write_stub
from tests.test_promote import SATELLITE_CASE, PromoteCase

NEWEST = "2026-10-14.5"


class Drill(PromoteCase):
    def setUp(self):
        super().setUp()
        self.python = write_stub(self.fixture.stubs, "drill-python",
                                 SATELLITE_CASE + f'exec "{sys.executable}" "$@"\n')
        self.drill = self.fixture.home / ".local/state/sd/promote-drill"

    def run_drill(self, *args, expect=0, **extra):
        return self.run_promote("--drill", *args, expect=expect, SD_DB_PYTHON=self.python, **extra)

    def test_a_drill_serves_the_newest_snapshot_and_leaves_nothing(self):
        before = self.hub_json.read_text()
        result = self.run_drill()
        self.assertIn("step 9  : one session served", result.stdout)
        self.assertRegex(result.stdout, r"total   : \d+ s")
        self.assertIn(f"drill   : passed on {self.nas}/sd-backups-hourly/{NEWEST}", result.stdout)
        self.assertFalse(self.drill.exists(), "the throwaway home outlived the drill")
        self.assertEqual(before, self.hub_json.read_text())
        self.assertFalse(self.fixture.db.exists(), "the drill wrote the live database")
        self.assertEqual([], self.calls("tailscale set") + self.calls("launchctl"))
        self.assertEqual(1, len(self.calls("serve --loopback")))

    def test_a_drill_on_a_machine_that_is_no_satellite_refuses(self):
        self.hub_json.unlink()
        result = self.run_drill(expect=1)
        self.assertIn("the drill runs on a standby satellite", result.stderr)
        self.assertEqual([], self.calls("restore"))

    def test_a_drill_whose_restore_fails_deletes_the_home(self):
        result = self.run_drill(expect=1, STUB_RESTORE_RC="1")
        self.assertIn("drill: restore --newest failed", result.stderr)
        self.assertFalse(self.drill.exists())

    def test_killed_before_cleanup_status_names_the_folder_and_the_next_drill_removes_it(self):
        """A drill killed by SIGKILL runs no trap: the restored records stay until the next drill."""
        left = self.drill / "home/.local/share/sd"
        left.mkdir(parents=True)
        (left / "sd.db").write_text("records\n")
        shown = self.run_promote(SD_DB_PYTHON=self.python)
        self.assertIn(f"NOT-READY a drill did not finish and left the records in {self.drill}", shown.stdout)
        result = self.run_drill()
        self.assertIn(f"drill   : removed {self.drill}", result.stdout)
        self.assertIn("drill   : passed", result.stdout)
        self.assertFalse(self.drill.exists())

    def test_drill_and_undo_do_not_go_together(self):
        result = self.run_drill("--undo", expect=1)
        self.assertIn("--drill and --undo do not go together", result.stderr)
