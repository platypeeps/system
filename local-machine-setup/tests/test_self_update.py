"""The macos stage turns self-update off in casks that update themselves (sd:3062).

The weekly upgrade-report moves every cask, so both machines change together
and the operator re-grants macOS privacy permissions in one sitting. An app
that updates itself in between can drop its grant on any day. The stage
converges each app's own documented switch, and `status` counts what differs
as drift.

The script runs from a copy of this folder under a temporary root. `defaults`
is a stub that answers from, and writes to, a file; the applications folder
and HOME are temporary, so nothing on this machine is read or written.
"""

import json
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

# `defaults read DOMAIN KEY` answers from $DEFAULTS_DB, one "DOMAIN KEY VALUE"
# line per key; `defaults write DOMAIN KEY -TYPE VALUE` appends one.
DEFAULTS_STUB = r"""#!/bin/sh
case "$1" in
  read)
    v=$(grep "^$2 $3 " "$DEFAULTS_DB" 2>/dev/null | tail -1 | cut -d' ' -f3-)
    [ -n "$v" ] || { echo "The domain/default pair of ($2, $3) does not exist" >&2; exit 1; }
    echo "$v" ;;
  write)
    case "$5" in true) v=1 ;; false) v=0 ;; *) v=$5 ;; esac
    echo "$2 $3 $v" >> "$DEFAULTS_DB" ;;
  *) exit 1 ;;
esac
"""

EXIT_1_STUB = "#!/bin/sh\nexit 1\n"

# A sandboxed app's container refuses a write from a terminal without Full
# Disk Access: `defaults write` to $REFUSED_DOMAIN exits 1 and writes nothing.
REFUSING_DEFAULTS_STUB = DEFAULTS_STUB.replace(
    "  write)\n",
    "  write)\n    [ \"$2\" != \"$REFUSED_DOMAIN\" ] || { echo \"Could not write domain $2\" >&2; exit 1; }\n")

ZED_JSONC = """// Zed settings
{
  "theme": "One Dark", // a comment the merge must keep
}
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class SelfUpdateOffTest(unittest.TestCase):
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
        self.apps = base / "Applications"
        for app in ("iTerm.app", "Zed.app", "Visual Studio Code.app", "Docker.app", "Claude.app"):
            (self.apps / app).mkdir(parents=True)
        self.defaults_db = base / "defaults.db"
        self.zed = self.home / ".config/zed/settings.json"
        self.zed.parent.mkdir(parents=True)
        self.zed.write_text(ZED_JSONC)
        self.vscode = self.home / "Library/Application Support/Code/User/settings.json"
        self.docker = self.home / "Library/Group Containers/group.com.docker/settings-store.json"
        self.docker.parent.mkdir(parents=True)
        self.docker.write_text(json.dumps({"AutoDownloadUpdates": True, "Other": 1}))
        self.stubs = base / "stubs"
        write_exec(self.stubs / "defaults", DEFAULTS_STUB)
        for name in ("sudo", "launchctl", "mas"):
            write_exec(self.stubs / name, EXIT_1_STUB)
        fixture_config.seal(self, self.stubs)
        self.refused_domain = ""

    def run_verb(self, *args):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
            "MACHINE_SETUP_APPLICATIONS_DIR": str(self.apps),
            "DEFAULTS_DB": str(self.defaults_db),
            "REFUSED_DOMAIN": self.refused_domain,
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args], env=env,
                              capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    def test_an_apply_turns_self_update_off_and_a_second_run_is_clean(self):
        result = self.run_verb("update", "macos", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        writes = self.defaults_db.read_text().splitlines()
        self.assertIn("com.googlecode.iterm2 SUEnableAutomaticChecks 0", writes)
        self.assertIn("com.googlecode.iterm2 SUAutomaticallyUpdate 0", writes)
        self.assertIn("com.anthropic.claudefordesktop disableAutoUpdates 1", writes)
        # Only installed apps are touched: BetterTouchTool is not in the folder.
        self.assertFalse([w for w in writes if "BetterTouchTool" in w], writes)
        zed = self.zed.read_text()
        self.assertIn('"auto_update": false', zed)
        self.assertIn("// a comment the merge must keep", zed)
        self.assertEqual(json.loads(self.vscode.read_text()), {"update.mode": "none"})
        self.assertEqual(json.loads(self.docker.read_text()),
                         {"AutoDownloadUpdates": False, "Other": 1})

        again = self.run_verb("update", "macos", "--apply")

        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.defaults_db.read_text().splitlines(), writes)
        for marker in ("MISSING", "DIFFERS", "defaults write"):
            self.assertNotIn(marker, again.stdout)

    def test_status_counts_each_self_update_setting_that_differs(self):
        clean = self.run_verb("status")
        self.run_verb("update", "macos", "--apply")
        converged = self.run_verb("status")

        drift_before = int(clean.stdout.split("drift   : ")[1].split()[0])
        drift_after = int(converged.stdout.split("drift   : ")[1].split()[0])
        # Two iTerm2 keys, Claude desktop, Zed, VS Code and Docker.
        self.assertEqual(drift_before - drift_after, 6, clean.stdout)
        self.assertIn("[dry-run] defaults write com.googlecode.iterm2 SUEnableAutomaticChecks -bool false",
                      clean.stdout)
        self.assertIn("MISSING", clean.stdout)

    def test_a_refused_write_is_reported_and_the_stage_goes_on(self):
        # sd:3103: Maccy's container refused the write, set -e ended the run,
        # and no later app or manifest key was converged.
        write_exec(self.stubs / "defaults", REFUSING_DEFAULTS_STUB)
        self.refused_domain = "org.p0deje.Maccy"
        (self.apps / "Maccy.app").mkdir()
        macos = self.config_root / "machine-setup/profiles/personal.macos"
        macos.write_text("org.p0deje.Maccy pasteByDefault bool true\n"
                         "com.apple.dock autohide bool true\n")

        result = self.run_verb("update", "macos", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for key in ("SUEnableAutomaticChecks", "SUAutomaticallyUpdate", "pasteByDefault"):
            self.assertIn(f"DIFFERS org.p0deje.Maccy {key} not written; set it by hand", result.stdout)
        self.assertIn("Full Disk Access", result.stdout)
        writes = self.defaults_db.read_text().splitlines()
        self.assertIn("com.googlecode.iterm2 SUAutomaticallyUpdate 0", writes)
        self.assertIn("com.apple.dock autohide 1", writes)
        self.assertFalse([w for w in writes if "Maccy" in w], writes)


if __name__ == "__main__":
    unittest.main()
