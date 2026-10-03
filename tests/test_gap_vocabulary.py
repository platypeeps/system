"""The system's protection gap ids are the pack's, read from the pinned pack (sd:1372).

The pack owns the gap vocabulary: `sd-status` emits the ids, and the
pack's `sd_lib` module accepts them as `ACKNOWLEDGEABLE_GAPS`. Two places here restate
them: `GAP_IDS` in `local-sd-db/sd_db/protection.py`, which the nightly
collector writes, and `GAPS` in the dashboard's `protection_screen.py`, which
draws one column per id. Nothing held the three together, so an id added on one
side only failed nothing, and the dashboard drew no column for it.

The pack side is enumerated, not restated: the test reads the pack copy that
`tests/check.sh` fetches into `.ci/pack` at the SHA in `.sd-pack-rev`, or the
one `SD_ACCEPTANCE_PACK` names. A missing or off-pin copy fails loudly; a skip
would fail the check anyway, and it would hide the one comparison this exists
for. `unprotected` is the system's status column, not a gap cell, as the pack's
half of this binding (its `GapVocabularyTests`, landed in pack PR #1305)
also reads it.

Stdlib only, read with `ast` so nothing is imported: this runs in the
`tests/ci-native.sh` preflight, before the virtualenv exists.
`python3 tests/test_gap_vocabulary.py` from the repository root runs it.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROTECTION = ROOT / "local-sd-db" / "sd_db" / "protection.py"
SCREEN = ROOT / "local-project-dashboard" / "sd_dashboard" / "protection_screen.py"
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
    remedy = "run `make check`, which fetches the pack at the pin into .ci/pack, or set SD_ACCEPTANCE_PACK"
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
        self.one_side_only(set(assigned(PROTECTION, "GAP_IDS")), "sd_db.protection.GAP_IDS")

    def test_the_dashboard_draws_a_column_for_each_of_the_pack_s_gap_ids(self) -> None:
        self.one_side_only({gap for gap, _ in assigned(SCREEN, "GAPS")}, "protection_screen.GAPS")


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
