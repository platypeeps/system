"""`capture --apply` keeps a copy of a file git does not hold (sd:3546).

capture rewrites manifests, dotfiles, agent plists and macOS values in the
config folder. In a git checkout the old version is one `git diff` away; in a
plain folder an overwrite had no way back. Each file capture changes there now
gets a dated `.bak-<stamp>` copy, an unchanged one is not rewritten, and a file
git tracks with no local edit gets none.

capture runs from a copy of this folder under a temporary root. brew, git
(in the plain-folder cases), defaults, sudo and launchctl are stubs, and
fixture_config.seal fails a case that reaches a real one.
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

# `brew leaves` lists one formula; every other call lists nothing.
BREW_STUB = """#!/bin/sh
[ "$1" = leaves ] && echo new-formula
exit 0
"""

OLD_BREW = "# personal machine brew. Captured by machine-setup.sh capture.\n\nold-formula\n"


def write_stub(directory, name, body):
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class CaptureBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.folder = base / "repo/local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(FOLDER.parent / "lib", self.folder.parent / "lib",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config_root = fixture_config.copy_capture_config(base)
        self.profiles = self.config_root / "machine-setup/profiles"
        self.brew = self.profiles / "personal.brew"
        self.brew.write_text(OLD_BREW)
        self.dotfile = self.config_root / "machine-setup/dotfiles/personal/.gitconfig"
        self.dotfile.parent.mkdir(parents=True)
        self.dotfile.write_text("[user]\n\tname = old\n")
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        (self.home / ".gitconfig").write_text("[user]\n\tname = new\n")
        self.stubs = base / "stubs"
        self.stubs.mkdir()
        write_stub(self.stubs, "brew", BREW_STUB)
        write_stub(self.stubs, "git", "#!/bin/sh\nexit 1\n")
        write_stub(self.stubs, "defaults", "#!/bin/sh\nexit 1\n")
        write_stub(self.stubs, "sudo", "#!/bin/sh\nexit 1\n")
        write_stub(self.stubs, "launchctl", "#!/bin/sh\nexit 0\n")
        fixture_config.seal(self, self.stubs)

    def capture(self):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
        }
        result = subprocess.run([str(self.folder / "machine-setup.sh"), "capture", "--apply"], env=env,
                                capture_output=True, text=True, cwd=self.tmp.name,
                                stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    @staticmethod
    def copies(path):
        return sorted(path.parent.glob(path.name + ".bak-*"))

    def test_a_plain_folder_keeps_a_copy_of_each_file_capture_changes(self):
        self.capture()
        self.assertIn("new-formula", self.brew.read_text())
        self.assertEqual([p.read_text() for p in self.copies(self.brew)], [OLD_BREW])
        self.assertEqual(self.dotfile.read_text(), "[user]\n\tname = new\n")
        self.assertEqual([p.read_text() for p in self.copies(self.dotfile)], ["[user]\n\tname = old\n"])

    def test_an_unchanged_file_is_not_rewritten_and_keeps_no_copy(self):
        self.capture()
        for copy in self.copies(self.brew) + self.copies(self.dotfile):
            copy.unlink()
        self.capture()
        self.assertEqual(self.copies(self.brew), [])
        self.assertEqual(self.copies(self.dotfile), [])

    def commit_config(self):
        (self.stubs / "git").unlink()
        git = ["git", "-C", str(self.config_root)]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "-c", "user.name=fixture", "-c", "user.email=fixture@example.test",
                        "add", "-A"], check=True)
        subprocess.run([*git, "-c", "user.name=fixture", "-c", "user.email=fixture@example.test",
                        "commit", "-qm", "seed"], check=True)

    def test_a_file_git_tracks_unedited_keeps_no_copy(self):
        self.commit_config()
        self.capture()
        self.assertIn("new-formula", self.brew.read_text())
        self.assertEqual(self.copies(self.brew), [])
        self.assertEqual(self.copies(self.dotfile), [])

    def test_a_tracked_symlink_keeps_a_copy_of_what_it_points_at(self):
        # git holds the link's target path, not the file capture writes
        # through it into.
        target = pathlib.Path(self.tmp.name) / "outside/.gitconfig"
        target.parent.mkdir()
        target.write_text("[user]\n\tname = old\n")
        self.dotfile.unlink()
        self.dotfile.symlink_to(target)
        self.commit_config()
        self.capture()
        self.assertEqual(target.read_text(), "[user]\n\tname = new\n")
        self.assertEqual([p.read_text() for p in self.copies(self.dotfile)], ["[user]\n\tname = old\n"])


if __name__ == "__main__":
    unittest.main()
