"""The bulkmove stage moves a satellite's bulk storage to the NAS (sd:8242).

The stage quiesces this host's run-starting jobs instead of naming
writers: it checks the database for open runs, stops the jobs, checks
again, copies, checks the copy, points sd.bulk_storage_root at the NAS
and removes the local folder. Every refusal keeps the folder, and every
exit restarts the jobs it stopped. One case per row of the failure table
in docs/work/2026-11-10-bulkmove-quiesce/design.md.

No case reads the Mac: launchctl, mount, sd and the database read are
stubs, and the "NAS" is a temporary folder. rsync is the real one behind
a wrapper that can fail a copy, list a file left, or signal the stage.
"""

import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "machine-setup.sh"
PREFIX = fixture_config.LABEL_PREFIX

# sd: `config set|get` keep the value in SD_CONFIG. sd.lane_root reads
# LANE_ROOT, unset as the real sd says it.
SD = """#!/bin/sh
if [ "$1 $2 $3" = "config get sd.lane_root" ]; then
  [ -n "${LANE_ROOT:-}" ] && { printf '%s\\n' "$LANE_ROOT"; exit 0; }
  echo "sd: sd.lane_root is not set (example). Run:" >&2; exit 1
fi
case "$1 $2" in
  "config set") [ -z "${SD_SET_FAIL:-}" ] || exit 1; printf '%s' "$4" > "$SD_CONFIG" ;;
  "config get") cat "$SD_CONFIG" 2>/dev/null || exit 1 ;;
  *) echo "sd stub: unexpected $*" >&2; exit 2 ;;
esac
"""

# sd-db fence: holder when FENCE_HOLDER is set.
SD_DB = """#!/bin/sh
[ "$1" = fence ] && [ -n "${FENCE_HOLDER:-}" ] && { echo "fence: holder: example"; exit 0; }
exit 1
"""

# The database read under SD_DB_PYTHON: the Nth call prints RUNS_N; DB_FAIL
# fails it.
PYTHON = """#!/bin/sh
[ -z "${DB_FAIL:-}" ] || exit 1
n=$(( $(cat "$DB_CALLS" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$DB_CALLS"
eval "out=\\${RUNS_$n:-}"
[ -z "$out" ] || printf '%s\\n' "$out"
"""

# launchctl: a label is loaded while LOADED/<label> exists; every call is
# logged. BOOTOUT_STUCK names a label whose bootout fails; BOOTSTRAP_FAIL
# fails every bootstrap.
LAUNCHCTL = """#!/bin/sh
echo "$*" >> "$LOADED/calls.log"
case "$1" in
  print) [ -e "$LOADED/${2##*/}" ] ;;
  bootout)
    [ "${2##*/}" != "${BOOTOUT_STUCK:-}" ] || exit 5
    rm -f "$LOADED/${2##*/}" ;;
  bootstrap)
    [ -z "${BOOTSTRAP_FAIL:-}" ] || exit 5
    l=${3##*/}; touch "$LOADED/${l%.plist}" ;;
  *) exit 1 ;;
esac
"""

MOUNT = """#!/bin/sh
echo "/dev/disk3s1 on / (apfs, local)"
[ -z "${NAS_MOUNTED:-}" ] || echo "//operator@nas.example.test/share on $NAS_MOUNTED (smbfs, nodev)"
"""

# rsync: the real one. A checksum dry run lists RSYNC_LEFT when set; the
# Nth copy fails when RSYNC_FAIL_COPY=N; RSYNC_TERM_COPY=N sends the stage
# TERM during the Nth copy.
RSYNC = """#!/bin/sh
case "$1" in
  -anc) if [ -n "${RSYNC_LEFT:-}" ]; then echo "$RSYNC_LEFT"; exit 0; fi ;;
  -a)
    n=$(( $(cat "$COPIES" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$COPIES"
    [ "${RSYNC_FAIL_COPY:-}" != "$n" ] || exit 23
    if [ "${RSYNC_TERM_COPY:-}" = "$n" ]; then
      # The outermost of the unbroken run of machine-setup.sh parents: a
      # bound step runs in a subshell. The walk stops above that run.
      p=$$ top=""
      while [ "$p" -gt 1 ]; do
        p=$(ps -o ppid= -p "$p" | tr -d ' ')
        case "$(ps -o args= -p "$p")" in
          "/bin/sh "*/machine-setup.sh" update bulkmove --apply") top=$p ;;
          *) [ -z "$top" ] || break ;;
        esac
      done
      [ -n "$top" ] && kill -TERM "$top"; sleep 5; exit 1
    fi ;;
esac
exec "$REAL_RSYNC" "$@"
"""

# rm: fails on the local folder when RM_FAIL is set.
RM = """#!/bin/sh
if [ -n "${RM_FAIL:-}" ]; then
  for a; do [ "$a" != "$RM_FAIL" ] || exit 1; done
fi
exec /bin/rm "$@"
"""


def stub(folder, name, body):
    path = folder / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class BulkmoveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = pathlib.Path(tmp.name).resolve()
        self.home = self.base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("work\n")
        self.hub = self.home / ".config/sd/hub.json"
        self.hub.parent.mkdir(parents=True)
        self.hub.write_text("{}\n")
        self.local = self.home / "repo-storage"
        (self.local / "system/lane").mkdir(parents=True)
        (self.local / "system/lane/run.log").write_text("example run\n")
        self.nas = self.base / "Volumes/share"
        self.nas.mkdir(parents=True)
        self.dst = self.nas / "repo-storage"
        self.stubs = self.base / "stubs"
        self.stubs.mkdir()
        self.pack = self.base / "pack"
        (self.pack / "bin").mkdir(parents=True)
        stub(self.pack / "bin", "sd", SD)
        stub(self.pack / "bin", "sd-db", SD_DB)
        stub(self.stubs, "python-db", PYTHON)
        stub(self.stubs, "launchctl", LAUNCHCTL)
        stub(self.stubs, "mount", MOUNT)
        stub(self.stubs, "rsync", RSYNC)
        stub(self.stubs, "rm", RM)
        self.loaded = self.base / "loaded"
        self.loaded.mkdir()
        # build-dispatch and lane-run run here; night-build is not loaded.
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        for job in ("build-dispatch", "lane-run"):
            (self.loaded / f"{PREFIX}.cron.{job}").touch()
            (agents / f"{PREFIX}.cron.{job}.plist").write_text("<plist/>\n")
        self.config = self.base / "sd-config"
        fixture_config.seal(self, self.stubs)

    def run_stage(self, apply=True, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
            "SD_PACK_ROOT": str(self.pack),
            "SD_DB_PYTHON": str(self.stubs / "python-db"),
            "DB_CALLS": str(self.base / "db-calls"),
            "COPIES": str(self.base / "copies"),
            "LOADED": str(self.loaded),
            "SD_CONFIG": str(self.config),
            "REAL_RSYNC": shutil.which("rsync"),
            "NAS_MOUNTED": str(self.nas),
            "MACHINE_SETUP_BULK_NAS": str(self.nas),
            **extra,
        }
        args = [str(SCRIPT), "update", "bulkmove"] + (["--apply"] if apply else [])
        result = subprocess.run(args, env=env, capture_output=True, text=True,
                                cwd=self.base, stdin=subprocess.DEVNULL, timeout=120)
        return result.returncode, result.stdout + result.stderr

    def loaded_jobs(self):
        return sorted(p.name.rsplit(".", 1)[-1] for p in self.loaded.iterdir() if p.name != "calls.log")

    def calls(self):
        log = self.loaded / "calls.log"
        return log.read_text() if log.exists() else ""

    def assert_kept(self, rc, out, why):
        self.assertEqual(rc, 1, out)
        self.assertIn(why, out)
        self.assertTrue((self.local / "system/lane/run.log").exists(), out)
        self.assertFalse(self.config.exists(), out)
        self.assertEqual(self.loaded_jobs(), ["build-dispatch", "lane-run"], out)

    # Row 0: nothing to do.
    def test_off_without_a_nas(self):
        rc, out = self.run_stage(MACHINE_SETUP_BULK_NAS="")
        self.assertEqual(rc, 0, out)
        self.assertIn("off: MACHINE_SETUP_BULK_NAS is unset", out)
        self.assertTrue(self.local.exists())

    def test_a_dry_run_changes_nothing(self):
        rc, out = self.run_stage(apply=False)
        self.assertEqual(rc, 0, out)
        self.assertIn("DIFFERS", out)
        self.assertTrue(self.local.exists())
        self.assertEqual(self.calls(), "")

    # sd:8322: a satellite that holds the hub role keeps its storage.
    def test_a_satellite_holding_the_hub_role_is_skipped(self):
        config = fixture_config.copy_config(self.base)
        (config / "machine-setup/profiles/work.satellite").write_text("hub.example.test\n")
        self.hub.unlink()
        db = self.home / ".local/share/sd/sd.db"
        db.parent.mkdir(parents=True)
        db.write_text("")
        rc, out = self.run_stage(SYSTEM_TOOLS_CONFIG=str(config), FENCE_HOLDER="1")
        self.assertEqual(rc, 0, out)
        self.assertIn("SKIP    this machine holds the hub role", out)
        self.assertTrue(self.local.exists())
        self.assertEqual(self.calls(), "")

    # Row 1: the share is not mounted.
    def test_an_unmounted_nas_keeps_the_folder(self):
        rc, out = self.run_stage(NAS_MOUNTED="")
        self.assert_kept(rc, out, "is not mounted")
        self.assertNotIn("bootout", self.calls())

    # Row 2: the lane root sits inside the folder.
    def test_a_lane_root_inside_the_folder_keeps_it(self):
        rc, out = self.run_stage(LANE_ROOT=str(self.local / "lanes"))
        self.assert_kept(rc, out, "is inside")
        self.assertNotIn("bootout", self.calls())

    # Row 3: an open run before the stop.
    def test_an_open_run_keeps_the_folder_and_the_jobs(self):
        rc, out = self.run_stage(RUNS_1="run example-run-1")
        self.assert_kept(rc, out, "open run(s) before the stop: run example-run-1")
        self.assertNotIn("bootout", self.calls())
        self.assertFalse(self.dst.exists(), out)

    def test_an_unreadable_database_keeps_the_folder(self):
        rc, out = self.run_stage(DB_FAIL="1")
        self.assert_kept(rc, out, "could not read this host's open runs")
        self.assertNotIn("bootout", self.calls())

    # Row 4: the warm copy fails.
    def test_a_failed_first_copy_keeps_the_folder_and_the_jobs(self):
        rc, out = self.run_stage(RSYNC_FAIL_COPY="1")
        self.assert_kept(rc, out, "FAILED")
        self.assertNotIn("bootout", self.calls())

    # Row 5: a job will not stop.
    def test_a_job_that_will_not_stop_keeps_the_folder_and_restarts_the_rest(self):
        rc, out = self.run_stage(BOOTOUT_STUCK=f"{PREFIX}.cron.lane-run")
        self.assert_kept(rc, out, "launchctl bootout gui/")
        self.assertIn("bootstrap gui/", self.calls())

    # Row 6: a run opened between the first check and the stop.
    def test_a_run_that_opened_during_the_stop_keeps_the_folder_and_restarts(self):
        rc, out = self.run_stage(RUNS_2="lane 8242 example-entry")
        self.assert_kept(rc, out, "open run(s) after the stop: lane 8242 example-entry")
        self.assertIn("bootout", self.calls())
        self.assertIn("started build-dispatch", out)

    # Row 7: the final copy fails.
    def test_a_failed_second_copy_keeps_the_folder_and_restarts(self):
        rc, out = self.run_stage(RSYNC_FAIL_COPY="2")
        self.assert_kept(rc, out, "FAILED")
        self.assertIn("started lane-run", out)

    # Row 8: the checksum dry run still lists a file.
    def test_a_file_left_after_the_copy_keeps_the_folder_and_restarts(self):
        rc, out = self.run_stage(RSYNC_LEFT="system/lane/run.log")
        self.assert_kept(rc, out, "1 file(s) of")
        self.assertIn("started lane-run", out)

    # Row 9: the root does not switch.
    def test_a_root_that_does_not_switch_keeps_the_folder_and_restarts(self):
        rc, out = self.run_stage(SD_SET_FAIL="1")
        self.assert_kept(rc, out, "config set sd.bulk_storage_root")
        self.assertIn("started lane-run", out)

    # Row 10: the removal fails part way; a rerun finishes it.
    def test_a_failed_removal_restarts_and_a_rerun_finishes(self):
        rc, out = self.run_stage(RM_FAIL=str(self.local))
        self.assertEqual(rc, 1, out)
        self.assertTrue(self.local.exists(), out)
        self.assertEqual(self.loaded_jobs(), ["build-dispatch", "lane-run"], out)
        rc, out = self.run_stage()
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.local.exists(), out)

    # Row 11: a job does not restart.
    def test_a_job_that_does_not_restart_is_failed(self):
        rc, out = self.run_stage(BOOTSTRAP_FAIL="1")
        self.assertEqual(rc, 1, out)
        self.assertIn("FAILED  restart build-dispatch", out)
        self.assertIn("FAILED  restart lane-run", out)
        self.assertFalse(self.local.exists(), out)

    # Row 12: a signal while the jobs are stopped still restarts them.
    def test_a_signal_while_stopped_restarts_the_jobs(self):
        rc, out = self.run_stage(RSYNC_TERM_COPY="2")
        self.assertNotEqual(rc, 0, out)
        self.assertTrue((self.local / "system/lane/run.log").exists(), out)
        self.assertEqual(self.loaded_jobs(), ["build-dispatch", "lane-run"], out)

    # Row 13: the move, then a rerun.
    def test_the_move_copies_switches_removes_and_restarts(self):
        rc, out = self.run_stage()
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.dst / "system/lane/run.log").read_text(), "example run\n")
        self.assertEqual(self.config.read_text(), str(self.dst))
        self.assertFalse(self.local.exists(), out)
        # Stopped before the final copy, started after; night-build was
        # never loaded, so it is neither stopped nor started.
        self.assertEqual(self.loaded_jobs(), ["build-dispatch", "lane-run"], out)
        self.assertNotRegex(self.calls(), r"(bootout|bootstrap) .*night-build")
        self.assertLess(out.index("stopped build-dispatch"), out.index("removed"))
        self.assertLess(out.index("removed"), out.index("started build-dispatch"))
        rc, out = self.run_stage()
        self.assertEqual(rc, 0, out)
        self.assertIn(f"ok      {self.local} is gone", out)

    # sd:8319: a NAS file the copy replaces is named.
    def test_a_nas_file_the_copy_replaces_is_reported(self):
        (self.dst / "system/lane").mkdir(parents=True)
        (self.dst / "system/lane/run.log").write_text("older example run, longer\n")
        rc, out = self.run_stage()
        self.assertEqual(rc, 0, out)
        self.assertIn("the copy replaces 1 NAS file(s): system/lane/run.log", out)
        self.assertEqual((self.dst / "system/lane/run.log").read_text(), "example run\n")


if __name__ == "__main__":
    unittest.main()
