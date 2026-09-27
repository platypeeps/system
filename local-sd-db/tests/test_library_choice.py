"""Which `sd_db` a database verb runs: the checkout's or the installed copy (sd:1765).

The primary checkout is shared and nobody may move its HEAD, so it can sit
behind `origin/main` while the live database is already migrated. The pack's
virtualenv holds a copy pinned to a commit, which does not follow the
checkout. On 2026-09-27 the checkout was at schema 14, the database and the
installed copy at 15, and every verb refused.

The installed copy here is a stand-in in a real virtualenv: `sd-db.sh` probes
it with `-I`, so no PYTHONPATH or wrapper script can fake it. Its modules
print a marker, which is how a case tells which library answered.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sd_db.schema import SCHEMA_VERSION

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = PACKAGE_ROOT / "sd-db.sh"
MARKER = "the installed stand-in answered"

STAND_IN = {
    "sd_db/__init__.py": "",
    "sd_db/jobs/__init__.py": "",
    "sd_db/jobs/cli.py": f"import sys\nprint({MARKER!r}, 'cli', *sys.argv[1:])\n",
    "sd_db/jobs/backup.py": f"import sys\nprint({MARKER!r}, 'backup', *sys.argv[1:])\n",
    "sd_db/serve.py": f"import sys\nprint({MARKER!r}, 'serve', *sys.argv[1:])\n",
}


def make_venv(root: Path, schema: int | None) -> Path:
    """A virtualenv whose site-packages holds a stand-in built for `schema`.

    `None` leaves `sd_db` out entirely.
    """
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)],
                   check=True, capture_output=True)
    python = root / "bin/python"
    purelib = subprocess.run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True, capture_output=True, text=True).stdout.strip()
    if schema is not None:
        for name, text in {**STAND_IN, "sd_db/schema.py": f"SCHEMA_VERSION = {schema}\n"}.items():
            path = Path(purelib) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return python


class LibraryCase(unittest.TestCase):
    """One virtualenv per installed version, shared by the cases of a class."""

    @classmethod
    def setUpClass(cls):
        cls.venvs = tempfile.TemporaryDirectory()
        root = Path(cls.venvs.name)
        cls.newer = make_venv(root / "newer", SCHEMA_VERSION + 1)
        cls.same = make_venv(root / "same", SCHEMA_VERSION)
        cls.older = make_venv(root / "older", SCHEMA_VERSION - 1)
        cls.none = make_venv(root / "none", None)

    @classmethod
    def tearDownClass(cls):
        cls.venvs.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".local/share/sd").mkdir(parents=True)

    def sd_db(self, python, *args, expect=0, **extra):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("PYTHONPATH", "SD_DB_LIBRARY", "SD_DB_PYTHON",
                                      "XDG_STATE_HOME")}
        environment.update(HOME=str(self.home), PYTHON=str(python), **extra)
        # Bounded: the checkout's `serve` answering instead would serve forever.
        done = subprocess.run([str(ENTRYPOINT), *args], capture_output=True, text=True,
                              input="", env=environment, check=False, timeout=60)
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done


class ANewerInstalledCopyAnswers(LibraryCase):
    def test_a_cli_verb_runs_the_installed_copy_and_says_so(self):
        done = self.sd_db(self.newer, "status")
        self.assertIn(f"{MARKER} cli status", done.stdout)
        self.assertIn(f"built for schema {SCHEMA_VERSION}", done.stderr)
        self.assertIn(f"one built for {SCHEMA_VERSION + 1}", done.stderr)
        self.assertIn("running the installed copy", done.stderr)

    def test_every_database_verb_takes_the_same_route(self):
        for verb, answer in ((("repo", "list"), "cli repo list"), (("migrate",), "cli migrate"),
                             (("backup", "--no-row-prune"), "backup --no-row-prune"),
                             (("serve", "--loopback"), "serve --loopback")):
            with self.subTest(verb=verb):
                done = self.sd_db(self.newer, *verb)
                self.assertIn(f"{MARKER} {answer}", done.stdout)

    def test_the_checkout_can_still_be_forced(self):
        done = self.sd_db(self.newer, "init", SD_DB_LIBRARY="checkout")
        self.assertNotIn(MARKER, done.stdout)
        self.assertIn(f"schema version {SCHEMA_VERSION}", done.stdout)
        self.assertTrue((self.home / ".local/share/sd/sd.db").is_file())

    def test_the_test_verb_pins_the_checkout_for_what_it_drives(self):
        """The suite tests this checkout; a verb it runs must not switch away."""
        stub = self.home / "argv-python"
        stub.write_text('#!/bin/sh\n[ "$1" = -c ] && exit 0\nprintf "library=%s\\n" "$SD_DB_LIBRARY"\n',
                        encoding="utf-8")
        stub.chmod(0o755)
        for verb in ("test", "check"):
            with self.subTest(verb=verb):
                done = self.sd_db(stub, verb, SD_DB_LIBRARY="auto")
                self.assertEqual(done.stdout.strip(), "library=checkout")


class TheCheckoutAnswersOtherwise(LibraryCase):
    def assert_the_checkout_ran(self, done):
        self.assertNotIn(MARKER, done.stdout)
        self.assertEqual(done.stderr, "")
        self.assertIn(f"schema version {SCHEMA_VERSION}", done.stdout)
        self.assertTrue((self.home / ".local/share/sd/sd.db").is_file())

    def test_a_tie_runs_the_checkout(self):
        """A branch standing on the installed commit runs its own files."""
        self.assert_the_checkout_ran(self.sd_db(self.same, "init"))

    def test_an_older_installed_copy_runs_the_checkout(self):
        """A branch that adds a migration runs it before anything is installed."""
        self.assert_the_checkout_ran(self.sd_db(self.older, "init"))

    def test_no_installed_copy_runs_the_checkout(self):
        self.assert_the_checkout_ran(self.sd_db(self.none, "init"))


class TheLibraryVerbPrintsTheChoice(LibraryCase):
    """`library` answers for a caller that imports `sd_db` itself (sd:1812)."""

    def test_each_installed_version_gets_the_answer_its_verbs_run(self):
        for python, answer in ((self.newer, "installed"), (self.same, "checkout"),
                               (self.older, "checkout"), (self.none, "checkout")):
            with self.subTest(python=python.parent.parent.name):
                self.assertEqual(self.sd_db(python, "library").stdout, f"{answer}\n")

    def test_the_setting_forces_the_answer(self):
        self.assertEqual(self.sd_db(self.newer, "library", SD_DB_LIBRARY="checkout").stdout,
                         "checkout\n")
        self.assertEqual(self.sd_db(self.older, "library", SD_DB_LIBRARY="installed").stdout,
                         "installed\n")


class TheSettingIsChecked(LibraryCase):
    def test_installed_without_a_copy_refuses_in_a_sentence(self):
        done = self.sd_db(self.none, "status", expect=1, SD_DB_LIBRARY="installed")
        self.assertIn("SD_DB_LIBRARY=installed", done.stderr)
        self.assertIn("no installed sd_db", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_installed_takes_even_an_older_copy(self):
        done = self.sd_db(self.older, "status", SD_DB_LIBRARY="installed")
        self.assertIn(f"{MARKER} cli status", done.stdout)

    def test_an_unknown_value_is_refused(self):
        done = self.sd_db(self.newer, "status", expect=1, SD_DB_LIBRARY="tree")
        self.assertIn("SD_DB_LIBRARY=tree is not one of auto, checkout or installed",
                      done.stderr)
        self.assertNotIn(MARKER, done.stdout)


if __name__ == "__main__":
    unittest.main()
