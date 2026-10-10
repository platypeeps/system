"""The installed pack, as the installer's receipt names it.

`installed()` reads the receipt and nothing else: the probe of `sd_lib` the
`docs/work` retire asked for went with the retire (sd:3231). Each way of not
finding a checkout says which it was, since `sources.vault` passes `why` on.
"""

import json
import tempfile
import unittest
from pathlib import Path

from sd_db.pack import UNKNOWN, installed, receipt_path, state_home

from . import support


class PackCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def installed(self):
        """Always with an empty environment: the real one may carry an
        `XDG_STATE_HOME` pointing at the machine's own installed pack, and a
        test that read that would pass or fail on what the operator installed
        this morning."""
        return installed(home=self.home, environ={})


class TheStateHomeIsTheXdgOne(PackCase):
    def test_an_absolute_xdg_state_home_is_used(self):
        self.assertEqual(
            state_home(self.home, {"XDG_STATE_HOME": "/somewhere/state"}),
            Path("/somewhere/state"),
        )

    def test_a_relative_one_is_not(self):
        """A relative value resolves against the working directory, which for
        a cron job is not the home anybody meant."""
        self.assertEqual(
            state_home(self.home, {"XDG_STATE_HOME": "state"}),
            self.home / ".local" / "state",
        )

    def test_the_receipt_sits_under_it(self):
        self.assertEqual(
            receipt_path(self.home, {}),
            self.home / ".local/state/sd-ai-command-pack/installed.json",
        )


class EveryWayOfHavingNoCheckout(PackCase):
    """Each of these names no checkout, and each one says which it was."""

    def test_no_receipt(self):
        found = self.installed()
        self.assertIsNone(found.checkout)
        self.assertEqual(found.version, UNKNOWN)
        self.assertIn("no receipt at", found.why)
        self.assertIn("installed.json", found.why)

    def test_a_receipt_that_will_not_parse(self):
        path = receipt_path(self.home, {})
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        found = self.installed()
        self.assertIsNone(found.checkout)
        self.assertIn("will not parse", found.why)

    def test_a_receipt_naming_no_checkout(self):
        path = receipt_path(self.home, {})
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"commit": "abc123def456"}), encoding="utf-8")
        found = self.installed()
        self.assertIsNone(found.checkout)
        self.assertIn("names no checkout", found.why)
        self.assertEqual(found.version, "abc123def456")


class TheReceiptSaysWhichCheckout(PackCase):
    def test_it_carries_the_checkout_and_the_short_commit_as_its_version(self):
        checkout = support.pack(self.home)
        found = self.installed()
        self.assertEqual(found.checkout, checkout)
        self.assertEqual(found.version, "47d41245a470")

    def test_a_checkout_the_receipt_does_not_name_is_not_consulted(self):
        """The receipt picks the directory. A second clone sitting somewhere
        else on disk is not what the installed skills load."""
        (self.home / "repos/sd-ai-command-pack/bin").mkdir(parents=True)
        support.pack(self.home, checkout=self.home / "installed-copy")
        self.assertEqual(self.installed().checkout, self.home / "installed-copy")


if __name__ == "__main__":
    unittest.main()
