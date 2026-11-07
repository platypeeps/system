"""Every instance a convention lists names that convention (sd:8023).

A convention in `docs/conventions/` ends with an Instances table. Its
checklist asks each instance's README to name the convention and say only
where it differs, so the next builder starts from the convention, not from a
copy of one instance. A row whose first cell is a folder here must be a
folder, and its README must name `docs/conventions/<file>`; a row naming
something outside this repo is skipped. Stdlib only, so it runs in the
`tests/ci-native.sh` preflight.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: A hyphenated folder name in backticks, alone in the first cell. Generic on
#: purpose: tests/test_product_name.py fails a vendor prefix named here.
FOLDER_CELL = re.compile(r"^\|\s*`([a-z0-9]+(?:-[a-z0-9]+)+)`\s*\|", re.M)


def instances(text):
    """The folder names in the `## Instances` table of one convention."""
    _, _, section = text.partition("\n## Instances\n")
    section = section.split("\n## ", 1)[0]
    return FOLDER_CELL.findall(section)


def unlinked(root):
    """Each (convention, folder, problem) the Instances tables break."""
    found = []
    for doc in sorted((root / "docs/conventions").glob("*.md")):
        ref = f"docs/conventions/{doc.name}"
        for folder in instances(doc.read_text(encoding="utf-8")):
            readme = root / folder / "README.md"
            if not readme.is_file():
                found.append((ref, folder, "no README.md"))
            elif ref not in readme.read_text(encoding="utf-8"):
                found.append((ref, folder, f"README does not name {ref}"))
    return found


class InstancesNameTheirConvention(unittest.TestCase):
    def test_every_listed_instance_names_its_convention(self):
        found = unlinked(ROOT)
        self.assertEqual(found, [], "\n".join(" ".join(f) for f in found))

    def test_the_file_intake_table_is_read(self):
        text = (ROOT / "docs/conventions/file-intake.md").read_text(encoding="utf-8")
        self.assertIn("local-mail-intake", instances(text))

    def test_a_row_outside_this_repo_is_skipped(self):
        text = "x\n## Instances\n\n| a | b |\n|---|---|\n| an importer | c |\n"
        self.assertEqual(instances(text), [])

    def test_the_preflight_runs_this_file(self):
        lines = (ROOT / "tests/ci-native.sh").read_text(encoding="utf-8").splitlines()
        self.assertIn("python3 tests/test_conventions.py", lines,
                      "the tests/ci-native.sh preflight no longer runs this file")


if __name__ == "__main__":
    unittest.main()
