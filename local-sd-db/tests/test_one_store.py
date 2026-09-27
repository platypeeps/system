"""One store, one caller: nothing outside this library opens the database.

Requirement 2 and criterion 2. This pull request **establishes** the
invariant in this repository and does not close the criterion: the criterion
also covers the pack and the dashboard, and the pack still has one caller of
its own, `dashboard/store.py`, which goes when its `dashboard` package
retires two slices from here. What is asserted here is the half this
repository can answer for.
"""

import re
import subprocess
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE_ROOT.parent

#: Where the library lives. Everything under it may open the database.
LIBRARY = "local-sd-db/sd_db/"

#: This file quotes the patterns it searches for.
SELF = "local-sd-db/tests/test_one_store.py"

#: Where a table may be created that no migration creates. Both are fixtures:
#: a test proves that `restore` refuses a store with a table the schema does
#: not have, and it cannot prove a refusal it cannot build. `tests/` is this
#: suite's fixtures; `sd_db/testing/` is the same fixtures shipped, because
#: `system`'s suites drive `restore` too and a store fixture *is* a database
#: -- building one out there would open one outside the library, which is the
#: rule the first test holds. Neither path is the schema, and the schema is
#: what this rule is about.
FIXTURES = ("local-sd-db/tests/", "local-sd-db/sd_db/testing/")

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
    def test_sqlite3_connect_appears_only_in_the_library(self):
        hits = grep(r"sqlite3\.connect")
        self.assertTrue(hits, "the grep found no callers at all, so it proves nothing")
        outside = [
            line for line in hits
            if not line.startswith(LIBRARY) and not line.startswith("local-sd-db/tests/")
            and not foreign(line)
        ]
        self.assertEqual(outside, [], "something outside the library opens the database")

    def test_the_tests_that_do_open_it_are_the_library_s_own(self):
        """A test may read the file raw -- to check a database the library
        would refuse to open. Those tests are here, beside the library, and
        nowhere else."""
        hits = [line for line in grep(r"sqlite3\.connect") if line.startswith("local-sd-db/tests/")]
        for line in hits:
            self.assertTrue(line.startswith("local-sd-db/tests/"), line)

    def test_no_create_table_outside_the_migration_files(self):
        """The schema grows by migration file, not by a string somewhere."""
        hits = grep(r"CREATE TABLE")
        outside = [
            line for line in hits
            if not line.startswith("local-sd-db/sd_db/schema/")
            and not line.startswith(FIXTURES)
            and not foreign(line)
        ]
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
