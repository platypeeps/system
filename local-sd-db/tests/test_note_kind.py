"""sd:3008: a note the library generates is a comment, not a decision.

Half the `decision` rows were templates a verb wrote as its audit line --
"Assignment 4 queued by ...", a gate verdict, a cancelled assignment -- and
they buried the decisions a person or a lead wrote. The writers now write
`comment`; the old rows stay as they are, since no code reads the kind. A
decision someone files on purpose (`workflow.add_item_note`, a session's own
notes, the dashboard's ruling) keeps its kind.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "sd_db"


def literal_decision_writers() -> list[str]:
    """Every `add_note(...)` in the library that names the kind `decision`."""
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "add_note"):
                continue
            kinds = node.args[2:3] + [k.value for k in node.keywords if k.arg == "kind"]
            if any(isinstance(kind, ast.Constant) and kind.value == "decision" for kind in kinds):
                found.append(f"{path.relative_to(PACKAGE.parent)}:{node.lineno}")
    return found


class TheWriters(unittest.TestCase):
    def test_no_library_writer_files_its_own_note_as_a_decision(self):
        """A literal `decision` is a template the code wrote, never a person."""
        self.assertEqual(literal_decision_writers(), [])


if __name__ == "__main__":
    unittest.main()
