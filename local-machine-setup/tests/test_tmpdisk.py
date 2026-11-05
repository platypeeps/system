"""The tmpdisk stage keeps a RAM disk for the gate's and test runs' temp folders (sd:7092).

fs_usage on the hub put about 13,000 of 54,801 file writes in 30 s under
$TMPDIR, in sd-tests-* and sd-local-gate-* folders, while fseventsd grew
to 17 GB. The stage keeps a small RAM disk with FSEvents logging and
Spotlight off, and writes `.tmpdisk-ready` only once a mount has read
both markers, so a consumer never takes a disk that is half made.

No case calls the real hdiutil or diskutil: fakes on PATH keep
the disks in a JSON file, and the volumes folder is a temporary one. A
case that stops the process between two steps writes the state that stop
leaves, and the next run must finish it or undo it.

Every call the fakes see must come while the stage holds its lock: the
login agent and the nightly update are two triggers that can meet. The
stage detaches only a device it attached in the same run, after its
erase failed; any other RAM disk may be the operator's, so it is named
with the command to detach it by hand and left alone.

The disk is APFS: HFS+ keeps whole-second mtimes, and pack tests that
see a change by mtime missed writes on it (sd:7594).
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
# (None for ram://), name, mount, fs, and for APFS its volume device.
# A mounted volume is a folder under MACHINE_SETUP_TMPDISK_VOLUMES; an
# unmount moves it to a stash, as a RAM disk keeps its files while it
# is attached. FAKE_FAIL names the verbs that exit 1, as a busy volume
# or a refused attach does. APFS is what macOS 27.2 showed on a probe
# disk (sd:7594): the erase makes a synthesized container and volume
# that `hdiutil info` lists under the ram:// image, and `diskutil
# mount` of the physical store fails; only the volume mounts. The fake
# lists the container first, with a shorter number than the device, as
# a reused low number can make it: the stage must find the device by
# its content hint, not by name (sd:7636). FAKE_HIDE_VOLUME leaves the
# APFS volume out of `hdiutil info`.
FAKE = r'''
import json, os, pathlib, plistlib, shutil, sys
state_file = pathlib.Path(os.environ["FAKE_DISKS"])
volumes = pathlib.Path(os.environ["MACHINE_SETUP_TMPDISK_VOLUMES"])
stash = state_file.parent / "stash"
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
lock = pathlib.Path(os.environ["FAKE_LOCK"])
try:
    holder = int(os.readlink(lock))
    os.kill(holder, 0)
    held = "locked"
except (OSError, ValueError):
    held = "unlocked"
with open(state_file.parent / "calls.log", "a") as log:
    log.write(" ".join([tool, *args]) + "\n")
with open(state_file.parent / "locks.log", "a") as log:
    log.write(f"{held} {tool} {' '.join(args)}\n")
if args and args[0] in os.environ.get("FAKE_FAIL", "").split(","):
    print(f"{tool}: {args[0]} failed", file=sys.stderr)
    sys.exit(1)
# FAKE_FAIL_NTH=verb:n fails only the nth call of that verb.
nth_verb, _, nth = os.environ.get("FAKE_FAIL_NTH", "").partition(":")
if args and args[0] == nth_verb:
    seen = state_file.parent / f"seen-{nth_verb}"
    count = int(seen.read_text()) + 1 if seen.exists() else 1
    seen.write_text(str(count))
    if count == int(nth):
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

APFS_CONTAINER = "EF57347C-0000-11AA-AA11-00306543ECAC"
APFS_VOLUME = "41504653-0000-11AA-AA11-00306543ECAC"

if tool == "hdiutil" and args[:2] == ["info", "-plist"]:
    out = []
    for item in images:
        entities = [{"content-hint": "", "dev-entry": item["dev"]}]
        if item.get("fs") == "apfs":
            entities.insert(0, {"content-hint": APFS_CONTAINER, "dev-entry": item["vol"][:-2]})
            if not os.environ.get("FAKE_HIDE_VOLUME"):
                entities.append({"content-hint": APFS_VOLUME, "dev-entry": item["vol"]})
        if item.get("mount"):
            entities[-1]["mount-point"] = item["mount"]
        out.append({"image-path": item.get("path") or f"ram://{item['sectors']}", "system-entities": entities})
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
    item.update(name=args[2], mount=str(mount), fs="apfs" if args[1].lower() == "apfs" else "hfs")
    if item["fs"] == "apfs":
        item["vol"] = f"/dev/disk{state.get('cnext', 1)}s1"
        state["cnext"] = state.get("cnext", 1) + 1
elif tool == "diskutil" and args[0] == "unmount":
    item = image("mount", args[1])
    stash.mkdir(exist_ok=True)
    shutil.move(item["mount"], stash / item["dev"].rsplit("/", 1)[1])
    item["mount"] = None
elif tool == "diskutil" and args[0] == "mount":
    item = next((i for i in images if args[1] in (i["dev"], i.get("vol"))), None) or image("dev", args[1])
    if item.get("fs") == "apfs" and args[1] != item["vol"]:
        print(f"Volume on {args[1]} failed to mount because it appears to be\n"
              "an APFS Physical Store (or is the APFS Container already mounted?)", file=sys.stderr)
        sys.exit(1)
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
        self.lock = self.state / "tmpdisk.lock"
        self.record = self.state / "tmpdisk.device"
        fixture_config.seal(self, self.stubs)

    def run_stage(self, *flags, mb=str(MB), fail="", fail_nth="", hide_volume="", code=0):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
            "MACHINE_SETUP_TMPDISK_VOLUMES": str(self.volumes),
            "FAKE_DISKS": str(self.disks),
            "FAKE_FAIL": fail,
            "FAKE_FAIL_NTH": fail_nth,
            "FAKE_HIDE_VOLUME": hide_volume,
            "FAKE_LOCK": str(self.lock),
        }
        if mb is not None:
            env["MACHINE_SETUP_TMPDISK_MB"] = mb
        result = subprocess.run([str(SCRIPT), "update", "tmpdisk", *flags], env=env,
                                capture_output=True, text=True, cwd=self.tmp.name,
                                stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        locks = self.fake / "locks.log"
        unlocked = [line for line in (locks.read_text().splitlines() if locks.exists() else [])
                    if not line.startswith("locked ")]
        self.assertEqual(unlocked, [], "the stage read or changed a disk without its lock")
        start = result.stdout.index("== tmpdisk\n") + len("== tmpdisk\n")
        return [line for line in result.stdout[start:].splitlines() if line.startswith("  ")]

    def images(self):
        return json.loads(self.disks.read_text())["images"]

    def calls(self):
        log = self.fake / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def mutations(self):
        return [call for call in self.calls() if not call.startswith("hdiutil info")]

    def add_image(self, sectors=SECTORS, mount=None, path=None, name="sd-tmp", fs="apfs"):
        state = json.loads(self.disks.read_text())
        dev = f"/dev/disk{state['next']}"
        state["next"] += 1
        item = {"dev": dev, "sectors": sectors, "mount": str(mount) if mount else None,
                "path": path, "name": name, "fs": fs}
        if fs == "apfs":
            item["vol"] = f"/dev/disk{state.get('cnext', 1)}s1"
            state["cnext"] = state.get("cnext", 1) + 1
        state["images"].append(item)
        self.disks.write_text(json.dumps(state))
        if mount:
            (pathlib.Path(mount) / ".fseventsd").mkdir(parents=True)
        return dev

    def left_alone(self, dev):
        return f"  --      {dev}: an unmounted RAM disk; left alone; detach it by hand: hdiutil detach {dev}"

    def blocked(self, dev):
        return (f"  DIFFERS {self.mount} is missing, and {dev} is an unmounted RAM disk of 64 MiB; "
                "no second disk is made beside it: detach it by hand, then rerun")

    def assert_ready(self):
        self.assertFalse(self.record.exists(), "the stage keeps no device record")
        self.assertFalse(os.path.lexists(self.lock), "the lock outlived the run")
        self.assertTrue((self.mount / ".fseventsd/no_log").is_file())
        self.assertTrue((self.mount / ".metadata_never_index").is_file())
        self.assertTrue((self.mount / ".tmpdisk-ready").is_file())
        # Another RAM disk a case left may still be named; it is not drift.
        lines = [line for line in self.run_stage() if not line.startswith("  --      ")]
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
            "diskutil erasevolume APFS sd-tmp /dev/disk10",
            f"diskutil unmount {self.mount}",
            "diskutil mount /dev/disk1s1",
        ])
        # HFS+ keeps whole-second mtimes; pack tests that see a change by
        # mtime missed writes on it (sd:7594).
        self.assertEqual([image["fs"] for image in self.images()], ["apfs"])
        self.assertFalse(os.path.lexists(self.lock))
        self.assert_ready()

    def test_a_refused_attach_fails_and_leaves_no_disk(self):
        lines = self.run_stage("--apply", fail="attach", code=1)
        self.assertTrue(any(line.startswith("  FAILED  hdiutil attach -nomount") for line in lines), lines)
        self.assertEqual(self.images(), [])
        self.assertFalse(os.path.lexists(self.lock))

    def test_a_failed_erase_detaches_the_device(self):
        lines = self.run_stage("--apply", fail="erasevolume", code=1)
        self.assertTrue(any(line.startswith("  FAILED  diskutil erasevolume APFS sd-tmp /dev/disk10") for line in lines),
                        lines)
        self.assertEqual(self.images(), [])
        self.assertIn("hdiutil detach /dev/disk10", self.calls())
        self.assertFalse(os.path.lexists(self.lock))

    def test_a_failed_read_after_the_erase_leaves_it_for_the_next_run(self):
        # The erase made the volume; the read that names it failed.
        lines = self.run_stage("--apply", fail_nth="info:2", code=1)
        self.assertIn("  FAILED  hdiutil info -plist: the volume on /dev/disk10 is unknown (exit 1)", lines)
        self.assertFalse((self.mount / ".tmpdisk-ready").exists(), "ready before a mount read the markers")
        self.assertNotIn("hdiutil detach /dev/disk10", self.calls())
        lines = self.run_stage()
        self.assertTrue(lines[0].startswith(f"  DIFFERS {self.mount} lacks"), lines)
        self.run_stage("--apply")
        self.assertEqual([image["dev"] for image in self.images()], ["/dev/disk10"])
        self.assert_ready()

    def test_a_volume_missing_after_the_erase_is_named_as_the_lookup(self):
        lines = self.run_stage("--apply", hide_volume="1", code=1)
        self.assertIn("  FAILED  hdiutil info -plist lists no APFS volume on /dev/disk10 after its erase (exit 1)",
                      lines)
        self.assertNotIn("is not APFS", "\n".join(lines))
        self.assertFalse((self.mount / ".tmpdisk-ready").exists())
        self.assertNotIn("hdiutil detach /dev/disk10", self.calls())

    def test_a_volume_still_missing_on_the_next_run_is_unknown_not_hfs(self):
        # The erase made APFS; only the lookup lacks its volume (sd:7645).
        self.run_stage("--apply", hide_volume="1", code=1)
        lines = self.run_stage(hide_volume="1")
        self.assertEqual(lines, [self.no_volume("/dev/disk10")])
        self.assertNotIn("is not APFS", "\n".join(lines))

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
        os.symlink(dead, self.lock)
        self.run_stage("--apply")
        self.assert_ready()

    def test_a_live_lock_taken_after_the_dead_one_was_read_stays(self):
        # A contender clears the dead lock and takes its own between this
        # run's read and its move: the mv stub plays that contender.
        dead = subprocess.run(["/bin/sh", "-c", "echo $$"], capture_output=True, text=True).stdout.strip()
        os.symlink(dead, self.lock)
        write_stub(self.stubs, "mv", f"""#!/bin/sh
case "$1" in
  "{self.lock}") rm -f "$1"; ln -s {os.getpid()} "$1" ;;
esac
exec /bin/mv "$@"
""")
        lines = self.run_stage("--apply")
        self.assertEqual(lines, [f"  SKIP    another run (pid {os.getpid()}) took {self.lock}"])
        self.assertEqual(os.readlink(self.lock), str(os.getpid()))
        self.assertEqual(self.calls(), [])

    def test_a_live_holder_is_left_alone(self):
        os.symlink(str(os.getpid()), self.lock)
        for flags in ((), ("--apply",)):
            with self.subTest(flags=flags):
                lines = self.run_stage(*flags)
                self.assertEqual(lines, [f"  SKIP    another run (pid {os.getpid()}) holds {self.lock}"])
        # Not even a read: the other run's attach may sit between two steps.
        self.assertEqual(self.calls(), [])
        self.assertEqual(os.readlink(self.lock), str(os.getpid()))

    def test_a_lock_that_names_no_pid_is_never_removed(self):
        os.symlink("not-a-pid", self.lock)
        lines = self.run_stage("--apply")
        self.assertEqual(lines, [f"  SKIP    {self.lock} names no pid; remove it once no run holds it"])
        self.assertEqual(os.readlink(self.lock), "not-a-pid")
        self.assertEqual(self.calls(), [])

    def test_a_folder_at_the_lock_path_is_never_taken(self):
        self.lock.mkdir()
        lines = self.run_stage("--apply")
        self.assertEqual(lines, [f"  SKIP    {self.lock} names no pid; remove it once no run holds it"])
        self.assertEqual(list(self.lock.iterdir()), [])
        self.assertEqual(self.calls(), [])

    def test_the_lock_is_one_atomic_link_to_the_pid(self):
        text = SCRIPT.read_text()
        self.assertIn('ln -s "$$" "$TMPDISK_LOCK"', text)
        self.assertNotIn("tmpdisk.lock/pid", text)

    def test_stopped_after_attach_the_device_is_left_and_no_second_is_made(self):
        # Attach done, erase not: an unmounted RAM disk of the size. The
        # stage cannot prove it its own, so it never detaches it.
        orphan = self.add_image(fs=None)
        lines = self.run_stage()
        self.assertEqual(lines, [self.left_alone(orphan), self.blocked(orphan)])
        self.run_stage("--apply")
        self.assertEqual(self.mutations(), [], "the stage changed a disk it cannot prove its own")
        self.assertEqual([image["dev"] for image in self.images()], [orphan])
        # The operator detaches it by hand; the next run makes the disk.
        self.disks.write_text(json.dumps({"next": 11, "images": []}))
        self.run_stage("--apply")
        self.assert_ready()

    def test_stopped_after_unmount_the_device_is_left_and_no_second_is_made(self):
        orphan = self.add_image(name="sd-tmp")
        (self.fake / "stash" / orphan.rsplit("/", 1)[1]).mkdir(parents=True)
        self.run_stage("--apply")
        self.assertEqual(self.mutations(), [])
        self.assertEqual([image["dev"] for image in self.images()], [orphan])

    def test_a_failed_remount_of_a_ready_disk_makes_no_second_disk(self):
        dev = self.add_image(mount=self.mount)
        (self.mount / ".tmpdisk-ready").touch()
        lines = self.run_stage("--apply", fail="mount", code=1)
        volume = self.images()[0]["vol"]
        self.assertTrue(any(line.startswith(f"  FAILED  diskutil mount {volume}") for line in lines), lines)
        lines = self.run_stage("--apply")
        self.assertEqual(lines, [self.left_alone(dev), self.blocked(dev)])
        self.assertNotIn("hdiutil attach", "\n".join(self.calls()))
        self.assertEqual([image["dev"] for image in self.images()], [dev])

    def test_an_unmounted_ram_disk_of_another_size_is_left_alone(self):
        theirs = self.add_image(sectors=SECTORS * 2)
        lines = self.run_stage()
        self.assertIn(self.left_alone(theirs), lines)
        self.run_stage("--apply")
        self.assertNotIn(f"hdiutil detach {theirs}", self.calls())
        self.assertIn(theirs, [image["dev"] for image in self.images()])
        self.assert_ready()

    def test_the_stage_detaches_only_after_its_own_failed_erase(self):
        text = SCRIPT.read_text()
        body = text[text.index("TMPDISK_NAME=sd-tmp"):text.index("do_stages() {")]
        self.assertEqual(body.count("hdiutil detach \"$"), 1, "a second detach path")
        self.assertNotIn("tmpdisk.device", text)

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
        self.assertEqual(self.mutations(), [f"diskutil unmount {self.mount}", "diskutil mount /dev/disk1s1"])
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

    def test_a_ready_disk_is_left_as_it_is(self):
        self.add_image(mount=self.mount)
        for name in (".fseventsd/no_log", ".metadata_never_index", ".tmpdisk-ready"):
            (self.mount / name).touch()
        self.run_stage("--apply")
        self.assertEqual(self.mutations(), [])
        self.assert_ready()

    def no_volume(self, dev):
        return (f"  DIFFERS {self.mount}: hdiutil info lists no APFS volume on {dev}, so its "
                "file system is unknown, and HFS+ mtimes hide changes from tests; "
                f"detach it while no gate runs ('hdiutil detach {dev}'), then rerun")

    def test_an_hfs_disk_is_drift_and_never_marked_ready(self):
        # The disk sd:7092 made: HFS+, its markers written, not marked.
        dev = self.add_image(mount=self.mount, fs="hfs")
        (self.mount / ".fseventsd/no_log").touch()
        (self.mount / ".metadata_never_index").touch()
        self.assertEqual(self.run_stage(), [self.no_volume(dev)])
        self.assertEqual(self.run_stage("--apply"), [self.no_volume(dev)])
        self.assertEqual(self.mutations(), [])
        self.assertFalse((self.mount / ".tmpdisk-ready").exists(), "an HFS+ disk was marked ready")

    def test_an_hfs_disk_marked_ready_loses_the_mark(self):
        dev = self.add_image(mount=self.mount, fs="hfs")
        for name in (".fseventsd/no_log", ".metadata_never_index", ".tmpdisk-ready"):
            (self.mount / name).touch()
        lines = self.run_stage()
        self.assertEqual(lines[0], self.no_volume(dev))
        self.assertTrue((self.mount / ".tmpdisk-ready").exists(), "a dry run removed the mark")
        lines = self.run_stage("--apply")
        self.assertEqual(lines, [self.no_volume(dev),
                                 f"  removed {self.mount}/.tmpdisk-ready: consumers keep $TMPDIR"])
        self.assertFalse((self.mount / ".tmpdisk-ready").exists())
        self.assertEqual(self.mutations(), [], "the stage changed the disk itself")
        self.assertEqual([image["dev"] for image in self.images()], [dev])

    def test_an_hfs_disk_of_another_size_loses_the_mark(self):
        # The APFS check comes before the size check: a ready HFS+ disk
        # of another size must not keep its mark.
        dev = self.add_image(sectors=SECTORS * 2, mount=self.mount, fs="hfs")
        for name in (".fseventsd/no_log", ".metadata_never_index", ".tmpdisk-ready"):
            (self.mount / name).touch()
        lines = self.run_stage("--apply")
        self.assertEqual(lines, [self.no_volume(dev),
                                 f"  removed {self.mount}/.tmpdisk-ready: consumers keep $TMPDIR"])
        self.assertFalse((self.mount / ".tmpdisk-ready").exists())
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
