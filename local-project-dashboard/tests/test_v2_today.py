"""The v2 shell and its Today page (sd:2110).

What this slice promises: `/v2/today` answers under the same policy as every
other response; the shell's files are served from the package with their
types; the page loads its data and script before `shell.js`, as the design's
page contract requires; Today's rows are `now_screen.document`'s, read by the
same `/api/now` v1 reads; and the v1 routes and files are as they were.
The browser half -- no console error, no policy refusal, no horizontal
scroll at 375 px -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
import urllib.request
from pathlib import Path

from sd_dashboard import now_screen, server, v2

from test_now_screen import JobsBackend, fleet_document, repo
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
TODAY_JS = (V2 / "static" / "today.js").read_text(encoding="utf-8")
SHELL_JS = (V2 / "static" / "shell.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")


class ThePage(BrowserSession):
    fleet_backend = staticmethod(lambda area: fleet_document(area, repos=[repo("pushy", ahead=1)]))

    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.backend = JobsBackend(root.name, jobs=[("nightly-sync", "failed", 7, None)])
        super().setUp()

    def test_the_route_answers_under_the_shared_policy_with_a_session(self):
        status, headers, body = self.request("/v2/today")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("sd_session=", headers["Set-Cookie"])
        self.assertRegex(body, r'<meta name="sd-csrf" content="[a-f0-9]{64}"></head>')
        self.assertIn("<title>Today · system</title>", body)

    def test_the_page_holds_nothing_the_policy_refuses(self):
        _, _, body = self.request("/v2/today")
        self.assertNotIn("<style", body)
        self.assertNotRegex(body, r"\sstyle=")
        self.assertNotRegex(body, r"\son[a-z]+=")
        for script in re.findall(r"<script\b[^>]*>(.*?)</script>", body, re.S):
            self.assertEqual(script.strip(), "", "an inline script")
        for url in re.findall(r'(?:src|href)="((?:https?:)?//[^"]*)"', body):
            self.fail(f"another origin: {url}")

    def test_every_file_the_page_names_is_served(self):
        _, _, body = self.request("/v2/today")
        named = re.findall(r'(?:src|href)="(/v2/static/[^"]+)"', body)
        self.assertEqual(len(named), 9)  # four stylesheets, five scripts
        for path in named:
            status, headers, _ = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["Content-Security-Policy"], server.CSP, path)

    def test_data_and_page_script_load_before_the_shell_and_the_shell_loads_last(self):
        _, _, body = self.request("/v2/today")
        scripts = re.findall(r'<script src="/v2/static/([^"]+)"', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "today.js", "shell.js"])
        self.assertLess(body.index('src="/v2/static/markup.js"'), body.index("</head>"))
        self.assertGreater(body.index('src="/v2/static/icons.js"'), body.index("<body"))

    def test_the_session_the_page_opens_reads_now(self):
        """The rows Today paints are `now_screen.document`'s, through the route v1 uses."""
        status, headers, _ = self.request("/v2/today")
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, body = self.request("/api/now", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        rows = json.loads(body)["rows"]
        self.assertEqual([(row["id"], row["band"], row["source"]) for row in rows],
                         [("job:nightly-sync:7", "broken", "jobs"), ("ahead:pushy:1", "look", "repos")])
        self.assertIn("fetch('/api/now'", TODAY_JS)

    def test_a_path_under_v2_that_is_not_a_page_or_a_file_is_a_404(self):
        for path in ("/v2/", "/v2/nowhere", "/v2/static/nowhere.js", "/v2/static/../__init__.py",
                     "/v2/static/%2e%2e/__init__.py", "/v2/static/", "/v2/today.html"):
            status, headers, _ = self.request(path)
            self.assertEqual(status, 404, path)
            self.assertEqual(headers["Content-Security-Policy"], server.CSP, path)


class TheAssets(BrowserSession):
    def test_each_file_is_served_with_its_type(self):
        expected = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                    ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}
        on_disk = sorted(path.relative_to(V2 / "static").as_posix()
                         for path in (V2 / "static").rglob("*") if path.is_file())
        self.assertEqual(sorted(v2.ASSETS), on_disk)
        for name in on_disk:
            with urllib.request.urlopen(f"{self.base}/v2/static/{name}", timeout=5) as answer:
                status, headers, body = answer.status, answer.headers, answer.read()
            self.assertEqual(status, 200, name)
            self.assertEqual(body, (V2 / "static" / name).read_bytes(), name)
            self.assertEqual(headers["Content-Type"], expected[Path(name).suffix], name)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff", name)
        self.assertIn("fonts/ibm-plex-sans-var.woff2", on_disk)

    def test_every_font_face_is_a_served_file_and_nothing_names_another_origin(self):
        """The foundation's fonts.css lists a Google copy after each local file.

        Chromium checks each source against the policy and logs a refusal for
        the Google one even when the local file loads, so the port drops them.
        """
        css = (V2 / "static" / "fonts.css").read_text(encoding="utf-8")
        faces = re.findall(r"src:\s*url\(([^)]+)\)", css)
        self.assertEqual(len(faces), 6)
        for first in faces:
            self.assertIn(first, v2.ASSETS)
        for name in v2.ASSETS:
            if name.endswith((".css", ".js")):
                text = (V2 / "static" / name).read_text(encoding="utf-8")
                self.assertNotRegex(text, r"url\(\s*['\"]?(https?:)?//", name)


class TheShellPort(BrowserSession):
    def test_the_shell_builds_nothing_the_policy_refuses(self):
        for source in (SHELL_JS, TODAY_JS):
            self.assertNotIn("createElement('style')", source)
            self.assertNotIn("setAttribute('style'", source)
            self.assertNotRegex(source, r"style=\\?\"")
        # The favicon count draws a data: URL; the build leaves it to the mockups.
        self.assertIn("if (!window.SHELL_PAGES) icon.href = c.toDataURL", SHELL_JS)
        self.assertIn("window.SHELL_PAGES = { Today: '/v2/today' }", TODAY_JS)

    def test_today_maps_every_band_source_and_kind_now_can_send(self):
        state = re.search(r"const STATE = \{([^}]*)\}", TODAY_JS).group(1)
        self.assertEqual(set(re.findall(r"(\w+):", state)), set(now_screen.BANDS))
        sources = re.search(r"const SRC = \{(.*?)\};", TODAY_JS).group(1)
        self.assertEqual(set(re.findall(r"(\w+): \[", sources)), set(now_screen.SOURCES))
        kinds = set(re.findall(r'"kind": "(\w+)"', Path(now_screen.__file__).read_text(encoding="utf-8")))
        types = re.search(r"const TYPE = \{([^}]*)\}", TODAY_JS).group(1)
        self.assertEqual(set(re.findall(r"(\w+):", types)), kinds)

    def test_the_v1_files_and_pages_are_unchanged(self):
        self.assertEqual(server.STATIC_FILES, ("dashboard.css", "dashboard.js"))
        for path in ("/", "/today", "/backlog", "/static/dashboard.js", "/static/dashboard.css"):
            status, headers, body = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertNotIn("/v2/", body, path)


class TheMarkup(unittest.TestCase):
    """markup.js is the v2 scripts' one HTML sink; test_markup's grep exempts it and nothing else."""

    def test_the_one_sink_is_a_template_fed_only_by_the_tag(self):
        self.assertEqual(MARKUP_JS.count("innerHTML"), 1)
        self.assertIn("const t = document.createElement('template');\n    t.innerHTML = m.text;", MARKUP_JS)
        self.assertIn("if (!made.has(m)) throw new TypeError", MARKUP_JS)
        self.assertIn("window.markup = Object.freeze({ html, nodes, put });", MARKUP_JS)
        for source in (SHELL_JS, TODAY_JS):
            self.assertNotIn("esc(", source)
            self.assertIn("window.markup", source)

    def test_the_tag_escapes_every_value_that_it_did_not_make(self):
        """The escaping, pinned as written. The gate's PATH has no node and
        allows no skipped test, so the behaviour itself (a quote, a tag and an
        entity in an attribute and in text; nesting; a forged value refused)
        is run under node by hand and recorded on the pull request."""
        table = re.search(r"const ESC = \{([^}]*)\};", MARKUP_JS).group(1)
        pairs = re.findall(r"""(['"])(.)\1: '([^']+)'""", table)
        self.assertEqual({char: entity for _, char, entity in pairs},
                         {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})
        self.assertIn("""  const value = v => made.has(v) ? v.text
    : Array.isArray(v) ? v.map(value).join('')
    : v === null || v === undefined || v === false ? ''
    : String(v).replace(/[&<>"']/g, c => ESC[c]);""", MARKUP_JS)
        self.assertEqual(MARKUP_JS.count("made.add("), 1)
