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
DRILL_MARK = ".sd-promote-drill"
# Names a backup root may hold that are not snapshot folders `sd-db backup` names.
NEAR_MISSES = ("2026-10-14.5.old", "2026-10-14-copy", "2026-10-14.05", "2026-10-14.0", "2026-1-14", "x2026-10-14")
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
        # ...and loaded: the test launchctl reads this list.
        self.loaded = self.tmp / "stub.log.loaded"
        self.loaded.write_text("".join(f"{label}\n" for label in ROLE_AGENTS + [f"{PREFIX}.cron.{j}" for j in ROLE_JOBS]))
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
        self.loaded.write_text("")

    def still_loaded(self):
        return self.loaded.read_text().split() if self.loaded.exists() else []

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

    def test_a_hub_profile_that_holds_the_role_refuses(self):
        """Undo hands a standby's role back; a hub profile holds it as the hub, and is demoted by the README."""
        profiles = self.config / "machine-setup/profiles"
        (profiles / "personal.agent").write_text(f"{PREFIX}.sd-dashboard\n{PREFIX}.sd-serve\n")
        (self.fixture.state / "profile").write_text("personal\n")
        result = self.run_undo("--apply", expect=1)
        self.assertIn("this hub profile holds the role as the hub", result.stderr)
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
        # The restored configuration is not undo's to delete: the folder stays with it.
        self.assertTrue((self.fixture.db.parent / "providers.yaml").exists())
        self.assertFalse(self.hourly.exists())
        self.assertFalse(self.nightly.exists())
        self.assertTrue((self.nas / "sd-backups-hourly" / "2026-10-14.4").exists(), "a NAS copy was deleted")
        self.assertIsNone(self.marker_step())
        self.assertIn("hand-back report", result.stdout)
        self.assertEqual(1, len(self.calls("backup --destination")))

    def test_a_hand_back_deletes_a_drill_folder_left_here(self):
        drill = self.fixture.home / ".local/state/sd/promote-drill/home/.local/share/sd"
        drill.mkdir(parents=True)
        (drill / "sd.db").write_text("records\n")
        (drill.parents[3] / DRILL_MARK).write_text("")
        self.handed_back()
        self.run_undo("--apply")
        self.assertFalse(drill.parents[3].exists())

    def test_a_root_outside_home_is_left(self):
        outside = self.tmp / "elsewhere/sd-backups"
        (outside / PROMOTED).mkdir(parents=True)
        (self.config / "cron-jobs/jobs/sd-db-backup.job").write_text(
            f'SD_DB_BACKUP_DESTINATION="{outside}"\nJOB_SCHEDULE="10 2 * * *"\nJOB_COMMAND="true"\n')
        self.handed_back()
        self.run_undo("--apply")
        self.assertTrue(outside.exists())
        self.assertFalse(self.hourly.exists())

    # Review round 6: undo deletes only what it can name as hub-role output,
    # then removes a root only when it is empty.

    def test_unrelated_files_and_near_miss_names_in_each_root_stay(self):
        for root in (self.hourly, self.nightly):
            (root / "notes.txt").write_text("mine\n")
            (root / "photos").mkdir()
            (root / "2026-10-14.7").write_text("a file, not a snapshot folder\n")
            for name in NEAR_MISSES:
                (root / name).mkdir()
        self.handed_back()
        result = self.run_undo("--apply")
        for root in (self.hourly, self.nightly):
            self.assertFalse((root / PROMOTED).exists())
            self.assertTrue((root / "notes.txt").exists(), f"undo deleted an unrelated file in {root}")
            self.assertTrue((root / "photos").is_dir())
            self.assertTrue((root / "2026-10-14.7").is_file())
            for name in NEAR_MISSES:
                self.assertTrue((root / name).is_dir(), f"undo deleted the near miss {name}")
            self.assertIn(f"kept    : {root} (not empty)", result.stdout)

    def test_the_database_folder_keeps_what_is_not_the_database(self):
        db = self.fixture.db
        for suffix in ("-wal", "-shm"):
            db.with_name(db.name + suffix).write_text("")
        db.with_name("sd.db.old").write_text("mine\n")
        (db.parent / "notes").mkdir()
        self.handed_back()
        self.run_undo("--apply")
        self.assertEqual(["sd.db.old"], self.records_here())
        self.assertTrue((db.parent / "notes").is_dir())
        self.assertTrue((db.parent / "providers.yaml").exists())

    def test_roots_with_only_sd_output_are_removed(self):
        for name in ("2026-10-14.6", "2026-10-14.6.seeding", "2026-10-14.10", "2026-10-15"):
            (self.hourly / name).mkdir()
        (self.fixture.db.parent / "providers.yaml").unlink()
        self.handed_back()
        self.run_undo("--apply")
        self.assertFalse(self.hourly.exists())
        self.assertFalse(self.nightly.exists())
        self.assertFalse(self.fixture.db.parent.exists())

    def test_a_drill_folder_without_the_drill_marker_is_kept(self):
        drill = self.fixture.home / ".local/state/sd/promote-drill"
        (drill / "home").mkdir(parents=True)
        (drill / "notes.txt").write_text("mine\n")
        self.handed_back()
        result = self.run_undo("--apply")
        self.assertTrue((drill / "notes.txt").exists())
        self.assertIn(f"kept    : {drill} (not empty)", result.stdout)

    # Review round 5: a recursive delete proves its target first. The real
    # path (cd -P) lies under $HOME, off the NAS, and each root is one word.

    def nightly_at(self, path):
        (self.config / "cron-jobs/jobs/sd-db-backup.job").write_text(
            f'SD_DB_BACKUP_DESTINATION="{path}"\nJOB_SCHEDULE="10 2 * * *"\nJOB_COMMAND="true"\n')

    def hourly_at(self, path):
        (self.config / "cron-jobs/jobs/sd-db-backup-hourly.job").write_text(
            f'SD_DB_BACKUP_HOURLY_DESTINATION="{path}"\nJOB_SCHEDULE="50 * * * *"\nJOB_COMMAND="true"\n')

    def test_a_root_that_climbs_out_of_home_through_dotdot_is_left(self):
        outside = self.tmp / "outside/sd-backups"
        (outside / PROMOTED).mkdir(parents=True)
        self.nightly_at(f"{self.fixture.home}/../outside/sd-backups")
        self.handed_back()
        self.run_undo("--apply")
        self.assertTrue((outside / PROMOTED).exists())
        self.assertFalse(self.hourly.exists())

    def test_a_root_behind_a_symlinked_ancestor_on_the_nas_is_left(self):
        (self.fixture.home / "nas-link").symlink_to(self.nas)
        self.nightly_at(self.fixture.home / "nas-link/sd-backups")
        self.handed_back()
        self.run_undo("--apply")
        self.assertTrue((self.nas / "sd-backups/2026-10-13.1").exists(), "undo deleted a NAS root")
        self.assertFalse(self.hourly.exists())

    def test_a_root_that_holds_the_nas_mount_is_left(self):
        """rm -rf crosses a mount point: a root over the NAS mount would empty the share."""
        mnt = self.fixture.home / "mnt"
        mnt.mkdir()
        (mnt / "nas").symlink_to(self.nas)
        self.nightly_at(mnt)
        self.run_undo("--apply", expect=1, OFFSITE_VERIFY_ROOT=mnt / "nas")
        self.new_hub_restores()
        result = self.run_undo("--apply", OFFSITE_VERIFY_ROOT=mnt / "nas")
        self.assertIn("holds the NAS", result.stderr, result.stdout)
        self.assertTrue((mnt / "nas").is_symlink())
        self.assertFalse(self.hourly.exists())

    def test_a_root_with_glob_characters_deletes_that_root_only(self):
        hourly = self.fixture.home / "hub-role/backups[old]"
        (hourly / PROMOTED).mkdir(parents=True)
        other = self.fixture.home / "hub-role/backupso"
        other.mkdir()
        self.hourly_at(hourly)
        self.handed_back()
        self.run_undo("--apply")
        self.assertFalse(hourly.exists())
        self.assertTrue(other.exists(), "the glob picked another folder")

    def test_a_root_with_spaces_in_its_name_is_deleted(self):
        hourly = self.fixture.home / "hub-role/hourly backups"
        (hourly / PROMOTED).mkdir(parents=True)
        (self.fixture.home / "hub-role/hourly").mkdir()
        self.hourly_at(hourly)
        self.handed_back()
        self.run_undo("--apply")
        self.assertFalse(hourly.exists())
        self.assertTrue((self.fixture.home / "hub-role/hourly").exists())

    def test_a_root_with_a_newline_in_its_name_stops_the_run_before_any_delete(self):
        """Split at the newline, the name would point at another folder; a root left out would pass the proof."""
        hourly = self.fixture.home / "hub-role/hourly\nbackups"
        (hourly / PROMOTED).mkdir(parents=True)
        other = self.fixture.home / "hub-role/hourly"
        other.mkdir()
        self.hourly_at(hourly)
        self.handed_back()
        result = self.run_undo("--apply", expect=1)
        self.assertIn("SD_DB_BACKUP_HOURLY_DESTINATION holds a newline", result.stderr)
        self.assertIn("fix its job file", result.stderr)
        self.assertEqual("4", self.marker_step())
        self.assertTrue(other.exists(), "the split name deleted another folder")
        self.assertTrue(hourly.exists())
        self.assertTrue(self.nightly.exists())

    def test_a_root_that_leads_to_a_folder_ending_in_a_newline_is_left(self):
        """codex: $() strips a trailing newline from pwd -P, which then names another folder."""
        real = self.fixture.home / "records\n"
        (real / PROMOTED).mkdir(parents=True)
        other = self.fixture.home / "records"
        other.mkdir()
        (self.fixture.home / "alias").symlink_to(real)
        self.hourly_at(f"{self.fixture.home}/alias/")
        self.handed_back()
        result = self.run_undo("--apply")
        self.assertIn("its real path holds a newline", result.stderr)
        self.assertTrue(other.exists(), "the stripped newline deleted another folder")
        self.assertTrue((real / PROMOTED).exists())

    def test_a_database_folder_behind_a_symlinked_ancestor_is_not_deleted(self):
        share = self.fixture.home / ".local/share"
        elsewhere = self.tmp / "elsewhere-share"
        share.rename(elsewhere)
        share.symlink_to(elsewhere)
        self.handed_back()
        result = self.run_undo("--apply", expect=1)
        self.assertIn("still here after the delete", result.stderr)
        self.assertTrue((elsewhere / "sd/sd.db").exists())

    def test_a_root_it_cannot_resolve_stops_the_run(self):
        """A failed probe is no answer: the run stops at step 4; it does not count the root gone.
        codex: the folder it cannot enter sits two levels up, so the parent's own test fails too."""
        locked = self.fixture.home / "locked"
        (locked / "sub/hourly" / PROMOTED).mkdir(parents=True)
        self.hourly_at(locked / "sub/hourly")
        self.handed_back()
        locked.chmod(0o000)
        try:
            result = self.run_undo("--apply", expect=1)
        finally:
            locked.chmod(0o755)
        self.assertEqual("4", self.marker_step())
        self.assertTrue((locked / "sub/hourly" / PROMOTED).exists())
        self.assertIn(f"could not tell where {locked}/sub/hourly lies", result.stderr)

    # Row undo 1: the role stopped, the hand-back snapshot written.

    def test_a_snapshot_that_fails_starts_the_role_again_and_deletes_nothing(self):
        result = self.run_undo("--apply", expect=1, STUB_BACKUP_RC="1")
        self.assertIn("the hand-back snapshot failed", result.stderr)
        self.assertIn(f"{PREFIX}.sd-serve", self.installed(), "the role did not start again")
        self.assertIn(f"{PREFIX}.cron.sd-db-backup-hourly", self.installed())
        self.assertTrue(self.fixture.db.exists())
        self.assertFalse(self.hub_json.exists())
        self.assertIsNone(self.marker_step())

    def test_a_job_launchd_still_runs_takes_no_snapshot_and_the_role_starts_again(self):
        """Round 4: cron-jobs.sh uninstall removes the plist even when its bootout
        fails; launchd, not the plist, says the job stopped."""
        label = f"{PREFIX}.cron.sd-db-backup-hourly"
        result = self.run_undo("--apply", expect=1, STUB_BOOTOUT_FAIL=label)
        self.assertIn(f"launchd still runs {label}; rerun once it stops", result.stderr)
        self.assertEqual([], self.calls("backup --destination"))
        for role in (f"{PREFIX}.sd-serve", f"{PREFIX}.cron.sd-db-backup"):
            self.assertIn(role, self.still_loaded(), "the role did not start again")
        self.assertIsNone(self.marker_step())

    def test_a_role_label_the_satellite_profile_lists_stops_too(self):
        """Every role label is hub-only: one a satellite profile lists still writes after the snapshot."""
        label = f"{PREFIX}.sd-serve"
        with open(self.config / "machine-setup/profiles/work.agent", "a") as f:
            f.write(f"{label}\n")
        self.run_undo("--apply", expect=1)
        self.assertEqual("3", self.marker_step())
        backup = next(i for i, line in enumerate(self.log.read_text().splitlines()) if "backup --destination" in line)
        stopped = [i for i, line in enumerate(self.log.read_text().splitlines()) if f"bootout gui/" in line and line.endswith(label)]
        self.assertTrue(stopped and stopped[0] < backup, "the listed role label was not stopped before the snapshot")

    def test_a_role_that_does_not_start_again_is_named(self):
        """The restart's stages log a failed bootstrap and exit 0; launchd says it."""
        label = f"{PREFIX}.cron.sd-db-backup-hourly"
        result = self.run_undo("--apply", expect=1, STUB_BACKUP_RC="1", STUB_BOOTSTRAP_FAIL=label)
        self.assertIn(f"promote --undo: {label} did not start again", result.stderr)
        self.assertNotIn(f"{PREFIX}.sd-serve did not start again", result.stderr)

    def test_a_loaded_agent_with_no_plist_is_stopped_before_the_snapshot(self):
        """A plist gone by hand does not stop its label."""
        label = f"{PREFIX}.sd-serve"
        (self.fixture.home / f"Library/LaunchAgents/{label}.plist").unlink()
        self.run_undo("--apply", expect=1)
        self.assertNotIn(label, self.still_loaded())
        self.assertEqual("3", self.marker_step())
        self.assertEqual(1, len(self.calls("backup --destination")))

    def test_a_launchd_that_does_not_answer_takes_no_snapshot(self):
        result = self.run_undo("--apply", expect=1, STUB_PRINT_RC="1")
        self.assertIn("could not ask launchd whether", result.stderr)
        self.assertEqual([], self.calls("backup --destination"))
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
