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

    def test_a_drill_passes_on_a_usb_root_while_the_nas_cannot_be_read(self):
        """sd:4012 round 3: the drill rehearses promote, which needs one readable root."""
        usb = self.tmp / "usb" / "sd-backups"
        (usb / "2026-10-14.7").mkdir(parents=True)
        (usb / "2026-10-14.7" / "manifest.json").write_text("{}\n")
        result = self.run_drill("--from", str(usb), OFFSITE_VERIFY_ROOT=self.tmp / "nas-unmounted")
        self.assertIn(f"drill   : passed on {usb}/2026-10-14.7", result.stdout)
        self.assertFalse(self.drill.exists())

    def test_a_drill_with_no_readable_root_refuses(self):
        """The monthly drill reports a NAS it cannot read: with no other root there is no path."""
        result = self.run_drill(expect=1, OFFSITE_VERIFY_ROOT=self.tmp / "nas-unmounted")
        self.assertIn("NOT-READY backup root not readable:", result.stdout)
        self.assertIn("drill: a hub-ready line is not ok", result.stderr)
        self.assertEqual([], self.calls("restore"))

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

    def test_a_leftover_home_behind_a_symlinked_ancestor_on_the_nas_stays(self):
        """Review round 5: the drill deletes its home only off the NAS, wherever the state folder leads."""
        (self.nas / "state").mkdir()
        state = self.fixture.home / "state-link"
        state.symlink_to(self.nas / "state")
        left = self.nas / "state/sd/promote-drill/home"
        left.mkdir(parents=True)
        result = self.run_drill(expect=1, XDG_STATE_HOME=state)
        self.assertIn("on the NAS", result.stderr)
        self.assertIn("drill: will not delete", result.stderr)
        self.assertTrue(left.exists(), "the drill deleted a folder on the NAS")

    def test_drill_and_undo_do_not_go_together(self):
        result = self.run_drill("--undo", expect=1)
        self.assertIn("--drill and --undo do not go together", result.stderr)
