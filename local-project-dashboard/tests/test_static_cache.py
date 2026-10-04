"""Static files cached under their content hash, and gzipped for a browser that takes it (sd:2141).

What this promises: a page links each asset with `?v=<digest>`; the server
answers that request `immutable`, and any request without the matching `v`
`no-store`; CSS, script and text go gzipped when `Accept-Encoding` allows and
plain when it does not; and the digest of a file follows its bytes.
"""

from __future__ import annotations

import gzip
import re
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from sd_dashboard import v2

from test_workflow_actions import BrowserSession

IMMUTABLE = "private, max-age=31536000, immutable"


class WhatTheServerCaches(BrowserSession):
    def fetch(self, path, **headers):
        """Status, headers and the raw bytes: `request` decodes text, and a gzipped body is not text."""
        try:
            answer = urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers), timeout=5)
        except urllib.error.HTTPError as error:
            answer = error
        with answer:
            return answer.status, answer.headers, answer.read()

    def linked(self, page, name):
        """The address `page` links `name` at, query included."""
        body = self.request(page)[2]
        found = re.findall(rf'(?:src|href)="(/(?:ui|static)/{re.escape(name)}(?:\?[^"]*)?)"', body)
        self.assertEqual(len(found), 1, (page, name, found))
        return found[0]

    def test_a_page_links_each_asset_with_its_digest_and_that_answer_is_immutable(self):
        for page, name in (("/today", "shell.js"), ("/today", "fonts.css"), ("/backlog", "dashboard.css")):
            with self.subTest(page=page, name=name):
                address = self.linked(page, name)
                self.assertRegex(address, r"\?v=[0-9a-f]{16}$")
                status, headers, _ = self.fetch(address)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], IMMUTABLE)

    def test_without_the_matching_v_nothing_is_cached(self):
        for address in ("/ui/shell.js", "/ui/shell.js?v=0000000000000000", "/ui/shell.js?v=a&v=b",
                        "/static/dashboard.js", "/static/dashboard.js?v=stale"):
            with self.subTest(address=address):
                status, headers, _ = self.fetch(address)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")

    def test_a_stylesheet_names_its_fonts_by_digest_too(self):
        body = self.fetch(self.linked("/today", "fonts.css"))[2].decode()
        fonts = re.findall(r"url\((fonts/[^)]+)\)", body)
        self.assertTrue(fonts)
        for font in fonts:
            self.assertRegex(font, r"\.woff2\?v=[0-9a-f]{16}$")
            self.assertEqual(self.fetch(f"/ui/{font}")[1]["Cache-Control"], IMMUTABLE, font)

    def test_text_is_gzipped_when_the_browser_takes_it_and_plain_when_not(self):
        for address in ("/ui/shell.js", "/ui/shell.css", "/static/dashboard.css"):
            with self.subTest(address=address):
                _, plain_headers, plain = self.fetch(address)
                self.assertIsNone(plain_headers["Content-Encoding"])
                _, headers, body = self.fetch(address, **{"Accept-Encoding": "br, gzip;q=0.8"})
                self.assertEqual(headers["Content-Encoding"], "gzip")
                self.assertEqual(headers["Vary"], "Accept-Encoding")
                self.assertEqual(int(headers["Content-Length"]), len(body))
                self.assertLess(len(body), len(plain))
                self.assertEqual(gzip.decompress(body), plain)
                _, refused, _ = self.fetch(address, **{"Accept-Encoding": "gzip;q=0"})
                self.assertIsNone(refused["Content-Encoding"])

    def test_a_font_is_not_gzipped_and_a_page_is_not_either(self):
        for address in ("/ui/fonts/ibm-plex-sans-var.woff2", "/today"):
            with self.subTest(address=address):
                _, headers, _ = self.fetch(address, **{"Accept-Encoding": "gzip"})
                self.assertIsNone(headers["Content-Encoding"])


class WhatTheDigestFollows(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "static"
        shutil.copytree(v2.STATIC, self.root)

    def test_it_is_the_digest_the_server_started_with(self):
        self.assertEqual(v2.digests(self.root), v2.VERSIONS)

    def test_an_edit_changes_the_files_digest_and_no_other(self):
        before = v2.digests(self.root)
        with (self.root / "shell.js").open("a", encoding="utf-8") as script:
            script.write("// an edit\n")
        after = v2.digests(self.root)
        self.assertNotEqual(after["shell.js"], before["shell.js"])
        self.assertEqual({name for name in before if before[name] != after[name]}, {"shell.js"})

    def test_a_font_edit_changes_the_stylesheet_that_names_it(self):
        before = v2.digests(self.root)
        with (self.root / "fonts" / "ibm-plex-sans-var.woff2").open("ab") as font:
            font.write(b"\0")
        after = v2.digests(self.root)
        self.assertEqual({name for name in before if before[name] != after[name]},
                         {"fonts/ibm-plex-sans-var.woff2", "fonts.css"})


if __name__ == "__main__":
    unittest.main()
