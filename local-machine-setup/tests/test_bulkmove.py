"""The bulkmove stage moves a satellite's bulk storage to the NAS (sd:8242).

The stage quiesces this host's run-starting jobs instead of naming
writers: it checks the database for open runs, holds each job's cron
lock so no slot starts a run, checks again, copies, checks the copy,
points sd.bulk_storage_root at the NAS and removes the local folder.
Every refusal keeps the folder; no job is booted out, and the locks go
with the stage's shell. One case per row of the failure table
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
  "config get")
    [ -z "${SD_GET_FAIL:-}" ] || { echo "sd: database is locked" >&2; exit 1; }
    cat "$SD_CONFIG" 2>/dev/null || { echo "sd: $3 is not set (example). Run:" >&2; exit 1; } ;;
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

# launchctl: a label is loaded while LOADED/<label> exists, and names its
# log under LOGS (none when NO_STDOUT is set). Every call is logged; any
# verb but print fails.
LAUNCHCTL = """#!/bin/sh
echo "$*" >> "$LOADED/calls.log"
[ "$1" = print ] || exit 1
[ -e "$LOADED/${2##*/}" ] || exit 113
[ -n "${NO_STDOUT:-}" ] || echo "	stdout path = $LOGS/${2##*.cron.}.log"
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
    if [ "${RSYNC_PROBE_COPY:-}" = "$n" ]; then
      perl -e 'use Fcntl ":flock"; open(my $fh, ">>", $ARGV[0]) or exit 2; exit(flock($fh, LOCK_EX|LOCK_NB) ? 0 : 1)' \
        "$LOGS/.lane-run.flock" && echo free > "$PROBE" || echo held > "$PROBE"
    fi
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


# Takes the flock on $1 and holds it until stdin closes, as a cron run
# holds its job's lock on fd 8.
HOLD = 'use Fcntl ":flock"; open(my $fh, ">>", $ARGV[0]) or die; flock($fh, LOCK_EX) or die; print "held\\n"; $| = 1; <STDIN>;'


# Exits 0 when the flock on $1 is free, 1 when another holds it.
PROBE = 'use Fcntl ":flock"; open(my $fh, ">>", $ARGV[0]) or exit 2; exit(flock($fh, LOCK_EX|LOCK_NB) ? 0 : 1);'


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
        self.logs = self.base / "cron-logs"
        self.logs.mkdir()
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
            "LOGS": str(self.logs),
            "PROBE": str(self.base / "probe"),
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

    def lock_free(self, job):
        return subprocess.run(["perl", "-e", PROBE, str(self.logs / f".{job}.flock")]).returncode == 0

    def assert_untouched(self, out):
        """The jobs stay loaded, no launchctl call but print, and no lock is left held."""
        self.assertEqual(self.loaded_jobs(), ["build-dispatch", "lane-run"], out)
        self.assertEqual([c for c in self.calls().splitlines() if not c.startswith("print ")], [], out)
        self.assertTrue(self.lock_free("build-dispatch"), out)
        self.assertTrue(self.lock_free("lane-run"), out)

    def assert_kept(self, rc, out, why):
        self.assertEqual(rc, 1, out)
        self.assertIn(why, out)
        self.assertTrue((self.local / "system/lane/run.log").exists(), out)
        self.assertFalse(self.config.exists(), out)
        self.assert_untouched(out)

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

    # Row 2: the lane root sits inside the folder.
    def test_a_lane_root_inside_the_folder_keeps_it(self):
        rc, out = self.run_stage(LANE_ROOT=str(self.local / "lanes"))
        self.assert_kept(rc, out, "is inside")

    # Row 3: an open run before the copy.
    def test_an_open_run_keeps_the_folder_and_the_jobs(self):
        rc, out = self.run_stage(RUNS_1="run example-run-1")
        self.assert_kept(rc, out, "open run(s) before the copy: run example-run-1")
        self.assertFalse(self.dst.exists(), out)

    def test_an_unreadable_database_keeps_the_folder(self):
        rc, out = self.run_stage(DB_FAIL="1")
        self.assert_kept(rc, out, "could not read this host's open runs")

    # Row 4: the warm copy fails.
    def test_a_failed_first_copy_keeps_the_folder_and_the_jobs(self):
        rc, out = self.run_stage(RSYNC_FAIL_COPY="1")
        self.assert_kept(rc, out, "FAILED")

    # Row 5: a run holds its job's lock. Review of build 772: it is never
    # stopped; the stage refuses and the run goes on.
    def test_a_job_mid_run_is_left_running_and_keeps_the_folder(self):
        holder = subprocess.Popen(["perl", "-e", HOLD, str(self.logs / ".lane-run.flock")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline(), "held\n")
        rc, out = self.run_stage()
        self.assertEqual(rc, 1, out)
        self.assertIn("a run of lane-run is in progress", out)
        self.assertTrue((self.local / "system/lane/run.log").exists(), out)
        self.assertIsNone(holder.poll(), out)
        self.assertNotIn("bootout", self.calls())
        self.assertTrue(self.lock_free("build-dispatch"), out)

    def test_a_loaded_job_with_no_log_keeps_the_folder(self):
        rc, out = self.run_stage(NO_STDOUT="1")
        self.assert_kept(rc, out, "launchd names no log for")

    # Row 6: a run opened between the first check and the locks.
    def test_a_run_that_opened_before_the_locks_keeps_the_folder(self):
        rc, out = self.run_stage(RUNS_2="lane 8242 example-entry")
        self.assert_kept(rc, out, "open run(s) under the job locks: lane 8242 example-entry")

    # Row 7: the final copy fails.
    def test_a_failed_second_copy_keeps_the_folder(self):
        rc, out = self.run_stage(RSYNC_FAIL_COPY="2")
        self.assert_kept(rc, out, "FAILED")

    # Row 8: the checksum dry run still lists a file.
    def test_a_file_left_after_the_copy_keeps_the_folder(self):
        rc, out = self.run_stage(RSYNC_LEFT="system/lane/run.log")
        self.assert_kept(rc, out, "1 file(s) of")

    # Row 9: the root does not switch.
    def test_a_root_that_does_not_switch_keeps_the_folder(self):
        rc, out = self.run_stage(SD_SET_FAIL="1")
        self.assert_kept(rc, out, "config set sd.bulk_storage_root")

    # Row 10: the removal fails part way; a rerun finishes it.
    def test_a_failed_removal_releases_the_locks_and_a_rerun_finishes(self):
        rc, out = self.run_stage(RM_FAIL=str(self.local))
        self.assertEqual(rc, 1, out)
        self.assertTrue(self.local.exists(), out)
        self.assert_untouched(out)
        rc, out = self.run_stage()
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.local.exists(), out)

    # Review of build 772: once the root names the NAS, jobs write there,
    # so a rerun must not copy the stale local folder over newer files.
    def test_a_rerun_after_the_switch_keeps_newer_nas_files(self):
        rc, out = self.run_stage(RM_FAIL=str(self.local))
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.config.read_text(), str(self.dst))
        (self.dst / "system/lane/run.log").write_text("newer example run on the NAS\n")
        rc, out = self.run_stage()
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.dst / "system/lane/run.log").read_text(), "newer example run on the NAS\n")
        self.assertFalse(self.local.exists(), out)
        self.assertIn("already names", out)

    def test_an_unreadable_root_keeps_the_folder(self):
        rc, out = self.run_stage(SD_GET_FAIL="1")
        self.assert_kept(rc, out, "could not read sd.bulk_storage_root")
        self.assertFalse(self.dst.exists(), out)

    # Row 11: a signal while the locks are held leaves the jobs as they were.
    def test_a_signal_under_the_locks_leaves_the_jobs_as_they_were(self):
        rc, out = self.run_stage(RSYNC_TERM_COPY="2")
        self.assertNotEqual(rc, 0, out)
        self.assertTrue((self.local / "system/lane/run.log").exists(), out)
        self.assert_untouched(out)

    # A slot that fires while the stage works finds its job's lock held
    # and skips, so no run starts during the final copy.
    def test_a_slot_during_the_final_copy_finds_its_lock_held(self):
        rc, out = self.run_stage(RSYNC_PROBE_COPY="2")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.base / "probe").read_text(), "held\n")
        self.assert_untouched(out)

    # Row 12: the move, then a rerun.
    def test_the_move_copies_switches_and_removes(self):
        rc, out = self.run_stage()
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.dst / "system/lane/run.log").read_text(), "example run\n")
        self.assertEqual(self.config.read_text(), str(self.dst))
        self.assertFalse(self.local.exists(), out)
        # night-build is not loaded: it cannot start a run, so no lock.
        self.assertNotIn("night-build's lock", out)
        self.assertLess(out.index("held    build-dispatch's lock"), out.index("removed"))
        self.assert_untouched(out)
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
