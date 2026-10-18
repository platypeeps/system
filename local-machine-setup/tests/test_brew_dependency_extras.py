"""A formula brew installed as a dependency is not extra (sd:5049).

`brew leaves` lists every installed formula no other installed formula needs
at run time. A build-only dependency is one of them, so `status` reported it
as extra drift and `capture` wrote it into the profile. Both now read
`brew leaves --installed-on-request`: what someone installed by name.

The stub `brew` answers as a machine with one formula installed by hand and
one left over from a build.
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

BREW_STUB = r"""#!/bin/sh
case "$*" in
  "list --formula"|leaves) printf '%s\n' example-tool example-build-dep ;;
  "leaves --installed-on-request") printf '%s\n' example-tool ;;
  --prefix) echo /usr/local ;;
esac
exit 0
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class DependencyExtras(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        repo = base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config_root = fixture_config.copy_config(base)
        self.profiles = self.config_root / "machine-setup/profiles"
        (self.profiles / "common.brew").write_text("")
        (self.profiles / "personal.brew").write_text("")
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.stubs = base / "stubs"
        write_exec(self.stubs / "brew", BREW_STUB)
        for name in ("defaults", "sudo", "launchctl"):
            write_exec(self.stubs / name, "#!/bin/sh\nexit 1\n")
        # No App Store apps: a failed `mas list` makes capture refuse (sd:3536).
        write_exec(self.stubs / "mas", "#!/bin/sh\nexit 0\n")
        fixture_config.seal(self, self.stubs)

    def run_verb(self, *args):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args], env=env,
                              capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    def test_status_counts_a_formula_installed_by_hand_not_a_dependency(self):
        result = self.run_verb("status")

        self.assertIn("  extra   : example-tool\n", result.stdout)
        self.assertNotIn("example-build-dep", result.stdout)

    def test_capture_writes_a_formula_installed_by_hand_not_a_dependency(self):
        result = self.run_verb("capture", "--apply", "--additive")

        written = [line for line in (self.profiles / "personal.brew").read_text().splitlines()
                   if line.strip() and not line.startswith("#")]
        self.assertEqual(written, ["example-tool"], result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
