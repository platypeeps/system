"""The system stage keeps soft SMB mounts in /etc/nsmb.conf (sd:4843).

A dead NAS session hung shutdown in smbfs_unmount; `soft=yes` under
`[default]` lets a request to a gone server fail instead. Every case points
MACHINE_SETUP_NSMB_CONF at a file under a temporary folder and puts a `sudo`
stub on PATH that runs `sh -c` as this user and only logs anything else, so
no case touches the real /etc or the stage's other root-owned state.
"""

import pathlib
import random
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
        text = "# kept\n  [default] ; trailing words\n  soft = yes\nkloglevel\t=  1\n"
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

    # ------------------------------------- the SMB client's own reading ----
    # Each case below is a place where Apple's rc_parse (SMBClient
    # lib/smb/rcfile.c) reads the file another way than a plain INI reader.

    def test_global_is_default_and_its_value_stays(self):
        text = "[global]\nsoft=no\nkloglevel=1\n"
        self.conf.write_text(text)
        lines = self.system("--apply")
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("  DIFFERS nsmb.conf soft mounts: [default] has soft=no"), lines)
        self.assertEqual(self.conf.read_text(), text)

    def test_global_gets_the_missing_keys(self):
        self.conf.write_text("[global]\nsigning_required=no\n")
        self.system("--apply")
        self.assertEqual(self.conf.read_text(),
                         "[global]\nsoft=yes\nkloglevel=1\nsigning_required=no\n")
        self.assertEqual(self.system(), ["  ok      nsmb.conf soft mounts"])

    def test_section_names_keep_case_and_spaces(self):
        for header in ("[Default]", "[DEFAULT]", "[ default ]", "[Global]"):
            with self.subTest(header=header):
                before = f"{header}\nsoft=yes\nkloglevel=1\n"
                self.conf.write_text(before)
                lines = self.system()
                self.assertTrue(lines[0].startswith("  MISSING nsmb.conf soft mounts: soft=yes kloglevel=1"), lines)
                self.system("--apply")
                self.assertEqual(self.conf.read_text(), before + "\n" + WANTED)

    def test_key_names_keep_case(self):
        self.conf.write_text("[default]\nSoft=yes\nkloglevel=1\n")
        self.system("--apply")
        self.assertEqual(self.conf.read_text(), "[default]\nsoft=yes\nSoft=yes\nkloglevel=1\n")

    def test_spaces_inside_a_key_are_dropped(self):
        self.conf.write_text("[default]\ns o f t = yes\nkloglevel=1\n")
        self.assertEqual(self.system(), ["  ok      nsmb.conf soft mounts"])

    def test_comment_lines_and_lines_without_equals_do_not_count(self):
        self.conf.write_text("[default]\n#soft=yes\n  ;kloglevel=1\nsoft\n")
        lines = self.system()
        self.assertTrue(lines[0].startswith("  MISSING nsmb.conf soft mounts: soft=yes kloglevel=1"), lines)

    def test_a_comment_after_a_value_is_part_of_it(self):
        self.conf.write_text("[default]\nsoft=yes # on\nkloglevel=1\n")
        lines = self.system()
        self.assertEqual(lines, ["  DIFFERS nsmb.conf soft mounts: [default] has soft=yes # on in "
                                 f"{self.conf}; want soft=yes kloglevel=1, edit by hand"])

    def test_a_repeated_section_adds_to_the_first_and_the_last_value_wins(self):
        self.conf.write_text("[default]\nsoft=yes\n[server.example.test]\nsoft=yes\n"
                             "[global]\nsoft=no\nkloglevel=1\n")
        lines = self.system()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("has soft=no", lines[0])
        self.conf.write_text("[default]\nsoft=yes\n[server.example.test]\n[default]\nkloglevel=1\n")
        self.assertEqual(self.system(), ["  ok      nsmb.conf soft mounts"])

    def test_keys_of_another_section_or_before_any_do_not_count(self):
        self.conf.write_text("soft=yes\nkloglevel=1\n[server.example.test]\nsoft=yes\nkloglevel=1\n")
        self.system("--apply")
        self.assertEqual(self.conf.read_text(),
                         "soft=yes\nkloglevel=1\n[server.example.test]\nsoft=yes\nkloglevel=1\n\n" + WANTED)

    def test_carriage_returns_are_dropped(self):
        self.conf.write_text("[default]\r\nsoft=yes\r\nkloglevel=1\r\n")
        self.assertEqual(self.system(), ["  ok      nsmb.conf soft mounts"])
        # The last value counts without a newline after it, too.
        self.conf.write_text("[default]\nsoft=yes\nkloglevel=1")
        self.assertEqual(self.system(), ["  ok      nsmb.conf soft mounts"])

    def test_a_header_with_no_bracket_or_a_long_line_is_unknown(self):
        for text in ("[default\nsoft=yes\nkloglevel=1\n",
                     "[default]\nsoft=yes\nkloglevel=1\n# " + "x" * 2100 + "\n"):
            with self.subTest(text=text[:20]):
                self.conf.write_text(text)
                lines = self.system("--apply")
                self.assertEqual(len(lines), 1, lines)
                self.assertTrue(lines[0].startswith("  DIFFERS nsmb.conf soft mounts:") and "unknown" in lines[0], lines)
                self.assertEqual(self.conf.read_text(), text)

    def test_values_are_read_as_the_client_reads_them(self):
        # soft through rc_getbool, kloglevel through (int)strtol(v, NULL, 0).
        cases = [("soft", v, True) for v in ("yes", "YES", "True", "1", "1x", "  yes")] + \
                [("soft", v, False) for v in ("no", "0", "0x1", "maybe", "", "yes ", "on")] + \
                [("kloglevel", v, True) for v in ("1", "0x1", "0X1", "01", "+1", "1abc", "1e3", "4294967297",
                                                  "-4294967295", "0x100000001", "-9223372036854775807", "019",
                                                  "9223372032559808513")] + \
                [("kloglevel", v, False) for v in ("0", "", "2", "-1", "0x", "08", "yes",
                                                   "9223372036854775807", "9223372036854775809",
                                                   "99999999999999999999", "-9223372036854775808",
                                                   "-99999999999999999999")]
        for key, value, good in cases:
            with self.subTest(key=key, value=value):
                other = "kloglevel=1" if key == "soft" else "soft=yes"
                self.conf.write_text(f"[default]\n{other}\n{key}={value}\n")
                lines = self.system()
                if good:
                    self.assertEqual(lines, ["  ok      nsmb.conf soft mounts"])
                else:
                    self.assertEqual(len(lines), 1, lines)
                    self.assertTrue(lines[0].startswith(f"  DIFFERS nsmb.conf soft mounts: [default] has {key}="), lines)


def c_int(text):
    """(int)strtol(text, NULL, 0) on a 64-bit long, as the SMB client reads kloglevel."""
    text = text.lstrip(" \t\n\v\f\r")
    neg = text[:1] == "-"
    if text[:1] in ("-", "+"):
        text = text[1:]
    if re.match(r"0[xX][0-9a-fA-F]", text):
        base, text = 16, text[2:]
    else:
        base = 8 if text.startswith("0") else 10
    digits = re.match({16: "[0-9a-fA-F]*", 8: "[0-7]*", 10: "[0-9]*"}[base], text).group()
    value = int(digits, base) if digits else 0
    value = max(min(-value if neg else value, 2**63 - 1), -2**63) & 0xFFFFFFFF
    return value - 2**32 if value >= 2**31 else value


class NsmbIntPropertyTest(unittest.TestCase):
    """The stage's kloglevel reading matches strtol over extreme magnitudes."""

    def test_random_magnitudes(self):
        rng = random.Random(4843)
        values = ["0", "-0", "1", "-1"]
        for _ in range(300):
            n = rng.choice([1, 2**31, 2**32, 2**63, 2**64]) * rng.randint(0, 3) + rng.randint(-2, 2)
            n = n if rng.random() < 0.7 else rng.getrandbits(rng.randint(1, 90))
            body = rng.choice([str(n), format(n, "#x"), "0" + format(n, "o")])
            values.append(rng.choice(["", "-", "+", " ", "\t"]) + body + rng.choice(["", "z", " 9", "x"]))
        program = SCRIPT.read_text().split("NSMB_AWK='", 1)[1].split("\n  }'\n", 1)[0] + "\n  }"
        with tempfile.TemporaryDirectory() as tmp:
            conf = pathlib.Path(tmp) / "nsmb.conf"
            for value in values:
                conf.write_text(f"[default]\nkloglevel={value}\n")
                out = subprocess.run(["awk", "-v", "mode=scan", "-v", "want=kloglevel=1", program, str(conf)],
                                     capture_output=True, text=True, timeout=30, check=True).stdout
                with self.subTest(value=value):
                    self.assertEqual(out == "", c_int(value) == 1, out)


if __name__ == "__main__":
    unittest.main()
