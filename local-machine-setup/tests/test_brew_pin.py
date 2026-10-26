"""machine-setup keeps Homebrew python pinned (sd:3062).

Homebrew python is ad-hoc signed, so every patch upgrade drops the macOS
grants its binary held. The operator unpins and re-grants on purpose, so the
brew stage converges `brew pin python@3.15`, and `status` counts an unpinned
one as drift. python@3.12 stays as it is. The pin follows Homebrew's default
python, so `status` names a PATH python3 of another version (sd:5047).

The script runs from a copy of this folder under a temporary root; `brew` is
a stub that keeps its pins in a file, and `python3` a stub that prints
$PY_VERSION.
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

# $PYTHONS are installed (three by default), plus $FORMULAE. `pin` records
# into $PINS; `list --pinned` reads it.
BREW_STUB = "#!/bin/sh\n" + STALL + r"""case "$*" in
  "list --formula") [ -z "$LIST_HANG" ] || stall
                    [ -z "$LIST_RC" ] || exit "$LIST_RC"
                    printf '%s\n' ${PYTHONS-python@3.12 python@3.14 python@3.15} $FORMULAE ;;
  "list --cask"|leaves|tap) : ;;
  "list --pinned") [ -z "$PINNED_RC" ] || exit "$PINNED_RC"
                   sed -n 's/^pin //p' "$PINS" 2>/dev/null ;;
  "pin "*|"install "*) echo "$*" >> "$PINS" ;;
  --prefix) echo /usr/local ;;
esac
exit 0
"""

EXIT_1_STUB = "#!/bin/sh\nexit 1\n"

# `python3 --version` prints $PY_VERSION, or fails with PY_RC; every other
# call is the real interpreter's, as other stages run python3.
PYTHON3_STUB = f"""#!/bin/sh
if [ "$*" = --version ]; then
  [ -z "$PY_RC" ] || exit "$PY_RC"
  echo "${{PY_VERSION:-Python 3.15.2}}"
  exit 0
fi
exec {sys.executable} "$@"
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class BrewPinTest(unittest.TestCase):
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

    def test_the_brew_stage_pins_python_and_leaves_3_12(self):
        result = self.run_verb("update", "brew", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pins.read_text().splitlines(), ["pin python@3.15"])

        again = self.run_verb("update", "brew", "--apply")

        self.assertIn("ok      pinned python@3.15", again.stdout)
        self.assertEqual(self.pins.read_text().splitlines(), ["pin python@3.15"])

    def test_the_brew_stage_pins_the_agent_clis_the_pack_gate_runs(self):
        # REGRESSION sd:3542: the weekly upgrade moved opencode under the pack gate's live test.
        result = self.run_verb("update", "brew", "--apply", formulae="opencode codex jq")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pins.read_text().splitlines(),
                         ["pin python@3.15", "pin opencode", "pin codex"])

    def test_status_counts_an_unpinned_python_as_drift(self):
        unpinned = self.run_verb("status")
        self.pins.write_text("pin python@3.15\n")
        pinned = self.run_verb("status")

        self.assertIn("unpinned: python@3.15", unpinned.stdout)
        self.assertNotIn("unpinned", pinned.stdout)
        before = int(unpinned.stdout.split("drift   : ")[1].split()[0])
        after = int(pinned.stdout.split("drift   : ")[1].split()[0])
        self.assertEqual(before - after, 1)


    def drift(self, result):
        return int(result.stdout.split("drift   : ")[1].split()[0])

    def test_status_names_a_python3_that_is_not_the_pinned_python(self):
        """sd:5047: Homebrew's default python moved to 3.15 and relinked
        python3 off the pinned 3.14, which dropped its Full Disk Access."""
        self.pins.write_text("pin python@3.15\n")
        moved = self.run_verb("status", PY_VERSION="Python 3.16.0")
        same = self.run_verb("status", PY_VERSION="Python 3.15.2")

        self.assertIn("  python  : python3 is 3.16, pin is python@3.15: move the pin and re-grant "
                      "Full Disk Access", moved.stdout)
        self.assertNotIn("python3 is", same.stdout)
        self.assertEqual(self.drift(moved) - self.drift(same), 1)

    def test_a_python3_that_cannot_answer_is_unknown(self):
        self.pins.write_text("pin python@3.15\n")
        result = self.run_verb("status", PY_RC="1")

        self.assertIn("  python  : python3 version unknown (brew or python3 did not answer)", result.stdout)

    def test_no_pinned_python_installed_checks_no_python3(self):
        result = self.run_verb("status", PYTHONS="python@3.12", PY_VERSION="Python 3.16.0")

        self.assertNotIn("python  :", result.stdout)


    def test_a_list_brew_cannot_give_changes_nothing(self):
        """sd:3536: read as empty, a failed list installed every formula again."""
        (self.config_root / "machine-setup/profiles/personal.brew").write_text("jq\n")
        result = self.run_verb("update", "brew", "--apply", LIST_RC="1")

        self.assertIn("FAILED  brew stage: brew could not list what is installed; nothing changed",
                      result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.pins.exists(), self.pins.read_text() if self.pins.exists() else "")

    def test_a_pin_list_brew_cannot_give_pins_nothing(self):
        """sd:3536: read as nothing pinned, a failed list pinned again."""
        result = self.run_verb("update", "brew", "--apply", PINNED_RC="1")

        self.assertIn("FAILED  brew stage: brew could not list its pins; none changed", result.stdout)
        self.assertFalse(self.pins.exists(), self.pins.read_text() if self.pins.exists() else "")
        status = self.run_verb("status", PINNED_RC="1")
        self.assertIn("unpinned: unknown (brew could not list its pins)", status.stdout)

    def test_a_brew_call_that_hangs_ends_at_its_bound_and_names_itself(self):
        """sd:3536: an unattended brew stage bounds each brew call."""
        result = self.run_verb("update", "brew", "--apply", LIST_HANG="1",
                               MACHINE_SETUP_STEP_TIMEOUT=str(STALL_BOUND))

        self.assertRegex(result.stderr, rf"\[step [\d:]+\] brew list --formula \(bound {STALL_BOUND}s\)")
        self.assertRegex(result.stderr, r"\[step [\d:]+\] brew list --formula exited 124")
        self.assertFalse(self.pins.exists(), self.pins.read_text() if self.pins.exists() else "")


if __name__ == "__main__":
    unittest.main()
