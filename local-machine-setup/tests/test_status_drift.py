"""`status` counts every pending remediation as drift (sd:3544).

status runs each stage as a dry run and counts the lines that carry a drift
word. A remediation that printed only its `[dry-run]` line counted as nothing:
a stopped service, a missing node version or rtk hook, Chrome not the mailto
handler, the firewall or stealth mode off, a credential folder not 700, the
Spotlight sudoers drop-in, and a missing tap all left `drift 0`, so
`--fail-on-drift` exited 0 with them pending.

Every case runs `status` from a copy of the folder, with stubs on PATH for
each tool a stage asks; nothing reads or changes this Mac.
"""

import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

# The words status_stage greps to count drift.
DRIFT_WORDS = re.compile(r"DIFFERS|MISSING|STALE|ABSENT|UNLOADED|EXTRA|defaults (-currentHost )?write|^  extra ")

TAP = "example-owner/tap"

# No tap, formula or cask installed; $BREW_TAPS lists the taps it has.
BREW_STUB = r"""#!/bin/sh
case "$*" in
  tap) [ -n "$BREW_TAPS" ] && printf '%s\n' $BREW_TAPS ;;
  --prefix) echo /usr/local ;;
esac
exit 0
"""

# $FIREWALL is on or off; both answers in socketfilterfw's words.
FIREWALL_STUB = r"""#!/bin/sh
case "$1:$FIREWALL" in
  --getglobalstate:on) echo "Firewall is enabled. (State = 1)" ;;
  --getglobalstate:*) echo "Firewall is disabled. (State = 0)" ;;
  --getstealthmode:on) echo "Firewall stealth mode is on" ;;
  --getstealthmode:*) echo "Firewall stealth mode is off" ;;
esac
exit 0
"""

EXIT_0 = "#!/bin/sh\nexit 0\n"
EXIT_1 = "#!/bin/sh\nexit 1\n"


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class StatusDrift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        repo = base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        # A docker service the profile wants; its entrypoint is never run.
        write_exec(repo / "local-example/example.sh", EXIT_1)

        self.config = fixture_config.copy_config(base)
        self.profiles = self.config / "machine-setup/profiles"
        (self.profiles / "personal.tap").write_text(f"{TAP}\n")
        (self.profiles / "personal.service").write_text("local-example\n")
        self.sudoers = base / "sudoers.d/machine-setup-spotlight-read"

        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        # A credential folder its tool made world-readable.
        (self.home / ".ssh").mkdir(mode=0o755)
        (self.home / ".ssh").chmod(0o755)

        self.stubs = base / "stubs"
        write_exec(self.stubs / "brew", BREW_STUB)
        write_exec(self.stubs / "socketfilterfw", FIREWALL_STUB)
        # fnm has node 18 only; docker runs nothing.
        write_exec(self.stubs / "fnm", '#!/bin/sh\n[ "$1" = ls ] && echo "* v18.20.0"\nexit 0\n')
        write_exec(self.stubs / "docker", EXIT_0)
        for name in ("rtk", "duti"):
            write_exec(self.stubs / name, EXIT_0)
        # defaults: no mailto handler, no app keys. sudo: no ticket.
        for name in ("defaults", "sudo", "launchctl", "mas"):
            write_exec(self.stubs / name, EXIT_1)
        fixture_config.seal(self, self.stubs)

    def status(self, *flags, firewall="off", taps="", sudoers=None):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config),
            "MACHINE_SETUP_SPOTLIGHT_SUDOERS": str(sudoers or self.sudoers),
            "FIREWALL": firewall,
            "BREW_TAPS": taps,
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), "status", *flags], env=env,
                              capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    @staticmethod
    def drift(out):
        found = re.search(r"^drift   : (\d+) item\(s\)$", out, re.M)
        assert found, out
        return int(found.group(1))

    def test_status_runs_every_stage_but_brew_in_stage_order(self):
        # sd:3548: status kept a hand copy of STAGES, in another order.
        stages = re.search(r'^STAGES="([^"]*)"', (FOLDER / "machine-setup.sh").read_text(), re.M).group(1)
        out = self.status().stdout
        ran = re.findall(r"^== (\S+)", out, re.M)
        self.assertEqual(ran, [s for s in stages.split() if s != "brew"], out)

    def test_every_planned_remediation_sits_under_a_drift_word(self):
        out = self.status().stdout
        last = ""
        uncounted = []
        for line in out.splitlines():
            if line.startswith("  [dry-run]"):
                if not DRIFT_WORDS.search(line) and not DRIFT_WORDS.search(last):
                    uncounted.append(line)
            else:
                last = line
        self.assertEqual(uncounted, [], out)

    def test_each_pending_tooling_services_and_system_item_is_named(self):
        out = self.status().stdout
        for line in ("  MISSING local-example — not running",
                     "  MISSING node 20 (fnm)",
                     "  MISSING node 24 (fnm)",
                     "  MISSING rtk hook in ~/.claude/settings.json",
                     "  MISSING rtk hook for Gemini CLI",
                     "  DIFFERS mailto handler is not com.google.Chrome",
                     "  DIFFERS application firewall off",
                     "  DIFFERS firewall stealth mode off",
                     "  MISSING Spotlight read sudoers drop-in",
                     "  DIFFERS ~/.ssh is not 700"):
            self.assertIn(line + "\n", out)

    def test_firewall_off_counts_twice_and_on_counts_nothing(self):
        on = self.drift(self.status(firewall="on").stdout)
        off = self.drift(self.status(firewall="off").stdout)
        self.assertEqual(off - on, 2)

    def test_a_missing_tap_is_drift(self):
        missing = self.status().stdout
        present = self.status(taps=TAP).stdout
        self.assertIn(f"taps\n  missing : {TAP}\n", missing)
        self.assertEqual(self.drift(missing) - self.drift(present), 1)

    def test_a_missing_release_tool_is_drift_in_its_own_section(self):
        # sd:4853: nothing is downloaded; status only reads ~/.local/bin and
        # the recorded tag.
        before = self.drift(self.status().stdout)
        (self.profiles / "personal.release").write_text(
            "example example-owner/example example-{version}.tar.gz\n")
        out = self.status().stdout
        self.assertIn("== release\n  MISSING release example (no example in ", out)
        self.assertEqual(self.drift(out) - before, 1)

    def test_a_missing_spotlight_sudoers_drop_in_is_drift(self):
        # Without the drop-in the Spotlight read only DEFERs, which is not a
        # drift word, so the missing drop-in was the one sign of the gap.
        missing = self.drift(self.status().stdout)
        self.sudoers.parent.mkdir(parents=True)
        self.sudoers.write_text("# fixture drop-in\n")
        present = self.drift(self.status().stdout)
        self.assertEqual(missing - present, 1)


if __name__ == "__main__":
    unittest.main()
