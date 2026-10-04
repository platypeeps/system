"""The protection table draws one column per flag id `sd_db` writes (sd:2560).

`sd_db.protection` writes each repository's `merge_settings` with the ids in
`MERGE_FLAG_IDS` and `BASELINE_FLAG_IDS`; `FLAGS` in `protection_screen.py`
restates them, one column each. Nothing held the two together, so a flag added
to the collector only drew no column, and a column whose id the collector
dropped read as not applicable on every row. The pack's half binds
`MERGE_FLAG_IDS` to what `sd-status` reports (sd:1372); this binds the columns.
"""

import unittest

from sd_dashboard.protection_screen import FLAGS
from sd_db import protection


class FlagVocabulary(unittest.TestCase):
    def test_the_table_draws_a_column_for_each_flag_the_collector_writes(self) -> None:
        written = set(protection.MERGE_FLAG_IDS) | set(protection.BASELINE_FLAG_IDS)
        drawn = {flag for flag, _ in FLAGS}
        self.assertEqual((sorted(written - drawn), sorted(drawn - written)), ([], []),
                         "(sd_db only, protection_screen.FLAGS only) flag ids")

    def test_merge_and_baseline_flags_share_no_id(self) -> None:
        self.assertEqual(sorted(set(protection.MERGE_FLAG_IDS) & set(protection.BASELINE_FLAG_IDS)), [])
