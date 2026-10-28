"""Unattended sudo on every managed machine (sd:6312).

The weekly upgrade reads the admin password from a login-Keychain item
through sudo-askpass.sh, for the casks in MACHINE_SETUP_SUDO_CASKS only; the
upgrade cases are in test_upgrade_steps. A tracked sudoers example names the
known repair commands. The operator creates the item and installs the
drop-in on each machine; doctor says on each machine whether both are there.

`security` is a stub here: no case reads a real Keychain. The drop-in's
brew line follows this machine's brew prefix (sd:6475): /opt/homebrew on
Apple silicon, /usr/local on Intel. `sudo` and `brew` are stubs too.
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

# `sudo -n -l` prints SUDO_LIST, sudo's listing of this user's rules, or exits
# SUDO_RC; `brew --prefix` prints BREW_PREFIX. Every other call fails.
SUDO_STUB = r"""#!/bin/sh
echo "sudo $*" >> "$CALL_LOG"
[ "$*" = "-n -l" ] || exit 1
[ -z "$SUDO_RC" ] || exit "$SUDO_RC"
printf '%s\n' "User example may run the following commands on example:" "$SUDO_LIST"
"""
BREW_STUB = r"""#!/bin/sh
echo "brew $*" >> "$CALL_LOG"
[ "$*" = --prefix ] && [ -n "$BREW_PREFIX" ] && echo "$BREW_PREFIX"
"""
RESTART = "services restart tailscale"


class Doctor(Case):
    def run_doctor(self, keychain="", sudoers=False, prefix="/usr/local",
               rule=f"    (root) NOPASSWD: /usr/local/bin/brew {RESTART}", **extra):
        write_exec(self.stubs / "security-stub", SECURITY_STUB)
        write_exec(self.stubs / "sudo", SUDO_STUB)
        write_exec(self.stubs / "brew", BREW_STUB)
        extra = {"BREW_PREFIX": prefix, "SUDO_LIST": rule, "SUDO_RC": "", **extra}
        drop_in = self.base / "sudoers.d/machine-setup-repairs"
        if sudoers:
            drop_in.parent.mkdir(exist_ok=True)
            drop_in.write_text("# installed\n")
        return self.run_script("doctor", fnm_have="18 20 24", STUB_KEYCHAIN=keychain,
                               MACHINE_SETUP_SECURITY=str(self.stubs / "security-stub"),
                               MACHINE_SETUP_REPAIRS_SUDOERS=str(drop_in), **extra)

    def doctor(self, *args, **kw):
        return self.run_doctor(*args, **kw).stdout

    def test_both_present_are_ok(self):
        out = self.doctor(keychain="example-password", sudoers=True)
        self.assertIn("  ok      Keychain item machine-setup-sudo (sudo askpass)", out)
        self.assertIn("  ok      repair sudoers drop-in ", out)
        self.assertNotIn("example-password", out)
        self.assertNotIn(" -w", self.call_log.read_text())

    def test_a_drop_in_for_another_brew_prefix_is_a_finding(self):
        """sd:6475: terra's drop-in allowed /opt/homebrew/bin/brew, which an
        Intel Mac does not have; the repair asked for a password while
        doctor said ok."""
        out = self.doctor(keychain="example-password", sudoers=True,
                          rule=f"    (root) NOPASSWD: /opt/homebrew/bin/brew {RESTART}")
        self.assertRegex(out, r"  WARN    repair sudoers drop-in \S+machine-setup-repairs does not "
                              rf"allow /usr/local/bin/brew {RESTART} without a password — "
                              r"fix its brew path: sudo visudo -f \S+machine-setup-repairs\n")
        self.assertNotIn("  ok      repair sudoers drop-in", out)

    def test_the_restart_line_is_dead_with_the_tailscale_app_cask(self):
        """sd:6554, sd:6475: the app's daemon is no brew service, so the
        drop-in's restart line repairs nothing there."""
        (self.profiles / "personal.cask").write_text("tailscale-app\n")
        out = self.doctor(keychain="example-password", sudoers=True)
        self.assertRegex(out, r"  WARN    repair sudoers drop-in \S+ allows brew services restart "
                              r"tailscale, dead with the tailscale-app cask — drop that line")
        out = self.doctor(keychain="example-password", sudoers=True, rule="    (root) NOPASSWD: /sbin/pfctl -sr")
        self.assertIn("  ok      repair sudoers drop-in ", out)
        self.assertNotIn("does not allow", out)

    def test_an_unread_sudo_listing_is_unknown_not_ok(self):
        out = self.doctor(keychain="example-password", sudoers=True, SUDO_RC="1")
        self.assertRegex(out, r"  WARN    repair sudoers drop-in \S+ unknown: sudo -n -l exited 1\n")
        out = self.doctor(keychain="example-password", sudoers=True, prefix="")
        self.assertRegex(out, r"  WARN    repair sudoers drop-in \S+ unknown: brew --prefix gave no answer\n")

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

    def test_an_item_with_no_cask_list_is_a_finding_naming_what_to_set(self):
        """sd:6474: with the item there and the list unset or change-me, the
        weekly upgrade upgraded no cask with the askpass, and doctor said
        ok. The ruling: list the casks with a pkg installer."""
        want = ("  WARN    MACHINE_SETUP_SUDO_CASKS {} — the weekly upgrade defers every cask "
                "whose pkg installer calls sudo; list those casks in machine-setup.env, "
                'e.g. "zoom google-drive" (README: Unattended sudo)\n')
        for casks, shown in (("", "unset"), ("change-me", "is change-me")):
            with self.subTest(casks=casks):
                result = self.run_doctor(keychain="example-password", MACHINE_SETUP_SUDO_CASKS=casks)
                self.assertIn(want.format(shown), result.stdout)
                self.assertNotIn("FAIL    MACHINE_SETUP_SUDO_CASKS", result.stdout)
        result = self.run_doctor(keychain="example-password", MACHINE_SETUP_SUDO_CASKS="zoom")
        self.assertIn("  ok      MACHINE_SETUP_SUDO_CASKS: zoom\n", result.stdout)
        result = self.run_doctor(keychain="", MACHINE_SETUP_SUDO_CASKS="")
        self.assertNotIn("MACHINE_SETUP_SUDO_CASKS", result.stdout)

    def test_no_cask_list_leaves_the_doctor_verdict_alone(self):
        verdicts = []
        for casks in ("", "zoom"):
            result = self.run_doctor(keychain="example-password", MACHINE_SETUP_SUDO_CASKS=casks)
            verdicts.append((result.returncode, result.stdout.splitlines()[-1]))
        self.assertEqual(verdicts[0], verdicts[1])

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
    def rendered(self, prefix):
        """The drop-in the README's install step writes on a machine whose
        `brew --prefix` is PREFIX, its rules only (sd:6475)."""
        readme = (FOLDER / "README.md").read_text()
        step = re.search(r"^ +(sed .+?\n.+? > )/tmp/machine-setup-repairs$", readme, re.M)
        self.assertIsNotNone(step, "README: no sed step that writes /tmp/machine-setup-repairs")
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        write_exec(tmp / "bin/brew", f'#!/bin/sh\n[ "$*" = --prefix ] && echo {prefix}\n')
        write_exec(tmp / "bin/id", '#!/bin/sh\n[ "$*" = -un ] && echo example\n')
        out = tmp / "drop-in"
        subprocess.run(["sh", "-c", step.group(1) + str(out)], cwd=FOLDER, check=True,
                       env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin"}, timeout=30)
        return [line for line in out.read_text().splitlines()
                if line.strip() and not line.startswith("#")]

    def test_each_rule_is_nopasswd_for_one_exact_command(self):
        """Exact arguments only: an absolute path, its arguments, no
        wildcard; sudoers then allows those arguments and no others."""
        rules = self.rendered("/opt/homebrew")
        self.assertEqual(len(rules), 2, rules)
        for rule in rules:
            self.assertRegex(rule, r"^example ALL=\(root\) NOPASSWD: /\S+( [^*?\[\]\\,:=]+)+$")
        self.assertTrue(rules[0].endswith("NOPASSWD: /opt/homebrew/bin/brew services restart tailscale"))
        self.assertTrue(rules[1].endswith("NOPASSWD: /sbin/pfctl -sr"), rules[1])

    def test_the_brew_rule_follows_the_machines_brew_prefix(self):
        """sd:6475: the example named /opt/homebrew/bin/brew, dead on an
        Intel Mac, where brew is /usr/local/bin/brew."""
        self.assertNotIn("/opt/homebrew", SUDOERS.read_text())
        rules = self.rendered("/usr/local")
        self.assertEqual(rules[0], "example ALL=(root) NOPASSWD: /usr/local/bin/brew services restart tailscale")


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
