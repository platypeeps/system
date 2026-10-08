"""machine-setup keeps Homebrew python pinned (sd:3062).

Homebrew python is ad-hoc signed, so every patch upgrade drops the macOS
grants its binary held. The operator unpins and re-grants on purpose, so the
brew stage converges `brew pin python@3.14`, and `status` counts an unpinned
one as drift. python@3.12 stays as it is.

The script runs from a copy of this folder under a temporary root; `brew` is
a stub that keeps its pins in a file.
"""

import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

# Two pythons are installed. `pin` records into $PINS; `list --pinned` reads it.
BREW_STUB = r"""#!/bin/sh
case "$*" in
  "list --formula") printf '%s\n' python@3.12 python@3.14 ;;
  "list --cask"|leaves|tap) : ;;
  "list --pinned") cat "$PINS" 2>/dev/null ;;
  "pin "*) echo "$2" >> "$PINS" ;;
  --prefix) echo /usr/local ;;
esac
exit 0
"""

EXIT_1_STUB = "#!/bin/sh\nexit 1\n"


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
        for name in ("defaults", "sudo", "launchctl", "mas"):
            write_exec(self.stubs / name, EXIT_1_STUB)
        fixture_config.seal(self, self.stubs)

    def run_verb(self, *args):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
            "PINS": str(self.pins),
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args], env=env,
                              capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    def test_the_brew_stage_pins_python_and_leaves_3_12(self):
        result = self.run_verb("update", "brew", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pins.read_text().split(), ["python@3.14"])

        again = self.run_verb("update", "brew", "--apply")

        self.assertIn("ok      pinned python@3.14", again.stdout)
        self.assertEqual(self.pins.read_text().split(), ["python@3.14"])

    def test_status_counts_an_unpinned_python_as_drift(self):
        unpinned = self.run_verb("status")
        self.pins.write_text("python@3.14\n")
        pinned = self.run_verb("status")

        self.assertIn("unpinned: python@3.14", unpinned.stdout)
        self.assertNotIn("unpinned", pinned.stdout)
        before = int(unpinned.stdout.split("drift   : ")[1].split()[0])
        after = int(pinned.stdout.split("drift   : ")[1].split()[0])
        self.assertEqual(before - after, 1)


if __name__ == "__main__":
    unittest.main()
