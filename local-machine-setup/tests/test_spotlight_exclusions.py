"""The system stage reads, applies and captures Spotlight's privacy list (sd:1654).

The list is the `Exclusions` array in the data volume's
VolumeConfiguration.plist. It is root-owned, and root also needs Full Disk
Access to open it, so every case points MACHINE_SETUP_SPOTLIGHT_PLIST at a
fixture plist and puts a `sudo` stub on PATH. The stub runs plutil and
PlistBuddy against the fixture, and only logs anything else: the
system stage's other sudo actions (useLS, the sudoers drop-in, the firewall)
must not run from a test. `launchctl` is a stub too, so no mds restarts.
On a Mac plutil and PlistBuddy are the real tools; where they are missing, as
on the Linux CI runner, `plist_tools` puts stand-ins on PATH.

The script runs from a copy of this folder under a temporary root, so the
fixture <profile>.spotlight and a capture write never touch the checkout.
"""

import pathlib
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config, plist_tools

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent

# The words status_stage greps to count drift.
DRIFT_WORDS = re.compile(r"DIFFERS|MISSING|STALE|ABSENT|UNLOADED|EXTRA|defaults write")

MANIFEST = (
    "# Fixture Spotlight exclusions.\n"
    "~/repos/\n"
    "/private/tmp/claude-501\n"
)
TMP_PATH = "/private/tmp/claude-501"
EXTRA_PATH = "/Volumes/Scratch"

# SUDO_STUB: ok (default) runs the read and edit tools; refuse fails every
# call, the way a declined or missing password does; nofda makes plutil fail
# the way root without Full Disk Access does; ticketless has no cached ticket,
# so `sudo -n` fails and only a `sudo -v` password prompt would succeed;
# silent makes plutil fail without a word, as the CI runner's did.
SUDO_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$SUDO_LOG"
mode="${SUDO_STUB:-ok}"
if [ "$mode" = ticketless ] && [ "$1" = "-n" ]; then echo "sudo: a password is required" >&2; exit 1; fi
if [ -n "$SUDO_RESET" ] && [ -e "$SUDO_RESET" ] && [ "$1" = "-n" ]; then echo "sudo: a password is required" >&2; exit 1; fi
while [ "$1" = "-n" ] || [ "$1" = "sudo" ]; do shift; done
case "$1" in
  -v|true)
    if [ "$mode" = refuse ]; then echo "sudo: a password is required" >&2; exit 1; fi
    exit 0 ;;
esac
if [ "$mode" = refuse ]; then echo "sudo: a password is required" >&2; exit 1; fi
case "$1" in
  plutil)
    if [ "$mode" = silent ]; then exit 1; fi
    if [ "$mode" = nofda ]; then
      echo "$6: (The file couldn't be opened: Operation not permitted)" >&2
      exit 1
    fi
    shift
    exec plutil "$@" ;;
  */PlistBuddy|launchctl|test) exec "$@" ;;
esac
exit 0
"""

# LAUNCHCTL_REVERT names a file copied over the fixture plist on a restart:
# what it would look like if mds wrote its old list back as it stopped.
LAUNCHCTL_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
if [ -n "$LAUNCHCTL_REVERT" ]; then cp "$LAUNCHCTL_REVERT" "$MACHINE_SETUP_SPOTLIGHT_PLIST"; fi
exit 0
"""

DEFAULTS_STUB = "#!/bin/sh\nexit 1\n"

# Real brew runs `sudo --reset-timestamp` first unless HOMEBREW_NO_SUDO is
# set. SUDO_RESET names the file that stands for the erased ticket.
BREW_STUB = r"""#!/bin/sh
if [ -z "$HOMEBREW_NO_SUDO" ] && [ -n "$SUDO_RESET" ]; then : > "$SUDO_RESET"; fi
exit 0
"""

KICKSTART = "kickstart -k system/com.apple.metadata.mds"


def write_stub(directory, name, body):
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class SpotlightExclusionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.folder = base / "repo/local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config_root = fixture_config.copy_config(base)
        self.profiles = self.config_root / "machine-setup/profiles"
        self.manifest = self.profiles / "personal.spotlight"
        self.manifest.write_text(MANIFEST)
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.repos = f"{self.home}/repos"
        self.plist = base / "VolumeConfiguration.plist"
        self.sudo_log = base / "sudo.log"
        self.launchctl_log = base / "launchctl.log"
        self.sudo_log.touch()
        self.launchctl_log.touch()
        self.stubs = base / "stubs"
        self.stubs.mkdir()
        write_stub(self.stubs, "sudo", SUDO_STUB)
        write_stub(self.stubs, "launchctl", LAUNCHCTL_STUB)
        write_stub(self.stubs, "brew", BREW_STUB)
        write_stub(self.stubs, "git", "#!/bin/sh\nexit 1\n")
        write_stub(self.stubs, "defaults", DEFAULTS_STUB)
        self.plist_env = plist_tools.install(self.stubs)
        self.plistbuddy = self.plist_env.get("MACHINE_SETUP_PLISTBUDDY", plist_tools.PLISTBUDDY)

    def write_plist(self, exclusions, path=None):
        """Binary, like the real store, so the read has to convert it."""
        data = {"ConfigurationModifiedVersion": "fixture", "Stores": {}}
        if exclusions is not None:
            data["Exclusions"] = list(exclusions)
        with open(path or self.plist, "wb") as handle:
            plistlib.dump(data, handle, fmt=plistlib.FMT_BINARY)

    def exclusions(self):
        with open(self.plist, "rb") as handle:
            return plistlib.load(handle).get("Exclusions")

    def run_script(self, *args, sudo="ok", **extra):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "MACHINE_SETUP_SPOTLIGHT_PLIST": str(self.plist),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
            "SUDO_STUB": sudo,
            "SUDO_LOG": str(self.sudo_log),
            "LAUNCHCTL_LOG": str(self.launchctl_log),
            **self.plist_env,
            **extra,
        }
        result = subprocess.run([str(self.folder / "machine-setup.sh"), *args], env=env,
                                capture_output=True, text=True, cwd=self.tmp.name,
                                stdin=subprocess.DEVNULL, timeout=120)
        # Never fail the run, whatever the store says.
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def system(self, *flags, **kwargs):
        return self.run_script("update", "system", *flags, **kwargs)

    @staticmethod
    def spotlight_lines(out):
        return [line for line in out.splitlines() if "Spotlight" in line]

    def restarts(self):
        return [line for line in self.launchctl_log.read_text().splitlines() if line == KICKSTART]

    # ------------------------------------------------------------- read ----

    def test_present_paths_are_ok_and_count_no_drift(self):
        self.write_plist([self.repos, TMP_PATH])
        lines = self.spotlight_lines(self.system())
        # Manifest order, which is sorted: `/` before `~`.
        self.assertEqual(lines, [f"  ok      Spotlight excludes {TMP_PATH}",
                                 f"  ok      Spotlight excludes {self.repos}"])
        self.assertFalse([line for line in lines if DRIFT_WORDS.search(line)])

    def test_a_missing_path_is_drift_and_a_dry_run_changes_nothing(self):
        self.write_plist([self.repos])
        out = self.system()
        self.assertIn(f"  MISSING Spotlight exclusion {TMP_PATH}\n", out)
        self.assertTrue(DRIFT_WORDS.search(f"  MISSING Spotlight exclusion {TMP_PATH}"))
        self.assertIn(f"  [dry-run] sudo {self.plistbuddy} -c 'Add :Exclusions: string {TMP_PATH}' "
                      f"{self.plist}\n", out)
        self.assertIn(f"  [dry-run] sudo launchctl {KICKSTART}\n", out)
        self.assertEqual(self.exclusions(), [self.repos])
        self.assertEqual(self.restarts(), [])

    def test_an_extra_path_is_reported_and_never_removed(self):
        self.write_plist([self.repos, EXTRA_PATH, TMP_PATH])
        out = self.system("--apply")
        self.assertIn(f"  EXTRA   Spotlight exclusion {EXTRA_PATH} (not in the profile; "
                      "left in place, capture records it)\n", out)
        self.assertEqual(self.exclusions(), [self.repos, EXTRA_PATH, TMP_PATH])
        self.assertEqual(self.restarts(), [])

    def test_status_keeps_the_ticket_brew_would_reset(self):
        # status runs `brew list` before the Spotlight read. Real brew erases
        # the sudo ticket unless HOMEBREW_NO_SUDO is set, and the read then
        # deferred though the operator had just run `sudo -v`.
        self.write_plist([self.repos, TMP_PATH])
        reset = pathlib.Path(self.tmp.name) / "sudo-reset"
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "MACHINE_SETUP_SPOTLIGHT_PLIST": str(self.plist),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
            "SUDO_LOG": str(self.sudo_log),
            "LAUNCHCTL_LOG": str(self.launchctl_log),
            "SUDO_RESET": str(reset),
            **self.plist_env,
        }
        result = subprocess.run([str(self.folder / "machine-setup.sh"), "status"], env=env,
                                capture_output=True, text=True, cwd=self.tmp.name,
                                stdin=subprocess.DEVNULL, timeout=120)
        lines = self.spotlight_lines(result.stdout)
        self.assertEqual(lines, [f"  ok      Spotlight excludes {TMP_PATH}",
                                 f"  ok      Spotlight excludes {self.repos}"], result.stdout + result.stderr)
        self.assertFalse(reset.exists())

    # ------------------------------------------------------------ apply ----

    def test_apply_adds_each_missing_path_then_restarts_mds_once(self):
        self.write_plist([EXTRA_PATH])
        out = self.system("--apply")
        self.assertIn(f"  MISSING Spotlight exclusion {self.repos}\n", out)
        self.assertIn(f"  MISSING Spotlight exclusion {TMP_PATH}\n", out)
        self.assertEqual(self.exclusions(), [EXTRA_PATH, TMP_PATH, self.repos])
        self.assertEqual(self.restarts(), [KICKSTART])
        self.assertNotIn("still absent", out)
        # The restart comes after the last add.
        log = self.sudo_log.read_text().splitlines()
        adds = [i for i, line in enumerate(log) if "Add :Exclusions:" in line]
        restart = [i for i, line in enumerate(log) if KICKSTART in line]
        self.assertEqual(len(adds), 2)
        self.assertEqual(len(restart), 1)
        self.assertGreater(restart[0], max(adds))

    def test_apply_creates_the_array_when_the_store_has_none(self):
        self.write_plist(None)
        out = self.system("--apply")
        self.assertIn(f"  MISSING Spotlight exclusion {self.repos}\n", out)
        self.assertEqual(self.exclusions(), [TMP_PATH, self.repos])
        self.assertEqual(self.restarts(), [KICKSTART])

    def test_a_path_written_back_over_the_add_is_still_reported(self):
        self.write_plist([self.repos])
        before = pathlib.Path(self.tmp.name) / "before.plist"
        shutil.copy(self.plist, before)
        out = self.system("--apply", LAUNCHCTL_REVERT=str(before))
        self.assertIn(f"  MISSING Spotlight exclusion {TMP_PATH} "
                      "(still absent after the add and the mds restart)\n", out)

    def test_a_path_with_a_quote_is_skipped_not_mangled(self):
        quoted = "/private/tmp/it's here"
        self.manifest.write_text(f"{quoted}\n")
        self.write_plist([])
        out = self.system("--apply")
        self.assertIn(f"  MISSING Spotlight exclusion {quoted}\n", out)
        self.assertIn(f"  SKIP    cannot add {quoted}: PlistBuddy mangles quotes and backslashes; "
                      "add it in System Settings > Spotlight\n", out)
        self.assertEqual(self.exclusions(), [])
        self.assertEqual(self.restarts(), [])

    # ------------------------------------------------------- unreadable ----

    def test_no_sudo_defers_with_the_cause_and_the_remedy(self):
        self.write_plist([])
        out = self.system(sudo="refuse")
        self.assertIn(f"  DEFER   Spotlight exclusions not read: reading {self.plist} needs sudo, "
                      "and no sudo ticket is cached; run 'sudo -v', then re-run "
                      "(or re-run with --apply)\n", out)
        self.assertNotIn("MISSING Spotlight", out)

    def test_a_dry_run_never_prompts_for_a_password(self):
        self.write_plist([])
        out = self.system(sudo="ticketless")
        self.assertIn("  DEFER   Spotlight exclusions not read:", out)
        self.assertNotIn("-v", self.sudo_log.read_text().splitlines())

    def test_a_refused_sudo_under_apply_defers_and_restarts_nothing(self):
        self.write_plist([])
        out = self.system("--apply", sudo="refuse")
        self.assertIn("  DEFER   Spotlight exclusions not read:", out)
        self.assertIn("    run 'sudo -v', then re-run (or re-run with --apply)  (Spotlight exclusions)\n",
                      out)
        self.assertEqual(self.exclusions(), [])
        self.assertEqual(self.restarts(), [])

    def test_no_full_disk_access_defers_and_names_it(self):
        self.write_plist([])
        out = self.system(sudo="nofda")
        self.assertIn(f"  DEFER   Spotlight exclusions not read: sudo cannot open {self.plist} "
                      "without Full Disk Access; grant this terminal Full Disk Access "
                      "(System Settings > Privacy & Security > Full Disk Access), then re-run\n", out)

    def test_a_missing_store_defers_and_does_not_read_as_empty(self):
        out = self.system("--apply")
        self.assertIn(f"  DEFER   Spotlight exclusions not read: {self.plist} does not exist; ", out)
        self.assertNotIn("MISSING Spotlight", out)
        self.assertFalse(self.plist.exists())

    def test_a_missing_store_is_named_when_plutil_says_nothing(self):
        out = self.system(sudo="silent")
        self.assertIn(f"  DEFER   Spotlight exclusions not read: {self.plist} does not exist; ", out)

    # A profile with no list has made no Spotlight decision: it reads nothing
    # and reports no EXTRA, so a machine nobody curated is not drift.
    def test_a_profile_without_a_list_never_reads_the_store(self):
        self.manifest.unlink()
        self.write_plist([EXTRA_PATH])
        out = self.system()
        self.assertIn("  --      no Spotlight exclusions in this profile\n", out)
        self.assertNotIn("plutil", self.sudo_log.read_text())

    # ---------------------------------------------------------- capture ----

    def capture(self, *flags, **kwargs):
        return self.run_script("capture", *flags, **kwargs)

    def test_capture_writes_the_live_list_with_home_as_tilde(self):
        self.write_plist([TMP_PATH, f"{self.repos}", EXTRA_PATH])
        out = self.capture("--apply")
        self.assertIn("  spotlight:\n    3 exclusion(s)\n", out)
        self.assertEqual(self.manifest.read_text(),
                         "# personal machine Spotlight privacy exclusions. Captured by machine-setup.sh capture.\n"
                         "# One absolute path per line; a leading ~ is this account's home. The system\n"
                         "# stage adds a missing path and restarts mds. It never removes one.\n"
                         "# Fixture Spotlight exclusions.\n"
                         "\n"
                         # Byte order, on every platform: the script sorts
                         # with LC_COLLATE=C, so `/V` comes before `/p`.
                         f"{EXTRA_PATH}\n"
                         f"{TMP_PATH}\n"
                         "~/repos\n")

    def test_capture_round_trips_the_committed_file(self):
        shutil.copy(fixture_config.PROFILES / "personal.spotlight", self.manifest)
        committed = self.manifest.read_text()
        # The plist holds exactly the committed paths, so the test follows
        # whatever the profile lists.
        paths = [line for line in committed.splitlines() if line and not line.startswith("#")]
        self.write_plist([f"{self.home}{p[1:]}" if p.startswith("~") else p for p in reversed(paths)])
        self.capture("--apply")
        self.assertEqual(self.manifest.read_text(), committed)

    def test_capture_leaves_out_what_common_holds_in_any_form(self):
        (self.profiles / "common.spotlight").write_text("~/repos/\n")
        self.write_plist([self.repos, TMP_PATH])
        out = self.capture("--apply")
        self.assertIn("  spotlight:\n    1 exclusion(s)\n", out)
        self.assertNotIn("~/repos", self.manifest.read_text())
        self.assertTrue(self.manifest.read_text().endswith(f"\n{TMP_PATH}\n"))

    def test_a_capture_dry_run_writes_nothing(self):
        self.write_plist([EXTRA_PATH])
        out = self.capture()
        self.assertIn("  spotlight:\n    1 exclusion(s)\n", out)
        self.assertEqual(self.manifest.read_text(), MANIFEST)

    def test_capture_without_access_says_why_and_keeps_the_file(self):
        self.write_plist([EXTRA_PATH])
        out = self.capture("--apply", sudo="nofda")
        self.assertIn("  spotlight:\n    --      not read: sudo cannot open", out)
        self.assertEqual(self.manifest.read_text(), MANIFEST)


if __name__ == "__main__":
    unittest.main()
