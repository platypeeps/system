"""The system stage keeps soft SMB mounts in /etc/nsmb.conf (sd:4843).

A dead NAS session hung shutdown in smbfs_unmount; `soft=yes` under
`[default]` lets a request to a gone server fail instead. Every case points
MACHINE_SETUP_NSMB_CONF at a file under a temporary folder and puts a `sudo`
stub on PATH that runs `sh -c` as this user and only logs anything else, so
no case touches the real /etc or the stage's other root-owned state.
"""

import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "machine-setup.sh"

# The words status_stage greps to count drift.
DRIFT_WORDS = re.compile(r"DIFFERS|MISSING|STALE|ABSENT|UNLOADED|EXTRA")

# SUDO_STUB=refuse fails every call, the way a declined password does.
SUDO_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$SUDO_LOG"
[ "${SUDO_STUB:-ok}" = refuse ] && { echo "sudo: a password is required" >&2; exit 1; }
while [ "$1" = "-n" ] || [ "$1" = "sudo" ]; do shift; done
case "$1" in
  sh) exec "$@" ;;
esac
exit 0
"""

FIREWALL_STUB = r"""#!/bin/sh
case "$1" in
  --getglobalstate) echo "Firewall is enabled. (State = 1)" ;;
  --getstealthmode) echo "Firewall stealth mode is on" ;;
esac
"""

WANTED = "[default]\nsoft=yes\nkloglevel=1\n"


def write_stub(directory, name, body):
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class NsmbConfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        # A profile with no .spotlight file, so the stage reads no Spotlight store.
        (self.state / "profile").write_text("work\n")
        self.etc = base / "etc"
        self.etc.mkdir()
        self.conf = self.etc / "nsmb.conf"
        self.timeout_sudoers = base / "timestamp-timeout"
        self.timeout_sudoers.touch()
        self.sudo_log = base / "sudo.log"
        self.sudo_log.touch()
        self.stubs = base / "stubs"
        self.stubs.mkdir()
        write_stub(self.stubs, "sudo", SUDO_STUB)
        write_stub(self.stubs, "socketfilterfw", FIREWALL_STUB)
        fixture_config.seal(self, self.stubs)

    def system(self, *flags, sudo="ok"):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
            "MACHINE_SETUP_NSMB_CONF": str(self.conf),
            "MACHINE_SETUP_TIMEOUT_SUDOERS": str(self.timeout_sudoers),
            "SUDO_STUB": sudo,
            "SUDO_LOG": str(self.sudo_log),
        }
        result = subprocess.run([str(SCRIPT), "update", "system", *flags], env=env,
                                capture_output=True, text=True, cwd=self.tmp.name,
                                stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [line for line in result.stdout.splitlines() if "nsmb.conf" in line]

    def backups(self):
        return sorted(p.name for p in self.etc.iterdir() if p.name.startswith("nsmb.conf.bak-"))

    def test_absent_file_is_missing_then_written(self):
        lines = self.system()
        self.assertTrue(lines and lines[0].startswith("  MISSING nsmb.conf soft mounts"), lines)
        self.assertIn("next mount", lines[0])
        self.assertFalse(self.conf.exists(), "a dry run wrote the file")
        self.system("--apply")
        self.assertEqual(self.conf.read_text(), WANTED)
        self.assertEqual(stat.S_IMODE(self.conf.stat().st_mode), 0o644)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.system(), ["  ok      nsmb.conf soft mounts"])

    def test_other_lines_kept_and_keys_added_under_default(self):
        before = ("# operator comment\n"
                  "[default]\n"
                  "signing_required=no\n"
                  "\n"
                  "[server.example.test]\n"
                  "soft=no\n")
        self.conf.write_text(before)
        lines = self.system()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith("  MISSING nsmb.conf soft mounts: soft=yes kloglevel=1"), lines)
        self.system("--apply")
        self.assertEqual(self.conf.read_text(),
                         "# operator comment\n"
                         "[default]\n"
                         "soft=yes\n"
                         "kloglevel=1\n"
                         "signing_required=no\n"
                         "\n"
                         "[server.example.test]\n"
                         "soft=no\n")
        backups = self.backups()
        self.assertEqual(len(backups), 1, backups)
        self.assertEqual((self.etc / backups[0]).read_text(), before)
        self.assertEqual(self.system(), ["  ok      nsmb.conf soft mounts"])

    def test_no_default_section_gets_one_at_the_end(self):
        self.conf.write_text("[server.example.test]\nsigning_required=no")
        self.system("--apply")
        self.assertEqual(self.conf.read_text(),
                         "[server.example.test]\nsigning_required=no\n\n" + WANTED)

    def test_one_missing_key_adds_only_that_key(self):
        self.conf.write_text("[default]\nsoft=yes\n")
        self.system("--apply")
        self.assertEqual(self.conf.read_text(), "[default]\nkloglevel=1\nsoft=yes\n")

    def test_correct_file_is_ok_and_untouched(self):
        text = "# kept\n[Default]\n  soft = yes\nkloglevel=1\n"
        self.conf.write_text(text)
        mtime = self.conf.stat().st_mtime_ns
        self.assertEqual(self.system("--apply"), ["  ok      nsmb.conf soft mounts"])
        self.assertEqual(self.conf.read_text(), text)
        self.assertEqual(self.conf.stat().st_mtime_ns, mtime)
        self.assertEqual(self.backups(), [])
        self.assertNotIn("nsmb", self.sudo_log.read_text())

    def test_operator_value_differs_and_stays(self):
        text = "[default]\nsoft=no\nkloglevel=1\n"
        self.conf.write_text(text)
        lines = self.system("--apply")
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("  DIFFERS nsmb.conf soft mounts: [default] has soft=no"), lines)
        self.assertEqual(self.conf.read_text(), text)

    def test_unreadable_file_counts_as_drift_and_is_not_written(self):
        self.conf.write_text("[default]\nsoft=yes\nkloglevel=1\n")
        self.conf.chmod(0)
        self.addCleanup(self.conf.chmod, 0o644)
        lines = self.system("--apply")
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(DRIFT_WORDS.search(lines[0]) and "unknown" in lines[0], lines)
        self.assertNotIn("nsmb", self.sudo_log.read_text())

    def test_refused_sudo_defers_and_leaves_the_file(self):
        self.conf.write_text("[default]\n")
        lines = self.system("--apply", sudo="refuse")
        self.assertTrue(any(line.startswith("  DEFER   needs sudo: add soft=yes") for line in lines), lines)
        self.assertEqual(self.conf.read_text(), "[default]\n")
        self.assertEqual(sorted(p.name for p in self.etc.iterdir()), ["nsmb.conf"])

    def test_a_stop_after_the_copy_is_finished_by_the_next_run(self):
        # A run stopped between root's copy and its move leaves the dotted
        # copy beside the old file; the next run overwrites it and moves it.
        self.conf.write_text("[default]\n")
        (self.etc / ".nsmb.conf.tmp").write_text("half")
        self.system("--apply")
        self.assertEqual(self.conf.read_text(), "[default]\nsoft=yes\nkloglevel=1\n")
        self.assertFalse((self.etc / ".nsmb.conf.tmp").exists())


if __name__ == "__main__":
    unittest.main()
