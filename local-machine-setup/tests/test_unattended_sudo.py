"""Unattended sudo on every managed machine (sd:6312).

The weekly upgrade reads the admin password from a login-Keychain item
through sudo-askpass.sh, for the casks in MACHINE_SETUP_SUDO_CASKS only; the
upgrade cases are in test_upgrade_steps. A tracked sudoers example names the
known repair commands. The operator creates the item and installs the
drop-in on each machine; doctor says on each machine whether both are there.

`security` is a stub here: no case reads a real Keychain.
"""

import pathlib
import re
import subprocess
import tempfile
import unittest

from tests import fixture_config
from tests.test_profile_prefs import Case, write_exec

FOLDER = pathlib.Path(__file__).resolve().parent.parent
ASKPASS = FOLDER / "sudo-askpass.sh"
SUDOERS = FOLDER / "machine-setup-repairs.sudoers.example"

# `security` with the item when STUB_KEYCHAIN is set, exit 44 without it, or
# SECURITY_RC. `-w` prints the password.
SECURITY_STUB = r"""#!/bin/sh
echo "security $*" >> "$CALL_LOG"
[ -z "$SECURITY_RC" ] || exit "$SECURITY_RC"
[ -n "$STUB_KEYCHAIN" ] || exit 44
case " $* " in *" -w "*) echo "$STUB_KEYCHAIN" ;; esac
exit 0
"""


class Doctor(Case):
    def doctor(self, keychain="", sudoers=False, **extra):
        write_exec(self.stubs / "security-stub", SECURITY_STUB)
        drop_in = self.base / "sudoers.d/machine-setup-repairs"
        if sudoers:
            drop_in.parent.mkdir()
            drop_in.write_text("# installed\n")
        result = self.run_script("doctor", fnm_have="18 20 24", STUB_KEYCHAIN=keychain,
                                 MACHINE_SETUP_SECURITY=str(self.stubs / "security-stub"),
                                 MACHINE_SETUP_REPAIRS_SUDOERS=str(drop_in), **extra)
        return result.stdout

    def test_both_present_are_ok(self):
        out = self.doctor(keychain="example-password", sudoers=True)
        self.assertIn("  ok      Keychain item machine-setup-sudo (sudo askpass)", out)
        self.assertIn("  ok      repair sudoers drop-in ", out)
        self.assertNotIn("example-password", out)
        self.assertNotIn(" -w", self.call_log.read_text())

    def test_each_missing_one_is_a_finding_naming_the_step(self):
        """A missing item or drop-in is a WARN, not a FAIL: the operator's
        step on this machine, spelled out."""
        out = self.doctor()
        self.assertIn("  WARN    no Keychain item machine-setup-sudo — run in a terminal: "
                      'security add-generic-password -a "$(id -un)" -s machine-setup-sudo '
                      "-T /usr/bin/security -w", out)
        self.assertRegex(out, r"  WARN    no repair sudoers drop-in at \S+machine-setup-repairs — "
                              r"install local-machine-setup/machine-setup-repairs\.sudoers\.example "
                              r"with visudo -cf")
        self.assertNotIn("FAIL    no Keychain", out)

    def test_a_keychain_that_does_not_answer_is_unknown_not_missing(self):
        out = self.doctor(SECURITY_RC="51")
        self.assertIn("  WARN    Keychain item machine-setup-sudo unknown: security exited 51", out)
        self.assertNotIn("no Keychain item", out)


class Askpass(unittest.TestCase):
    def run_askpass(self, *args, keychain="example-password"):
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        stub = tmp / "security"
        write_exec(stub, SECURITY_STUB)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "CALL_LOG": str(tmp / "calls.log"),
               "STUB_KEYCHAIN": keychain, "SECURITY_RC": "", "MACHINE_SETUP_SECURITY": str(stub)}
        result = subprocess.run(["sh", str(ASKPASS), *args], env=env, capture_output=True,
                                text=True, stdin=subprocess.DEVNULL, timeout=30)
        calls = (tmp / "calls.log").read_text() if (tmp / "calls.log").exists() else ""
        return result, calls

    def test_the_prompt_gets_the_password_from_the_keychain_item(self):
        result, calls = self.run_askpass("[sudo] password for example:")
        self.assertEqual((result.returncode, result.stdout), (0, "example-password\n"))
        self.assertRegex(calls, r"^security find-generic-password -a \S+ -s machine-setup-sudo -w\n$")

    def test_no_item_prints_nothing_and_fails(self):
        result, _ = self.run_askpass("Password:", keychain="")
        self.assertEqual((result.returncode, result.stdout), (44, ""))

    def test_no_argument_is_usage(self):
        result, calls = self.run_askpass()
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage: sudo-askpass.sh", result.stderr)
        self.assertEqual(calls, "")


class Sudoers(unittest.TestCase):
    def test_each_rule_is_nopasswd_for_one_exact_command(self):
        """Exact arguments only: an absolute path, its arguments, no
        wildcard; sudoers then allows those arguments and no others."""
        rules = [line for line in SUDOERS.read_text().splitlines()
                 if line.strip() and not line.startswith("#")]
        self.assertEqual(len(rules), 2, rules)
        for rule in rules:
            self.assertRegex(rule, r"^change-me ALL=\(root\) NOPASSWD: /\S+( [^*?\[\]\\,:=]+)+$")
        self.assertIn("NOPASSWD: /opt/homebrew/bin/brew services restart tailscale", rules[0])
        self.assertTrue(rules[1].endswith("NOPASSWD: /sbin/pfctl -sr"), rules[1])


class EveryMachine(unittest.TestCase):
    def test_nothing_ties_it_to_the_hub(self):
        """The operator's ruling: every managed machine, satellites too. No
        part of it reads the hub role or SD_HUB_ONLY_AGENTS."""
        text = (FOLDER / "machine-setup.sh").read_text()
        parts = [ASKPASS.read_text(), SUDOERS.read_text()]
        for name in ("sudo_keychain_item", "askpass_upgrade", "doctor_unattended_sudo"):
            body = re.search(rf"^{name}\(\) {{\n.*?^}}\n", text, re.M | re.S)
            self.assertIsNotNone(body, name)
            parts.append(body.group(0))
        self.assertIn("askpass_upgrade\n", text)
        self.assertIn("doctor_unattended_sudo\n", text)
        for part in parts:
            self.assertNotRegex(part, r"(?i)hub|SD_HUB_ONLY_AGENTS|is_satellite")

    def test_the_suite_never_reaches_a_real_keychain(self):
        self.assertIn("security", fixture_config.MACHINE_READERS)
        self.assertEqual(subprocess.run([str(fixture_config.NO_KEYCHAIN)]).returncode, 44)


if __name__ == "__main__":
    unittest.main()
