#!/usr/bin/env python3
"""What the backup path has to keep true (sd:1107).

Two programs are under test. `mirror-sync.sh` grew per-pair excludes and an
additive mode, and the things to hold are that excludes stay per pair, that a
destination whose parent is missing -- which is what a detached drive looks
like -- fails loudly instead of quietly mirroring nothing, and that the repo
fleet's copy outlives a `git reset --hard`. `offsite-verify.py` is the check
that reports a silent absence of the database snapshot on the NAS, so its own
failure paths are what matter: a share that is not there, a snapshot that will
not open, and counts that do not match the manifest.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FOLDER.parent / "lib"))
import system_tools_config  # noqa: E402

MIRROR_SYNC = FOLDER / "mirror-sync.sh"
OFFSITE_VERIFY = FOLDER / "offsite-verify.py"


def _load_verifier():
    """Import offsite-verify.py, whose file name is not an identifier."""
    spec = importlib.util.spec_from_file_location("offsite_verify", OFFSITE_VERIFY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mirror-sync-test-"))
        self.addCleanup(self._remove, self.work)

    @staticmethod
    def _remove(path: Path) -> None:
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_sync(
        self,
        conf: Path,
        mode: str = "sync",
        *,
        backup_root: Path | None = None,
        additive: bool = False,
    ) -> subprocess.CompletedProcess:
        environment = dict(os.environ, MIRROR_SYNC_CONF=str(conf))
        environment.pop("MIRROR_SYNC_BACKUP_ROOT", None)
        environment.pop("MIRROR_SYNC_ADDITIVE", None)
        if backup_root is not None:
            environment["MIRROR_SYNC_BACKUP_ROOT"] = str(backup_root)
        if additive:
            environment["MIRROR_SYNC_ADDITIVE"] = "1"
        return subprocess.run(
            ["/bin/sh", str(MIRROR_SYNC), mode],
            capture_output=True,
            text=True,
            env=environment,
        )


# A `stat` or `date` of one flavour, for PruneReplaced.fake_tools. "gnu" reads
# stat -f as file-system mode and date -r as a reference file; "bsd" has no
# stat -c and no date -d; "none" is a BSD stat with a date that formats no
# epoch at all. Only the flags mirror-sync.sh can use are modelled.
FAKE_TOOL = """#!{python}
import os, sys, time
flavour, tool, args = "{flavour}", "{tool}", sys.argv[1:]
with open("{log}", "a") as log:
    log.write(" ".join([tool] + args) + "\\n")

def fail(message):
    print(tool + ": " + message, file=sys.stderr)
    sys.exit(1)

def fmt_stat(fmt, path):
    try:
        st = os.stat(path)
    except OSError as error:
        fail(str(error))
    return fmt.replace("%d", str(st.st_dev)).replace("%i", str(st.st_ino))

def fmt_date(fmt, when):
    return fmt.replace("%s", str(int(when))) if fmt == "%s" else time.strftime(fmt, time.localtime(when))

if tool == "stat":
    fmt, fs, files = None, False, []
    while args:
        arg = args.pop(0)
        if arg == "--":
            files += args
            break
        if arg == "-c" and flavour == "gnu":
            fmt = args.pop(0)
        elif arg == "-f" and flavour == "gnu":
            fs = True
        elif arg == "-f":
            fmt = args.pop(0)
        elif arg.startswith("-"):
            fail("illegal option -- " + arg[1:])
        else:
            files.append(arg)
    status = 0
    for path in files:
        if fs:
            try:
                os.statvfs(path)
                print("  File: " + path + "\\n    ID: 0 Namelen: 255 Type: apfs")
            except OSError as error:
                print("stat: cannot read file system information for " + path + ": " + str(error), file=sys.stderr)
                status = 1
        else:
            print(fmt_stat(fmt or "%d %i", path))
    sys.exit(status)

when, fmt = time.time(), "%a %b %d %H:%M:%S %Z %Y"
while args:
    arg = args.pop(0)
    if arg.startswith("+"):
        fmt = arg[1:]
    elif arg == "-d" and flavour == "gnu":
        value = args.pop(0)
        if not value.startswith("@"):
            fail("unsupported -d value " + value)
        when = int(value[1:])
    elif arg == "-r" and flavour == "gnu":
        name = args.pop(0)
        try:
            when = os.stat(name).st_mtime
        except OSError as error:
            fail(name + ": " + str(error))
    elif arg == "-r" and flavour == "bsd":
        value = args.pop(0)
        when = int(value) if value.isdigit() else os.stat(value).st_mtime
    else:
        fail("illegal option -- " + arg.lstrip("-"))
print(fmt_date(fmt, when))
"""


class PerPairExcludes(Fixture):
    def test_third_field_excludes_only_its_own_pair(self) -> None:
        first = self.work / "first"
        second = self.work / "second"
        self.write(first / "keep.txt", "keep\n")
        self.write(first / "node_modules" / "junk.js", "junk\n")
        self.write(second / "keep.txt", "keep\n")
        self.write(second / "node_modules" / "wanted.js", "wanted\n")
        conf = self.write(
            self.work / "pairs.conf",
            f"{first}|{self.work}/first-mirror|node_modules/\n"
            f"{second}|{self.work}/second-mirror\n",
        )

        result = self.run_sync(conf)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.work / "first-mirror" / "keep.txt").is_file())
        self.assertFalse((self.work / "first-mirror" / "node_modules").exists())
        # The same pattern, one line further down, must not have been applied.
        self.assertTrue((self.work / "second-mirror" / "node_modules" / "wanted.js").is_file())

    def test_several_patterns_are_comma_separated(self) -> None:
        source = self.work / "source"
        self.write(source / "keep.txt", "keep\n")
        self.write(source / "target" / "debug.o", "o\n")
        self.write(source / "a" / "__pycache__" / "m.pyc", "pyc\n")
        self.write(source / "b" / "x.pyc", "pyc\n")
        conf = self.write(
            self.work / "pairs.conf",
            f"{source}|{self.work}/mirror|target/, __pycache__/ ,*.pyc\n",
        )

        result = self.run_sync(conf)

        self.assertEqual(result.returncode, 0, result.stderr)
        mirror = self.work / "mirror"
        self.assertTrue((mirror / "keep.txt").is_file())
        self.assertFalse((mirror / "target").exists())
        self.assertFalse((mirror / "a" / "__pycache__").exists())
        self.assertFalse((mirror / "b" / "x.pyc").exists())

    def test_excluded_path_already_in_the_destination_is_removed(self) -> None:
        source = self.work / "source"
        mirror = self.work / "mirror"
        self.write(source / "keep.txt", "keep\n")
        self.write(mirror / "node_modules" / "stale.js", "stale\n")
        conf = self.write(
            self.work / "pairs.conf",
            f"{source}|{mirror}|node_modules/\n",
        )

        result = self.run_sync(conf)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((mirror / "node_modules").exists())

    def test_two_field_line_still_mirrors_everything(self) -> None:
        source = self.work / "source"
        self.write(source / "keep.txt", "keep\n")
        self.write(source / "node_modules" / "wanted.js", "wanted\n")
        conf = self.write(self.work / "pairs.conf", f"{source}|{self.work}/mirror\n")

        result = self.run_sync(conf)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.work / "mirror" / "node_modules" / "wanted.js").is_file())

    def test_list_names_the_excludes(self) -> None:
        conf = self.write(
            self.work / "pairs.conf",
            f"{self.work}/a|{self.work}/b|node_modules/,target/\n",
        )

        result = self.run_sync(conf, "list")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("excluding node_modules/,target/", result.stdout)


class SpecialFiles(Fixture):
    """A socket in a source is skipped, not recreated: openrsync's
    mkstempsock failed on the backup disk and failed the whole pair."""

    def _source_with_socket(self) -> Path:
        import socket

        source = self.work / "s"
        self.write(source / "keep.txt", "keep\n")
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(source / "k"))
        return source

    def test_a_socket_is_skipped_and_the_pair_succeeds(self) -> None:
        source = self._source_with_socket()
        conf = self.write(self.work / "pairs.conf", f"{source}|{self.work}/m\n")

        result = self.run_sync(conf)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.work / "m" / "keep.txt").is_file())
        self.assertFalse(os.path.lexists(self.work / "m" / "k"))

    def test_an_additive_pass_skips_the_socket_too(self) -> None:
        source = self._source_with_socket()
        conf = self.write(self.work / "pairs.conf", f"{source}|{self.work}/m\n")

        result = self.run_sync(conf, additive=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.work / "m" / "keep.txt").is_file())
        self.assertFalse(os.path.lexists(self.work / "m" / "k"))


class UnmountedShare(Fixture):
    """A detached drive or unmounted share is a missing destination parent, and must be loud."""

    def test_missing_destination_parent_fails_and_writes_nothing(self) -> None:
        source = self.work / "source"
        self.write(source / "keep.txt", "keep\n")
        absent = self.work / "Volumes" / "Offsite" / "Backup" / "mirror" / "repos"
        conf = self.write(self.work / "pairs.conf", f"{source}|{absent}\n")

        result = self.run_sync(conf)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("destination parent missing", result.stdout + result.stderr)
        self.assertFalse(absent.exists())
        self.assertFalse(absent.parent.exists())

    def test_missing_source_fails_rather_than_emptying_the_destination(self) -> None:
        mirror = self.work / "mirror"
        self.write(mirror / "precious.txt", "precious\n")
        conf = self.write(self.work / "pairs.conf", f"{self.work}/gone|{mirror}\n")

        result = self.run_sync(conf)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue((mirror / "precious.txt").is_file())


# Stand-ins for the tools behind an evicted destination file and a source
# that loses files mid-pass. Neither can be staged for real in CI: a dataless
# file needs iCloud, and a vanished file needs a race. State lives in files
# under $MS_FAKE: `changed` names what a pass would copy, `dataless` lists the
# destination paths iCloud has evicted, `stderr` and `rc` script a failure.
FAKE_RSYNC = """#!{python}
import os, sys
state = os.environ["MS_FAKE"]
def lines(name):
    try:
        with open(os.path.join(state, name)) as f:
            return [l.rstrip("\\n") for l in f if l.strip()]
    except FileNotFoundError:
        return []
args = sys.argv[1:]
with open(os.path.join(state, "rsync.log"), "a") as log:
    log.write(" ".join(args).replace("\\r", "<CR>") + "\\n")
changed = lines("changed")
if "--dry-run" in args:
    print("Transfer starting: %d files" % len(changed))
    for name in changed:
        print(name)
    sys.exit(0)
dst = args[-1].rstrip("/")
evicted = set(lines("dataless"))
for name in changed:
    if os.path.join(dst, name) in evicted:
        print("rsync(2): error: " + name + ": mmap: Resource deadlock avoided", file=sys.stderr)
        print("rsync(1): error: unexpected end of file", file=sys.stderr)
        sys.exit(11)
for line in lines("stderr"):
    print(line, file=sys.stderr)
sys.exit(int((lines("rc") or ["0"])[0]))
"""

# A BSD stat that reports SF_DATALESS (0x40000000) for the listed paths.
FAKE_BSD_STAT = """#!{python}
import os, sys
state = os.environ["MS_FAKE"]
args = sys.argv[1:]
if args[0] != "-f":
    print("stat: illegal option -- " + args[0].lstrip("-"), file=sys.stderr)
    sys.exit(1)
fmt, path = args[1], args[-1]
if fmt == "%Xf":
    with open(os.path.join(state, "dataless")) as f:
        evicted = {{l.rstrip("\\n") for l in f}}
    print("40008060" if path in evicted else "8040")
    sys.exit(0)
try:
    st = os.stat(path)
except OSError as error:
    print("stat: " + str(error), file=sys.stderr)
    sys.exit(1)
print(fmt.replace("%d", str(st.st_dev)).replace("%i", str(st.st_ino)))
"""

# brctl download: iCloud fetches the file, so it is evicted no longer, unless
# the path is listed in `stuck`.
FAKE_BRCTL = """#!{python}
import os, sys
state = os.environ["MS_FAKE"]
path = sys.argv[2]
with open(os.path.join(state, "brctl.log"), "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
stuck_file = os.path.join(state, "stuck")
stuck = open(stuck_file).read().splitlines() if os.path.exists(stuck_file) else []
if path not in stuck:
    name = os.path.join(state, "dataless")
    kept = [l for l in open(name).read().splitlines() if l != path]
    with open(name, "w") as f:
        f.write("".join(l + "\\n" for l in kept))
"""


class FakeTools(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.state = self.work / "state"
        self.state.mkdir()
        (self.state / "dataless").write_text("")
        bin_dir = self.work / "fake-bin"
        bin_dir.mkdir()
        for name, body in (("rsync", FAKE_RSYNC), ("stat", FAKE_BSD_STAT), ("brctl", FAKE_BRCTL)):
            tool = bin_dir / name
            tool.write_text(body.format(python=sys.executable))
            tool.chmod(0o755)
        self.source = self.work / "s"
        self.write(self.source / "vault" / ".gitignore", "new\n")
        self.mirror = self.work / "m"
        self.write(self.mirror / "vault" / ".gitignore", "old\n")
        self.conf = self.write(self.work / "pairs.conf", f"{self.source}|{self.mirror}\n")

    def state_file(self, name: str, lines: list[str]) -> None:
        (self.state / name).write_text("".join(line + "\n" for line in lines))

    def sync(self) -> subprocess.CompletedProcess:
        environment = dict(
            os.environ,
            MIRROR_SYNC_CONF=str(self.conf),
            MS_FAKE=str(self.state),
            MIRROR_SYNC_MATERIALIZE_WAIT="2",
            PATH=f"{self.work / 'fake-bin'}:{os.environ['PATH']}",
        )
        for name in ("MIRROR_SYNC_BACKUP_ROOT", "MIRROR_SYNC_ADDITIVE"):
            environment.pop(name, None)
        return subprocess.run(
            ["/bin/sh", str(MIRROR_SYNC), "sync"], capture_output=True, text=True, env=environment
        )

    def rsync_runs(self) -> list[str]:
        log = self.state / "rsync.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if "--dry-run" not in line]


class EvictedDestination(FakeTools):
    """An evicted iCloud file at the destination cannot be rsync's basis:
    openrsync answers "Resource deadlock avoided" and aborts the pair, with
    or without -W (sd:1947). The pass downloads what it is about to update
    and tries the pair once more; it never deletes the evicted copy."""

    def test_an_evicted_file_is_downloaded_and_the_pair_retried(self) -> None:
        evicted = f"{self.mirror}/vault/.gitignore"
        self.state_file("changed", ["vault/.gitignore"])
        self.state_file("dataless", [evicted])

        result = self.sync()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.state / "brctl.log").read_text(), f"download {evicted}\n")
        self.assertEqual(len(self.rsync_runs()), 2)
        self.assertIn(f"--- downloaded: {evicted}", result.stdout)
        self.assertTrue((self.mirror / "vault" / ".gitignore").is_file())

    def test_only_evicted_files_the_pass_would_update_are_downloaded(self) -> None:
        self.write(self.mirror / "untouched.txt", "stays evicted\n")
        evicted = f"{self.mirror}/vault/.gitignore"
        self.state_file("changed", ["vault/.gitignore"])
        self.state_file("dataless", [evicted, f"{self.mirror}/untouched.txt"])

        result = self.sync()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.state / "brctl.log").read_text(), f"download {evicted}\n")

    def test_a_file_that_stays_evicted_fails_the_pair_by_name(self) -> None:
        evicted = f"{self.mirror}/vault/.gitignore"
        self.state_file("changed", ["vault/.gitignore"])
        self.state_file("dataless", [evicted])
        self.state_file("stuck", [evicted])

        result = self.sync()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f"still evicted after 2s: {evicted}", result.stderr)
        self.assertIn(f"{self.source} (rsync error)", result.stdout)
        self.assertEqual(len(self.rsync_runs()), 1)


class VanishedSource(FakeTools):
    """A live tree loses files during a pass. openrsync exits 23 for that, as
    for any partial transfer; when every error is a vanished source file the
    pass copied all that still exists and must not alarm (sd:1947)."""

    VANISHED = "rsync(9): error: {src}/logs/.job.attempt: open (2) in /home/example: No such file or directory"

    def test_only_vanished_files_is_not_a_failure(self) -> None:
        self.state_file("stderr", [self.VANISHED.format(src=self.source)])
        self.state_file("rc", ["23"])

        result = self.sync()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("vanished during the pass", result.stderr)
        self.assertIn("all 1 pair(s) mirrored", result.stdout)

    def test_gnu_rsync_vanished_exit_is_not_a_failure(self) -> None:
        self.state_file("stderr", ["file has vanished: \"/example/logs/.job.attempt\""])
        self.state_file("rc", ["24"])

        result = self.sync()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_vanished_file_beside_another_error_still_fails(self) -> None:
        self.state_file(
            "stderr",
            [
                "rsync(9): error: hoa/downloads: unlinkat: Directory not empty",
                self.VANISHED.format(src=self.source),
            ],
        )
        self.state_file("rc", ["23"])

        result = self.sync()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f"{self.source} (rsync error)", result.stdout)

    def test_a_partial_transfer_without_error_lines_still_fails(self) -> None:
        self.state_file("rc", ["23"])

        result = self.sync()

        self.assertEqual(result.returncode, 1, result.stdout)


class NestedByIdentity(Fixture):
    """Source and destination must not nest, however either is spelled.

    The text rail sees two unrelated paths when one runs through a link to the
    other. The identity rail compares device and inode, as the backup root
    check does.
    """

    def setUp(self) -> None:
        super().setUp()
        self.work = self.work.resolve()

    def test_a_destination_reached_through_a_link_into_the_source_is_refused(self) -> None:
        source = self.work / "source"
        self.write(source / "work.md", "work\n")
        alias = self.work / "alias"
        alias.symlink_to(source, target_is_directory=True)
        for destination in (alias, alias / "copy"):
            with self.subTest(destination=destination):
                conf = self.write(self.work / "pairs.conf", f"{source}|{destination}\n")

                result = self.run_sync(conf)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("by identity", result.stderr)
                self.assertFalse((source / "copy").exists())
                self.assertEqual(sorted(p.name for p in source.iterdir()), ["work.md"])

    def test_a_source_reached_through_a_link_into_the_destination_is_refused(self) -> None:
        # Unrefused, --delete would remove mirror/inner: the source itself.
        mirror = self.work / "mirror"
        inner = self.write(mirror / "inner" / "work.md", "work\n")
        alias = self.work / "alias"
        alias.symlink_to(mirror, target_is_directory=True)
        conf = self.write(self.work / "pairs.conf", f"{alias / 'inner'}|{mirror}\n")

        result = self.run_sync(conf)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("by identity", result.stderr)
        self.assertEqual(inner.read_text(), "work\n")

    def test_unrelated_paths_through_links_still_mirror(self) -> None:
        # The guard on the two above: a link is not by itself an overlap.
        source = self.work / "source"
        self.write(source / "work.md", "work\n")
        (self.work / "disk").mkdir()
        alias = self.work / "alias"
        alias.symlink_to(self.work / "disk", target_is_directory=True)
        conf = self.write(self.work / "pairs.conf", f"{source}|{alias / 'mirror'}\n")

        result = self.run_sync(conf)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.work / "disk" / "mirror" / "work.md").read_text(), "work\n")


class VersionedMirror(Fixture):
    """An exact mirror copies a destructive edit as faithfully as a useful one."""

    def test_a_deleted_source_file_survives_in_the_destination(self) -> None:
        source = self.work / "source"
        mirror = self.work / "mirror"
        self.write(source / "keep.txt", "keep\n")
        self.write(source / "work.md", "uncommitted work\n")
        conf = self.write(self.work / "pairs.conf", f"{source}|{mirror}\n")
        self.assertEqual(self.run_sync(conf, additive=True).returncode, 0)

        # What `git reset --hard` did in sd:1107: the work is gone from the
        # source, and an exact mirror would carry that into the only other copy.
        (source / "work.md").unlink()
        result = self.run_sync(conf, additive=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((mirror / "work.md").read_text(), "uncommitted work\n")

    def test_an_exact_pass_still_deletes(self) -> None:
        # Additive is opt-in. The local lists must keep the semantics they
        # have, or this change would quietly rewrite what they mean.
        source = self.work / "source"
        mirror = self.work / "mirror"
        self.write(source / "keep.txt", "keep\n")
        self.write(source / "work.md", "work\n")
        conf = self.write(self.work / "pairs.conf", f"{source}|{mirror}\n")
        self.assertEqual(self.run_sync(conf).returncode, 0)

        (source / "work.md").unlink()
        self.assertEqual(self.run_sync(conf).returncode, 0)

        self.assertFalse((mirror / "work.md").exists())

    def test_an_overwritten_file_keeps_its_previous_contents(self) -> None:
        source = self.work / "source"
        mirror = self.work / "mirror"
        replaced = self.work / "replaced"
        self.write(source / "work.md", "the good version\n")
        conf = self.write(self.work / "pairs.conf", f"{source}|{mirror}\n")
        self.assertEqual(
            self.run_sync(conf, backup_root=replaced, additive=True).returncode, 0
        )

        self.write(source / "work.md", "clobbered\n")
        self.assertEqual(
            self.run_sync(conf, backup_root=replaced, additive=True).returncode, 0
        )

        kept = list(replaced.rglob("work.md"))
        self.assertEqual(len(kept), 1, f"expected one preserved copy, got {kept}")
        self.assertEqual(kept[0].read_text(), "the good version\n")
        self.assertEqual((mirror / "work.md").read_text(), "clobbered\n")

    def test_a_backup_root_inside_the_destination_is_refused(self) -> None:
        source = self.work / "source"
        mirror = self.work / "mirror"
        self.write(source / "keep.txt", "keep\n")
        conf = self.write(self.work / "pairs.conf", f"{source}|{mirror}\n")

        result = self.run_sync(conf, backup_root=mirror / "replaced", additive=True)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("inside the destination", result.stdout + result.stderr)

    def test_versioning_is_off_unless_the_root_is_set(self) -> None:
        source = self.work / "source"
        mirror = self.work / "mirror"
        self.write(source / "work.md", "work\n")
        conf = self.write(self.work / "pairs.conf", f"{source}|{mirror}\n")
        self.assertEqual(self.run_sync(conf).returncode, 0)
        (source / "work.md").unlink()
        self.write(source / "other.txt", "other\n")

        self.assertEqual(self.run_sync(conf).returncode, 0)

        self.assertFalse((mirror / "work.md").exists())
        self.assertEqual([p.name for p in mirror.iterdir()], ["other.txt"])


class PruneReplaced(Fixture):
    """The replaced store keeps a run folder for MIRROR_SYNC_BACKUP_KEEP_DAYS.

    Every keep test also plants a stale run and asserts it went, so each one
    proves the kept folder survived a pass that did prune.
    """

    def setUp(self) -> None:
        super().setUp()
        # The prune logs the physical path; /var is /private/var here.
        self.work = self.work.resolve()
        self.source = self.work / "source"
        self.mirror = self.work / "mirror"
        self.replaced = self.work / "replaced"
        self.write(self.source / "work.md", "work\n")
        self.conf = self.write(self.work / "pairs.conf", f"{self.source}|{self.mirror}\n")

    def run_dir(self, days_old: float, *, name: str | None = None) -> Path:
        # Named the way mirror-sync.sh names BACKUP_RUN: local time, to the second.
        import datetime

        if name is None:
            when = datetime.datetime.now() - datetime.timedelta(days=days_old)
            name = when.strftime("%Y-%m-%dT%H%M%S")
        return self.write(self.replaced / name / "mirror" / "work.md", "old\n").parent.parent

    def prune(
        self,
        *,
        mode: str = "sync",
        root: Path | str | None = None,
        keep_days: str | None = None,
        path: str | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        environment = dict(os.environ, MIRROR_SYNC_CONF=str(self.conf), MIRROR_SYNC_ADDITIVE="1")
        if path is not None:
            environment["PATH"] = path
        environment["MIRROR_SYNC_BACKUP_ROOT"] = str(root) if root is not None else str(self.replaced)
        environment.pop("MIRROR_SYNC_BACKUP_KEEP_DAYS", None)
        if keep_days is not None:
            environment["MIRROR_SYNC_BACKUP_KEEP_DAYS"] = keep_days
        # A bound, so a resolver that loops on a link cycle fails the test
        # instead of hanging the suite.
        return subprocess.run(
            ["/bin/sh", str(MIRROR_SYNC), mode],
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
            cwd=cwd,
        )

    def test_a_run_older_than_the_window_is_pruned_and_logged(self) -> None:
        stale = self.run_dir(30)

        result = self.prune()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())
        self.assertIn(f"pruned replaced run: {stale}", result.stdout)

    def test_a_recent_run_is_kept(self) -> None:
        stale, recent = self.run_dir(30), self.run_dir(10)

        self.assertEqual(self.prune().returncode, 0)

        self.assertFalse(stale.exists())
        self.assertTrue((recent / "mirror" / "work.md").exists())

    def test_the_current_run_is_kept(self) -> None:
        stale = self.run_dir(30)
        self.assertEqual(self.prune().returncode, 0)
        self.write(self.source / "work.md", "clobbered\n")

        # Zero days puts every earlier second outside the window; the run this
        # pass is writing into must survive anyway.
        result = self.prune(keep_days="0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())
        kept = list(self.replaced.rglob("work.md"))
        self.assertEqual([p.read_text() for p in kept], ["work\n"], result.stdout)

    def test_a_folder_not_named_like_a_run_is_kept(self) -> None:
        # An old timestamp, but not in the shape BACKUP_RUN writes.
        stale = self.run_dir(30)
        others = [self.run_dir(0, name=name) for name in ("keep-me", "20200101000000")]
        for other in others:
            os.utime(other, (0, 0))

        self.assertEqual(self.prune().returncode, 0)

        self.assertFalse(stale.exists())
        for other in others:
            self.assertTrue((other / "mirror" / "work.md").exists(), other)

    def test_a_symlinked_run_folder_is_not_followed(self) -> None:
        stale = self.run_dir(30)
        elsewhere = self.write(self.work / "elsewhere" / "precious.txt", "precious\n")
        import datetime

        name = (datetime.datetime.now() - datetime.timedelta(days=40)).strftime("%Y-%m-%dT%H%M%S")
        link = self.replaced / name
        link.symlink_to(elsewhere.parent, target_is_directory=True)

        self.assertEqual(self.prune().returncode, 0)

        self.assertFalse(stale.exists())
        self.assertTrue(link.is_symlink())
        self.assertEqual(elsewhere.read_text(), "precious\n")

    def test_a_root_that_is_not_its_own_physical_path_is_not_walked(self) -> None:
        # The rule: the prune walks the root only when the root as written,
        # trailing slashes aside, is already its physical path. A link as the
        # root (however many slashes follow it), a link in any component, or a
        # "." component is skipped; the pass itself still succeeds.
        link = self.work / "link"
        link.symlink_to(self.replaced, target_is_directory=True)
        holder = self.work / "holder"
        holder.symlink_to(self.work, target_is_directory=True)
        for root in (f"{link}/", f"{link}///", str(holder / "replaced"), f"{self.work}/./replaced"):
            with self.subTest(root=root):
                stale = self.run_dir(30)

                result = self.prune(root=root)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(stale.exists())
                self.assertIn("not pruning", result.stderr)
                shutil.rmtree(stale)

    def test_a_plain_root_with_trailing_slashes_is_still_pruned(self) -> None:
        # A guard on the rule above: slashes alone do not make a root indirect.
        stale = self.run_dir(30)

        result = self.prune(root=f"{self.replaced}//")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())

    def test_nothing_is_pruned_when_the_root_is_unset(self) -> None:
        # A guard, not a behaviour: without a root there is nothing to find.
        stale = self.run_dir(30)

        result = self.run_sync(self.conf, additive=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(stale.exists())
        self.assertNotIn("pruned", result.stdout)

    def test_a_root_inside_a_destination_is_not_pruned(self) -> None:
        # The pair is refused for this root; the prune must not act on it.
        self.replaced = self.mirror / "replaced"
        stale = self.run_dir(30)

        result = self.prune(root=self.replaced)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(stale.exists())

    def test_a_dot_segment_root_inside_a_destination_is_not_pruned(self) -> None:
        # The same folder as above, spelled so no text prefix matches. A str,
        # because pathlib would drop the "." on the way in.
        self.replaced = self.mirror / "replaced"
        stale = self.run_dir(30)

        result = self.prune(root=f"{self.work}/./mirror/replaced")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("inside the destination", result.stderr)
        self.assertTrue(stale.exists())

    def test_a_root_reached_through_a_symlink_into_a_destination_is_not_pruned(self) -> None:
        # Only an intermediate component is a link; the root itself is a
        # real folder, so a check of the last component sees nothing.
        self.replaced = self.mirror / "replaced"
        stale = self.run_dir(30)
        alias = self.work / "alias"
        alias.symlink_to(self.mirror, target_is_directory=True)

        result = self.prune(root=alias / "replaced")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("inside the destination", result.stderr)
        self.assertTrue(stale.exists())

    def test_a_destination_spelled_indirectly_still_encloses_the_root(self) -> None:
        # The other side of the comparison: the root is plain, the pair list
        # names the destination through "." or a link.
        self.replaced = self.mirror / "replaced"
        stale = self.run_dir(30)
        alias = self.work / "alias"
        alias.symlink_to(self.mirror, target_is_directory=True)
        for dst in (f"{self.work}/./mirror", str(alias)):
            with self.subTest(dst=dst):
                self.write(self.conf, f"{self.source}|{dst}\n")

                result = self.prune(root=self.replaced)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("inside the destination", result.stderr)
                self.assertTrue(stale.exists())

    def test_a_root_resolving_inside_a_destination_fails_before_any_pair_runs(self) -> None:
        # rsync takes the root as --backup-dir, so a check after the pairs
        # is too late: an overwrite would already sit inside the mirror.
        alias = self.work / "alias"
        alias.symlink_to(self.mirror, target_is_directory=True)
        # The last one climbs out of a folder that does not exist, so nothing
        # can resolve it; rsync would create "nothing" and land in the mirror.
        for root, said in (
            (f"{self.work}/./mirror/replaced", "inside the destination"),
            (str(alias / "replaced"), "inside the destination"),
            (f"{self.work}/nothing/../mirror/replaced", "does not resolve"),
        ):
            with self.subTest(root=root):
                self.write(self.mirror / "work.md", "the copy\n")
                self.write(self.source / "work.md", "changed\n")

                result = self.prune(root=root)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(said, result.stderr)
                self.assertFalse((self.mirror / "replaced").exists())
                self.assertEqual((self.mirror / "work.md").read_text(), "the copy\n")

    def test_a_destination_that_cannot_be_resolved_counts_as_enclosing(self) -> None:
        # "nothing" does not exist, so the destination has no physical path.
        # Its text must not stand in for one: that text misses the root.
        self.replaced = self.mirror / "replaced"
        stale = self.run_dir(30)
        self.write(self.conf, f"{self.source}|{self.work}/nothing/../mirror\n")

        result = self.prune(root=self.replaced)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(stale.exists())

    def test_a_destination_that_is_or_resolves_to_slash_encloses_every_root(self) -> None:
        # "/" with its slash stripped is empty, which reads as no destination.
        to_root = self.work / "to-root"
        to_root.symlink_to("/", target_is_directory=True)
        for dst in ("/", "//", str(to_root), f"{self.work}/{'../' * len(self.work.parts)}"):
            with self.subTest(dst=dst):
                stale = self.run_dir(30)
                self.write(self.conf, f"{self.source}|{dst}\n")

                result = self.prune()

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("inside the destination", result.stderr)
                self.assertTrue(stale.exists())
                shutil.rmtree(stale)

    def test_a_root_that_encloses_a_source_or_destination_is_refused(self) -> None:
        # The prune owns every run-shaped child of the root. A source or a
        # destination under it, named like a run, would be deleted as one.
        # The linked case names the source through a link into the root, so
        # its text does not start with the root's.
        data = self.work / "data"
        alias = self.work / "alias"
        alias.symlink_to(data, target_is_directory=True)
        for side, linked in (("source", False), ("destination", False), ("source", True)):
            with self.subTest(side=side, linked=linked):
                inner = data / "2020-01-01T000000"
                self.write(inner / "work.md", "live\n")
                named = alias / inner.name if linked else inner
                if side == "source":
                    pair = f"{named}|{self.mirror}"
                else:
                    pair = f"{self.source}|{named}"
                self.write(self.conf, pair + "\n")

                result = self.prune(root=data)

                self.assertTrue((inner / "work.md").exists(), result.stdout)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"encloses the {side}", result.stderr)
                shutil.rmtree(data)

    def test_a_root_inside_a_source_is_refused_and_prunes_nothing(self) -> None:
        # Pass 7: root data/replaced inside source data pruned a run, because
        # only destinations were checked for enclosing the root. The alias
        # spelling reaches the same folder through a link, so its text does
        # not start with the source's.
        data = self.work / "data"
        alias = self.work / "alias"
        alias.symlink_to(data, target_is_directory=True)
        self.write(self.conf, f"{data}|{self.mirror}\n")
        for root in (data / "replaced", alias / "replaced"):
            with self.subTest(root=str(root)):
                self.replaced = data / "replaced"
                stale = self.run_dir(30)

                result = self.prune(root=root)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("is inside the source", result.stderr)
                self.assertTrue(stale.exists(), result.stdout)
                self.assertFalse(self.mirror.exists(), result.stdout)
                shutil.rmtree(data)

    def test_a_root_equal_to_a_source_or_destination_is_refused(self) -> None:
        alias = self.work / "alias"
        alias.symlink_to(self.work, target_is_directory=True)
        for side in ("source", "destination"):
            for spelled in ("plain", "linked"):
                with self.subTest(side=side, spelled=spelled):
                    path = self.source if side == "source" else self.mirror
                    self.write(path / "work.md", "live\n")
                    root = path if spelled == "plain" else alias / path.name

                    result = self.prune(root=root)

                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn(f"is the {side}", result.stderr)
                    self.assertTrue((path / "work.md").exists())

    def test_a_source_or_destination_that_cannot_be_resolved_stops_the_pass(self) -> None:
        # "nothing" does not exist, so ".." after it has no physical meaning.
        stale = self.run_dir(30)
        unresolved = f"{self.work}/nothing/../elsewhere"
        for side, pair in (
            ("source", f"{unresolved}|{self.mirror}"),
            ("destination", f"{self.source}|{unresolved}"),
        ):
            with self.subTest(side=side):
                self.write(self.conf, pair + "\n")

                result = self.prune()

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"the {side} {unresolved}, which does not resolve", result.stderr)
                self.assertTrue(stale.exists())

    def test_a_source_that_resolves_to_slash_encloses_every_root(self) -> None:
        # "/" as a source must count as enclosing the root. rsync is a stub
        # that fails loudly, so a regression cannot copy the whole disk.
        to_root = self.work / "to-root"
        to_root.symlink_to("/", target_is_directory=True)
        stub = self.write(self.work / "stub" / "rsync", "#!/bin/sh\necho 'stub rsync ran' >&2\nexit 99\n")
        stub.chmod(0o755)
        path = f"{stub.parent}:{os.environ['PATH']}"
        for src in ("/", "//", str(to_root)):
            with self.subTest(src=src):
                stale = self.run_dir(30)
                self.write(self.conf, f"{src}|{self.mirror}\n")

                result = self.prune(path=path)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("is inside the source", result.stderr)
                self.assertTrue(stale.exists())
                shutil.rmtree(stale)

    def link_to_a_file_under_backups(self) -> tuple[Path, Path, Path]:
        # Round 5: source /data/current, a link to a FILE under a run-shaped
        # folder of the root. cd -P resolves only directories, so the link name
        # stood in for its target and the prune deleted the source's data.
        backups = self.work / "backups"
        target = self.write(backups / "2020-01-01T000000" / "work.txt", "live\n")
        self.mirror.mkdir()
        return backups, target, self.work / "current"

    def test_a_source_linked_to_a_file_inside_the_root_is_refused(self) -> None:
        backups, target, current = self.link_to_a_file_under_backups()
        current.symlink_to(target)
        for src in (str(current), f"{current}/"):
            with self.subTest(src=src):
                self.write(self.conf, f"{src}|{self.mirror}\n")

                result = self.prune(root=backups)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("encloses the source", result.stderr)
                self.assertEqual(target.read_text(), "live\n")

    def test_a_chain_of_links_to_a_file_inside_the_root_is_refused(self) -> None:
        # Absolute, then relative, then through a linked directory: each hop
        # must be followed, not only the first.
        backups, target, current = self.link_to_a_file_under_backups()
        hops = self.work / "hops"
        hops.mkdir()
        (self.work / "dir-alias").symlink_to(target.parent, target_is_directory=True)
        (hops / "third").symlink_to(self.work / "dir-alias" / target.name)
        (hops / "second").symlink_to("../hops/third")
        current.symlink_to(hops / "second")
        self.write(self.conf, f"{current}|{self.mirror}\n")

        result = self.prune(root=backups)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("encloses the source", result.stderr)
        self.assertEqual(target.read_text(), "live\n")

    def test_a_dangling_link_into_the_root_is_refused_on_either_side(self) -> None:
        # The target does not exist yet; where it points still decides.
        backups, target, current = self.link_to_a_file_under_backups()
        current.symlink_to(target.parent / "not-yet")
        for side, pair in (
            ("source", f"{current}|{self.mirror}"),
            ("destination", f"{self.source}|{current}"),
        ):
            with self.subTest(side=side):
                self.write(self.conf, pair + "\n")

                result = self.prune(root=backups)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"encloses the {side}", result.stderr)
                self.assertEqual(target.read_text(), "live\n")

    def test_a_dangling_link_keeps_the_rest_of_the_path(self) -> None:
        # lk/sub, with lk a link to a folder that does not exist yet, is
        # missing/sub: the part after the link still counts.
        missing = self.work / "missing"
        (self.work / "lk").symlink_to(missing, target_is_directory=True)
        self.write(self.conf, f"{self.work / 'lk' / 'sub'}|{self.mirror}\n")

        result = self.prune(root=missing / "sub")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("is the source", result.stderr)

    def test_a_link_loop_or_a_climb_out_of_a_file_does_not_resolve(self) -> None:
        backups, target, current = self.link_to_a_file_under_backups()
        (self.work / "loop-a").symlink_to(self.work / "loop-b")
        (self.work / "loop-b").symlink_to(self.work / "loop-a")
        current.symlink_to(target)
        for src in (str(self.work / "loop-a"), f"{current}/../elsewhere"):
            with self.subTest(src=src):
                self.write(self.conf, f"{src}|{self.mirror}\n")

                result = self.prune(root=backups)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"the source {src}, which does not resolve", result.stderr)
                self.assertEqual(target.read_text(), "live\n")

    def test_a_source_linked_to_a_file_outside_the_root_still_runs(self) -> None:
        # The guard on the tests above: a file link is not refused as such.
        outside = self.write(self.work / "outside" / "work.txt", "outside\n")
        current = self.work / "current"
        current.symlink_to(outside)
        self.mirror.mkdir()
        self.write(self.conf, f"{current}|{self.mirror}\n")
        stale = self.run_dir(30)

        result = self.prune()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())
        self.assertEqual(outside.read_text(), "outside\n")

    def case_insensitive(self) -> bool:
        # APFS by default folds case; a case-sensitive volume does not. Each
        # case test asserts whichever holds for the folder the tests run in.
        probe = self.work / "CaseProbe"
        probe.mkdir()
        try:
            return (self.work / "caseprobe").exists()
        finally:
            probe.rmdir()

    def test_a_case_alias_of_a_source_is_compared_by_identity(self) -> None:
        # Round 6: pwd -P keeps the case as typed, so root /Backups and
        # source /backups/2020-01-01T000000 differed as text, and the prune
        # deleted the live source as an old run.
        folded = self.case_insensitive()
        backups = self.work / "backups"
        live = self.write(backups / "2020-01-01T000000" / "work.md", "live\n")
        data = self.work / "data"
        self.write(data / "work.md", "live\n")
        for label, src, root, said in (
            ("encloses", live.parent, self.work / "Backups", "encloses the source"),
            ("inside", data, self.work / "DATA" / "replaced", "is inside the source"),
            ("equal", data, self.work / "Data", "is the source"),
        ):
            with self.subTest(case=label, folded=folded):
                self.write(self.conf, f"{src}|{self.mirror}\n")

                result = self.prune(root=root)

                self.assertEqual(live.read_text(), "live\n")
                if folded:
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn(said, result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                shutil.rmtree(self.mirror, ignore_errors=True)

    def test_a_firmlinked_spelling_of_a_source_is_compared_by_identity(self) -> None:
        # /private/var is a firmlink into /System/Volumes/Data: two physical
        # spellings of one folder, and cd -P keeps whichever was typed. Where
        # the temp folder has no such twin, a linked parent stands in for it.
        data = self.work / "data"
        self.write(data / "work.md", "live\n")
        twin = Path("/System/Volumes/Data" + str(data))
        if not (twin.exists() and os.path.samefile(twin, data)):
            twin = self.work / "twin"
            twin.symlink_to(self.work, target_is_directory=True)
            twin = twin / "data"
        self.write(self.conf, f"{data}|{self.mirror}\n")
        for root, said in (
            (twin / "replaced", "is inside the source"),
            (twin, "is the source"),
            (twin.parent, "encloses the source"),
        ):
            with self.subTest(root=str(root)):
                result = self.prune(root=root)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(said, result.stderr)
                self.assertFalse(self.mirror.exists(), result.stdout)

    def test_a_root_that_does_not_exist_yet_is_judged_by_its_existing_part(self) -> None:
        # Under a source, however deep the missing part runs. The last case
        # has a missing part on both sides, spelled in different case: it is
        # refused whether or not the volume folds case, erring toward refusal.
        data = self.work / "data"
        self.write(data / "work.md", "live\n")
        for src, root in (
            (data, data / "new" / "deeper" / "replaced"),
            (data, self.work / "DATA" / "new" / "replaced")
            if self.case_insensitive()
            else (data, data / "New" / "replaced"),
            (data / "NotYet", data / "notyet" / "replaced"),
        ):
            with self.subTest(src=str(src), root=str(root)):
                self.write(self.conf, f"{src}|{self.mirror}\n")

                result = self.prune(root=root)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("is inside the source", result.stderr)
                self.assertFalse(root.exists())

    def test_an_identity_that_cannot_be_read_stops_the_pass(self) -> None:
        # stat is a stub that fails: no identity, so no overlap can be ruled
        # out, and the pass must not go on as if none were found.
        stale = self.run_dir(30)
        stub = self.write(self.work / "stub" / "stat", "#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)

        result = self.prune(path=f"{stub.parent}:{os.environ['PATH']}")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("resolves to /, or does not resolve", result.stderr)
        self.assertTrue(stale.exists())
        self.assertFalse(self.mirror.exists())

    def test_a_missing_sibling_of_a_missing_path_is_not_an_overlap(self) -> None:
        # The guard on the case above: a shared existing parent is not by
        # itself an overlap, or a first pass with a new destination and a new
        # root beside it could never run.
        stale = self.run_dir(30)
        self.write(self.conf, f"{self.source}|{self.work / 'fresh' / 'mirror'}\n")
        (self.work / "fresh").mkdir()

        result = self.prune(root=self.replaced)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())

    def test_the_same_missing_names_under_different_folders_do_not_overlap(self) -> None:
        # A missing part is compared by name only below the SAME existing
        # folder: p1/new and p2/new/replaced share a name, not a folder.
        for parent in ("p1", "p2"):
            (self.work / parent).mkdir()
        self.write(self.conf, f"{self.source}|{self.work / 'p1' / 'new'}\n")

        result = self.prune(root=self.work / "p2" / "new" / "replaced")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.work / "p1" / "new" / "work.md").read_text(), "work\n")

    def test_a_root_that_resolves_to_slash_is_refused(self) -> None:
        link = self.work / "to-root"
        link.symlink_to("/", target_is_directory=True)
        dotted = str(self.work.resolve()) + "/.." * (len(self.work.resolve().parts) - 1)
        for root in (str(link), dotted):
            with self.subTest(root=root):
                result = self.prune(root=root)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("MIRROR_SYNC_BACKUP_ROOT resolves to /, or does not resolve", result.stderr)

    def test_the_keep_days_value_is_honoured(self) -> None:
        older, newer = self.run_dir(5), self.run_dir(1)

        self.assertEqual(self.prune(keep_days="3").returncode, 0)

        self.assertFalse(older.exists())
        self.assertTrue(newer.exists())

    def test_a_keep_days_value_with_leading_zeros_is_decimal(self) -> None:
        # Shell arithmetic reads 08 as bad octal and 010 as eight.
        for keep_days, pruned, kept in (("08", 9, 7), ("010", 11, 9), ("00", 1, None)):
            with self.subTest(keep_days=keep_days):
                old, recent = self.run_dir(pruned), kept and self.run_dir(kept)

                result = self.prune(keep_days=keep_days)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(old.exists())
                if recent:
                    self.assertTrue(recent.exists())

    def test_a_keep_days_value_that_is_not_a_number_fails_and_prunes_nothing(self) -> None:
        stale = self.run_dir(30)

        result = self.prune(keep_days="two weeks")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("MIRROR_SYNC_BACKUP_KEEP_DAYS", result.stderr)
        self.assertTrue(stale.exists())

    def test_plan_names_the_stale_run_and_deletes_nothing(self) -> None:
        stale = self.run_dir(30)

        result = self.prune(mode="plan")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"would prune replaced run: {stale}", result.stdout)
        self.assertTrue(stale.exists())

    def test_a_name_that_is_not_a_real_date_and_time_is_kept(self) -> None:
        # The shape alone lets impossible stamps through: month 99, 29 February
        # of a common year, hour 24. Such a name was not written by BACKUP_RUN.
        stale = self.run_dir(30)
        leap = self.run_dir(0, name="2020-02-29T235959")
        others = [
            self.run_dir(0, name=name)
            for name in (
                "2020-99-99T999999",
                "2020-13-01T000000",
                "2020-00-10T000000",
                "2020-04-31T000000",
                "2021-02-29T000000",
                "1900-02-29T000000",
                "2020-01-00T000000",
                "2020-01-01T240000",
                "2020-01-01T006000",
                "2020-01-01T000060",
            )
        ]

        result = self.prune()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())
        self.assertFalse(leap.exists(), "a real leap day is a run like any other")
        for other in others:
            self.assertTrue((other / "mirror" / "work.md").exists(), other)

    def test_a_bad_keep_days_value_stops_the_pass_before_any_pair_runs(self) -> None:
        # Checked before the pair loop, not at prune time: a value that cannot
        # prune must not first let the pass mirror under a root it cannot keep.
        stale = self.run_dir(30)
        for mode in ("sync", "plan"):
            with self.subTest(mode=mode):
                result = self.prune(mode=mode, keep_days="two weeks")

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("MIRROR_SYNC_BACKUP_KEEP_DAYS", result.stderr)
                self.assertNotIn("===", result.stdout)
                self.assertFalse(self.mirror.exists())
                self.assertTrue(stale.exists())

    def fake_tools(self, flavour: str, tools: tuple[str, ...] = ("stat", "date")) -> tuple[str, Path]:
        """Put a GNU- or BSD-flavoured `stat` and `date` first on PATH.

        Both are Python fakes, so every platform runs both branches. Each
        call is logged, one line per call, to the returned file.
        """
        folder = self.work / f"fake-{flavour}"
        log = self.work / f"calls-{flavour}.log"
        for tool in tools:
            script = folder / tool
            self.write(script, FAKE_TOOL.format(python=sys.executable, flavour=flavour, tool=tool, log=log))
            script.chmod(0o755)
        return f"{folder}:{os.environ['PATH']}", log

    def test_each_stat_and_date_flavour_is_driven_with_its_own_flags(self) -> None:
        # GNU stat reads -f as "file system" and GNU date reads -r as a
        # reference FILE; BSD has no stat -c and no date -d. The pass must
        # probe which it has and never hand one the other's flags. The cwd
        # holds a file named like the stat format, which GNU stat -f would
        # happily read, and one named like an epoch, which GNU date -r would.
        cwd = self.work / "cwd"
        self.write(cwd / "%d:%i", "not a format\n")
        flags = {"gnu": {"stat": "-c", "date": "-d"}, "bsd": {"stat": "-f", "date": "-r"}}
        probes = {"stat": ["stat", "-c", "%d:%i", "--", "/"], "date": ["date", "-d", "@0", "+%s"]}
        for flavour, wrong in (("gnu", {"stat": "-f", "date": "-r"}), ("bsd", {"stat": "-c", "date": "-d"})):
            with self.subTest(flavour=flavour):
                stale, recent = self.run_dir(30), self.run_dir(10)
                path, log = self.fake_tools(flavour)

                result = self.prune(path=path, cwd=cwd)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(stale.exists())
                self.assertTrue(recent.exists())
                calls = [line.split() for line in log.read_text().splitlines()]
                for tool in ("stat", "date"):
                    used = [call for call in calls if call[0] == tool]
                    self.assertTrue(any(flags[flavour][tool] in call for call in used), used)
                    # The other flavour's flag appears at most once: in the
                    # probe, on a fixed input, which fails here.
                    wrong_calls = [call for call in used if wrong[tool] in call]
                    self.assertIn(wrong_calls, ([], [probes[tool]]), used)
                shutil.rmtree(self.replaced)
                shutil.rmtree(self.mirror)

    def test_a_gnu_date_is_never_asked_for_a_reference_file(self) -> None:
        # GNU date alone, with the system stat: `date -r EPOCH` would read a
        # file of that name in the cwd as the reference time, so the cutoff
        # must come from `date -d @EPOCH` and -r must never be passed.
        stale, recent = self.run_dir(30), self.run_dir(10)
        path, log = self.fake_tools("gnu", tools=("date",))

        result = self.prune(path=path)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())
        self.assertTrue(recent.exists())
        calls = [line.split() for line in log.read_text().splitlines()]
        self.assertEqual([call for call in calls if "-r" in call], [])
        self.assertTrue(any(call[1:2] == ["-d"] and call[2] != "@0" for call in calls), calls)

    def test_no_usable_date_fails_the_prune_and_deletes_nothing(self) -> None:
        # Neither flavour's epoch flag works: no cutoff, so nothing is judged
        # old; the pass says so rather than pruning against a guess.
        stale = self.run_dir(30)
        path, _log = self.fake_tools("none")

        result = self.prune(path=path)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("not pruning", result.stderr)
        self.assertTrue(stale.exists())


class OffsiteVerify(Fixture):
    """The check that has to report an absence nobody else would notice."""

    @staticmethod
    def _entries(snapshot: Path) -> dict[str, str | None]:
        """The inventory `sd-db-backup` writes: every entry, directories as null.

        Recursive and relative, because the writer's is, and the verifier now
        holds the directory to it in both directions.
        """
        return {
            path.relative_to(snapshot).as_posix():
                None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(snapshot.rglob("*"))
            if path != snapshot / "backup-manifest.json"
        }

    @staticmethod
    def _writer_counts(database: Path) -> dict[str, int]:
        """The counts the manifest writer records: every table the store holds.

        `sd-db-backup` counts each table present, not only the ones the
        fixture fills, and the validator's counts come from the same
        function. A manifest holding only the filled tables is one the writer
        never produces, and the verifier now refuses it for the missing ones.
        """
        from sd_db.backup import check_restorable

        check = check_restorable(database)
        assert check.accepted, check.refusal
        return dict(check.counts)

    def rewrite_manifest(self, snapshot: Path, **changes) -> None:
        path = snapshot / "backup-manifest.json"
        manifest = json.loads(path.read_text())
        manifest.update(changes)
        path.write_text(json.dumps(manifest))

    @staticmethod
    def make_database(path: Path, version: int | None = None) -> dict[str, int]:
        """A real sd_db store, because the committed restore validator checks one.

        A toy two-table database would be refused for its schema alone, and
        then every failure test below would pass for the wrong reason. The
        fixture is the library's own: nothing outside `sd_db` opens the
        database, and `local-sd-db/tests/test_one_store.py` greps this
        repository to keep it that way.
        """
        from sd_db.testing import make_store

        return make_store(path, version=version)

    def build_share(self) -> Path:
        root = self.work / "share" / "Backup"
        snapshot = root / "sd-backups" / "2026-09-21"
        snapshot.mkdir(parents=True)
        filled = self.make_database(snapshot / "sd.db")
        counts = self._writer_counts(snapshot / "sd.db")
        assert filled.items() <= counts.items(), (filled, counts)
        (snapshot / "providers.yaml").write_text("providers: []\n")
        (snapshot / "commands.yaml").write_text("commands: []\n")
        (snapshot / "backup-manifest.json").write_text(
            json.dumps(
                {
                    "format": "sd-db-backup",
                    "counts": counts,
                    "entries": self._entries(snapshot),
                }
            )
        )
        # The run's HOME, where tests put the things that are NOT on the share.
        (self.work / "home").mkdir()
        return root

    def run_verify(self, root: Path, *extra: str) -> subprocess.CompletedProcess:
        environment = dict(os.environ, HOME=str(self.work / "home"))
        return subprocess.run(
            [sys.executable, str(OFFSITE_VERIFY), "--root", str(root),
             "--allow-local-root", *extra],
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_unmounted_root_fails(self) -> None:
        result = self.run_verify(self.work / "not-mounted")

        self.assertEqual(result.returncode, 1)
        self.assertIn("off-machine backup root is not there", result.stderr)

    def test_a_good_share_passes(self) -> None:
        root = self.build_share()

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("restore validator accepts the restored copy", result.stdout)
        tables = len(self._writer_counts(root / "sd-backups" / "2026-09-21" / "sd.db"))
        self.assertIn(f"{tables}/{tables} tables match the manifest", result.stdout)

    def test_row_count_drift_fails_and_names_the_table(self) -> None:
        root = self.build_share()
        snapshot = root / "sd-backups" / "2026-09-21"
        counts = self._writer_counts(snapshot / "sd.db")
        counts["item"] = 99
        self.rewrite_manifest(snapshot, counts=counts)

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("table item: 3 rows restored, manifest says 99", result.stderr)

    # sd:1410: a manifest that is malformed or incomplete is a named failure,
    # not a traceback and not a pass. Each of these passed or crashed before.

    def test_a_manifest_that_is_not_an_object_fails_by_name(self) -> None:
        root = self.build_share()
        manifest = root / "sd-backups" / "2026-09-21" / "backup-manifest.json"
        for body in ("[]", "null", '"counts"'):
            with self.subTest(body=body):
                manifest.write_text(body)

                result = self.run_verify(root)

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn("manifest is a JSON object", result.stderr)

    def test_a_manifest_missing_a_table_fails(self) -> None:
        # Walking only the manifest's tables reported `N/N` and passed while
        # the restored copy held a table nobody had counted.
        root = self.build_share()
        snapshot = root / "sd-backups" / "2026-09-21"
        counts = self._writer_counts(snapshot / "sd.db")
        del counts["note"]
        self.rewrite_manifest(snapshot, counts=counts)

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("table note is in the restored copy (3 rows) but the manifest"
                      " does not count it", result.stderr)

    def test_a_row_count_that_is_not_an_integer_fails_by_name(self) -> None:
        root = self.build_share()
        snapshot = root / "sd-backups" / "2026-09-21"
        counts = self._writer_counts(snapshot / "sd.db")
        counts["item"] = "3"
        counts["note"] = True
        self.rewrite_manifest(snapshot, counts=counts)

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("manifest row counts are nonnegative integers (not for: item, note)",
                      result.stderr)

    def test_a_companion_the_manifest_does_not_name_fails(self) -> None:
        # Hashing only the listed names passed a manifest that had lost a
        # companion: the file was there, and nothing asked why it was unlisted.
        root = self.build_share()
        snapshot = root / "sd-backups" / "2026-09-21"
        entries = self._entries(snapshot)
        del entries["commands.yaml"]
        self.rewrite_manifest(snapshot, entries=entries)

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("commands.yaml is a file in the snapshot the manifest does not name",
                      result.stderr)

    def test_a_manifest_that_does_not_record_the_database_fails(self) -> None:
        root = self.build_share()
        snapshot = root / "sd-backups" / "2026-09-21"
        entries = self._entries(snapshot)
        del entries["sd.db"]
        self.rewrite_manifest(snapshot, entries=entries)

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("the manifest records the database, sd.db, with a hash", result.stderr)

    def test_a_manifest_entry_that_is_not_a_hash_fails_by_name(self) -> None:
        root = self.build_share()
        snapshot = root / "sd-backups" / "2026-09-21"
        entries = self._entries(snapshot)
        entries["providers.yaml"] = 12345
        self.rewrite_manifest(snapshot, entries=entries)

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("the manifest entry for 'providers.yaml' is neither a SHA256"
                      " nor a directory marker", result.stderr)

    def test_a_manifest_directory_that_is_not_there_fails(self) -> None:
        root = self.build_share()
        snapshot = root / "sd-backups" / "2026-09-21"
        entries = self._entries(snapshot)
        entries["executions"] = None
        self.rewrite_manifest(snapshot, entries=entries)

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("executions is in the manifest but missing", result.stderr)

    def test_finder_metadata_on_the_share_is_not_a_difference(self) -> None:
        # Browsing the share in Finder leaves `.DS_Store` behind -- the live
        # share already has one beside the snapshots. The writer never records
        # it and restore never reads it, so it must not fail a night.
        root = self.build_share()
        snapshot = root / "sd-backups" / "2026-09-21"
        (snapshot / ".DS_Store").write_bytes(b"\0Bud1")
        (snapshot / "._providers.yaml").write_bytes(b"\0\5\26\7")

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_foreign_key_violations_fail(self) -> None:
        # `sd-db.sh restore` refuses a snapshot in this state -- it is a
        # forensic copy, not a restorable one -- so integrity_check and
        # matching counts must not be enough to report a good backup.
        from sd_db.testing import break_foreign_keys

        root = self.build_share()
        snapshot = root / "sd-backups" / "2026-09-21"
        manifest = json.loads((snapshot / "backup-manifest.json").read_text())
        # The counts the fixture reports, so the manifest agrees with the
        # store: a mismatch here would fail this test for the other reason.
        manifest["counts"] = break_foreign_keys(snapshot / "sd.db")
        manifest["entries"] = self._entries(snapshot)
        (snapshot / "backup-manifest.json").write_text(json.dumps(manifest))

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("restore would refuse this snapshot", result.stderr)
        self.assertIn("foreign key violation", result.stderr)

    def test_a_linked_journal_entry_is_refused_not_dereferenced(self) -> None:
        # Copying the snapshot off the share must not repair it. `restore`
        # refuses a symlinked runner-journal entry ("unsafe run journal
        # entry"); a copy that dereferenced the link would hand restore an
        # ordinary file and report a snapshot as good that restore rejects.
        root = self.build_share()
        snapshot = root / "sd-backups" / "2026-09-21"
        journal = snapshot / "runner-journal"
        journal.mkdir()
        real = snapshot / "elsewhere.json"
        real.write_text(json.dumps({"record": {"id": "a" * 32}}))
        (journal / f"{'a' * 32}.json").symlink_to(real)

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("unsafe run journal entry", result.stderr)

    def test_a_snapshot_from_an_older_schema_still_counts(self) -> None:
        # Most of what a share holds was taken before the last migration.
        # `restore` validates such a snapshot against the shape it claims and
        # migrates a staged copy forward; refusing it here would report the
        # whole backup as broken on the day after any migration.
        from sd_db.schema import SCHEMA_VERSION

        root = self.build_share()
        snapshot = root / "sd-backups" / "2026-09-21"
        (snapshot / "sd.db").unlink()
        self.make_database(snapshot / "sd.db", version=SCHEMA_VERSION - 1)
        # The writer that took it counted the tables that version had.
        self.rewrite_manifest(snapshot, counts=self._writer_counts(snapshot / "sd.db"),
                              entries=self._entries(snapshot))

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"schema v{SCHEMA_VERSION - 1}", result.stdout)

    def test_an_incompatible_table_set_fails(self) -> None:
        # The finding that closed this loop: hashes, counts, integrity_check
        # and foreign_key_check all pass on a database whose table set restore
        # will not accept. Only the committed validator sees it.
        from sd_db.testing import add_unknown_table

        root = self.build_share()
        snapshot = root / "sd-backups" / "2026-09-21"
        add_unknown_table(snapshot / "sd.db")
        manifest = json.loads((snapshot / "backup-manifest.json").read_text())
        manifest["entries"] = self._entries(snapshot)
        (snapshot / "backup-manifest.json").write_text(json.dumps(manifest))

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("incompatible table set", result.stderr)

    def test_a_companion_file_that_came_back_changed_fails(self) -> None:
        root = self.build_share()
        (root / "sd-backups" / "2026-09-21" / "providers.yaml").write_text("tampered\n")

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("providers.yaml came back with different bytes", result.stderr)

    def test_a_missing_companion_file_fails(self) -> None:
        root = self.build_share()
        (root / "sd-backups" / "2026-09-21" / "commands.yaml").unlink()

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("commands.yaml is in the manifest but missing", result.stderr)

    def test_a_snapshot_that_will_not_open_fails(self) -> None:
        root = self.build_share()
        (root / "sd-backups" / "2026-09-21" / "sd.db").write_bytes(b"not a database at all")

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("restore would refuse this snapshot", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_backup_root_on_this_machine_fails(self) -> None:
        # Containment says the backups are under the root. It cannot say the
        # root left the machine: a `Backup` linked at local storage resolves
        # to a real directory and passes every other check, while the copies
        # it certifies would die with the Mac.
        root = self.build_share()
        environment = dict(os.environ, HOME=str(self.work / "home"))
        result = subprocess.run(
            [sys.executable, str(OFFSITE_VERIFY), "--root", str(root)],
            capture_output=True, text=True, env=environment,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("attached to this machine", result.stderr)

    def test_a_snapshot_linked_off_the_share_is_not_a_backup(self) -> None:
        # Everything below the snapshot follows links: the copy back, the
        # restore and the manifest hashes all pass against snapshots that
        # never left this machine. Losing the Mac would then destroy the
        # backups this run certified as being off it.
        root = self.build_share()
        snapshot = root / "sd-backups" / "2026-09-21"
        elsewhere = self.work / "home" / "local-snapshots"
        shutil.move(str(snapshot), str(elsewhere))
        snapshot.symlink_to(elsewhere)

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not a link off it", result.stderr)

    def test_a_manifest_naming_a_path_outside_the_snapshot_fails(self) -> None:
        # The manifest is only as trustworthy as the snapshot holding it. A
        # `..` name would have the companion check hash a file elsewhere on
        # the machine and report it as present in the backup.
        root = self.build_share()
        manifest_path = root / "sd-backups" / "2026-09-21" / "backup-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        outside = self.work / "home" / "elsewhere.yaml"
        outside.write_text("not in the snapshot\n")
        manifest["entries"]["../../../home/elsewhere.yaml"] = hashlib.sha256(
            outside.read_bytes()
        ).hexdigest()
        manifest_path.write_text(json.dumps(manifest))

        result = self.run_verify(root)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not inside the snapshot", result.stderr)

class SnapshotOrder(unittest.TestCase):
    """The newest snapshot is the newest one, not the last one alphabetically."""

    def setUp(self) -> None:
        self.verifier = _load_verifier()
        self.work = Path(tempfile.mkdtemp(prefix="snapshot-order-test-"))
        self.addCleanup(self._remove, self.work)

    @staticmethod
    def _remove(path: Path) -> None:
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    def test_a_two_digit_suffix_is_newer_than_a_one_digit_suffix(self) -> None:
        # `sd-db-backup` does not zero pad the suffix, so `sorted()` puts
        # "2026-09-21.9" after "2026-09-21.10" and the verifier would restore
        # the ninth run of the day while calling it the newest. The tenth
        # same-day snapshot is reachable today: the live tree already holds
        # 2026-09-21 and 2026-09-21.1.
        for name in ("2026-09-20", "2026-09-21", "2026-09-21.1", "2026-09-21.9", "2026-09-21.10"):
            (self.work / name).mkdir()

        newest = self.verifier._newest_snapshot(self.work)

        self.assertIsNotNone(newest)
        assert newest is not None
        self.assertEqual(newest.name, "2026-09-21.10")

    def test_a_bare_date_is_the_first_run_of_its_day(self) -> None:
        for name in ("2026-09-21.3", "2026-09-22"):
            (self.work / name).mkdir()

        newest = self.verifier._newest_snapshot(self.work)

        assert newest is not None
        self.assertEqual(newest.name, "2026-09-22")


class ConfigDirTest(Fixture):
    """The pair lists live in <config>/mirror-sync/, not beside the script."""

    def run_named(self, name: str | None, config: Path) -> subprocess.CompletedProcess:
        environment = {k: v for k, v in os.environ.items() if k != "MIRROR_SYNC_CONF"}
        environment["SYSTEM_TOOLS_CONFIG"] = str(config)
        if name is not None:
            environment["MIRROR_SYNC_CONF"] = name
        return subprocess.run(["/bin/sh", str(MIRROR_SYNC), "list"],
                              capture_output=True, text=True, env=environment)

    def test_the_default_list_and_a_bare_name_resolve_in_the_config_dir(self) -> None:
        config = self.work / "config"
        self.write(config / "mirror-sync" / "mirrors.conf", "/src/default|/dst/default\n")
        self.write(config / "mirror-sync" / "mirrors-repos.conf", "/src/repos|/dst/repos\n")
        default = self.run_named(None, config)
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertIn("/src/default", default.stdout)
        named = self.run_named("mirrors-repos.conf", config)
        self.assertEqual(named.returncode, 0, named.stderr)
        self.assertIn("/src/repos", named.stdout)

    def test_a_missing_list_names_the_config_path_and_the_example(self) -> None:
        config = self.work / "config"
        result = self.run_named(None, config)
        self.assertEqual(result.returncode, 1)
        want = config / "mirror-sync" / "mirrors.conf"
        self.assertIn(f"copy local-mirror-sync/mirrors.conf.example to {want}", result.stderr)


FIXTURE_JOBS = FOLDER / "tests" / "fixtures" / "jobs"


def _shipped_and_local(name: str) -> list[Path]:
    """The committed `<name>.example` list, plus this machine's `<name>` in
    `<config>/mirror-sync/` when it has one: the property holds for both."""
    found = [FOLDER / f"{name}.example"]
    live = system_tools_config.config_dir("mirror-sync") / name
    if live.is_file():
        found.append(live)
    return found


def _pairs(conf: Path) -> list[list[str]]:
    return [
        body.split("|")
        for body in (line.split("#", 1)[0].strip() for line in conf.read_text().splitlines())
        if body
    ]


class PairOrder(unittest.TestCase):
    """A pair that feeds another pair's source runs before that pair.

    `mirror-sync.sh` runs the pairs one at a time, in file order. Documents
    reaches iCloud in two hops: into /Volumes/local/Backup, then with that
    folder to iCloud. With the hops the wrong way round, iCloud gets the
    previous night's Documents, a day behind the local drive.
    """

    CONFS = _shipped_and_local("mirrors.conf")

    @staticmethod
    def _inside(path: str, tree: str) -> bool:
        return (path.rstrip("/") + "/").startswith(tree.rstrip("/") + "/")

    def test_no_pair_writes_into_a_tree_an_earlier_pair_already_read(self) -> None:
        for conf in self.CONFS:
            with self.subTest(conf=conf.name):
                pairs = [pair[:2] for pair in _pairs(conf)]
                self.assertTrue(pairs, f"{conf.name} lists no pair")
                late = [
                    f"{later_src} -> {later_dst} runs after {src} -> {dst}"
                    for i, (src, dst) in enumerate(pairs)
                    for later_src, later_dst in pairs[i + 1:]
                    if self._inside(later_dst, src)
                ]
                self.assertEqual(late, [])

    def test_file_order_is_run_order(self) -> None:
        # The property the committed order relies on: a two-hop chain listed
        # in order lands in one pass.
        work = Path(tempfile.mkdtemp(prefix="pair-order-test-"))
        self.addCleanup(shutil.rmtree, work, True)
        documents, local, icloud = work / "Documents", work / "local", work / "icloud"
        documents.mkdir()
        (documents / "note.md").write_text("tonight\n")
        (local / "Backup").mkdir(parents=True)
        (local / "Backup" / "keep").write_text("so the source is not empty\n")
        icloud.mkdir()
        conf = work / "pairs.conf"
        conf.write_text(f"{documents}|{local}/Backup/Documents\n{local}/Backup|{icloud}/Backup\n")

        run = subprocess.run(["/bin/sh", str(MIRROR_SYNC), "sync"], capture_output=True, text=True,
                             env=dict(os.environ, MIRROR_SYNC_CONF=str(conf)))

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual((icloud / "Backup" / "Documents" / "note.md").read_text(), "tonight\n")


class RepoMirror(unittest.TestCase):
    """The repo fleet's copy: its pair list and the job that runs it.

    These read the committed example list (and the local list when this
    checkout has one) and a neutral fixture job in tests/fixtures/jobs, the
    shape the real, local job has to keep: a guarantee the job does not ask
    for is no guarantee.
    """

    CONF_NAME = "mirrors-repos.conf"
    CONFS = _shipped_and_local(CONF_NAME)
    JOB = FIXTURE_JOBS / "repos-mirror.job"

    def setUp(self) -> None:
        run = subprocess.run(
            ["/bin/bash", "-c", f'. "{self.JOB}"; printf "%s\\n%s" "$JOB_SCHEDULE" "$JOB_COMMAND"'],
            capture_output=True, text=True, env=dict(os.environ, HOME="/home/operator"),
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.schedule, self.command = run.stdout.split("\n", 1)

    def test_the_fleet_is_one_repos_tree_to_one_repos_tree(self) -> None:
        for conf in self.CONFS:
            with self.subTest(conf=conf.name):
                pairs = _pairs(conf)
                self.assertEqual(len(pairs), 1, pairs)
                source, destination = pairs[0][:2]
                self.assertTrue(source.rstrip("/").endswith("/repos"), source)
                self.assertTrue(destination.rstrip("/").endswith("/repos"), destination)

    def test_the_excludes_are_regenerable_trees_only(self) -> None:
        for conf in self.CONFS:
            with self.subTest(conf=conf.name):
                patterns = {pattern.strip() for pattern in _pairs(conf)[0][2].split(",")}
                for regenerable in ("node_modules/", ".venv/", "target/", "__pycache__/"):
                    self.assertIn(regenerable, patterns)
                # Both names are also used for hand-written files, and
                # excluding them could lose uncommitted work.
                self.assertNotIn("dist/", patterns)
                self.assertNotIn("build/", patterns)

    def test_it_runs_every_twelve_hours(self) -> None:
        _minute, hours, *rest = self.schedule.split()
        self.assertEqual(rest, ["*", "*", "*"])
        first, second = (int(hour) for hour in hours.split(","))
        self.assertEqual(second - first, 12)

    def test_the_pass_is_additive_and_keeps_what_it_overwrites(self) -> None:
        # An exact mirror carries a `git reset --hard` into the copy within
        # twelve hours. Additive keeps deletions out; the backup root keeps
        # overwrites. Both have to be on.
        self.assertIn("MIRROR_SYNC_ADDITIVE=1", self.command)
        self.assertIn('MIRROR_SYNC_BACKUP_ROOT="/Volumes/local/Backup Local/replaced"', self.command)
        self.assertIn(f"MIRROR_SYNC_CONF={self.CONF_NAME} ", self.command)

    def test_a_backup_root_with_a_space_keeps_what_it_overwrites(self) -> None:
        # The real root is "Backup Local/replaced". A path with a space is the
        # one a missing quote in mirror-sync.sh would split.
        work = Path(tempfile.mkdtemp(prefix="repo-mirror-test-"))
        self.addCleanup(shutil.rmtree, work, True)
        source = work / "repos"
        source.mkdir()
        (source / "work.md").write_text("the good version\n")
        drive = work / "Backup Local"
        drive.mkdir()
        conf = work / "pairs.conf"
        conf.write_text(f"{source}|{drive}/repos|node_modules/\n")
        environment = dict(os.environ, MIRROR_SYNC_CONF=str(conf), MIRROR_SYNC_ADDITIVE="1",
                           MIRROR_SYNC_BACKUP_ROOT=str(drive / "replaced"))

        def sync() -> None:
            run = subprocess.run(["/bin/sh", str(MIRROR_SYNC), "sync"],
                                 capture_output=True, text=True, env=environment)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

        sync()
        (source / "work.md").write_text("clobbered\n")
        sync()

        kept = list((drive / "replaced").rglob("work.md"))
        self.assertEqual([path.read_text() for path in kept], ["the good version\n"])
        self.assertEqual((drive / "repos" / "work.md").read_text(), "clobbered\n")


class NasCopy(unittest.TestCase):
    """The USB disk's Backup folder to the NAS: its pair list and its job.

    These read the committed example list (and the local list when this
    checkout has one) and a neutral fixture job in tests/fixtures/jobs, the
    shape the real, local job has to keep.
    """

    CONF_NAME = "mirrors-nas.conf"
    CONFS = _shipped_and_local(CONF_NAME)
    JOBS = FIXTURE_JOBS

    def job(self, name: str) -> tuple[str, str]:
        run = subprocess.run(
            ["/bin/bash", "-c", f'. "{self.JOBS / (name + ".job")}"; printf "%s\\n%s" "$JOB_SCHEDULE" "$JOB_COMMAND"'],
            capture_output=True, text=True,
            env=dict(os.environ, HOME="/home/operator", ROOT=str(self.JOBS.parent)),
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        schedule, command = run.stdout.split("\n", 1)
        return schedule, command

    def test_the_backup_folder_goes_to_one_backup_folder(self) -> None:
        for conf in self.CONFS:
            with self.subTest(conf=conf.name):
                pairs = _pairs(conf)
                self.assertEqual(len(pairs), 1, pairs)
                source, destination = pairs[0][:2]
                self.assertTrue(source.rstrip("/").endswith("/Backup"), source)
                self.assertTrue(destination.rstrip("/").endswith("/Backup"), destination)
                self.assertNotEqual(source, destination)

    def test_the_pass_is_additive_and_keeps_no_overwrite_store(self) -> None:
        # The NAS Backup already holds sd-backups/ history the USB disk does
        # not have; an exact mirror would delete it on its first pass. The
        # operator wants no "replaced" folder on the NAS (2026-09-26).
        _schedule, command = self.job("sd-db-backup")
        self.assertIn("MIRROR_SYNC_ADDITIVE=1", command)
        self.assertNotIn("MIRROR_SYNC_BACKUP_ROOT", command)
        self.assertNotIn("replaced", command)
        self.assertIn(f"MIRROR_SYNC_CONF={self.CONF_NAME} ", command)

    def test_the_copy_runs_only_after_the_snapshot_succeeded(self) -> None:
        # A separate schedule does not order them: after a night asleep,
        # launchd runs missed jobs together at wake, and a copy could read a
        # snapshot directory that is not finished. `&&` in one job does.
        _schedule, command = self.job("sd-db-backup")
        snapshot = command.index("sd-db.sh")
        preflight = command.index("--preflight-only")
        copy = command.index("mirror-sync.sh")
        self.assertLess(snapshot, preflight)
        self.assertLess(preflight, copy)
        self.assertIn(" && ", command[snapshot:preflight])
        self.assertIn(" && ", command[preflight:copy])

    def test_an_additive_pass_leaves_what_only_the_destination_holds(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="nas-copy-test-"))
        self.addCleanup(shutil.rmtree, work, True)
        source = work / "local" / "Backup"
        (source / "sd-backups" / "2026-09-27").mkdir(parents=True)
        (source / "sd-backups" / "2026-09-27" / "sd.db").write_text("tonight\n")
        nas = work / "nas"
        (nas / "Backup" / "sd-backups" / "2026-09-20").mkdir(parents=True)
        (nas / "Backup" / "sd-backups" / "2026-09-20" / "sd.db").write_text("last week\n")
        (nas / "Backup" / "only-on-nas").mkdir()
        conf = work / "pairs.conf"
        conf.write_text(f"{source}|{nas}/Backup\n")

        run = subprocess.run(["/bin/sh", str(MIRROR_SYNC), "sync"], capture_output=True, text=True,
                             env={**{k: v for k, v in os.environ.items() if k != "MIRROR_SYNC_BACKUP_ROOT"},
                                  "MIRROR_SYNC_CONF": str(conf), "MIRROR_SYNC_ADDITIVE": "1"})

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual((nas / "Backup/sd-backups/2026-09-27/sd.db").read_text(), "tonight\n")
        self.assertEqual((nas / "Backup/sd-backups/2026-09-20/sd.db").read_text(), "last week\n")
        self.assertTrue((nas / "Backup" / "only-on-nas").is_dir())


class WriterPreflight(unittest.TestCase):
    """Both writers ask the root's question before they write, not after.

    The verifier rejecting a local root protects the morning report. It does
    not protect the night: a mount point with nothing mounted on it is an
    ordinary local directory, and a pass that finds one writes the whole
    backup onto the boot disk and reports success. These read a neutral
    fixture job in tests/fixtures/jobs, the shape the real, local job has to
    keep: a preflight nothing calls is no preflight.
    """

    JOBS = FIXTURE_JOBS

    def command(self, job: str, **environment: str) -> str:
        run = subprocess.run(
            ["/bin/bash", "-c", f'. "{self.JOBS / (job + ".job")}"; printf "%s" "$JOB_COMMAND"'],
            capture_output=True, text=True,
            env=dict(os.environ, ROOT=str(self.JOBS.parent), **environment),
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return run.stdout

    def test_the_snapshot_names_the_directory_it_writes_into(self) -> None:
        env = {key: value for key, value in os.environ.items() if key != "SD_DB_BACKUP_DESTINATION"}
        run = subprocess.run(
            ["/bin/bash", "-c",
             f'. "{self.JOBS / "sd-db-backup.job"}"; printf "%s" "$JOB_COMMAND"'],
            capture_output=True, text=True, env=dict(env, ROOT=str(self.JOBS.parent)),
        )

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('--destination "/Volumes/local/Backup/sd-backups"', run.stdout)

    def test_the_snapshot_checks_the_disk_before_it_writes_anything(self) -> None:
        # /Volumes/local with the disk detached is a directory on the boot
        # disk. sd-db.sh asks before it writes; the job has to pass the flag.
        env = {key: value for key, value in os.environ.items() if key != "SD_DB_BACKUP_DESTINATION"}
        run = subprocess.run(
            ["/bin/bash", "-c",
             f'. "{self.JOBS / "sd-db-backup.job"}"; printf "%s" "$JOB_COMMAND"'],
            capture_output=True, text=True, env=dict(env, ROOT=str(self.JOBS.parent)),
        )

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("--require-mount /Volumes/local", run.stdout)

    def test_an_operator_who_names_the_destination_is_not_second_guessed(self) -> None:
        # The override is documented for a machine whose disk is mounted
        # elsewhere and for a one-off run to a local path. Somebody who says
        # where the snapshot goes has already answered the question.
        command = self.command("sd-db-backup", SD_DB_BACKUP_DESTINATION="/tmp/somewhere")

        self.assertNotIn("--require-mount", command)
        self.assertIn("/tmp/somewhere", command)
        # A run that names one snapshot's destination is a one-off or a test;
        # it does not copy the USB disk to the NAS.
        self.assertNotIn("mirror-sync.sh", command)
        self.assertNotIn("--preflight-only", command)

    def test_the_nas_copy_checks_the_share_before_it_writes_anything(self) -> None:
        command = self.command("sd-db-backup")

        self.assertIn("--preflight-only", command)
        self.assertLess(command.index("--preflight-only"), command.index("mirror-sync.sh"), command)
        self.assertIn(" && ", command[command.index("--preflight-only"):command.index("mirror-sync.sh")])
        # The default root, Backup, is judged, and so is the directory the
        # copy writes into. There is no overwrite store beside it any more.
        self.assertNotIn("--root", command)
        self.assertIn("--destination /Volumes/Offsite/Backup ", command)
        self.assertNotIn("Backup-replaced", command)


class BackupRootIdentity(unittest.TestCase):
    """The root must be served by another machine, and by the expected one.

    A different filesystem is not enough: an attached USB disk is one, and
    it is lost in the fire, theft or spilled drink that the off-machine copy
    exists to survive. Only the mount table says which machine serves the
    bytes, so these state a mount table and ask what the check makes of it.
    """

    def setUp(self) -> None:
        self.verifier = _load_verifier()
        self.work = Path(tempfile.mkdtemp(prefix="backup-root-test-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.point = self.verifier._mount_point(self.work)
        self.expected = "192.0.2.10/Offsite"

    def check(self, device: str, options: str, expected: str | None = None):
        report = self.verifier.Report()
        table = f"{device} on {self.point} ({options})\n"
        passed = self.verifier._off_machine(
            self.work,
            self.expected if expected is None else expected,
            report,
            table=table,
        )
        return passed, report

    def test_a_usb_disk_at_the_backup_root_is_not_off_the_machine(self) -> None:
        passed, report = self.check("/dev/disk4s1", "exfat, local, nodev, nosuid")

        self.assertFalse(passed)
        self.assertTrue(any("exfat" in failure and "attached to this machine" in failure
                            for failure in report.failures), report.failures)

    def test_a_disk_image_at_the_backup_root_is_not_off_the_machine(self) -> None:
        # A `Backup.dmg` mounted over the share's path is the same trap with
        # a different filesystem name, and it dies with the machine too.
        passed, report = self.check("/dev/disk9s1", "apfs, local, nodev, nosuid")

        self.assertFalse(passed)
        self.assertTrue(any("apfs" in failure for failure in report.failures), report.failures)

    def test_another_nas_mounted_at_the_backup_root_is_not_the_backup_share(self) -> None:
        # A network share proves another machine holds the bytes. It does not
        # prove it is the machine the backups were written to.
        passed, report = self.check("//guest@198.51.100.9/Someone-Else", "smbfs, nodev, nosuid")

        self.assertFalse(passed)
        self.assertTrue(any("expected share" in failure for failure in report.failures),
                        report.failures)

    def test_a_server_whose_address_merely_contains_the_expected_one_is_refused(self) -> None:
        # `192.0.2.100` contains `192.0.2.10`. A search for the expected
        # text accepts it, and it is a different machine's disk.
        passed, report = self.check("//user@192.0.2.100/Offsite", "smbfs, nodev")

        self.assertFalse(passed)
        self.assertTrue(any("expected share" in failure for failure in report.failures),
                        report.failures)

    def test_a_share_whose_name_merely_starts_with_the_expected_one_is_refused(self) -> None:
        # `Offsite-old` is a different share on the right server, and
        # it is where last year's backups went.
        passed, report = self.check("//user@192.0.2.10/Offsite-old", "smbfs, nodev")

        self.assertFalse(passed)
        self.assertTrue(any("expected share" in failure for failure in report.failures),
                        report.failures)

    def test_the_expected_share_is_accepted(self) -> None:
        passed, report = self.check(
            "//nasuser;token@192.0.2.10/Offsite", "smbfs, nodev, nosuid")

        self.assertTrue(passed, report.failures)
        self.assertEqual(report.failures, [])

    def test_an_unlisted_mount_point_is_not_off_the_machine(self) -> None:
        # The mount table is what the answer rests on. If it does not name the
        # root's mount, nothing has been shown, and nothing is what is said.
        report = self.verifier.Report()
        passed = self.verifier._off_machine(
            self.work, self.expected, report,
            table=f"//token@192.0.2.10/Offsite on {self.point}-elsewhere (smbfs)\n")

        self.assertFalse(passed)
        self.assertTrue(any("mount table does not list" in failure
                            for failure in report.failures), report.failures)

    def test_an_unset_root_fails_by_naming_its_variable(self) -> None:
        # There is no built-in root: a hardcoded mount that exists on no
        # machine failed every call that omitted --root (sd:2048).
        config = self.work / "config"
        (config / "mirror-sync").mkdir(parents=True)
        environment = {key: value for key, value in os.environ.items()
                       if key != "OFFSITE_VERIFY_ROOT"}
        environment["SYSTEM_TOOLS_CONFIG"] = str(config)
        result = subprocess.run(
            [sys.executable, str(OFFSITE_VERIFY), "--preflight-only"],
            capture_output=True, text=True, env=environment,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("OFFSITE_VERIFY_ROOT is not set", result.stderr)
        self.assertNotIn("/Volumes/Offsite", result.stdout + result.stderr)

    def test_the_preflight_stops_before_the_rest_of_the_run(self) -> None:
        # A writer needs the root's answer before it writes, not the whole
        # verification pass, so the flag has to end the run where the root
        # checks end -- and still refuse a local root.
        local = self.work / "Backup"
        local.mkdir()
        allowed = subprocess.run(
            [sys.executable, str(OFFSITE_VERIFY), "--preflight-only", "--allow-local-root",
             "--root", str(local)],
            capture_output=True, text=True,
        )
        refused = subprocess.run(
            [sys.executable, str(OFFSITE_VERIFY), "--preflight-only", "--root", str(local)],
            capture_output=True, text=True,
        )

        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
        self.assertNotIn("sd-backups", allowed.stdout)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("attached to this machine", refused.stderr)

    def test_a_write_destination_linked_off_the_share_is_refused(self) -> None:
        # The root can be the NAS while `sd-backups` under it is a link to
        # local storage: the preflight passes, the snapshot lands on the Mac,
        # and every later check follows the same link and agrees.
        root = self.work / "Backup"
        (root / "elsewhere").mkdir(parents=True)
        local = self.work / "local-snapshots"
        local.mkdir()
        destination = root / "sd-backups"
        destination.symlink_to(local)

        refused = subprocess.run(
            [sys.executable, str(OFFSITE_VERIFY), "--preflight-only", "--allow-local-root",
             "--root", str(root), "--destination", str(destination)],
            capture_output=True, text=True,
        )
        accepted = subprocess.run(
            [sys.executable, str(OFFSITE_VERIFY), "--preflight-only", "--allow-local-root",
             "--root", str(root), "--destination", str(root / "elsewhere")],
            capture_output=True, text=True,
        )

        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("outside", refused.stderr)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)

    def test_a_write_destination_the_writer_will_create_is_judged_by_its_parent(self) -> None:
        # The first run has no `sd-backups` yet. Judging a path that is not
        # there by the parent the writer will create it in is the difference
        # between checking the first night and checking none of it.
        root = self.work / "Backup"
        root.mkdir()

        accepted = subprocess.run(
            [sys.executable, str(OFFSITE_VERIFY), "--preflight-only", "--allow-local-root",
             "--root", str(root), "--destination", str(root / "sd-backups")],
            capture_output=True, text=True,
        )
        refused = subprocess.run(
            [sys.executable, str(OFFSITE_VERIFY), "--preflight-only", "--allow-local-root",
             "--root", str(root), "--destination", str(self.work / "not-the-share" / "new")],
            capture_output=True, text=True,
        )

        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("outside", refused.stderr)

    def test_a_linux_mount_line_is_read_too(self) -> None:
        # Linux writes the type after the mount point rather than inside the
        # parentheses. Misreading it would call every root unknown.
        entries = self.verifier._mount_entries(
            "/dev/sda1 on /mnt/backup type ext4 (rw,relatime)\n")

        self.assertEqual(entries["/mnt/backup"], ("/dev/sda1", "ext4"))


if __name__ == "__main__":
    unittest.main()
