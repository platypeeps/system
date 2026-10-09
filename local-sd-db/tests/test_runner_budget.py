"""A `budget_usd` at creation: refused on a `start` author naming the entry,
accepted on a `url` one. sd:235 criterion 4's last clause, the creation half.

The ledger (`set_budget`) already refuses a budget on a row whose author is a
`start` entry; `enqueue` is where the row is made, and a budget given there
has no row to be set on afterwards. So `enqueue` applies the same rule before
it writes anything, against the entry the runner will resolve for the role.
The registry is `test_ledger.py`'s, read from beside the fixture database.
"""
import tempfile
import unittest
from pathlib import Path

from sd_db import runner, seed
from sd_db.database import connect, transaction
from sd_db.ledger import LedgerRefused
from sd_db.migrate import initialise
from sd_db.registry import parse
from sd_db.writes import create_item, upsert_repo

from .test_ledger import REGISTRY

#: The `start` entry first in the author list: the runner would run it.
SPAWNED_AUTHOR = REGISTRY.replace("  author:   [kimi, mini, claude]", "  author:   [claude, kimi, mini]")
#: Every author a `url` entry: the only order the ledger can stand in front of.
CALLED_AUTHORS = REGISTRY.replace("  author:   [kimi, mini, claude]", "  author:   [kimi, mini]")


class BudgetAtCreation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / "sd.db"
        initialise(self.database)
        self.db = connect(self.database)
        self.addCleanup(self.db.close)
        self.registry_path = self.root / "providers.yaml"
        self.registry(REGISTRY)
        upsert_repo(self.db, "/fixture/repo", remote="/fixture/remote")
        self.items = [create_item(self.db, kind="task", title=f"Task {n}", repo="/fixture/repo",
                                  branch=f"work/{n}", status="ready") for n in range(2)]

    def registry(self, text):
        """The file, and the rows seeded from it afresh: `order` ranks from
        the rows where they exist, so a re-ordered file alone changes nothing."""
        self.registry_path.write_text(text, encoding="utf-8")
        with transaction(self.db):
            self.db.execute("DELETE FROM provider")
            self.db.execute("DELETE FROM bill")
        seed(self.db, parse(text, "providers.yaml"))

    def budgets(self):
        return [row["budget_usd"] for row in self.db.execute("SELECT budget_usd FROM assignment ORDER BY id")]

    def test_a_budget_on_a_start_author_is_refused_at_creation_naming_the_entry(self):
        self.registry(SPAWNED_AUTHOR)
        with self.assertRaises(LedgerRefused) as raised:
            runner.enqueue(self.db, self.items, budget_usd=5.0, who="operator")
        message = str(raised.exception)
        self.assertIn("author 'claude' is a 'start' entry", message)
        self.assertIn(f"the selection of items {self.items[0]}, {self.items[1]}", message)
        # The whole selection is refused, nothing written: no row and no note.
        self.assertEqual(self.budgets(), [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM note WHERE kind = 'comment'").fetchone()[0], 0)
        # A `start` entry behind two `url` entries is the one the runner
        # would run, since it skips what it cannot execute (#415's review).
        self.registry(REGISTRY)
        with self.assertRaisesRegex(LedgerRefused, "author 'claude' is a 'start' entry"):
            runner.enqueue(self.db, self.items[:1], budget_usd=5.0, who="operator")
        # A `start` reviewer refuses a reviewer row the same way, and an
        # author row too, as `set_budget` does.
        self.registry(CALLED_AUTHORS.replace("  reviewer: [mini, kimi]", "  reviewer: [claude, kimi]"))
        for role, named in (("reviewer", "reviewer 'claude'"), ("author", "reviewer 'claude'")):
            with self.subTest(role=role):
                with self.assertRaisesRegex(LedgerRefused, named + " is a 'start' entry"):
                    runner.enqueue(self.db, self.items[:1], role=role, budget_usd=5.0, who="operator")
        # A registry that orders no author has nothing to stand in front of.
        self.registry(REGISTRY.replace("  author:   [kimi, mini, claude]", "  author:   []"))
        with self.assertRaisesRegex(LedgerRefused, "orders no author"):
            runner.enqueue(self.db, self.items[:1], budget_usd=5.0, who="operator")
        # An `exec` or a `merge` row calls no provider.
        self.registry(CALLED_AUTHORS)
        for role, scope in (("exec", "palette:{}"), ("merge", "item")):
            with self.subTest(role=role):
                with self.assertRaisesRegex(LedgerRefused, f"a {role!r} row calls no provider"):
                    runner.enqueue(self.db, self.items[:1], role=role, scope=scope, budget_usd=5.0, who="operator")
        # The money rule is the ledger's: a `nan` budget is NULL to SQLite,
        # and an int too large for a float is not finite either.
        for bad in (float("nan"), float("inf"), "5", True, -1.0, 10 ** 400):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(LedgerRefused, "finite, non-negative number"):
                    runner.enqueue(self.db, self.items[:1], budget_usd=bad, who="operator")
        self.assertEqual(self.budgets(), [])

    def test_no_budget_writes_null_and_a_budget_on_a_url_author_is_stored(self):
        # Every author and every reviewer a `url` entry, so the ledger can
        # stand in front of whichever the runner takes.
        self.registry(CALLED_AUTHORS)
        runner.enqueue(self.db, self.items[:1], who="operator")
        accepted = runner.enqueue(self.db, self.items[1:], budget_usd=7.5, who="operator")
        self.assertEqual(self.budgets(), [None, 7.5])
        self.assertEqual(accepted[0]["budget_usd"], 7.5)
        note = self.db.execute("SELECT body FROM note WHERE kind = 'comment' AND item = ?", (self.items[1],)).fetchone()
        self.assertIn("budget 7.50 USD", note["body"])
        # An integer bound is stored as a float, and an empty field is None.
        third = create_item(self.db, kind="task", title="Task 3", repo="/fixture/repo", branch="work/3", status="ready")
        fourth = create_item(self.db, kind="task", title="Task 4", repo="/fixture/repo", branch="work/4", status="ready")
        result = runner.enqueue(self.db, [third], budget_usd=2, who="operator")
        self.assertEqual(result[0]["budget_usd"], 2.0)
        result = runner.enqueue(self.db, [fourth], budget_usd=None, who="operator")
        self.assertIsNone(result[0]["budget_usd"])


if __name__ == "__main__":
    unittest.main()
