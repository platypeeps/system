"""The tmpdisk stage keeps a RAM disk for the gate's and test runs' temp folders (sd:7092).

fs_usage on the hub put about 13,000 of 54,801 file writes in 30 s under
$TMPDIR, in sd-tests-* and sd-local-gate-* folders, while fseventsd grew
to 17 GB. The stage keeps a small RAM disk with FSEvents logging and
Spotlight off, and writes `.tmpdisk-ready` only once a mount has read
both markers, so a consumer never takes a disk that is half made.

No case calls the real hdiutil or diskutil: fakes on PATH keep the disks
in a JSON file, and the volumes folder is a temporary one. A case that
stops the process between two steps writes the state that stop leaves,
and the next run must finish it or undo it.
"""

import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "machine-setup.sh"
EXAMPLE = HERE.parent / "examples/launchagents/local.system-tools.tmpdisk.plist"

DRIFT_WORDS = re.compile(r"DIFFERS|MISSING|STALE|ABSENT|UNLOADED|EXTRA")
MB = 64
SECTORS = MB * 2048

# hdiutil and diskutil over a JSON file of images: dev, sectors, path
# (None for ram://), name and mount. A mounted volume is a folder under
# MACHINE_SETUP_TMPDISK_VOLUMES; an unmount moves it to a stash, as a
# RAM disk keeps its files while it is attached. FAKE_FAIL names the
# verbs that exit 1, as a busy volume or a refused attach does.
FAKE = r'''
import json, os, pathlib, plistlib, shutil, sys
state_file = pathlib.Path(os.environ["FAKE_DISKS"])
volumes = pathlib.Path(os.environ["MACHINE_SETUP_TMPDISK_VOLUMES"])
stash = state_file.parent / "stash"
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(state_file.parent / "calls.log", "a") as log:
    log.write(" ".join([tool, *args]) + "\n")
if args and args[0] in os.environ.get("FAKE_FAIL", "").split(","):
    print(f"{tool}: {args[0]} failed", file=sys.stderr)
    sys.exit(1)
state = json.loads(state_file.read_text())
images = state["images"]

def image(key, value):
    for item in images:
        if item.get(key) == value:
            return item
    print(f"{tool}: no such disk {value}", file=sys.stderr)
    sys.exit(1)

if tool == "hdiutil" and args[:2] == ["info", "-plist"]:
    out = []
    for item in images:
        entity = {"dev-entry": item["dev"]}
        if item.get("mount"):
            entity["mount-point"] = item["mount"]
        out.append({"image-path": item.get("path") or f"ram://{item['sectors']}", "system-entities": [entity]})
    sys.stdout.buffer.write(plistlib.dumps({"images": out}))
elif tool == "hdiutil" and args[:2] == ["attach", "-nomount"]:
    dev = f"/dev/disk{state['next']}"
    state["next"] += 1
    images.append({"dev": dev, "sectors": int(args[2].split("//", 1)[1]), "mount": None})
    print(dev + "          \t")
elif tool == "hdiutil" and args[0] == "detach":
    item = image("dev", args[1])
    if item.get("mount"):
        shutil.rmtree(item["mount"])
    shutil.rmtree(stash / item["dev"].rsplit("/", 1)[1], ignore_errors=True)
    images.remove(item)
elif tool == "diskutil" and args[0] == "erasevolume":
    item = image("dev", args[3])
    mount = volumes / args[2]
    (mount / ".fseventsd").mkdir(parents=True)
    item.update(name=args[2], mount=str(mount))
elif tool == "diskutil" and args[0] == "unmount":
    item = image("mount", args[1])
    stash.mkdir(exist_ok=True)
    shutil.move(item["mount"], stash / item["dev"].rsplit("/", 1)[1])
    item["mount"] = None
elif tool == "diskutil" and args[0] == "mount":
    item = image("dev", args[1])
    mount = volumes / item["name"]
    shutil.move(stash / item["dev"].rsplit("/", 1)[1], mount)
    item["mount"] = str(mount)
else:
    print(f"{tool}: the fake does not know {args}", file=sys.stderr)
    sys.exit(2)
state_file.write_text(json.dumps(state))
'''


def write_stub(directory, name, body):
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class TmpdiskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("work\n")
        self.volumes = base / "Volumes"
        self.volumes.mkdir()
        self.mount = self.volumes / "sd-tmp"
        self.fake = base / "fake"
        self.fake.mkdir()
        self.disks = self.fake / "disks.json"
        self.disks.write_text(json.dumps({"next": 10, "images": []}))
        self.stubs = base / "stubs"
        self.stubs.mkdir()
        for name in ("hdiutil", "diskutil"):
            write_stub(self.stubs, name, f"#!{sys.executable}\n{FAKE}")
        fixture_config.seal(self, self.stubs)

    def run_stage(self, *flags, mb=str(MB), fail="", code=0):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
            "MACHINE_SETUP_TMPDISK_VOLUMES": str(self.volumes),
            "FAKE_DISKS": str(self.disks),
            "FAKE_FAIL": fail,
        }
        if mb is not None:
            env["MACHINE_SETUP_TMPDISK_MB"] = mb
        result = subprocess.run([str(SCRIPT), "update", "tmpdisk", *flags], env=env,
                                capture_output=True, text=True, cwd=self.tmp.name,
                                stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        start = result.stdout.index("== tmpdisk\n") + len("== tmpdisk\n")
        return [line for line in result.stdout[start:].splitlines() if line.startswith("  ")]

    def images(self):
        return json.loads(self.disks.read_text())["images"]

    def calls(self):
        log = self.fake / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def mutations(self):
        return [call for call in self.calls() if not call.startswith("hdiutil info")]

    def add_image(self, sectors=SECTORS, mount=None, path=None, name="sd-tmp"):
        state = json.loads(self.disks.read_text())
        dev = f"/dev/disk{state['next']}"
        state["next"] += 1
        state["images"].append({"dev": dev, "sectors": sectors, "mount": str(mount) if mount else None,
                                "path": path, "name": name})
        self.disks.write_text(json.dumps(state))
        if mount:
            (pathlib.Path(mount) / ".fseventsd").mkdir(parents=True)
        return dev

    def assert_ready(self):
        self.assertTrue((self.mount / ".fseventsd/no_log").is_file())
        self.assertTrue((self.mount / ".metadata_never_index").is_file())
        self.assertTrue((self.mount / ".tmpdisk-ready").is_file())
        lines = self.run_stage()
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], r"^  ok      .*/sd-tmp — RAM disk /dev/disk\d+, 64 MiB, FSEvents log and Spotlight off$")

    def test_off_without_a_size_reads_no_disk(self):
        lines = self.run_stage("--apply", mb=None)
        self.assertEqual(lines, ["  --      off: MACHINE_SETUP_TMPDISK_MB is unset"])
        self.assertEqual(self.calls(), [])

    def test_a_size_that_is_not_a_number_is_drift(self):
        for value in ("4g", "0", "-1", "08"):
            with self.subTest(value=value):
                lines = self.run_stage("--apply", mb=value)
                self.assertEqual(len(lines), 1, lines)
                self.assertTrue(lines[0].startswith(f"  DIFFERS MACHINE_SETUP_TMPDISK_MB='{value}' is not a size in MiB"),
                                lines)
        self.assertEqual(self.calls(), [])

    def test_absent_disk_is_missing_then_made_with_markers_before_the_last_mount(self):
        lines = self.run_stage()
        self.assertTrue(lines[0].startswith(f"  MISSING {self.mount} — no RAM disk of 64 MiB"), lines)
        self.assertNotRegex("\n".join(lines[1:]), DRIFT_WORDS)
        self.assertEqual(self.mutations(), [], "a dry run changed a disk")
        lines = self.run_stage("--apply")
        self.assertIn(f"  created {self.mount} — RAM disk /dev/disk10, 64 MiB, FSEvents log and Spotlight off", lines)
        self.assertEqual(self.mutations(), [
            f"hdiutil attach -nomount ram://{SECTORS}",
            "diskutil erasevolume HFS+ sd-tmp /dev/disk10",
            f"diskutil unmount {self.mount}",
            "diskutil mount /dev/disk10",
        ])
        self.assertFalse((self.state / "tmpdisk.lock").exists())
        self.assert_ready()

    def test_a_refused_attach_fails_and_leaves_no_disk(self):
        lines = self.run_stage("--apply", fail="attach", code=1)
        self.assertTrue(any(line.startswith("  FAILED  hdiutil attach -nomount") for line in lines), lines)
        self.assertEqual(self.images(), [])
        self.assertFalse((self.state / "tmpdisk.lock").exists())

    def test_a_failed_erase_detaches_the_device(self):
        lines = self.run_stage("--apply", fail="erasevolume", code=1)
        self.assertTrue(any(line.startswith("  FAILED  diskutil erasevolume HFS+ sd-tmp /dev/disk10") for line in lines),
                        lines)
        self.assertEqual(self.images(), [])
        self.assertIn("hdiutil detach /dev/disk10", self.calls())
        self.assertFalse((self.state / "tmpdisk.lock").exists())

    def test_a_busy_remount_fails_and_the_next_run_finishes_it(self):
        lines = self.run_stage("--apply", fail="unmount", code=1)
        self.assertTrue(any(line.startswith(f"  FAILED  diskutil unmount {self.mount}") for line in lines), lines)
        self.assertFalse((self.mount / ".tmpdisk-ready").exists(), "ready before a mount read the markers")
        lines = self.run_stage()
        self.assertTrue(lines[0].startswith(f"  DIFFERS {self.mount} has not been mounted since its markers"), lines)
        self.run_stage("--apply")
        self.assert_ready()

    # Each state a stop between two steps leaves, and the run after it.

    def test_stopped_after_the_lock_a_dead_holder_is_cleared(self):
        dead = subprocess.run(["/bin/sh", "-c", "echo $$"], capture_output=True, text=True).stdout.strip()
        (self.state / "tmpdisk.lock").mkdir()
        (self.state / "tmpdisk.lock/pid").write_text(dead + "\n")
        self.run_stage("--apply")
        self.assert_ready()

    def test_a_live_holder_is_left_alone(self):
        (self.state / "tmpdisk.lock").mkdir()
        (self.state / "tmpdisk.lock/pid").write_text(f"{os.getpid()}\n")
        lines = self.run_stage("--apply")
        self.assertIn(f"  SKIP    another run (pid {os.getpid()}) is making {self.mount}", lines)
        self.assertEqual(self.mutations(), [])

    def test_stopped_after_attach_the_device_is_detached_and_made_again(self):
        orphan = self.add_image()
        other = self.add_image(sectors=SECTORS * 2)
        lines = self.run_stage()
        self.assertIn(f"  EXTRA   {orphan} — a {MB} MiB RAM disk with no volume, left by a stopped run", lines)
        self.assertNotIn(other, "\n".join(lines))
        self.run_stage("--apply")
        self.assertIn(f"hdiutil detach {orphan}", self.calls())
        self.assertNotIn(f"hdiutil detach {other}", self.calls())
        self.assertEqual(sorted(image["dev"] for image in self.images()), [other, "/dev/disk12"])
        self.assert_ready()

    def test_stopped_after_erase_the_markers_are_written_and_mounted(self):
        self.add_image(mount=self.mount)
        lines = self.run_stage()
        self.assertTrue(lines[0].startswith(f"  DIFFERS {self.mount} lacks .fseventsd/no_log .metadata_never_index"), lines)
        self.run_stage("--apply")
        self.assertNotIn("hdiutil attach", "\n".join(self.calls()))
        self.assert_ready()

    def test_stopped_after_the_markers_the_disk_is_mounted_again(self):
        self.add_image(mount=self.mount)
        (self.mount / ".fseventsd/no_log").touch()
        (self.mount / ".metadata_never_index").touch()
        lines = self.run_stage()
        self.assertTrue(lines[0].startswith(f"  DIFFERS {self.mount} has not been mounted since its markers"), lines)
        self.run_stage("--apply")
        self.assertEqual(self.mutations(), [f"diskutil unmount {self.mount}", "diskutil mount /dev/disk10"])
        self.assert_ready()

    def test_stopped_after_unmount_the_device_is_detached_and_made_again(self):
        orphan = self.add_image()
        self.run_stage("--apply")
        self.assertIn(f"hdiutil detach {orphan}", self.calls())
        self.assert_ready()

    # What the stage leaves alone.

    def test_a_folder_that_is_not_a_ram_disk_is_drift_and_kept(self):
        self.mount.mkdir()
        (self.mount / "keep").write_text("x")
        lines = self.run_stage("--apply")
        self.assertEqual(lines, [f"  DIFFERS {self.mount} exists and is not a RAM disk; move it away, then rerun"])
        self.assertEqual(self.mutations(), [])
        self.assertTrue((self.mount / "keep").exists())

    def test_a_disk_image_mounted_there_is_not_a_ram_disk(self):
        self.add_image(mount=self.mount, path="/Users/example/disk.dmg")
        lines = self.run_stage("--apply")
        self.assertEqual(lines, [f"  DIFFERS {self.mount} exists and is not a RAM disk; move it away, then rerun"])
        self.assertEqual(self.mutations(), [])

    def test_another_size_is_drift_and_never_detached(self):
        dev = self.add_image(sectors=SECTORS * 2, mount=self.mount)
        for name in (".fseventsd/no_log", ".metadata_never_index", ".tmpdisk-ready"):
            (self.mount / name).touch()
        lines = self.run_stage("--apply")
        self.assertIn(f"  DIFFERS {self.mount} is 128 MiB, MACHINE_SETUP_TMPDISK_MB wants 64; "
                      f"detach it while no gate runs ('hdiutil detach {dev}'), then rerun", lines)
        self.assertEqual(self.mutations(), [])

    def test_an_unreadable_disk_list_is_unknown_not_absent(self):
        lines = self.run_stage("--apply", fail="info", code=1)
        self.assertTrue(lines and lines[0].startswith("  FAILED  hdiutil info -plist"), lines)
        self.assertNotIn("ok", " ".join(line.split()[0] for line in lines))
        self.assertEqual(self.mutations(), [])

    def test_the_stage_runs_before_cron_and_agents(self):
        text = SCRIPT.read_text()
        stages = re.search(r'^STAGES="([^"]*)"', text, re.M).group(1).split()
        self.assertLess(stages.index("tmpdisk"), stages.index("cron"), stages)
        self.assertLess(stages.index("tmpdisk"), stages.index("agents"), stages)

    def test_the_example_agent_makes_the_disk_at_login(self):
        import plistlib
        plist = plistlib.loads(EXAMPLE.read_bytes())
        self.assertEqual(plist["Label"], "@LABEL@")
        self.assertIs(plist["RunAtLoad"], True)
        self.assertEqual(plist["ProgramArguments"][-1], "@ROOT@/local-machine-setup/machine-setup.sh")
        self.assertEqual(plist["ProgramArguments"][2], '"$0" update tmpdisk --apply; exit $?')
        self.assertIn("SYSTEM_TOOLS_CONFIG", plist["EnvironmentVariables"])


if __name__ == "__main__":
    unittest.main()
