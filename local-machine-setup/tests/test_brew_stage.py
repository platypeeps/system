"""The brew stage: taps, formulae and casks, and one pin (sd:6463).

The python@X.Y that PATH python3 runs stays pinned: each upgrade is a new
Python.app, which the application firewall blocked, so sd-serve admitted
no satellite. Any other pin an older stage made (python@*, opencode,
codex) is removed (sd:6433).

The script runs from a copy of this folder under a temporary root; `brew` is
a stub that logs each install and pin in a file, `python3` a stub that
reports $PY_PREFIX as its install path, and the firewall tool a stub that prints $FW_LIST.
"""

import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from tests import fixture_config
from tests.fixture_config import STALL, STALL_BOUND

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

# $PYTHONS are installed (three by default), plus $FORMULAE. `pin`,
# `install` and a bare `upgrade` record into $PINS; `list --pinned` reads it, and `unpin` logs
# into $PINS.unpin and drops the pin.
BREW_STUB = "#!/bin/sh\n" + STALL + r"""case "$*" in
  "list --formula") [ -z "$LIST_HANG" ] || stall
                    [ -z "$LIST_RC" ] || exit "$LIST_RC"
                    printf '%s\n' ${PYTHONS-python@3.12 python@3.14 python@3.15} $FORMULAE ;;
  "list --cask"|leaves|tap) : ;;
  "list --pinned") [ -z "$PINNED_RC" ] || exit "$PINNED_RC"
                   sed -n 's/^pin //p' "$PINS" 2>/dev/null ;;
  "pin "*|"install "*|upgrade) echo "$*" >> "$PINS" ;;
  "unpin "*) echo "$*" >> "$PINS.unpin"
             grep -vx "pin ${2}" "$PINS" > "$PINS.new" || :
             mv "$PINS.new" "$PINS" ;;
  --prefix) echo /usr/local ;;
esac
exit 0
"""

EXIT_1_STUB = "#!/bin/sh\nexit 1\n"

FIREWALL_STUB = "#!/bin/sh\n[ \"$1\" = --listapps ] && printf '%b' \"$FW_LIST\"\nexit 0\n"

# The install path the stubbed python3 reports by default, and its app.
PY_PREFIX = "/opt/homebrew/opt/python@3.15/Frameworks/Python.framework/Versions/3.15"
PYTHON_APP = PY_PREFIX + "/Resources/Python.app"

# Asked for sys.base_prefix, python3 prints $PY_PREFIX; every other call is
# the real interpreter's, as other stages run python3.
PYTHON3_STUB = f"""#!/bin/sh
if [ "$*" = "-c import sys; print(sys.base_prefix)" ]; then
  echo "${{PY_PREFIX:-{PY_PREFIX}}}"
  exit 0
fi
exec {sys.executable} "$@"
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class BrewStageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        repo = base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config_root = fixture_config.copy_config(base)
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.pins = base / "pins"
        self.stubs = base / "stubs"
        write_exec(self.stubs / "brew", BREW_STUB)
        write_exec(self.stubs / "python3", PYTHON3_STUB)
        write_exec(self.stubs / "socketfilterfw", FIREWALL_STUB)
        for name in ("defaults", "sudo", "launchctl", "mas"):
            write_exec(self.stubs / name, EXIT_1_STUB)
        fixture_config.seal(self, self.stubs)

    def run_verb(self, *args, formulae="", **extra):
        env = {
            "FORMULAE": formulae,
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
            "PINS": str(self.pins),
            **extra,
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args], env=env,
                              capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    def pin_calls(self):
        text = self.pins.read_text() if self.pins.exists() else ""
        return [line for line in text.splitlines() if line.startswith("pin ")]

    def test_the_script_keeps_no_pin_list(self):
        """sd:6433: the pinned python is read from PATH python3, not a list."""
        script = (FOLDER / "machine-setup.sh").read_text()

        self.assertNotIn("BREW_PINNED", script)

    def test_the_brew_stage_pins_only_the_serving_python(self):
        """sd:6463: an unpinned upgrade blocked sd-serve in the firewall."""
        dry = self.run_verb("update", "brew", formulae="opencode codex jq")
        self.assertIn("DIFFERS python@3.15 not pinned", dry.stdout)
        self.assertEqual(self.pin_calls(), [])

        result = self.run_verb("update", "brew", "--apply", formulae="opencode codex jq")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pin_calls(), ["pin python@3.15"])
        again = self.run_verb("update", "brew", formulae="opencode codex jq")
        self.assertNotIn("pinned", again.stdout)

    def test_the_python_is_pinned_before_any_install(self):
        """Review: an install that needs a newer python upgraded it first."""
        (self.config_root / "machine-setup/profiles/personal.brew").write_text("jq\n")
        self.run_verb("update", "brew", "--apply")

        self.assertEqual(self.pins.read_text().splitlines()[:2], ["pin python@3.15", "install jq"])

    def test_the_upgrade_sweep_pins_the_python_first(self):
        """Review class pass: a bare `brew upgrade` moved an unpinned python."""
        self.run_verb("upgrade", "--apply")

        self.assertEqual(self.pins.read_text().splitlines()[:2], ["pin python@3.15", "upgrade"])

    def test_the_pin_follows_the_python_on_path(self):
        self.run_verb("update", "brew", "--apply",
                      PY_PREFIX="/opt/homebrew/Cellar/python@3.14/3.14.8/Frameworks/Python.framework/Versions/3.14")

        self.assertEqual(self.pin_calls(), ["pin python@3.14"])

    def test_a_python_brew_does_not_own_is_never_pinned(self):
        """Review: Apple's or pyenv's python3 named a formula by version alone."""
        for prefix in ("/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9",
                       f"{self.home}/.pyenv/versions/3.15.2"):
            with self.subTest(prefix):
                result = self.run_verb("update", "brew", "--apply", PY_PREFIX=prefix)

                self.assertNotIn("not pinned", result.stdout)
                self.assertEqual(self.pin_calls(), [])

    def test_status_counts_an_unpinned_python_as_drift(self):
        unpinned = self.run_verb("status")
        self.pins.write_text("pin python@3.15\n")
        clean = self.run_verb("status")

        self.assertIn("  unpinned: python@3.15", unpinned.stdout)
        self.assertNotIn("unpinned", clean.stdout)
        drift = [int(r.stdout.split("drift   : ")[1].split()[0]) for r in (unpinned, clean)]
        self.assertEqual(drift[0] - drift[1], 1)

    def test_a_firewall_block_on_the_serving_python_is_reported(self):
        """sd:6463: fixing it needs sudo, so it is reported, never changed."""
        self.pins.write_text("pin python@3.15\n")
        blocked = {"FW_LIST": f"1 : {PYTHON_APP} \\n             (Block incoming connections)\\n"}
        allowed = {"FW_LIST": f"1 : {PYTHON_APP} \\n             (Allow incoming connections)\\n"}

        stage = self.run_verb("update", "brew", **blocked)
        self.assertIn(f"DIFFERS firewall blocks {PYTHON_APP}", stage.stdout)
        status = self.run_verb("status", **blocked)
        self.assertIn(f"  blocked : {PYTHON_APP}", status.stdout)
        self.assertNotIn("blocked", self.run_verb("status", **allowed).stdout)

    def unpin_calls(self):
        log = pathlib.Path(f"{self.pins}.unpin")
        return log.read_text().splitlines() if log.exists() else []

    def test_a_pin_the_old_stage_made_is_unpinned_under_apply(self):
        """sd:6433 review: a machine the old stage ran on keeps an older
        python pinned, and a bare `brew upgrade` fails on a dependent that
        needs a newer one. The serving python and a pin made by hand on
        another formula stay."""
        self.pins.write_text("pin python@3.12\npin python@3.15\npin opencode\npin jq\n")
        dry = self.run_verb("update", "brew", formulae="opencode jq")

        self.assertIn("DIFFERS python@3.12 pinned", dry.stdout)
        self.assertIn("DIFFERS opencode pinned", dry.stdout)
        self.assertNotIn("python@3.15 pinned", dry.stdout)
        self.assertNotIn("jq pinned", dry.stdout)
        self.assertEqual(self.unpin_calls(), [])

        result = self.run_verb("update", "brew", "--apply", formulae="opencode jq")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.unpin_calls(), ["unpin python@3.12", "unpin opencode"])
        again = self.run_verb("update", "brew", formulae="opencode jq")
        self.assertNotIn("pinned", again.stdout)

    def test_status_counts_a_pin_the_old_stage_made_as_drift(self):
        self.pins.write_text("pin python@3.15\npin python@3.12\n")
        pinned = self.run_verb("status")
        self.pins.write_text("pin python@3.15\n")
        clean = self.run_verb("status")

        self.assertIn("  pinned  : python@3.12", pinned.stdout)
        self.assertNotIn("pinned  :", clean.stdout)
        drift = [int(r.stdout.split("drift   : ")[1].split()[0]) for r in (pinned, clean)]
        self.assertEqual(drift[0] - drift[1], 1)

    def test_a_pin_list_brew_cannot_give_unpins_nothing(self):
        """sd:3536: a failed list is unknown, not "nothing pinned"."""
        result = self.run_verb("update", "brew", "--apply", PINNED_RC="1")

        self.assertIn("FAILED  brew stage: brew could not list its pins; nothing changed", result.stdout)
        self.assertEqual(self.unpin_calls(), [])
        status = self.run_verb("status", PINNED_RC="1")
        self.assertIn("pinned  : unknown (brew could not list its pins)", status.stdout)

    def test_a_list_brew_cannot_give_changes_nothing(self):
        """sd:3536: read as empty, a failed list installed every formula again."""
        (self.config_root / "machine-setup/profiles/personal.brew").write_text("jq\n")
        result = self.run_verb("update", "brew", "--apply", LIST_RC="1")

        self.assertIn("FAILED  brew stage: brew could not list what is installed; nothing changed",
                      result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.pins.exists(), self.pins.read_text() if self.pins.exists() else "")

    def test_a_brew_call_that_hangs_ends_at_its_bound_and_names_itself(self):
        """sd:3536: an unattended brew stage bounds each brew call."""
        result = self.run_verb("update", "brew", "--apply", LIST_HANG="1",
                               MACHINE_SETUP_STEP_TIMEOUT=str(STALL_BOUND))

        self.assertRegex(result.stderr, rf"\[step [\d:]+\] brew list --formula \(bound {STALL_BOUND}s\)")
        self.assertRegex(result.stderr, r"\[step [\d:]+\] brew list --formula exited 124")
        self.assertFalse(self.pins.exists(), self.pins.read_text() if self.pins.exists() else "")


if __name__ == "__main__":
    unittest.main()
