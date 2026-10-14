"""`machine-setup.sh promote --undo`: a promoted satellite hands the role back (sd:4048).

docs/work/2026-10-10-hub-failover/design.md, "How the records leave" and the
failure table's undo rows. Each case starts from a promoted satellite: no
hub.json, the restored database, the fence reading holder and the hub role
installed. The new hub's session is the pack's own `remote.connect`, answering
from a fixture database; the fence's tailnet name is STUB_TAILNET_NAME. Each
step's case stops the run there and checks the next run finishes it or undoes it.
"""

import json
import subprocess
import sys

from tests.test_backbone import write_stub
from tests.test_promote import HUB, PREFIX, ROLE_AGENTS, ROLE_JOBS, SATELLITE_CASE, PromoteCase

# test_backbone puts the pinned pack's lib/ on the path.
from sd_db import database, writes  # noqa: E402
from sd_db.migrate import initialise  # noqa: E402

PROMOTED = "2026-10-14.5"
HANDBACK = "2026-10-14.6"

# The pack's interpreter: the step-3 checks run for real, with `remote.connect`
# answering from STUB_REMOTE_DB (no file: no answer) and the tailnet name from
# STUB_TAILNET_NAME; the satellite check prints STUB_SATELLITE_LINE.
PRELUDE = ("import os, sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
           "from sd_db import database, hub, remote\n"
           "def connect(*a, **k):\n"
           "    if not os.path.exists(os.environ.get('STUB_REMOTE_DB', '')): raise OSError('no answer')\n"
           "    return database.open_local(Path(os.environ['STUB_REMOTE_DB']), write=False, create=False)\n"
           "remote.connect = connect\n"
           "hub._this_name = lambda: os.environ['STUB_TAILNET_NAME']\n")


def make_db(path, *, restored, checkpoint=None, hub_name=HUB):
    initialise(path)
    connection = database.open_local(path, write=True, create=False)
    try:
        writes.set_setting(connection, "sd.hub_name", hub_name)
        writes.record_state(connection, "restore", key=restored, body={"restored_at": "fixture"})
        if checkpoint:
            writes.record_state(connection, "checkpoint", key=checkpoint, body={"at": "fixture"})
        connection.commit()
    finally:
        connection.close()


class Undo(PromoteCase):
    def setUp(self):
        super().setUp()
        f = self.fixture
        self.python = write_stub(f.stubs, "undo-python", SATELLITE_CASE +
                                 f'code=$3; shift 3\nexec "{sys.executable}" -I -c "{PRELUDE}$code" "$@"\n')
        # The hub-role jobs' local roots, on this machine's disk.
        self.hourly = f.home / "hub-role/sd-backups-hourly"
        self.nightly = f.home / "hub-role/sd-backups"
        jobs = self.config / "cron-jobs/jobs"
        (jobs / "sd-db-backup-hourly.job").write_text(
            f'SD_DB_BACKUP_HOURLY_DESTINATION="{self.hourly}"\nJOB_SCHEDULE="50 * * * *"\nJOB_COMMAND="true"\n')
        (jobs / "sd-db-backup.job").write_text(
            f'SD_DB_BACKUP_DESTINATION="{self.nightly}"\nJOB_SCHEDULE="10 2 * * *"\nJOB_COMMAND="true"\n')
        for root in (self.hourly, self.nightly):
            (root / PROMOTED).mkdir(parents=True)
        (self.config / "machine-setup/profiles/work.satellite").write_text(f"{HUB}:{self.port}\n")
        # Promoted: hub.json aside, the database restored, the role installed.
        self.aside = self.hub_json.parent / "hub.json.promoted-20261014T090000Z"
        self.hub_json.rename(self.aside)
        f.db.parent.mkdir(parents=True)
        make_db(f.db, restored=PROMOTED)
        (f.db.parent / "providers.yaml").write_text("providers: {}\n")
        self.fence.write_text("holder\n")
        agents = f.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        for label in ROLE_AGENTS + [f"{PREFIX}.cron.{j}" for j in ROLE_JOBS]:
            (agents / f"{label}.plist").write_text("<plist/>\n")
        self.marker = f.home / ".local/state/sd/promote-undo"
        # The new hub, once it restored the hand-back snapshot.
        self.new_hub = self.tmp / "new-hub.db"

    def new_hub_restores(self, restored=HANDBACK, checkpoint="run-handback"):
        make_db(self.new_hub, restored=restored, checkpoint=checkpoint)

    def run_undo(self, *args, expect=0, **extra):
        env = {"SD_DB_PYTHON": self.python, "STUB_REMOTE_DB": self.new_hub,
               "STUB_TAILNET_NAME": "work-satellite.tailnet.example.test", **extra}
        return self.run_promote("--undo", *args, expect=expect, **env)

    def write_marker(self, **fields):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        base = {"hub": HUB, "port": self.port, "moved": self.aside, "nas": self.nas / "sd-backups-hourly"}
        self.marker.write_text("".join(f"{k}={v}\n" for k, v in {**base, **fields}.items()))

    def handed_back(self):
        """Steps 1 and 2 done, as a first run leaves them while the new hub is not up."""
        self.run_undo("--apply", expect=1)
        self.assertEqual("3", self.marker_step())
        self.new_hub_restores()

    def role_stopped(self):
        for plist in (self.fixture.home / "Library/LaunchAgents").glob("*.plist"):
            plist.unlink()

    def records_here(self):
        return sorted(p.name for p in self.fixture.db.parent.glob("sd.db*")) if self.fixture.db.parent.exists() else []

    # Dry run and refusals before step 1.

    def test_a_dry_run_moves_nothing(self):
        out = self.run_undo().stdout
        self.assertIn("this machine holds the hub role", out)
        self.assertIn("4 delete the local records", out)
        self.assertTrue(self.fixture.db.exists())
        self.assertIsNone(self.marker_step())
        self.assertEqual([], self.calls("backup --destination"))

    def test_a_machine_without_the_role_refuses(self):
        self.fence.write_text("elsewhere\n")
        result = self.run_undo("--apply", expect=1)
        self.assertIn("this machine does not hold the hub role", result.stderr)
        self.assertIsNone(self.marker_step())
        self.assertEqual([], self.calls("backup --destination"))

    def test_an_unfinished_promotion_refuses(self):
        (self.fixture.home / ".local/state/sd/promote-intent").parent.mkdir(parents=True, exist_ok=True)
        (self.fixture.home / ".local/state/sd/promote-intent").write_text("hub=x\nstep=9\n")
        result = self.run_undo("--apply", expect=1)
        self.assertIn("finish the promotion first", result.stderr)
        self.assertEqual([], self.calls("backup --destination"))

    def test_an_unmounted_nas_root_refuses_and_is_not_created(self):
        nas = self.tmp / "unmounted"
        result = self.run_undo("--apply", expect=1, OFFSITE_VERIFY_ROOT=nas)
        self.assertIn("is not there; mount the share", result.stderr)
        self.assertFalse(nas.exists())
        self.assertIsNone(self.marker_step())

    # The whole hand-back.

    def test_a_hand_back_deletes_the_records_once_the_new_hub_serves_the_snapshot(self):
        first = self.run_undo("--apply", expect=1)
        self.assertIn(f"promote --apply: {self.nas}/sd-backups-hourly/{HANDBACK} is the newest", first.stderr)
        self.assertEqual([], self.installed(), "the hub role still runs after the hand-back snapshot")
        self.assertTrue((self.nas / "sd-backups-hourly" / HANDBACK / "backup-manifest.json").exists())
        self.assertTrue(self.hub_json.exists(), "hub.json is not back")
        self.assertEqual(["tailscale set --hostname="], self.calls("tailscale set"))
        self.assertTrue(self.fixture.db.exists(), "deleted before the new hub served the snapshot")

        self.new_hub_restores()
        result = self.run_undo("--apply")

        self.assertEqual([], self.records_here())
        self.assertFalse(self.fixture.db.parent.exists())
        self.assertFalse(self.hourly.exists())
        self.assertFalse(self.nightly.exists())
        self.assertTrue((self.nas / "sd-backups-hourly" / "2026-10-14.4").exists(), "a NAS copy was deleted")
        self.assertIsNone(self.marker_step())
        self.assertIn("hand-back report", result.stdout)
        self.assertEqual(1, len(self.calls("backup --destination")))

    def test_a_root_outside_home_is_left(self):
        outside = self.tmp / "elsewhere/sd-backups"
        (outside / PROMOTED).mkdir(parents=True)
        (self.config / "cron-jobs/jobs/sd-db-backup.job").write_text(
            f'SD_DB_BACKUP_DESTINATION="{outside}"\nJOB_SCHEDULE="10 2 * * *"\nJOB_COMMAND="true"\n')
        self.handed_back()
        self.run_undo("--apply")
        self.assertTrue(outside.exists())
        self.assertFalse(self.hourly.exists())

    # Row undo 1: the role stopped, the hand-back snapshot written.

    def test_a_snapshot_that_fails_starts_the_role_again_and_deletes_nothing(self):
        result = self.run_undo("--apply", expect=1, STUB_BACKUP_RC="1")
        self.assertIn("the hand-back snapshot failed", result.stderr)
        self.assertIn(f"{PREFIX}.sd-serve", self.installed(), "the role did not start again")
        self.assertIn(f"{PREFIX}.cron.sd-db-backup-hourly", self.installed())
        self.assertTrue(self.fixture.db.exists())
        self.assertFalse(self.hub_json.exists())
        self.assertIsNone(self.marker_step())

    def test_killed_after_the_stop_a_rerun_writes_the_snapshot(self):
        """Between the stop and the snapshot: the marker names step 1 and the role is gone."""
        self.write_marker(step=1)
        self.role_stopped()
        self.run_undo("--apply", expect=1)
        self.assertEqual("3", self.marker_step())
        self.assertEqual(1, len(self.calls("backup --destination")))
        self.assertTrue(self.hub_json.exists())

    def test_killed_before_hub_json_went_back_a_rerun_writes_a_new_snapshot(self):
        """Past the snapshot with hub.json still aside, sd writes here still land: the old snapshot misses them."""
        old = self.nas / "sd-backups-hourly" / "2026-10-13.1"
        old.mkdir()
        (old / "backup-manifest.json").write_text('{"run_id": "run-older"}\n')
        for step in ("2", "3"):
            with self.subTest(step=step):
                self.write_marker(step=step, snapshot=old)
                self.role_stopped()
                self.run_undo("--apply", expect=1)
                self.assertEqual(1, len(self.calls("backup --destination")))
                self.assertIn(f"snapshot={self.nas}/sd-backups-hourly/{HANDBACK}", self.marker.read_text())
                self.assertTrue(self.hub_json.exists())
                self.assertTrue(self.fixture.db.exists())
                self.hub_json.rename(self.aside)
                self.log.write_text("")

    def test_killed_after_hub_json_went_back_in_step_1_a_rerun_keeps_the_snapshot(self):
        snapshot = self.nas / "sd-backups-hourly" / HANDBACK
        snapshot.mkdir()
        (snapshot / "backup-manifest.json").write_text('{"run_id": "run-handback"}\n')
        self.write_marker(step=1, snapshot=snapshot)
        self.role_stopped()
        self.aside.rename(self.hub_json)
        self.run_undo("--apply", expect=1)
        self.assertEqual([], self.calls("backup --destination"))
        self.assertEqual("3", self.marker_step())

    # Step 2: hub.json back, the tailnet name given up.

    def test_killed_after_hub_json_went_back_a_rerun_gives_up_the_name(self):
        snapshot = self.nas / "sd-backups-hourly" / HANDBACK
        snapshot.mkdir()
        (snapshot / "backup-manifest.json").write_text('{"run_id": "run-handback"}\n')
        self.write_marker(step=2, snapshot=snapshot)
        self.role_stopped()
        self.aside.rename(self.hub_json)
        self.new_hub_restores()
        self.run_undo("--apply")
        self.assertEqual(["tailscale set --hostname="], self.calls("tailscale set"))
        self.assertEqual([], self.records_here())

    # Row undo 3: nothing deleted until the new hub serves the snapshot.

    def test_a_new_hub_that_does_not_answer_deletes_nothing(self):
        self.run_undo("--apply", expect=1)
        result = self.run_undo("--apply", expect=1)
        self.assertIn("does not answer a session", result.stderr)
        self.assertEqual("3", self.marker_step())
        self.assertTrue(self.fixture.db.exists())
        self.assertTrue(self.hourly.exists())

    def test_a_new_hub_that_restored_another_snapshot_deletes_nothing(self):
        self.run_undo("--apply", expect=1)
        self.new_hub_restores(restored="2026-10-14.4")
        result = self.run_undo("--apply", expect=1)
        self.assertIn(f"last restored 2026-10-14.4, not the hand-back snapshot {HANDBACK}", result.stderr)
        self.assertTrue(self.fixture.db.exists())

    def test_a_new_hub_that_restored_nothing_deletes_nothing(self):
        self.run_undo("--apply", expect=1)
        initialise(self.new_hub)
        result = self.run_undo("--apply", expect=1)
        self.assertIn(f"last restored nothing, not the hand-back snapshot {HANDBACK}", result.stderr)
        self.assertTrue(self.fixture.db.exists())

    def test_a_new_hub_without_the_hand_back_checkpoint_deletes_nothing(self):
        """A snapshot of the same name from another root carries another checkpoint."""
        self.run_undo("--apply", expect=1)
        self.new_hub_restores(checkpoint="run-other")
        result = self.run_undo("--apply", expect=1)
        self.assertIn("holds no checkpoint run-handback", result.stderr)
        self.assertTrue(self.fixture.db.exists())

    def test_a_session_that_reaches_this_machine_deletes_nothing(self):
        """This machine's own database names its own promotion, not the hand-back."""
        self.run_undo("--apply", expect=1)
        result = self.run_undo("--apply", expect=1, STUB_REMOTE_DB=self.fixture.db)
        self.assertIn(f"last restored {PROMOTED}", result.stderr)
        self.assertTrue(self.fixture.db.exists())

    def test_a_machine_that_still_holds_the_name_deletes_nothing(self):
        self.handed_back()
        result = self.run_undo("--apply", expect=1, STUB_TAILNET_NAME=HUB)
        self.assertIn("the fence here reads holder, not elsewhere", result.stderr)
        self.assertTrue(self.fixture.db.exists())

    # Row undo 4: records deleted.

    def test_killed_mid_delete_a_rerun_deletes_the_rest(self):
        self.handed_back()
        self.write_marker(step=4, snapshot=self.nas / "sd-backups-hourly" / HANDBACK)
        subprocess.run(["rm", "-rf", str(self.hourly)], check=True)
        self.run_undo("--apply")
        self.assertEqual([], self.records_here())
        self.assertFalse(self.nightly.exists())
        self.assertIsNone(self.marker_step())

    # Row undo 5: hub.json back, the satellite stage.

    def test_a_satellite_stage_that_fails_stops_at_step_5_and_status_names_it(self):
        self.handed_back()
        result = self.run_undo("--apply", expect=1, STUB_SATELLITE_LINE="  DIFFERS sd_db build: satellite 1, hub 2")
        self.assertIn("the satellite stage did not prove this machine a satellite", result.stderr)
        self.assertEqual("5", self.marker_step())
        self.assertEqual([], self.records_here())
        out = self.run_promote().stdout
        self.assertIn("NOT-READY promote --undo stopped before its end", out)
        self.assertIn("names step 5", out)
        self.run_undo("--apply")
        self.assertIsNone(self.marker_step())
