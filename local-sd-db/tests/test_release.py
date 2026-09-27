"""`sd-db.sh release`: the tag the pack pins `sd_db` to (sd:1854).

Each case copies the entrypoint and the three version files into a throwaway
git repository, so no case reads or tags this checkout.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
FILES = ("sd-db.sh", "pyproject.toml", "_build.py", "sd_db/__init__.py")


class ReleaseCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "repo"
        self.folder = self.repo / "local-sd-db"
        for name in FILES:
            target = self.folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PACKAGE_ROOT / name, target)
        self.version = self.read_version()
        self.git("init", "-q")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "initial")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=True,
            env=self.environment(),
        ).stdout.strip()

    def environment(self):
        # No global or system git config: a signing or tag setting on the
        # machine must not decide what these cases see.
        return {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

    def read_version(self):
        for line in (self.folder / "pyproject.toml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version = "):
                return line.split('"')[1]
        raise AssertionError("no version in pyproject.toml")

    def release(self, *args, expect=0):
        done = subprocess.run(
            [str(self.folder / "sd-db.sh"), "release", *args],
            capture_output=True, text=True, input="", env=self.environment())
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done

    def tags(self):
        return self.git("tag", "--list").split()


class TheRelease(ReleaseCase):
    def test_a_dry_run_names_the_tag_and_creates_nothing(self):
        done = self.release("--dry-run")
        self.assertIn(f"would tag {self.git('rev-parse', 'HEAD')} as sd-db-v{self.version}", done.stdout)
        self.assertEqual(self.tags(), [])

    def test_it_cuts_an_annotated_tag_on_head(self):
        self.release()
        tag = f"sd-db-v{self.version}"
        self.assertEqual(self.tags(), [tag])
        self.assertEqual(self.git("cat-file", "-t", tag), "tag", "a lightweight tag carries no message")
        self.assertEqual(self.git("rev-parse", f"{tag}^{{commit}}"), self.git("rev-parse", "HEAD"))

    def test_it_never_pushes_and_prints_the_push_command(self):
        # A remote that exists: a push to it would succeed, so no tag there
        # afterwards means none was attempted.
        remote = self.repo.parent / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=self.environment())
        self.git("remote", "add", "origin", str(remote))
        done = self.release()
        self.assertIn(f"push origin sd-db-v{self.version}", done.stdout)
        listed = subprocess.run(["git", "-C", str(remote), "tag", "--list"], capture_output=True,
                                text=True, check=True, env=self.environment())
        self.assertEqual(listed.stdout, "")

    def test_an_existing_tag_is_refused(self):
        self.release()
        done = self.release(expect=1)
        self.assertIn(f"tag sd-db-v{self.version} exists", done.stderr)
        done = self.release("--dry-run", expect=1)
        self.assertIn("exists", done.stderr)

    def test_a_modified_file_is_refused(self):
        with (self.folder / "pyproject.toml").open("a", encoding="utf-8") as handle:
            handle.write("# edit\n")
        done = self.release("--dry-run", expect=1)
        self.assertIn("working tree is dirty", done.stderr)
        self.assertEqual(self.tags(), [])

    def test_an_untracked_file_is_refused(self):
        (self.folder / "sd_db" / "new.py").write_text("", encoding="utf-8")
        done = self.release(expect=1)
        self.assertIn("working tree is dirty", done.stderr)
        self.assertEqual(self.tags(), [])

    def test_versions_that_disagree_are_refused(self):
        init = self.folder / "sd_db" / "__init__.py"
        init.write_text(init.read_text(encoding="utf-8").replace(
            f'__version__ = "{self.version}"', '__version__ = "9.9.9"'), encoding="utf-8")
        self.git("commit", "-q", "-am", "drift")
        done = self.release("--dry-run", expect=1)
        self.assertIn("make them agree", done.stderr)

    def test_a_backend_version_that_disagrees_is_refused(self):
        backend = self.folder / "_build.py"
        backend.write_text(backend.read_text(encoding="utf-8").replace(
            f'VERSION = "{self.version}"', 'VERSION = "9.9.9"'), encoding="utf-8")
        self.git("commit", "-q", "-am", "drift")
        done = self.release("--dry-run", expect=1)
        self.assertIn("_build.py says 9.9.9", done.stderr)

    def test_an_unknown_argument_is_refused(self):
        done = self.release("--push", expect=1)
        self.assertIn("the only option is --dry-run", done.stderr)
        self.assertEqual(self.tags(), [])

    def test_it_needs_no_python(self):
        """Tagging is git's work; a machine without Python 3.13 can still cut one."""
        done = subprocess.run(
            [str(self.folder / "sd-db.sh"), "release", "--dry-run"], capture_output=True, text=True,
            input="", env={**self.environment(), "PYTHON": "/nonexistent/python"})
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_live_versions_agree(self):
        """The release refuses a mismatch, so this checkout must never carry one."""
        init = (PACKAGE_ROOT / "sd_db" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn(f'__version__ = "{self.version}"', init)
        backend = (PACKAGE_ROOT / "_build.py").read_text(encoding="utf-8")
        self.assertIn(f'VERSION = "{self.version}"', backend)


if __name__ == "__main__":
    unittest.main()
