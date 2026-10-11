"""One store, one caller: nothing in this repository opens the sd database.

The library lives in the command pack (`sd_db` under its `lib/`), so no path
here may open the database or create a table: callers run the pack's
`bin/sd-db`. What is asserted here is the half this repository can answer for.
Stdlib only, so it runs in the `tests/ci-native.sh` preflight.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: This file quotes the patterns it searches for.
SELF = "tests/test_one_store.py"

#: Files that open a database which is not this store. They open claude-mem's
#: own ~/.claude-mem/claude-mem.db, a third-party file, to prune its tool_uses
#: table (sd:1592), and the test builds a scratch copy of that file. Exact
#: paths, never a folder, and a guard below holds that neither names the sd
#: store, so this cannot become a way around the rule.
FOREIGN_STORES = ("local-claude/claude.sh", "local-claude/tests/test_prune_mem_logs.py")

#: What naming the sd store looks like: the file, the package, the wrapper,
#: or the directory it lives in.
NAMES_THE_STORE = re.compile(r"sd[._-]db|share/sd\b")


def foreign(line):
    """A grep hit ("path:line:text") in one of the FOREIGN_STORES."""
    return line.split(":", 1)[0] in FOREIGN_STORES


def grep(pattern):
    """Code, not prose. The planning documents quote these strings by the
    dozen, and a criterion about what opens the database is not a criterion
    about what discusses it."""
    completed = subprocess.run(
        ["git", "grep", "-nIE", "--untracked", pattern, "--", ".",
         ":(exclude)*.md", ":(exclude)docs/", f":(exclude){SELF}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if completed.returncode not in (0, 1):
        raise AssertionError(completed.stderr)
    return [line for line in completed.stdout.splitlines() if line]


class TheOnlyStore(unittest.TestCase):
    def test_sqlite3_connect_appears_only_in_the_foreign_stores(self):
        hits = grep(r"sqlite3\.connect")
        self.assertTrue(hits, "the grep found no callers at all, so it proves nothing")
        outside = [line for line in hits if not foreign(line)]
        self.assertEqual(outside, [], "something here opens the database")

    def test_no_create_table_outside_the_foreign_stores(self):
        """The schema grows by migration file in the pack, not by a string here."""
        outside = [line for line in grep(r"CREATE TABLE") if not foreign(line)]
        self.assertEqual(outside, [])

    def test_the_foreign_stores_do_not_touch_this_one(self):
        """The exemption covers claude-mem's database only. A file on the
        list that names the sd store, or that no longer opens any database,
        comes off the list rather than keeping a hole open."""
        opens = {line.split(":", 1)[0] for line in grep(r"sqlite3\.connect")}
        for path in FOREIGN_STORES:
            text = (REPO_ROOT / path).read_text()
            self.assertIsNone(NAMES_THE_STORE.search(text),
                              "%s names the sd store but is exempt as a foreign one" % path)
            self.assertIn(path, opens, "%s opens no database; drop it from FOREIGN_STORES" % path)


if __name__ == "__main__":
    unittest.main()
