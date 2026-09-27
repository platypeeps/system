"""This folder no longer describes the standing set by what it does not read.

`runner_exec.standing_refusal` is asked once per repository because its
refusals come out alike for every row. For a year `sd_plan.py` said instead
that they were the refusals `prepare` makes without reading the item, and
that criterion separates nothing: every refusal in `_values` that the set
leaves out reads no item row either (sd:820). The pin on the definition lives
in `local-sd-db` and cannot reach this folder, so this is the pin here.

It matches prose, not lines. The old sentence was split across two comment
lines, with the `#` of the second between "the" and "item", and a line grep
for the phrase found nothing. So the pattern allows any run of non-word
characters between the words, and ignores case.
"""

import re
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
LOOSE = re.compile(r"without\W+reading\W+the\W+item", re.IGNORECASE)


class StandingCriterion(unittest.TestCase):
    def test_the_loose_criterion_is_not_restated(self):
        """Neither `sd_plan.py` nor the README carries it, wrapped or not."""
        for name in ("sd_plan.py", "README.md"):
            with self.subTest(file=name):
                match = LOOSE.search((FOLDER / name).read_text())
                self.assertIsNone(
                    match,
                    f"{name} describes the standing set as refusals made "
                    "without reading the item; it is the refusals that come "
                    "out alike for every row (sd:820)",
                )


if __name__ == "__main__":
    unittest.main()
