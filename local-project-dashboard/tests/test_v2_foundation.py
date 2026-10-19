"""The dashboard's copies of the ui-design foundation stay the foundation's files (sd:3588).

`foundation/README.md` § Apply it: copy `tokens.css` unchanged and load `theme.js`; a new token goes into the
foundation through a pull request, never into a copy. Each copy in `v2/static/` is pinned here by the SHA-256 of the
foundation file at FOUNDATION, so an edit to a copy fails. To take a foundation change, copy the files again, then
move FOUNDATION and each digest together; `git -C <ui-design> diff <FOUNDATION> -- foundation/` shows what moved.
"""

from __future__ import annotations

import base64
import hashlib
import re
import unittest

from sd_dashboard import documents
from test_v2_tasks import V2

STATIC = V2 / "static"

#: The ui-design commit the copies below came from.
FOUNDATION = "279dcb137cc86da497339d2c95e70b3d4e422758"

#: Copy in v2/static -> (foundation path, SHA-256 of that file at FOUNDATION).
COPIES = {
    "tokens.css": ("foundation/tokens.css", "6de06baf1da8dddbce8434b1db3591f935778764cb017aca7f1028139bff540a"),
    "theme.js": ("foundation/theme.js", "3d4cf4fc4b54c168212325d12c5d60b5487feee78329131c9e4ea17d029b3a24"),
    "annunciator-strip.css": ("foundation/components/annunciator-strip.css",
                              "5e85672762ea8b596291f5e2389756bddf3baa466a733ce7db3cf3528c7c5784"),
    "annunciator-strip.js": ("foundation/components/annunciator-strip.js",
                             "9e76cb8d13ea602e32e960abf33e30fc50dd982994463da88b552092721f2637"),
}

DECLARED = re.compile(r"(?:^|[{;])\s*(--[a-z0-9-]+)\s*:", re.M)


class FoundationCopies(unittest.TestCase):
    def test_each_copy_is_the_foundation_file(self):
        for name, (source, pinned) in COPIES.items():
            with self.subTest(name=name):
                found = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()
                self.assertEqual(found, pinned,
                                 f"v2/static/{name} drifted from ui-design {source} at {FOUNDATION[:7]}: "
                                 "change the foundation, then copy the file and move the pin")

    def test_the_documents_policy_pins_the_same_theme_script(self):
        # Research and design pages inline theme.js under a digest; it is the file the v2 pages load.
        digest = base64.b64encode(hashlib.sha256((STATIC / "theme.js").read_bytes()).digest()).decode()
        self.assertEqual(documents.THEME_SCRIPT, f"sha256-{digest}")

    def test_no_page_stylesheet_redefines_a_foundation_token(self):
        tokens = set(DECLARED.findall((STATIC / "tokens.css").read_text(encoding="utf-8")))
        self.assertGreater(len(tokens), 50, "tokens.css declares the foundation tokens")
        for sheet in sorted(STATIC.glob("*.css")):
            if sheet.name == "tokens.css":
                continue
            with self.subTest(sheet=sheet.name):
                shadowed = tokens & set(DECLARED.findall(sheet.read_text(encoding="utf-8")))
                self.assertEqual(sorted(shadowed), [], f"{sheet.name} sets a foundation token; add it to the foundation")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
