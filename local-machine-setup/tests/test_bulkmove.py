"""The bulkmove stage moves a satellite's bulk storage to the NAS (sd:8242).

A satellite kept its bulk storage in a local folder while a hung NAS could
stall the dispatch tick (sd:6705). Once this machine's pack holds pack
sd:8275, the stage
copies the folder to the NAS, checks the copy, points
sd.bulk_storage_root there and removes the folder. Every refusal leaves
the local folder intact.

No case reads the Mac: lsof, mount and sd are stubs, and the "NAS" is a
temporary folder. rsync is the real one behind a wrapper that can report
a file left in a dry run.
"""

import pathlib
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "machine-setup.sh"

# sd: `config set|get` keep the value in SD_CONFIG; anything else fails.
# sd.lane_root reads LANE_ROOT, unset as the real sd says it; LANE_FAIL
# makes that read fail another way.
SD = """#!/bin/sh
if [ "$1 $2 $3" = "config get sd.lane_root" ]; then
  [ -z "${LANE_FAIL:-}" ] || { echo "sd: database is locked" >&2; exit 1; }
  [ -n "${LANE_ROOT:-}" ] && { printf '%s\\n' "$LANE_ROOT"; exit 0; }
  echo "sd: sd.lane_root is not set (example). Run:" >&2; exit 1
fi
case "$1 $2" in
  "config set") [ -z "${SD_SET_FAIL:-}" ] || exit 1; printf '%s' "$4" > "$SD_CONFIG" ;;
  "config get") cat "$SD_CONFIG" 2>/dev/null || exit 1 ;;
  *) echo "sd stub: unexpected $*" >&2; exit 2 ;;
esac
"""

# lsof prints LSOF_OPEN as the one open file, if set.
LSOF = """#!/bin/sh
echo p1
[ -z "${LSOF_OPEN:-}" ] || printf 'n%s\\n' "$LSOF_OPEN"
"""

# mount lists the NAS when NAS_MOUNTED is set.
MOUNT = """#!/bin/sh
echo "/dev/disk3s1 on / (apfs, local)"
[ -z "${NAS_MOUNTED:-}" ] || echo "//operator@nas.example.test/share on $NAS_MOUNTED (smbfs, nodev)"
"""

# rsync: the real one, but a dry run lists RSYNC_LEFT when set.
RSYNC = """#!/bin/sh
case "$1" in
  -an*) if [ -n "${RSYNC_LEFT:-}" ]; then echo "$RSYNC_LEFT"; exit 0; fi ;;
esac
exec "$REAL_RSYNC" "$@"
"""


# ps lists PS_LINES ("pid etime args", one per line), as `ps -x -o
# pid=,etime=,args=` does; none when unset.
PS = """#!/bin/sh
[ -z "${PS_LINES:-}" ] || printf '%b\\n' "$PS_LINES"
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
        hub = self.home / ".config/sd/hub.json"
        hub.parent.mkdir(parents=True)
        hub.write_text("{}\n")
        self.local = self.home / "repo-storage"
        (self.local / "system/lane").mkdir(parents=True)
        (self.local / "system/lane/run.log").write_text("example run\n")
        self.nas = self.base / "Volumes/share"
        self.nas.mkdir(parents=True)
        self.stubs = self.base / "stubs"
        self.stubs.mkdir()
        self.pack = self.base / "pack"
        (self.pack / "bin").mkdir(parents=True)
        stub(self.pack / "bin", "sd", SD)
        stub(self.stubs, "lsof", LSOF)
        stub(self.stubs, "mount", MOUNT)
        stub(self.stubs, "rsync", RSYNC)
        stub(self.stubs, "ps", PS)
        self.config = self.base / "sd-config"
        # The pack holds the commit that stands in for pack sd:8275's.
        self.pin = fixture_config.dispatch_pack(self.pack, new=True)["SD_DISPATCH_PACK_MIN"]
        fixture_config.seal(self, self.stubs)

    def run_stage(self, apply=True, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
            "SD_PACK_ROOT": str(self.pack),
            "SD_CONFIG": str(self.config),
            "REAL_RSYNC": shutil.which("rsync"),
            "NAS_MOUNTED": str(self.nas),
            "MACHINE_SETUP_BULK_NAS": str(self.nas),
            "MACHINE_SETUP_BULK_PACK_MIN": self.pin,
            **extra,
        }
        args = [str(SCRIPT), "update", "bulkmove"] + (["--apply"] if apply else [])
        result = subprocess.run(args, env=env, capture_output=True, text=True,
                                cwd=self.base, stdin=subprocess.DEVNULL, timeout=120)
        return result.returncode, result.stdout + result.stderr

    def assert_kept(self, out):
        self.assertTrue((self.local / "system/lane/run.log").is_file(), out)
        self.assertFalse(self.config.exists(), out)

    def test_a_clean_move_copies_sets_the_root_and_removes_the_folder(self):
        code, out = self.run_stage()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.nas / "repo-storage/system/lane/run.log").read_text(), "example run\n")
        self.assertEqual(self.config.read_text(), str(self.nas / "repo-storage"))
        self.assertFalse(self.local.exists(), out)
        self.assertIn("removed", out)
        code, out = self.run_stage()
        self.assertEqual(code, 0, out)
        self.assertIn(f"ok      {self.local} is gone", out)

    def test_a_file_open_under_a_non_ascii_folder_keeps_it(self):
        # Under a C locale lsof escapes each byte past ASCII, so a raw
        # path compare saw no open file under caf\u00e9 (review of sd:8242).
        local = self.home / "caf\u00e9-storage"
        (local / "system").mkdir(parents=True)
        (local / "system/run.log").write_text("example run\n")
        escaped = str(local).replace("\u00e9", "\\xc3\\xa9") + "/system/run.log"
        code, out = self.run_stage(MACHINE_SETUP_BULK_LOCAL=str(local), LSOF_OPEN=escaped)
        self.assertEqual(code, 1, out)
        self.assertIn("a process holds a file under", out)
        self.assertTrue((local / "system/run.log").is_file(), out)

    def test_a_pack_process_from_before_the_switch_keeps_the_folder_until_it_ends(self):
        # A supervisor read the old root at its start and may archive
        # there with no file open (review of sd:8242): the folder stays
        # until every pack process older than the switch has ended.
        supervisor = f"4242 01:02:03 python3 {self.pack}/bin/sd assignments supervise"
        code, out = self.run_stage(PS_LINES=supervisor)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.config.read_text(), str(self.nas / "repo-storage"))
        self.assertTrue((self.local / "system/lane/run.log").is_file(), out)
        self.assertIn("4242", out)
        # It archived to the old root after the first run's copy.
        (self.local / "system/lane/late.log").write_text("late output\n")
        code, out = self.run_stage(PS_LINES=supervisor)
        self.assertTrue(self.local.exists(), out)
        code, out = self.run_stage()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.nas / "repo-storage/system/lane/late.log").read_text(), "late output\n")
        self.assertFalse(self.local.exists(), out)
        self.assertFalse((self.state / "bulkmove.switched").exists(), out)

    def test_a_pack_process_started_through_a_link_keeps_the_folder(self):
        # An installed `sd` link or a trailing slash on the pack path
        # hid the process from a literal match (review of sd:8242).
        link = self.home / ".local/bin/sd"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.pack / "bin/sd")
        for root, ps in ((str(self.pack), f"4244 01:02:03 python3 {link} assignments supervise"),
                         (f"{self.pack}/", f"4245 01:02:03 python3 {self.pack}/bin/sd x"),
                         (str(self.pack), "4246 01:02:03 python3 -m sd_db.dispatch")):
            code, out = self.run_stage(PS_LINES=ps, SD_PACK_ROOT=root)
            self.assertTrue((self.local / "system/lane/run.log").is_file(), out)
            self.assertIn(ps.split()[0], out)

    def test_a_pack_process_started_after_the_switch_does_not_hold_the_folder(self):
        # An earlier run switched the root an hour ago; this tick is 10 s old.
        (self.state / "bulkmove.switched").write_text(f"{int(time.time()) - 3600}\n")
        code, out = self.run_stage(PS_LINES=f"4243 00:10 python3 {self.pack}/bin/sd assignments dispatch")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.local.exists(), out)

    def test_a_lane_root_inside_the_folder_keeps_it(self):
        # The lane queue may live under the local folder; removing it
        # would drop the queued runs while sd.lane_root still names it
        # (review of sd:8242).
        for lane in (str(self.local), str(self.local / "lanes")):
            for how in ({"LANE_ROOT": lane}, {"SD_LANE_ROOT": lane}):
                code, out = self.run_stage(**how)
                self.assertEqual(code, 1, out)
                self.assertIn("sd.lane_root", out)
                self.assert_kept(out)
                self.assertFalse((self.nas / "repo-storage").exists(), out)

    def test_a_lane_root_linked_into_the_folder_keeps_it(self):
        link = self.home / "lanes"
        link.symlink_to(self.local / "system")
        code, out = self.run_stage(LANE_ROOT=str(link))
        self.assertEqual(code, 1, out)
        self.assertIn("sd.lane_root", out)
        self.assert_kept(out)

    def test_a_lane_root_that_does_not_read_keeps_the_folder(self):
        code, out = self.run_stage(LANE_FAIL="1")
        self.assertEqual(code, 1, out)
        self.assertIn("could not read sd.lane_root", out)
        self.assert_kept(out)

    def test_a_lane_root_outside_the_folder_moves_it(self):
        code, out = self.run_stage(LANE_ROOT=str(self.home / "repo-storage-lanes"))
        self.assertEqual(code, 0, out)
        self.assertFalse(self.local.exists(), out)

    def test_a_dry_run_reports_drift_and_changes_nothing(self):
        code, out = self.run_stage(apply=False)
        self.assertEqual(code, 0, out)
        self.assertIn("DIFFERS", out)
        self.assert_kept(out)
        self.assertFalse((self.nas / "repo-storage").exists(), out)

    def test_a_nonempty_checksum_dry_run_keeps_the_folder(self):
        code, out = self.run_stage(RSYNC_LEFT="system/lane/run.log")
        self.assertEqual(code, 1, out)
        self.assertIn("differ on", out)
        self.assert_kept(out)

    def test_a_held_file_keeps_the_folder(self):
        code, out = self.run_stage(LSOF_OPEN=str(self.local / "system/lane/run.log"))
        self.assertEqual(code, 1, out)
        self.assertIn("a process holds a file under", out)
        self.assert_kept(out)
        self.assertFalse((self.nas / "repo-storage").exists(), out)

    def test_an_unmounted_nas_keeps_the_folder(self):
        code, out = self.run_stage(NAS_MOUNTED="")
        self.assertEqual(code, 1, out)
        self.assertIn("is not mounted", out)
        self.assert_kept(out)

    def test_a_pack_without_sd_8275_keeps_the_folder(self):
        # The dispatcher this machine runs is its pack's: an item marked
        # done elsewhere proves nothing about it (review of sd:8242).
        subprocess.run(["git", "-C", str(self.pack), "checkout", "-q", "--detach", "HEAD~1"],
                       check=True, env=fixture_config.git_env())
        code, out = self.run_stage()
        self.assertEqual(code, 1, out)
        self.assertIn(f"does not hold {self.pin} (pack sd:8275)", out)
        self.assert_kept(out)

    def test_no_pinned_pack_commit_keeps_the_folder(self):
        code, out = self.run_stage(MACHINE_SETUP_BULK_PACK_MIN="")
        self.assertEqual(code, 1, out)
        self.assertIn("MACHINE_SETUP_BULK_PACK_MIN is unset", out)
        self.assert_kept(out)

    def test_a_root_that_does_not_read_back_keeps_the_folder(self):
        code, out = self.run_stage(SD_SET_FAIL="1")
        self.assertEqual(code, 1, out)
        self.assertTrue((self.local / "system/lane/run.log").is_file(), out)

    def test_the_hub_keeps_its_bulk_storage(self):
        (self.home / ".config/sd/hub.json").unlink()
        code, out = self.run_stage()
        self.assertEqual(code, 0, out)
        self.assertIn("not a satellite", out)
        self.assert_kept(out)

    def test_unset_is_off(self):
        code, out = self.run_stage(MACHINE_SETUP_BULK_NAS="")
        self.assertEqual(code, 0, out)
        self.assertIn("off: MACHINE_SETUP_BULK_NAS is unset", out)
        self.assert_kept(out)


if __name__ == "__main__":
    unittest.main()
