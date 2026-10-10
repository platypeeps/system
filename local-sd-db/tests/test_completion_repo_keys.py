"""Completion receipts and migration 014's repository keys (sd:1450).

014 rewrote `item.repo` to the `~/` key and left `item.fields` as written,
so a completion receipt from before it names the absolute path of the same
repository. `progress.completion_record` compares both sides in the keyed
form: that receipt still binds, a receipt for another repository still does
not, and nothing stored is rewritten. New receipts carry the key.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import connect, create_item, upsert_repo
from sd_db.migrate import initialise
from sd_db.progress import cancel_work, completion_record, deliver_work
from sd_db.workflow import item_state

KEY = "~/repos/x"


class CompletionRepoKeys(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sd1450-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "home"
        self.repo = self.home / "repos" / "x"
        self.repo.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = Path(self.temp.name) / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        upsert_repo(self.db, KEY)
        self.item = create_item(self.db, kind="work", title="Keyed work", repo=KEY,
                                path="docs/work/keyed/prd.md", source="docs/work",
                                external_id=f"{KEY}::docs/work/keyed/prd.md")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], text=True,
                              capture_output=True, check=True).stdout.strip()

    def receipt(self):
        return json.loads(item_state(self.db, self.item)["item"]["fields"])["completion"]

    def rewrite_receipt_repo(self, value):
        """The receipt as a writer before 014 left it: fields untouched by 014."""
        self.db.execute("UPDATE item SET fields = json_set(fields, '$.completion.repo', ?) WHERE id = ?",
                        (value, self.item))
        self.db.commit()
        return item_state(self.db, self.item)["item"]

    def test_the_row_holds_the_key(self):
        self.assertEqual(item_state(self.db, self.item)["item"]["repo"], KEY)

    def test_a_new_cancellation_receipt_carries_the_key(self):
        cancel_work(self.db, self.item, reason="Dropped", who="operator")
        self.assertEqual(self.receipt()["repo"], KEY)

    def test_a_pre_014_cancellation_receipt_still_binds(self):
        cancel_work(self.db, self.item, reason="Dropped", who="operator")
        absolute = str(self.repo)
        row = self.rewrite_receipt_repo(absolute)
        self.assertEqual(json.loads(row["fields"])["completion"]["repo"], absolute)
        record = completion_record(row)
        self.assertIsNotNone(record, "a receipt naming $HOME/repos/x binds to the row keyed ~/repos/x")
        self.assertEqual(record["outcome"], "cancelled")
        self.assertEqual(record["repo"], absolute, "the stored receipt is returned as written")
        # The caller that reads the receipt: a repeated cancel is idempotent,
        # not refused as a reclassification.
        before = item_state(self.db, self.item)
        self.assertEqual(cancel_work(self.db, self.item, reason="Dropped", who="operator"), before)
        self.assertEqual(self.receipt()["repo"], absolute, "nothing is rewritten in place")

    def test_a_receipt_for_another_repository_stays_invalid(self):
        cancel_work(self.db, self.item, reason="Dropped", who="operator")
        for other in (str(self.home / "repos" / "y"), "~/repos/y", "/srv/repos/x", str(self.repo) + "x"):
            with self.subTest(other=other):
                self.assertIsNone(completion_record(self.rewrite_receipt_repo(other)))

    def test_without_home_a_pre_014_receipt_fails_closed(self):
        cancel_work(self.db, self.item, reason="Dropped", who="operator")
        keyed = item_state(self.db, self.item)["item"]
        row = self.rewrite_receipt_repo(str(self.repo))
        with mock.patch.dict(os.environ, {"HOME": ""}):
            self.assertIsNone(completion_record(row))
            self.assertIsNotNone(completion_record(keyed), "a keyed receipt needs no conversion")

    def test_a_delivery_receipt_carries_the_key_and_a_pre_014_one_binds(self):
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Workflow test")
        self.git("config", "user.email", "workflow@example.test")
        self.git("commit", "--allow-empty", "-m", f"A slice\n\nDelivers: sd:{self.item}")
        commit = self.git("rev-parse", "HEAD")
        deliver_work(self.db, self.item, commit, who="operator")
        self.assertEqual(self.receipt()["repo"], KEY)
        row = self.rewrite_receipt_repo(str(self.repo))
        self.assertEqual(completion_record(row)["outcome"], "delivered")
        before = item_state(self.db, self.item)
        self.assertEqual(deliver_work(self.db, self.item, commit, who="operator"), before)


if __name__ == "__main__":
    unittest.main()
