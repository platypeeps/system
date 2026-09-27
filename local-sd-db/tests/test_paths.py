"""The stored key and the disk path of a repository (sd:1439).

`sd_db.paths` is the one place a path under `$HOME` becomes `~/...` and back.
These cases are the ones implement.md step 2 names: a path under the home,
the home itself, a path outside it, a symlinked home, `~user`, a relative
input, and an unset `HOME`.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import paths


class HomeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Resolved: macOS puts the temporary directory under `/var`, a link to
        # `/private/var`. The symlinked-home case below tests that on purpose.
        self.home = Path(tmp.name).resolve()
        (self.home / "repos/one").mkdir(parents=True)
        self.outside = tempfile.TemporaryDirectory()
        self.addCleanup(self.outside.cleanup)
        self.set_home(self.home)

    def set_home(self, value):
        patcher = mock.patch.dict(os.environ, {"HOME": str(value)})
        patcher.start()
        self.addCleanup(patcher.stop)


class AStoredKey(HomeCase):
    def test_a_path_under_the_home_is_stored_home_relative(self):
        self.assertEqual(paths.store(self.home / "repos/one"), "~/repos/one")
        self.assertEqual(paths.store("~/repos/one"), "~/repos/one")
        self.assertEqual(paths.expand("~/repos/one"), self.home / "repos/one")

    def test_the_home_itself_is_a_tilde(self):
        self.assertEqual(paths.store(self.home), "~")
        self.assertEqual(paths.expand("~"), self.home)

    def test_a_path_outside_the_home_stays_absolute(self):
        outside = Path(self.outside.name).resolve()
        self.assertEqual(paths.store(outside), str(outside))
        self.assertEqual(paths.expand(str(outside)), outside)
        # The writer form keeps the value exactly as given (prd R1).
        self.assertEqual(paths.key(self.outside.name), self.outside.name)

    def test_a_writer_converts_only_a_path_under_the_home(self):
        self.assertEqual(paths.key(str(self.home / "repos/one")), "~/repos/one")
        self.assertIsNone(paths.key(None))
        self.assertEqual(paths.key("owner/name"), "owner/name")

    def test_a_probe_carries_the_key_and_the_legacy_absolute_form(self):
        probe = paths.keys(str(self.home / "repos/one"))
        self.assertEqual(probe[0], "~/repos/one")
        self.assertIn(str(self.home / "repos/one"), probe)
        self.assertEqual(paths.keys("~/repos/one"), ("~/repos/one", str(self.home / "repos/one")))

    def test_same_compares_the_keys(self):
        self.assertTrue(paths.same("~/repos/one", self.home / "repos/one"))
        self.assertFalse(paths.same("~/repos/one", "~/repos/two"))
        self.assertFalse(paths.same(None, "~/repos/one"))

    def test_disk_expands_a_key_and_passes_anything_else(self):
        self.assertEqual(paths.disk("~/repos/one"), self.home / "repos/one")
        self.assertEqual(paths.disk("relative/dir"), Path("relative/dir"))


class ASymlinkedHome(HomeCase):
    """`$HOME` may be a link, and a path may arrive through either spelling."""

    def test_both_spellings_store_one_key(self):
        link = Path(self.outside.name) / "home-link"
        link.symlink_to(self.home)
        self.set_home(link)
        self.assertEqual(paths.store(link / "repos/one"), "~/repos/one")
        self.assertEqual(paths.store(self.home / "repos/one"), "~/repos/one")
        self.assertEqual(paths.home_relative(str(link / "repos/one")), "~/repos/one")
        self.assertEqual(paths.home_relative(str(self.home / "repos/one")), "~/repos/one")
        # The reverse writes the resolved home, which is what `repos.add` wrote.
        self.assertEqual(paths.home_absolute("~/repos/one"), str(self.home / "repos/one"))


class NotAPath(HomeCase):
    def test_another_accounts_home_is_refused(self):
        with self.assertRaises(paths.NotAPath):
            paths.store("~root/repos")
        with self.assertRaises(paths.NotAPath):
            paths.expand("~root/repos")
        self.assertEqual(paths.key("~root/repos"), "~root/repos")

    def test_a_relative_input_is_refused(self):
        with self.assertRaises(paths.NotAPath):
            paths.store("repos/one")
        self.assertEqual(paths.keys("repos/one"), ("repos/one",))
        self.assertTrue(paths.same("repos/one", "repos/one"))


class AnUnsetHome(unittest.TestCase):
    """A wrong home fails open, so a missing one is an error, never a guess."""

    def test_unset_empty_and_relative_homes_are_refused(self):
        for value in (None, "", "relative/home"):
            environment = {key: item for key, item in os.environ.items() if key != "HOME"}
            if value is not None:
                environment["HOME"] = value
            with mock.patch.dict(os.environ, environment, clear=True):
                with self.subTest(home=value):
                    with self.assertRaises(paths.PathRefused):
                        paths.store("/anywhere/repo")
                    with self.assertRaises(paths.PathRefused):
                        paths.expand("~/repos/one")


class TheMigrationFunctions(HomeCase):
    def test_install_registers_both_and_a_replay_changes_nothing(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        paths.install(connection)
        under = str(self.home / "repos/one")
        self.assertEqual(connection.execute("SELECT sd_home_relative(?)", (under,)).fetchone()[0], "~/repos/one")
        self.assertEqual(connection.execute("SELECT sd_home_relative('~/repos/one')").fetchone()[0], "~/repos/one")
        self.assertEqual(connection.execute("SELECT sd_home_relative('/opt/x')").fetchone()[0], "/opt/x")
        self.assertEqual(connection.execute("SELECT sd_home_absolute('~/repos/one')").fetchone()[0], under)
        self.assertIsNone(connection.execute("SELECT sd_home_relative(NULL)").fetchone()[0])
        # A sibling that only shares the prefix text is not under the home.
        self.assertEqual(paths.home_relative(str(self.home) + "-other/x"), str(self.home) + "-other/x")


if __name__ == "__main__":
    unittest.main()
