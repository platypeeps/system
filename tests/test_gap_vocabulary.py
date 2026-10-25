"""The system's protection gap ids are the pack's, read from the pinned pack (sd:1372).

The pack owns the gap vocabulary: `sd-status` emits the ids, and the
pack's `sd_lib` module accepts them as `ACKNOWLEDGEABLE_GAPS`. `GAP_IDS` in the
pack's `lib/sd_db/protection.py`, which the nightly collector writes, restates
them. Nothing held them together, so an id added on one side only failed
nothing. The dashboard's `GAPS` columns, checked here too until pack sd:5987
moved the dashboard to the pack, are the pack's to check.

The pack side is enumerated, not restated: the test reads the pack copy that
`tests/check.sh` fetches into `.ci/pack` at the SHA in `.sd-pack-rev`, or the
one `SD_ACCEPTANCE_PACK` names. A missing or off-pin copy fails loudly; a skip
would fail the check anyway, and it would hide the one comparison this exists
for. `unprotected` is the system's status column, not a gap cell, as the pack's
half of this binding (its `GapVocabularyTests`, landed in pack PR #1305)
also reads it.

Stdlib only, read with `ast` so nothing is imported: this runs in the
`tests/ci-native.sh` preflight, before the virtualenv exists.
`python3 tests/test_gap_vocabulary.py` from the repository root runs it once
`.ci/pack` holds the pin, which any `make test SUITE=<name>` fetches.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
#: The gap the system reports as a status, never as a column of its own.
STATUS_GAP = "unprotected"


def assigned(path: pathlib.Path, name: str) -> object:
    """The literal a module assigns to `name` at its top level."""
    for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name
                                                for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path} assigns no {name} at its top level")


def pinned_pack() -> pathlib.Path:
    """The pack copy at the pin, or an AssertionError that says how to get one."""
    pin = (ROOT / ".sd-pack-rev").read_text(encoding="utf-8").strip()
    pack = pathlib.Path(os.environ.get("SD_ACCEPTANCE_PACK") or ROOT / ".ci" / "pack")
    remedy = ("run `make test SUITE=<any suite>` or `make check`, which fetch the pack at the pin into .ci/pack "
              "and run this in the preflight, or set SD_ACCEPTANCE_PACK")
    try:
        head = (pack / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError as error:
        raise AssertionError(f"no pinned pack copy at {pack} ({error.strerror}): {remedy}") from None
    if head != pin:
        raise AssertionError(f"{pack} is at {head}, not the pin {pin} in .sd-pack-rev: {remedy}")
    return pack


class GapVocabulary(unittest.TestCase):
    def one_side_only(self, ours: set[str], where: str) -> None:
        pack = set(assigned(pinned_pack() / "bin" / "sd_lib.py", "ACKNOWLEDGEABLE_GAPS")) - {STATUS_GAP}
        self.assertEqual((sorted(pack - ours), sorted(ours - pack)), ([], []),
                         f"(pack only, {where} only) gap ids: the pack's ACKNOWLEDGEABLE_GAPS owns them")

    def test_the_collector_s_gap_ids_are_the_pack_s(self) -> None:
        self.one_side_only(set(assigned(pinned_pack() / "lib" / "sd_db" / "protection.py", "GAP_IDS")), "sd_db.protection.GAP_IDS")


class PinnedPack(unittest.TestCase):
    def test_a_missing_copy_fails_naming_the_one_suite_command(self) -> None:
        # A4 F12, sd:3555: a hand run without .ci/pack fails rather than read
        # the live pack (sd:3534); the message names the command that fetches the pin.
        with tempfile.TemporaryDirectory() as empty, mock.patch.dict(os.environ, {"SD_ACCEPTANCE_PACK": empty}):
            with self.assertRaises(AssertionError) as caught:
                pinned_pack()
        self.assertIn("make test SUITE=", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
