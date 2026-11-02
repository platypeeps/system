"""The tooling stage installs Xcode's Metal toolchain where the profile installs Xcode.

Xcode ships without the Metal compiler; it is a separate component
(`xcodebuild -downloadComponent MetalToolchain`). A formula built with
custom kernels, such as omlx, fails without it (2026-11-02). The
profiles that install Xcode (App Store id 497799835 in a `.mas` file) get it;
a profile without Xcode is left alone.

xcodebuild and xcrun are stubs: XCODE_INSTALLED says whether Xcode answers,
METAL_INSTALLED whether `xcrun metal` does, and every call is logged.
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

XCODEBUILD = r"""#!/bin/sh
echo "xcodebuild $*" >> "$XC_LOG"
case "$1" in
  -version) [ "$XCODE_INSTALLED" = 1 ] ;;
  *) exit 0 ;;
esac
"""

XCRUN = r"""#!/bin/sh
echo "xcrun $*" >> "$XC_LOG"
[ "$1" = metal ] && [ "$METAL_INSTALLED" = 1 ]
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class ToolingMetalTest(unittest.TestCase):
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
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.log = base / "xc.log"
        self.stubs = base / "stubs"
        write_exec(self.stubs / "xcodebuild", XCODEBUILD)
        write_exec(self.stubs / "xcrun", XCRUN)
        write_exec(self.stubs / "brew", "#!/bin/sh\nexit 0\n")
        write_exec(self.stubs / "sudo", "#!/bin/sh\nexit 1\n")
        fixture_config.seal(self, self.stubs)

    def tooling(self, *flags, xcode="1", metal="0"):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
            "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
            "XC_LOG": str(self.log),
            "XCODE_INSTALLED": xcode,
            "METAL_INSTALLED": metal,
        }
        result = subprocess.run([str(self.folder / "machine-setup.sh"), "update", "tooling", *flags],
                                env=env, capture_output=True, text=True, cwd=self.tmp.name,
                                stdin=subprocess.DEVNULL, timeout=60)
        return result.stdout + result.stderr

    def downloads(self):
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return [c for c in calls if "-downloadComponent MetalToolchain" in c]

    def with_xcode(self):
        (self.profiles / "personal.mas").write_text("497799835 Xcode\n")

    def test_a_profile_with_xcode_downloads_a_missing_metal_toolchain(self):
        self.with_xcode()
        out = self.tooling("--apply")
        self.assertIn("  MISSING Metal toolchain", out)
        self.assertEqual(self.downloads(), ["xcodebuild -downloadComponent MetalToolchain"])

    def test_a_dry_run_names_it_and_downloads_nothing(self):
        self.with_xcode()
        out = self.tooling()
        self.assertIn("  MISSING Metal toolchain", out)
        self.assertEqual(self.downloads(), [])

    def test_a_present_toolchain_is_ok(self):
        self.with_xcode()
        out = self.tooling("--apply", metal="1")
        self.assertIn("  ok      Metal toolchain", out)
        self.assertEqual(self.downloads(), [])

    def test_xcode_not_yet_installed_skips(self):
        self.with_xcode()
        out = self.tooling("--apply", xcode="0")
        self.assertIn("  SKIP    Metal toolchain", out)
        self.assertEqual(self.downloads(), [])

    def test_a_profile_without_xcode_is_left_alone(self):
        (self.profiles / "personal.mas").write_text("")
        out = self.tooling("--apply")
        self.assertNotIn("Metal toolchain", out)
        self.assertEqual(self.log.read_text().splitlines() if self.log.exists() else [], [])


if __name__ == "__main__":
    unittest.main()
