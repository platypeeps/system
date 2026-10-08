"""Criterion 7's clause at `prd.md:1303-1307`, whole.

    "A test changes an item's status three times and asserts three
    `status_change` notes in order, each with old, new and time, that the item
    screen lists them, and that a status write outside the library's one
    function is not possible, by grepping the repository for a second
    `UPDATE item SET status`."

Three assertions and a grep. The grep is the half that is easy to skip and is
the whole point: the notes prove that *this* path writes its history, and the
grep proves there is no other path.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

from sd_db import reads, transition

from support import ScreenCase

#: The repository root: this file is `<root>/local-project-dashboard/tests/`.
ROOT = Path(__file__).resolve().parents[2]

#: The one function allowed to write the column, and the file it lives in.
THE_ONE_WRITER = Path("local-sd-db/sd_db/writes.py")


class ThreeStatusChanges(ScreenCase):
    def setUp(self) -> None:
        super().setUp()
        self.id = self.item("a work item", status="planning")
        for target in ("ready", "in_progress", "blocked"):
            transition(self.connection, self.id, target, who="a test")

    def test_three_notes_in_order_each_with_old_new_and_time(self):
        """Three changes, three notes, and the opening one that is not a change.

        `create_item` writes a `status_change` of its own -- "opened as
        planning" -- so the history starts at the first status rather than at
        the second. That is four rows for three changes, and the criterion's
        three are the transitions.
        """
        notes = reads.status_changes(self.connection, self.id)
        self.assertEqual(notes[0]["body"], "opened as planning")

        changes = notes[1:]
        self.assertEqual(len(changes), 3)
        self.assertEqual(
            [note["body"] for note in changes],
            [
                "planning -> ready by a test",
                "ready -> in_progress by a test",
                "in_progress -> blocked by a test",
            ],
        )
        for note in changes:
            old, new = re.match(r"(\w+) -> (\w+) by ", note["body"]).groups()
            self.assertIn(old, ("planning", "ready", "in_progress"))
            self.assertIn(new, ("ready", "in_progress", "blocked"))
            self.assertRegex(note["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

        stamps = [note["timestamp"] for note in notes]
        self.assertEqual(stamps, sorted(stamps), "the notes come back in order")
        ids = [note["id"] for note in notes]
        self.assertEqual(ids, sorted(ids), "and the order is the order they were written")

    def test_the_item_screen_lists_them(self):
        page = self.render(f"/item/{self.id}")
        self.assertIn("Status history", page)
        for body in (
            "planning → ready by a test",
            "ready → in progress by a test",
            "in progress → blocked by a test",
        ):
            self.assertIn(body, page)
        # In order on the page too, not merely present.
        positions = [
            page.index("planning → ready"),
            page.index("ready → in progress"),
            page.index("in progress → blocked"),
        ]
        self.assertEqual(positions, sorted(positions))
        # Each entry carries its time, which is what makes the list a history.
        history = re.search(r'<ol class="history">(.*?)</ol>', page).group(1)
        self.assertEqual(history.count("<time datetime="), 4)

    def test_no_second_writer_of_item_status(self):
        """The grep the criterion names, over the tracked files of the repository.

        `git grep` rather than a walk of the filesystem: an untracked scratch
        file is not the repository, and a build directory would make this
        assertion fail for a reason that has nothing to do with the codebase.

        Which is why there is **no `--untracked`** here. The first version of
        this test wrote the paragraph above and then passed the flag, doing
        the exact opposite of what the paragraph argues for.
        """
        found = subprocess.run(
            ["git", "-C", str(ROOT), "grep", "-n", "-I",
             "-e", "UPDATE item SET status",
             # Code, not prose. `prd.md` and `implement.md` quote the pattern
             # in the sentence that asks for this grep; neither writes a row.
             "--", "*.py", "*.sql", "*.sh", "*.rs", "*.js"],
            capture_output=True, text=True,
        )
        # `git grep` exits 1 when it matches nothing, which is not a failure.
        self.assertIn(found.returncode, (0, 1), found.stderr)
        hits = [line for line in found.stdout.splitlines() if line.strip()]
        outside = [
            line for line in hits
            if not line.startswith(f"{THE_ONE_WRITER}:")
            and ":tests/" not in line
            and "/tests/" not in line.split(":", 1)[0]
        ]
        self.assertEqual(
            outside, [],
            "`item.status` is written by `sd_db.writes.transition` and by nothing "
            "else; these lines are a second writer",
        )
        self.assertTrue(
            any(line.startswith(f"{THE_ONE_WRITER}:") for line in hits),
            "the one writer is still there -- a grep that finds nothing at all "
            "is a grep whose pattern has stopped matching",
        )
        self.assertEqual(
            len([line for line in hits if line.startswith(f"{THE_ONE_WRITER}:")]),
            1,
            "and there is exactly one of it inside that file",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
