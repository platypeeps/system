"""The `.unity` kind: Unity editor modules with no cask (sd:10302).

Homebrew has casks for the iOS, WebGL and Windows modules, none for Mac
IL2CPP, so a player build with that backend failed. A `.unity` line is
`<receipt> <installer>`; the brew stage installs each module for the
unity cask's version and changeset, and reinstalls one whose receipt or
files are gone.

brew, pkgutil, curl and sudo are stubs on PATH: no case reads this Mac's
receipts, downloads, or installs. The stubbed installer writes the receipt
and the module's first file under the case's Unity root.
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

BASE = "https://unity.example.test/download"
VERSION = "6000.6.10f1"
CHANGESET = "40e81493c67d"
RECEIPT = f"com.unity3d.MacStandaloneSupportIL2CPP-{VERSION}"
URL = (f"{BASE}/{CHANGESET}/MacEditorTargetInstaller/"
       f"UnitySetup-Mac-IL2CPP-Support-for-Editor-{VERSION}.pkg")
SIGNED = ("Package \"m.pkg\":\n   Status: signed by a developer certificate issued by Apple\n"
          "   Certificate Chain:\n    1. Developer ID Installer: Unity Technologies SF (9QW8UQUTAA)\n")

# `list --cask` prints $CASKS; `list --cask --versions unity` prints
# $UNITY_VERSIONS, or fails with VERSIONS_RC.
BREW_STUB = r"""#!/bin/sh
case "$*" in
  "list --cask") printf '%s\n' $CASKS ;;
  "list --cask --versions unity") [ -z "$VERSIONS_RC" ] || exit "$VERSIONS_RC"
                                  printf '%s\n' "$UNITY_VERSIONS" ;;
esac
exit 0
"""

# A receipt is the file $RECEIPTS/<id>; it holds the module's first file,
# relative to $LOCATION. --check-signature prints $SIG and fails with SIG_RC.
PKGUTIL_STUB = r"""#!/bin/sh
echo "pkgutil $*" >> "$LOG"
case "$1" in
  --pkg-info) [ -f "$RECEIPTS/$2" ] || exit 1
              printf 'package-id: %s\nvolume: /\nlocation: %s\n' "$2" "${LOCATION#/}" ;;
  --only-files) [ -f "$RECEIPTS/$3" ] || exit 1
                cat "$RECEIPTS/$3" ;;
  --check-signature) printf '%s' "$SIG"; exit "${SIG_RC:-0}" ;;
esac
"""

CURL_STUB = r"""#!/bin/sh
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    --max-time) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
echo "curl $url" >> "$LOG"
[ -z "$CURL_RC" ] || exit "$CURL_RC"
echo pkg > "$out"
"""

# `-n true` answers $SUDO_N, `-v` $SUDO_V; `[-A] installer -pkg F -target /`
# logs the call and, unless INSTALL_RC, writes the receipt and its file.
SUDO_STUB = r"""#!/bin/sh
echo "sudo $* askpass=${SUDO_ASKPASS:+set}" >> "$LOG"
case "$1" in
  -n) exit "${SUDO_N:-0}" ;;
  -v) exit "${SUDO_V:-1}" ;;
  -A) shift ;;
esac
[ "$1 $2" = "installer -pkg" ] && [ -f "$3" ] || exit 2
[ -z "$INSTALL_RC" ] || exit "$INSTALL_RC"
[ -n "$INSTALL_NO_RECEIPT" ] && exit 0
mkdir -p "$RECEIPTS" "$LOCATION/Variations/il2cpp"
echo "Variations/il2cpp/UnityPlayer.dylib" > "$RECEIPTS/$RECEIPT"
touch "$LOCATION/Variations/il2cpp/UnityPlayer.dylib"
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class UnityModules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        repo = base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config = fixture_config.copy_config(base)
        self.profile = self.config / "machine-setup/profiles/personal.unity"
        self.profile.write_text("# modules no cask installs\nMacStandaloneSupportIL2CPP Mac-IL2CPP\n")
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.unity = base / "Applications/Unity"
        (self.unity / f"Unity-{VERSION}/Unity.app").mkdir(parents=True)
        self.location = self.unity / f"Unity-{VERSION}/PlaybackEngines/MacStandaloneSupport"
        self.receipts = base / "receipts"
        self.log = base / "calls.log"
        self.stubs = base / "stubs"
        write_exec(self.stubs / "brew", BREW_STUB)
        write_exec(self.stubs / "pkgutil", PKGUTIL_STUB)
        write_exec(self.stubs / "curl", CURL_STUB)
        write_exec(self.stubs / "sudo", SUDO_STUB)
        for name in ("defaults", "launchctl", "mas"):
            write_exec(self.stubs / name, "#!/bin/sh\nexit 1\n")
        fixture_config.seal(self, self.stubs)

    def run_brew(self, *args, **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config),
            "MACHINE_SETUP_UNITY_ROOT": str(self.unity),
            "MACHINE_SETUP_UNITY_BASE": BASE,
            "CASKS": "unity unity-hub",
            "UNITY_VERSIONS": f"unity {VERSION},{CHANGESET}",
            "RECEIPTS": str(self.receipts),
            "RECEIPT": RECEIPT,
            "LOCATION": str(self.location),
            "SIG": SIGNED,
            "LOG": str(self.log),
            **extra,
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), "update", "brew", *args],
                              env=env, capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def installs(self):
        return [c for c in self.calls() if "installer -pkg" in c]

    def test_a_dry_run_names_the_missing_module_and_installs_nothing(self):
        result = self.run_brew()

        self.assertIn(f"  MISSING unity MacStandaloneSupportIL2CPP {VERSION}", result.stdout)
        self.assertIn(f"  [dry-run] unity_module_install {RECEIPT} {URL}", result.stdout)
        self.assertEqual([c for c in self.calls() if c.startswith(("curl", "sudo"))], [])

    def test_apply_downloads_the_cask_versions_module_and_installs_it(self):
        result = self.run_brew("--apply")

        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assertIn(f"curl {URL}", self.calls())
        self.assertEqual(len(self.installs()), 1, out)
        self.assertIn(" -target / ", self.installs()[0] + " ")
        self.assertIn(f"  installed unity {RECEIPT}", result.stdout)
        self.assertTrue((self.location / "Variations/il2cpp/UnityPlayer.dylib").exists())

    def test_a_second_run_finds_the_module_and_installs_nothing(self):
        self.run_brew("--apply")
        self.log.unlink()

        result = self.run_brew("--apply")

        self.assertIn(f"  ok      unity MacStandaloneSupportIL2CPP {VERSION}", result.stdout)
        self.assertEqual(self.installs(), [])
        self.assertNotIn("curl", "\n".join(self.calls()))

    def test_a_receipt_whose_files_are_gone_is_installed_again(self):
        """A same-version reinstall of the editor deletes the files, not the receipt."""
        self.run_brew("--apply")
        shutil.rmtree(self.location / "Variations")
        self.log.unlink()

        result = self.run_brew("--apply")

        self.assertIn("  MISSING unity MacStandaloneSupportIL2CPP", result.stdout)
        self.assertEqual(len(self.installs()), 1)

    def test_a_new_cask_version_needs_its_own_module(self):
        self.run_brew("--apply")
        new = "6000.7.1f1"
        (self.unity / f"Unity-{new}/Unity.app").mkdir(parents=True)

        result = self.run_brew(UNITY_VERSIONS=f"unity {new},0123456789ab")

        self.assertIn(f"  MISSING unity MacStandaloneSupportIL2CPP {new}", result.stdout)
        self.assertIn(f"{BASE}/0123456789ab/MacEditorTargetInstaller/"
                      f"UnitySetup-Mac-IL2CPP-Support-for-Editor-{new}.pkg", result.stdout)

    def test_a_pkg_without_unitys_signature_is_not_installed(self):
        for sig, rc in ((SIGNED.replace("9QW8UQUTAA", "ABCDE12345"), "0"),
                        ("Status: no signature\n", "1"),
                        ("   1. Developer ID Installer: Unity Technologies ApS (9QW8UQUTAA) extra\n", "0"),
                        ("   2. Developer ID Installer: Other (9QW8UQUTAA)\n", "0")):
            with self.subTest(sig=sig):
                result = self.run_brew("--apply", SIG=sig, SIG_RC=rc)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not signed by Unity's Developer ID Installer", result.stdout)
                self.assertIn("FAILED  unity_module_install", result.stdout)
                self.assertEqual(self.installs(), [])

    def test_a_failed_download_or_install_is_a_failure(self):
        for extra in ({"CURL_RC": "22"}, {"INSTALL_RC": "1"}, {"INSTALL_NO_RECEIPT": "1"}):
            with self.subTest(extra=extra):
                result = self.run_brew("--apply", **extra)

                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("FAILED  unity_module_install", result.stdout)
                self.assertNotIn("installed unity", result.stdout)

    def test_no_sudo_defers_and_installs_nothing(self):
        result = self.run_brew("--apply", SUDO_N="1", SUDO_V="1")

        self.assertIn(f"  DEFER   needs sudo: install {RECEIPT}", result.stdout)
        self.assertIn(f"    sh {self.folder}/machine-setup.sh update brew --apply\n", result.stdout)
        self.assertEqual(self.installs(), [])
        self.assertNotIn("curl", "\n".join(self.calls()))

    def test_an_unattended_run_installs_through_askpass(self):
        """The nightly run has no terminal; the Keychain item answers sudo."""
        write_exec(self.stubs / "security-item", "#!/bin/sh\nexit 0\n")

        result = self.run_brew("--apply", SUDO_N="1", SUDO_V="1",
                               MACHINE_SETUP_SECURITY=str(self.stubs / "security-item"))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.installs()), 1, result.stdout)
        self.assertIn("sudo -A installer -pkg", self.installs()[0])
        self.assertTrue(self.installs()[0].endswith("askpass=set"))

    def test_unreadable_versions_install_nothing(self):
        for versions in ("unity 6000.6.10f1", "unity 6000.6.10f1,40e81493c67d extra",
                         "unity 6000.6.10f1,40E81493C67D", "unity 6000.6.10f1;rm,40e81493c67d",
                         f"unity {VERSION},{CHANGESET}\nunity 6000.5.1f1,0123456789ab", ""):
            with self.subTest(versions=versions):
                result = self.run_brew("--apply", UNITY_VERSIONS=versions)

                self.assertIn("cannot read the unity cask's version", result.stdout)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.installs(), [])

    def test_a_brew_failure_installs_nothing(self):
        result = self.run_brew("--apply", VERSIONS_RC="1")

        self.assertIn("could not read the unity cask's version", result.stdout)
        self.assertEqual(self.installs(), [])

    def test_no_unity_cask_or_editor_installs_nothing(self):
        result = self.run_brew("--apply", CASKS="unity-hub")
        self.assertIn("the unity cask is not installed", result.stdout)

        shutil.rmtree(self.unity)
        result = self.run_brew("--apply")
        self.assertIn(f"no Unity {VERSION} editor", result.stdout)
        self.assertEqual(self.installs(), [])

    def test_a_line_of_another_shape_fails_and_the_rest_install(self):
        self.profile.write_text("Mac IL2CPP extra\nbad/id Mac-IL2CPP\nOk Mac--IL2CPP\n"
                                "MacStandaloneSupportIL2CPP Mac-IL2CPP\n")

        result = self.run_brew("--apply")

        self.assertEqual(result.stdout.count("FAILED  unity: cannot read the line"), 3, result.stdout)
        self.assertEqual(len(self.installs()), 1)

    def test_no_unity_file_reads_no_cask_version(self):
        self.profile.unlink()

        result = self.run_brew("--apply")

        self.assertNotIn("unity", result.stdout.replace("unity-hub", ""))
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
