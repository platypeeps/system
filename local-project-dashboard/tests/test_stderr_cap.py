"""sd:830. A tile's error line keeps its tab name however long the paths in it.

`Budget.run` keeps the tail of a command's stderr, and for a tile that tail is
one line, `sd_tile.main`'s `<tab>: <error>`, with the tab name at the front.
The Full Disk Access refusal embeds the vault path and the interpreter path,
so its length grows with both, and at 512 bytes a checkout under a
136-character HOME lost exactly `vault` from the front: the suite went red
with a message that named nothing, and whether it did depended on who ran it.
The cap is now sized for the longest such line, and a cut that still happens
is named at the front of what is kept rather than left for a reader to find
mid-word.

sd:834, review-375's N1 and N2 on that fix. The cap protected the name only
while the line fitted it: a longer error still pushed `vault: ` out of the
reader's tail, and the marker that then said so named the interpreter,
`python3.14`, not the tab. Now the writer bounds its own line, in
`sd_tile.main`, so the name survives whatever the error's length, and the
reader's marker names the tab it asked for.
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sd_tile
from sd_dashboard import reports_screen

HERE = Path(__file__).resolve().parents[1]

# The longest path macOS accepts. Both paths in the refusal are bounded by it,
# so a line built from two of them is the longest the tile can write.
PATH_MAX = 1024


def collectors(environment):
    """A fresh `collectors` module, its `VAULT` derived under `environment`."""
    with patch.dict(os.environ, environment):
        os.environ.pop("VAULT", None)
        spec = importlib.util.spec_from_file_location("stderr_cap_collectors", HERE / "collectors.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def segments(prefix, length):
    """A path of exactly `length` characters under `prefix`, in legal components."""
    path = prefix
    while len(path) < length:
        path += "/" + "x" * min(200, length - len(path) - 1)
    return path


def failing_tile(root, *, reason, noise=""):
    """The real `sd_tile.py` whose collectors fail to load with `reason`.

    `noise` is written to stderr first, as a chatty child would. Same shape as
    `test_tile_deadline.PROBE_TILE`: the deadline, `main` and its error line
    are the checkout's, and only what raises is stood in.
    """
    script = root / "tile.py"
    script.write_text(f"import sys\nsys.path.insert(0, {str(HERE)!r})\n"
                      "import sd_tile\n"
                      f"sys.stderr.write({noise!r})\n"
                      "def broken():\n"
                      f"    raise RuntimeError({reason!r})\n"
                      "sd_tile.load_collectors = broken\n"
                      "sys.exit(sd_tile.main(sys.argv[1:]))\n")
    return patch.object(reports_screen, "TILE", script)


def refused(area):
    """What `reports_screen.collect` tells its caller for a tile that exited 1."""
    with unittest.TestCase().assertRaises(ValueError) as error:
        reports_screen.collect(area)
    return str(error.exception)


def written(argv):
    """(exit status, stderr) of `sd_tile.main` run in this process."""
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = sd_tile.main(argv)
    return code, stderr.getvalue()


class ErrorLine(unittest.TestCase):
    def test_a_vault_refusal_under_a_long_home_still_names_the_tab(self):
        # The real tile, the real interpreter, through the reader that shows
        # its stderr as a view's reason. The vault is a path long enough that
        # the tile's one error line outgrows the cap this bug was found at,
        # and one that does not exist, so the probe answers `missing` and the
        # line is written without waiting on TCC. The tab name is the first
        # thing on it, and it is still there.
        vault = segments("/nonexistent-sd-830", 600)
        with patch.dict(os.environ, {"VAULT": vault}), self.assertRaises(ValueError) as refused:
            reports_screen.collect("vault")
        told = str(refused.exception)
        self.assertRegex(told, r"^vault: RuntimeError\('vault path does not exist: /nonexistent-sd-830")
        # And it was a line the old cap would have cut: whole, it is longer.
        self.assertGreater(len(told.encode()), 512, told)

    def test_the_longest_full_disk_access_refusal_fits_the_cap_whole(self):
        # The refusal as `vault_blocked` writes it, with both of its paths at
        # PATH_MAX: the vault's, derived from HOME, and the interpreter's.
        # What `Budget.run` returns for a child that writes that line is that
        # line, untouched, with the tab name at its front.
        home = segments("/home-sd-830", PATH_MAX - len("/Documents/Vault"))
        module = collectors({"HOME": home})
        self.assertEqual(len(str(module.VAULT)), PATH_MAX)
        module.run = lambda *_, **__: "denied"
        with patch.object(sys, "executable", segments("/python-sd-830", PATH_MAX)):
            line = f"vault: {RuntimeError(module.vault_blocked())!r}\n"
        self.assertLessEqual(len(line.encode()), module.BUDGET_STDERR, line)
        result = module.Budget(5.0).run(["/bin/sh", "-c", 'printf "%s" "$1" >&2', "sh", line])
        self.assertEqual(result.stderr, line)

    def test_a_cut_is_said_at_the_front_with_its_size(self):
        # Past the cap the head is gone by design. What is kept then starts
        # by saying so, and how much, so a reader expecting `<tab>: ` there
        # meets the cut and not a line that begins mid-word.
        module = collectors({})
        cap = module.BUDGET_STDERR
        noise = "n" * (cap + 37)
        line = "vault: RuntimeError('cut')\n"
        result = module.Budget(5.0).run(
            ["/bin/sh", "-c", 'printf "%s%s" "$1" "$2" >&2', "sh", noise, line])
        self.assertTrue(result.stderr.startswith(
            f"[sh: the first {37 + len(line)} bytes of its stderr were dropped; the last {cap} bytes follow] "),
            result.stderr[:160])
        self.assertTrue(result.stderr.endswith(line), result.stderr[-80:])
        self.assertEqual(len(result.stderr) - len(result.stderr.partition("] ")[0]) - 2, cap)

    def test_stderr_within_the_cap_is_returned_as_written(self):
        module = collectors({})
        line = "vault: RuntimeError('whole')\n"
        result = module.Budget(5.0).run(["/bin/sh", "-c", 'printf "%s" "$1" >&2', "sh", line])
        self.assertEqual(result.stderr, line)


class WriterCut(unittest.TestCase):
    """sd:834 N1. The tile bounds its own error line, so the name is at the
    front of what the reader keeps whatever the error's length."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="stderr-cap-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_the_writers_bound_is_the_readers_cap(self):
        # Copied rather than imported, as `BUDGET_BYTES` is: the line is
        # written when loading the collectors may be what failed. So the two
        # are pinned to each other here.
        self.assertEqual(sd_tile.ERROR_BYTES, collectors({}).BUDGET_STDERR)

    def test_an_error_longer_than_the_cap_still_names_the_tab_at_the_reader(self):
        # Through the reader, for real: an error five thousand bytes long,
        # which is more than the reader's tail. Before, the tail began
        # mid-error and the reader's own cut marker was the first thing on
        # it; now the line starts with the tab, ends by saying what the tile
        # cut, and the reader had nothing to drop.
        cap = collectors({}).BUDGET_STDERR
        with failing_tile(self.root, reason="x" * 5000):
            told = refused("vault")
        self.assertTrue(told.startswith("vault: RuntimeError('xxxx"), told[:120])
        self.assertRegex(told, r" \[vault: the last \d+ bytes of its reason were cut\]$")
        self.assertNotIn("were dropped", told)
        self.assertLessEqual(len(told.encode()) + 1, cap, len(told))

    def test_a_line_that_fits_is_written_whole(self):
        # The bound counts the newline `print` adds. A line that fills the
        # cap exactly is not cut; one byte more is.
        head = "vault: RuntimeError('"
        room = sd_tile.ERROR_BYTES - 1 - len(head) - len("')")
        whole = "x" * room
        code, line = written_with(whole)
        self.assertEqual(code, 1)
        self.assertEqual(line, f"{head}{whole}')\n")
        self.assertEqual(len(line.encode()), sd_tile.ERROR_BYTES)
        code, line = written_with(whole + "y")
        self.assertEqual(code, 1)
        self.assertEqual(len(line.encode()), sd_tile.ERROR_BYTES, line[-120:])
        self.assertTrue(line.startswith(head + "xxx"), line[:80])
        self.assertRegex(line, r" \[vault: the last \d+ bytes of its reason were cut\]\n$")

    def test_the_cut_counts_what_it_cut(self):
        # The count is of bytes that are missing from the reason, so a reader
        # can add it to what is shown and get the length of the whole.
        code, line = written_with("x" * 10000)
        self.assertEqual(code, 1)
        kept, _, marker = line.rpartition(" [vault: the last ")
        cut = int(marker.split(" ")[0])
        whole = f"vault: {RuntimeError('x' * 10000)!r}"
        self.assertEqual(len(kept.encode()) + cut, len(whole.encode()))
        self.assertEqual(len(line.encode()), sd_tile.ERROR_BYTES)

    def test_a_multibyte_character_is_not_split_by_the_cut(self):
        # The bound is in bytes and the reason is text; a cut falls between
        # characters, never inside one, so what is shown decodes as written.
        code, line = written_with("\u00e9" * 5000)
        self.assertEqual(code, 1)
        self.assertLessEqual(len(line.encode()), sd_tile.ERROR_BYTES)
        self.assertTrue(line.startswith("vault: RuntimeError('\u00e9\u00e9"), line[:40])
        self.assertNotIn("\ufffd", line)


def written_with(reason):
    """`sd_tile.main(["vault"])`'s exit and stderr when its collectors raise `reason`."""
    def broken():
        raise RuntimeError(reason)
    with patch.object(sd_tile, "load_collectors", broken):
        return written(["vault"])


class ReaderCut(unittest.TestCase):
    """sd:834 N2. When the reader drops the front of a tile's stderr, the
    marker names the tab it asked for, not the interpreter that ran it."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="stderr-cap-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_a_cut_names_the_label_it_was_given(self):
        module = collectors({})
        cap = module.BUDGET_STDERR
        noise = "n" * (cap + 37)
        line = "vault: RuntimeError('cut')\n"
        result = module.Budget(5.0).run(
            ["/bin/sh", "-c", 'printf "%s%s" "$1" "$2" >&2', "sh", noise, line], label="vault")
        self.assertTrue(result.stderr.startswith(
            f"[vault: the first {37 + len(line)} bytes of its stderr were dropped; the last {cap} bytes follow] "),
            result.stderr[:160])
        self.assertTrue(result.stderr.endswith(line), result.stderr[-80:])

    def test_a_resource_view_names_the_tab_a_chatty_tile_was_cut_from(self):
        # Through the reader: a tile that wrote more than the cap before its
        # one error line. The line itself survives at the end, and the marker
        # at the front says which tab was cut.
        cap = collectors({}).BUDGET_STDERR
        line = "vault: RuntimeError('cut')\n"
        with failing_tile(self.root, reason="cut", noise="n" * (cap + 37)):
            told = refused("vault")
        self.assertTrue(told.startswith(
            f"[vault: the first {37 + len(line)} bytes of its stderr were dropped; the last {cap} bytes follow] n"),
            told[:160])
        self.assertTrue(told.endswith(line.rstrip()), told[-80:])


if __name__ == "__main__":
    unittest.main()
