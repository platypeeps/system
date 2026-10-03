"""Only the `mezmo-*` folders name the product they are helpers for (sd:2535).

CLAUDE.md states the rule, and nothing checked it: a generic tool that cited
the vendor as "one documented example" read as fine to every reviewer, and
six such lines sat in two `local-*` folders when this landed. So this scans
every tracked file outside a `mezmo-*` folder, case-insensitively on a word
boundary, and fails naming each path and line that names the product.

A folder reference is not a product reference. `mezmo-*`, the bare prefix
and the name of a tracked `mezmo-*` folder (`mezmo-pipeline/pipeline.sh`)
point at the folders the rule permits, so they are struck out before the
search. The folder names are read from `git ls-files`, not listed here, so a
new helper folder is a reference the day it is tracked. The strike is
case-sensitive: `Mezmo-style` names the product, `mezmo-pipeline` names a
folder.

`ALLOWED` is the short list of places that may name it, each with the reason
a reader can check. Every entry must still match a file with a hit, so the
list only shrinks when a hit goes away.

Stdlib and git only. This runs in the `tests/ci-native.sh` preflight beside
`tests/test_citations.py`, before the virtualenv exists, once per
`make check`. `python3 tests/test_product_name.py` from the repository root
is the whole invocation, and the preflight's unwired-suite guard fails when
`tests/ci-native.sh` stops naming it.
"""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PREFIX = "mezmo-"

#: Where the product may be named, and why. A key ending in `/` covers a
#: folder; any other key is one file.
ALLOWED = {
    "CLAUDE.md": "states the rule, so it names the product the rule is about",
    "README.md": "its folder table tells a reader what the mezmo-* folders hold",
    "docs/work/archive/": "delivered records that are never edited; the citation gate exempts them too",
    "local-aura/": (
        "wraps the vendor's aura and its hosted MCP server; a move to a mezmo- "
        "folder also moves the operator's config folder, so it is a follow-up"
    ),
    "tests/test_product_name.py": "spells the word it searches for",
}

PRODUCT = re.compile(r"\b" + PREFIX[:-1] + r"\b", re.IGNORECASE)


def tracked(root=ROOT):
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True,
    )
    return [p for p in out.stdout.split("\0") if p]


def folders(paths):
    """The tracked top-level `mezmo-*` folders, by name."""
    return sorted({p.split("/", 1)[0] for p in paths if p.startswith(PREFIX) and "/" in p})


def reference(names):
    """A folder reference: the glob, the bare prefix, or a tracked folder's name."""
    named = "|".join(re.escape(n[len(PREFIX):]) for n in names)
    tail = r"[*>`]" + (rf"|(?:{named})\b" if named else "")
    return re.compile(rf"\b{re.escape(PREFIX)}(?:{tail})")


def allowed(path):
    return any(path.startswith(k) if k.endswith("/") else path == k for k in ALLOWED)


def text_of(root, path):
    """A tracked regular file inside the checkout as text, or None."""
    full = (root / path)
    try:
        real = full.resolve()
        real.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if full.is_symlink() or not real.is_file():
        return None
    data = real.read_bytes()
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


def hits(root, paths, honour_allowed=True):
    """(path, line number, line) for each line outside a `mezmo-*` folder that names the product.

    Line 0 is the path itself.
    """
    strike = reference(folders(paths))
    found = []
    for path in paths:
        if path.startswith(PREFIX) or (honour_allowed and allowed(path)):
            continue
        if PRODUCT.search(strike.sub("", path)):
            found.append((path, 0, path))
        text = text_of(root, path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if PRODUCT.search(strike.sub("", line)):
                found.append((path, number, line.strip()))
    return found


def report(found):
    return "\n  ".join(f"{p}:{n}: {line[:160]}" for p, n, line in found)


class TheTree(unittest.TestCase):
    def test_no_tracked_file_outside_the_vendor_folders_names_the_product(self):
        found = hits(ROOT, tracked())
        if found:
            self.fail(
                "these name the product outside a mezmo-* folder; move the text into "
                "one, say it generically, or add an ALLOWED entry with its reason "
                "(tests/test_product_name.py):\n  " + report(found)
            )

    def test_every_allowed_entry_has_a_reason_and_is_still_needed(self):
        paths = tracked()
        found = hits(ROOT, paths, honour_allowed=False)
        for key, reason in ALLOWED.items():
            with self.subTest(key=key):
                self.assertTrue(reason.strip(), f"{key}: an entry needs its reason")
                covered = [p for p, _, _ in found
                           if (p.startswith(key) if key.endswith("/") else p == key)]
                self.assertTrue(covered, f"{key}: no longer names the product; delete the entry")

    def test_the_preflight_runs_this_file(self):
        lines = (ROOT / "tests/ci-native.sh").read_text(encoding="utf-8").splitlines()
        self.assertIn(
            "python3 tests/test_product_name.py", lines,
            "the tests/ci-native.sh preflight no longer runs this file",
        )


class TheScan(unittest.TestCase):
    def scan(self, files):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for rel, body in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                if isinstance(body, bytes):
                    (root / rel).write_bytes(body)
                else:
                    (root / rel).write_text(body, encoding="utf-8")
            return hits(root, sorted(files))

    def test_a_file_outside_the_vendor_folders_that_names_the_product_fails(self):
        found = self.scan({"local-x/README.md": "Intro.\nOne example is Mezmo: its URL.\n"})
        self.assertEqual([("local-x/README.md", 2, "One example is Mezmo: its URL.")], found)

    def test_the_match_ignores_case_and_needs_a_word_boundary(self):
        found = self.scan({"local-x/a.sh": "# MEZMO\nx=mezmo.example.test\nMEZMO_KEY=1\nmezmoish\n"})
        self.assertEqual([1, 2], [n for _, n, _ in found])

    def test_a_vendor_folder_may_name_it(self):
        self.assertEqual([], self.scan({"mezmo-pipeline/README.md": "Mezmo pipeline API\n"}))

    def test_a_folder_reference_is_not_a_product_reference(self):
        found = self.scan({
            "mezmo-pipeline/pipeline.sh": "",
            "local-x/README.md": (
                "Run `mezmo-pipeline/pipeline.sh`.\n"
                "A `mezmo-*` folder keeps its full name.\n"
                "Named after the folder minus `local-`/`mezmo-`.\n"
                "Strip <folder minus local-/mezmo->.\n"
                "Use Mezmo-style ingestion.\n"
                "See mezmo-gone/README.md.\n"
            ),
        })
        self.assertEqual([5, 6], [n for _, n, _ in found])

    def test_a_path_that_names_the_product_fails(self):
        found = self.scan({"docs/mezmo-notes.md": "nothing here\n"})
        self.assertEqual([("docs/mezmo-notes.md", 0, "docs/mezmo-notes.md")], found)

    def test_an_allowed_file_is_skipped_and_a_binary_is_not_read(self):
        self.assertEqual([], self.scan({"CLAUDE.md": "Mezmo\n", "local-x/a.bin": b"\0Mezmo"}))


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
