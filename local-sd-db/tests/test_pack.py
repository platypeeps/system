"""The installed pack, and every way `sd_lib` can fail to answer `delivered`.

Criterion 7's clause is "refuses under a pack whose `sd_lib` has no
`delivered`, naming the version". Four things can be meant by "has no", and
this file exists because the shape that gets written by accident answers
three of them with "no pack found, so nothing to check". That reading retires
the source under exactly the pack that cannot read what replaces it -- the
fail-open shape that has produced five defects across these two work items.

There is a fifth, and it is the one the criterion's own wording walks you
into: probing `delivered` and stopping there. `delivered` answers "has this
item shipped" from git history and is untouched by anything the retire
removes. `status_marker` is what reads the marker the retire writes.
`TheProbeIsTheReaderContract` below pins that with a pack from `eb7695c7`,
a real version of the real pack, which carries `delivered` and neither of
the row readers.

So every case is enumerated, each one is asserted, and the default is
refusal: `usable` is true only where every wanted name was found.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from sd_db import pack
from sd_db.pack import UNKNOWN, WANTED, installed, receipt_path, state_home

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


class EveryWayOfHavingNoDelivered(PackCase):
    """Each of these is a refusal, and each one says which it was."""

    def test_no_receipt_is_a_refusal_and_not_a_pass(self):
        found = self.installed()
        self.assertFalse(found.usable)
        self.assertEqual(found.version, UNKNOWN)
        self.assertIn("no receipt at", found.why)
        self.assertIn("installed.json", found.why)

    def test_a_receipt_that_will_not_parse_is_a_refusal(self):
        path = receipt_path(self.home, {})
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        found = self.installed()
        self.assertFalse(found.usable)
        self.assertIn("will not parse", found.why)

    def test_a_receipt_naming_no_checkout_is_a_refusal(self):
        path = receipt_path(self.home, {})
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"commit": "abc123def456"}), encoding="utf-8")
        found = self.installed()
        self.assertFalse(found.usable)
        self.assertIn("names no checkout", found.why)
        self.assertEqual(found.version, "abc123def456")

    def test_a_checkout_that_is_gone_is_a_refusal_naming_the_version(self):
        support.pack(self.home, checkout=self.home / "gone", library=None)
        found = self.installed()
        self.assertFalse(found.usable)
        self.assertIn("does not exist", found.why)
        self.assertIn("sd_lib.py", found.why)
        self.assertIn(found.version, found.refusal())

    def test_a_library_that_will_not_import_is_a_refusal(self):
        support.pack(self.home, library="def delivered(:\n")
        found = self.installed()
        self.assertFalse(found.usable)
        self.assertIn("will not import", found.why)
        self.assertIn("SyntaxError", found.why)

    def test_a_library_that_raises_at_import_is_a_refusal(self):
        """A module body runs when it is loaded, and one that dies takes the
        answer with it. Caught as a refusal rather than escaping as whatever
        the pack happened to raise."""
        support.pack(self.home, library="raise RuntimeError('the pack is broken')\n")
        found = self.installed()
        self.assertFalse(found.usable)
        self.assertIn("will not import", found.why)
        self.assertIn("the pack is broken", found.why)

    def test_a_library_that_exits_at_import_is_a_refusal(self):
        """`sys.exit` in a module body is a `SystemExit`, a `BaseException`
        and not an `Exception`; it is still a pack that will not import."""
        support.pack(self.home, library="import sys\nsys.exit(3)\n")
        found = self.installed()
        self.assertFalse(found.usable)
        self.assertIn("SystemExit", found.why)

    def test_an_interrupt_during_the_probe_is_not_swallowed(self):
        """Ctrl-C while the module body runs is the operator stopping the
        command, not a verdict on the pack. The `BaseException` arm turned it
        into a refusal and the command carried on (sd:1219)."""
        support.pack(self.home, library="raise KeyboardInterrupt\n")
        with self.assertRaises(KeyboardInterrupt):
            self.installed()
        self.assertNotIn(pack.PROBE, sys.modules)

    def test_a_library_with_none_of_the_wanted_names_is_a_refusal(self):
        support.pack(self.home, library="def status(root, item):\n    return 'no'\n")
        found = self.installed()
        self.assertFalse(found.usable)
        for name in WANTED:
            self.assertIn(repr(name), found.why)

    def test_a_wanted_name_bound_to_none_is_a_refusal(self):
        """`status_marker = None` in a half-finished pack imports fine and
        answers nothing. The check is that the name resolves to something
        callable, not that it is bound."""
        support.pack(
            self.home,
            library="status_marker = None\n\n\ndef delivered(root, item):\n"
                    "    return 'yes'\n",
        )
        found = self.installed()
        self.assertFalse(found.usable)
        self.assertIn("'status_marker'", found.why)

    def test_a_wanted_name_bound_to_a_string_is_a_refusal(self):
        support.pack(self.home, library="status_marker = 'row'\ndelivered = 'yes'\n")
        found = self.installed()
        self.assertFalse(found.usable)
        self.assertIn("not a function", found.why)


class TheProbeRunsRealModuleBodies(unittest.TestCase):
    """A module body needs a `sys.modules` entry, and the probe is not exempt.

    The first version executed the file unregistered, on the reasoning that
    an inspection is not a dependency. `@dataclass` reads
    `sys.modules[cls.__module__].__dict__` with no guard, so every real
    `sd_lib.py` died on its first dataclass and the guard refused every
    pack -- naming a version, so the refusal read as "too old" for a pack
    that was current. Fail-closed and silent is still wrong.
    """

    def test_a_library_defining_a_dataclass_is_probed_not_killed(self) -> None:
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        support.pack(home, library=support.PACK_LIBRARY)
        found = pack.installed(home=home, environ={})
        self.assertTrue(found.usable, found.refusal())

    def test_the_probe_leaves_no_entry_behind_on_success_or_failure(self) -> None:
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        support.pack(home, library=support.PACK_LIBRARY)
        # An empty environment, as `PackCase.installed` passes: an exported
        # `XDG_STATE_HOME` sends the probe to a state home with no receipt,
        # nothing is loaded, and both assertions pass without testing the
        # probe (sd:1220). The usability and `why` checks prove it ran.
        self.assertTrue(pack.installed(home=home, environ={}).usable)
        self.assertNotIn(pack.PROBE, sys.modules)
        broken = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, broken, True)
        support.pack(broken, library="raise RuntimeError('boom')\n")
        found = pack.installed(home=broken, environ={})
        self.assertFalse(found.usable)
        self.assertIn("will not import", found.why)
        self.assertNotIn(pack.PROBE, sys.modules)


class TheProbeIsTheReaderContract(PackCase):
    """Which names prove the pack can read what the retire leaves behind.

    `delivered` is not one of them on its own, and this is the test that
    stops the next person re-deriving that from criterion 7's wording.
    """

    def test_a_pack_with_delivered_and_no_status_marker_is_refused(self):
        """`eb7695c7`: `delivered` landed two commits before the row readers.

        A pack from there passes a `delivered`-only guard, and then the
        retire strips every `status:` line and writes the marker under a pack
        that cannot read either one.
        """
        support.pack(self.home, library=support.PACK_BEFORE_THE_ROW_READERS)
        found = self.installed()
        self.assertFalse(found.usable)
        self.assertIn("'status_marker'", found.why)
        self.assertNotIn("'delivered'", found.why)

    def test_a_pack_with_status_marker_and_no_delivered_is_refused(self):
        """The other half, and it is not symmetry for its own sake.

        Criterion 13: a database-free clone reads "the line while the marker
        is absent and git alone once it is present". The retire's commit is
        what makes the marker present, so it switches CI onto `delivered` at
        the same moment it switches everything else onto the row.
        """
        support.pack(
            self.home,
            library='def status_marker(root, work_dir="docs/work"):\n'
                    '    return "file", ""\n',
        )
        found = self.installed()
        self.assertFalse(found.usable)
        self.assertIn("'delivered'", found.why)
        self.assertNotIn("'status_marker'", found.why)

    def test_the_refusal_names_every_missing_name_and_not_just_the_first(self):
        support.pack(self.home, library="x = 1\n")
        why = self.installed().why
        for name in WANTED:
            self.assertIn(repr(name), why)

    def test_both_names_are_what_is_wanted_and_rows_is_deliberately_not(self):
        """The list is a decision. `Rows` landed in the pack's same commit as
        `status_marker`, so probing it discriminates no version that has ever
        existed; if the two are ever split, this test is where it is noticed.
        """
        self.assertEqual(WANTED, ("status_marker", "delivered"))


class APackThatCanAnswer(PackCase):
    def test_it_is_usable_and_carries_the_short_commit_as_its_version(self):
        support.pack(self.home)
        found = self.installed()
        self.assertTrue(found.usable, found.why)
        self.assertEqual(found.version, "47d41245a470")
        self.assertEqual(found.checkout, self.home / "pack")

    def test_the_probe_is_not_left_in_sys_modules(self):
        """An inspection, not a dependency: a half-initialised `sd_lib` left
        registered would be picked up by a later import instead of the real
        one."""
        before = set(sys.modules)
        support.pack(self.home)
        self.assertTrue(self.installed().usable)
        self.assertEqual({name for name in sys.modules if "sd_lib" in name}, set())
        self.assertEqual(set(sys.modules) - before, set())


class TheReceiptSaysWhichCheckout(PackCase):
    def test_a_checkout_the_receipt_does_not_name_is_not_consulted(self):
        """The receipt's job for `bin/` is to pick the directory, and only
        that. A second clone sitting somewhere else on disk is not what the
        installed skills load, so it is not what is asked -- however new it
        is."""
        newer = self.home / "repos/sd-ai-command-pack"
        (newer / "bin").mkdir(parents=True)
        (newer / "bin/sd_lib.py").write_text(support.PACK_LIBRARY, encoding="utf-8")
        support.pack(self.home, checkout=self.home / "installed-copy",
                     library="def status(root, item):\n    return 'no'\n")
        found = self.installed()
        self.assertFalse(found.usable)
        self.assertEqual(found.checkout, self.home / "installed-copy")


class TheVersionAndTheProbeAreTwoDifferentCommits(PackCase):
    """The receipt records a commit; the probe reads a file on disk. Nothing
    keeps them equal, and the refusal must not imply they are.

    `installed()` resolves `checkout / bin/sd_lib.py` and loads it as it
    stands now. The receipt's `commit` reaches nothing but the version
    string. A refusal reading "pack 47d41245a470 defines no `status_marker`"
    while having probed a checkout that has since moved to `main` sends the
    operator to inspect a commit that is not what failed.
    """

    def test_the_file_decides_usability_and_the_receipt_does_not(self):
        """Same receipt, same recorded commit, two different files: the
        answer follows the file. This is the whole separation in one test."""
        support.pack(self.home, library=support.PACK_BEFORE_THE_ROW_READERS)
        before = self.installed()
        self.assertFalse(before.usable)
        (self.home / "pack/bin/sd_lib.py").write_text(
            support.PACK_LIBRARY, encoding="utf-8"
        )
        after = self.installed()
        self.assertTrue(after.usable, after.why)
        self.assertEqual(before.version, after.version)

    def test_the_refusal_labels_the_version_as_the_receipts_record(self):
        support.pack(self.home, library=support.PACK_BEFORE_THE_ROW_READERS)
        said = self.installed().refusal()
        self.assertIn(
            "records version 47d41245a470 as the commit it was installed from",
            said,
        )

    def test_the_refusal_names_the_file_it_actually_probed(self):
        support.pack(self.home, library=support.PACK_BEFORE_THE_ROW_READERS)
        said = self.installed().refusal()
        self.assertIn(
            f"what was probed is {self.home / 'pack/bin/sd_lib.py'} as it "
            f"stands on disk now",
            said,
        )

    def test_it_never_says_the_version_is_what_defines_the_names(self):
        """The sentence shape that merges them again. If someone rewrites the
        refusal as "pack <version> defines no ...", this fails."""
        support.pack(self.home, library=support.PACK_BEFORE_THE_ROW_READERS)
        found = self.installed()
        for merged in (
            f"pack at version {found.version}",
            f"version {found.version} defines",
            f"version {found.version} cannot",
        ):
            with self.subTest(merged=merged):
                self.assertNotIn(merged, found.refusal())

    def test_with_no_checkout_it_says_nothing_was_probed(self):
        """`version` is still reported, and the refusal says plainly that no
        file backs it, rather than leaving the reader to assume one does."""
        support.pack(self.home, library=None, receipt=True)
        (self.home / ".local/state/sd-ai-command-pack/installed.json").write_text(
            json.dumps({"schema": 1, "commit": "a" * 40}), encoding="utf-8"
        )
        found = self.installed()
        self.assertIsNone(found.checkout)
        self.assertIn("no checkout was opened, so nothing was probed", found.refusal())
        self.assertIn("records version aaaaaaaaaaaa", found.refusal())


class TheRefusalNamesTheVersion(PackCase):
    def test_every_unusable_pack_names_a_version_in_its_refusal(self):
        """`unknown` is a version too, and is what the operator is told when
        the receipt cannot supply one. What is never allowed is a refusal
        with no version in it at all."""
        cases = [
            lambda: None,
            lambda: support.pack(self.home, library=None),
            lambda: support.pack(self.home, library="def status(r, i): return 'no'\n"),
            lambda: support.pack(self.home, library=support.PACK_BEFORE_THE_ROW_READERS),
        ]
        for build in cases:
            with self.subTest(build=build):
                for stale in self.home.glob(".local/state/**/installed.json"):
                    stale.unlink()
                build()
                found = self.installed()
                self.assertFalse(found.usable)
                self.assertIn(f"version {found.version}", found.refusal())
                for name in WANTED:
                    self.assertIn(f"sd_lib.{name}", found.refusal())


if __name__ == "__main__":
    unittest.main()
