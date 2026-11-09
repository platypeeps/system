"""The bulkmove stage moves a satellite's bulk storage to the NAS (sd:8242).

A satellite kept its bulk storage in a local folder while a hung NAS could
stall the dispatch tick (sd:6705). Once pack sd:8275 is done, the stage
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
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "machine-setup.sh"

# sd: `task show N --json` prints SD_ITEM_STATE; `config set|get` keep
# the value in SD_CONFIG; anything else fails.
SD = """#!/bin/sh
case "$1 $2" in
  "task show") printf '{"item": {"status": "%s"}}\\n' "$SD_ITEM_STATE" ;;
  "config set") [ -z "${SD_SET_FAIL:-}" ] || exit 1; printf '%s' "$4" > "$SD_CONFIG" ;;
  "config get") cat "$SD_CONFIG" 2>/dev/null || exit 1 ;;
  *) echo "sd stub: unexpected $*" >&2; exit 2 ;;
esac
"""

# lsof prints LSOF_OPEN as the one open file, if set.
LSOF = """#!/bin/sh
echo p1
[ -z "${LSOF_OPEN:-}" ] || echo "n$LSOF_OPEN"
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
            "SD_CONFIG": str(self.config),
            "REAL_RSYNC": shutil.which("rsync"),
            "SD_ITEM_STATE": "done",
            "NAS_MOUNTED": str(self.nas),
            "MACHINE_SETUP_BULK_NAS": str(self.nas),
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

    def test_it_waits_for_pack_sd_8275(self):
        code, out = self.run_stage(SD_ITEM_STATE="in_progress")
        self.assertEqual(code, 1, out)
        self.assertIn("pack sd:8275 is in_progress, not done", out)
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
