"""The Designs screen: what it lists, what it serves, and what it refuses.

The tab serves a checkout's tree under the dashboard's own origin, several
directories deep, so most of these tests are about addresses that must not
resolve, and about the headers that keep a mockup's script away from the
dashboard.
"""

from __future__ import annotations

import io
import json
import os
import posixpath
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import designs


def tree(base: Path) -> None:
    """The ui-design layout in miniature, plus the things that must not be served."""
    (base / "foundation").mkdir(parents=True)
    (base / "foundation" / "tokens.css").write_text(":root{}", encoding="utf-8")
    (base / "foundation" / "fonts").mkdir()
    (base / "foundation" / "fonts" / "plex.woff2").write_bytes(b"wOF2")
    shop = base / "products" / "system" / "designs"
    (shop / "shots").mkdir(parents=True)
    (shop / "v1-today.html").write_text(
        '<link rel="stylesheet" href="../../../foundation/tokens.css">', encoding="utf-8")
    (shop / "shots" / "v1.png").write_bytes(b"\x89PNG\r\n")
    (base / "products" / "system" / "README.md").write_text("# brief", encoding="utf-8")
    (base / "products" / "empty").mkdir()
    (base / ".git").mkdir()
    (base / ".git" / "config.html").write_text("secret", encoding="utf-8")
    (base / ".hidden.html").write_text("secret", encoding="utf-8")


class Resolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.outside = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ui-design"
        tree(self.root)
        self.secret = Path(self.outside.name) / "secret.html"
        self.secret.write_text("outside", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()
        self.outside.cleanup()

    def resolve(self, tail):
        return designs.resolve(tail, self.root)

    def test_a_mockup_resolves_with_its_type(self):
        target, kind = self.resolve("products/system/designs/v1-today.html")
        self.assertEqual(target.name, "v1-today.html")
        self.assertEqual(kind, "text/html; charset=utf-8")

    def test_the_stylesheet_a_mockup_links_resolves(self):
        """`../../../foundation/tokens.css` from the page is this path in the tree."""
        self.assertEqual(self.resolve("foundation/tokens.css")[1], "text/css; charset=utf-8")

    def test_a_shared_script_resolves_as_javascript(self):
        """A v2 mockup loads `../../../../foundation/theme.js` and its shell as scripts."""
        (self.root / "foundation" / "theme.js").write_text("//", encoding="utf-8")
        self.assertEqual(self.resolve("foundation/theme.js")[1], "text/javascript; charset=utf-8")

    def test_a_screenshot_resolves_as_an_image(self):
        self.assertEqual(self.resolve("products/system/designs/shots/v1.png")[1], "image/png")

    def test_traversal_is_unsayable(self):
        for tail in ("../x.html", "products/../../x.html", "products/%2e%2e/%2e%2e/x.html",
                     "products//system/designs/v1-today.html", "/etc/hosts.html", ""):
            self.assertIsNone(self.resolve(tail), tail)

    def test_dotfiles_and_the_git_directory_are_refused(self):
        self.assertIsNone(self.resolve(".git/config.html"))
        self.assertIsNone(self.resolve(".hidden.html"))

    def test_an_extension_outside_the_allow_list_is_refused(self):
        self.assertIsNone(self.resolve("products/system/README.md"))

    def test_a_missing_file_is_refused(self):
        self.assertIsNone(self.resolve("products/system/designs/v2.html"))

    def test_a_symlinked_file_out_of_the_checkout_is_refused(self):
        (self.root / "products" / "system" / "designs" / "out.html").symlink_to(self.secret)
        self.assertIsNone(self.resolve("products/system/designs/out.html"))

    def test_a_symlinked_directory_out_of_the_checkout_is_refused(self):
        (self.root / "products" / "escape").symlink_to(self.outside.name, target_is_directory=True)
        self.assertIsNone(self.resolve("products/escape/secret.html"))

    def test_a_symlink_that_stays_inside_is_served(self):
        link = self.root / "products" / "system" / "designs" / "latest.html"
        link.symlink_to("v1-today.html")
        self.assertIsNotNone(self.resolve("products/system/designs/latest.html"))


class Listing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ui-design"
        tree(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pages_are_grouped_by_product(self):
        self.assertEqual(designs.designs(self.root),
                         {"system": ["products/system/designs/v1-today.html"]})

    def test_the_page_links_each_mockup_at_its_tree_path(self):
        body = designs.render(None, root=self.root)
        self.assertIn('href="/designs/products/system/designs/v1-today.html"', body)
        self.assertNotIn("config.html", body)
        self.assertNotIn("README.md", body)

    def test_the_tab_is_current_in_the_navigation(self):
        body = designs.render(None, root=self.root)
        self.assertRegex(body, r'<a[^>]*href="/designs"[^>]*aria-current="page"'
                               r'|<a[^>]*aria-current="page"[^>]*href="/designs"')

    def test_no_checkout_says_where_it_looked(self):
        missing = Path(self.tmp.name) / "absent"
        body = designs.render(None, root=missing)
        self.assertIn("No design pages found", body)
        self.assertIn(str(missing / "products"), body)

    def test_the_router_serves_the_listing_without_a_database(self):
        from sd_dashboard.server import route

        with mock.patch.object(designs, "ROOT", self.root):
            body = route(None, "/designs", {}, now="2026-09-27T00:00:00Z")
        self.assertIn("v1-today.html", body)


class Policy(unittest.TestCase):
    def directives(self):
        return {d.strip().split()[0]: d.strip().split()[1:]
                for d in designs.POLICY.split(";") if d.strip()}

    def test_a_mockup_runs_sandboxed_without_same_origin(self):
        """Its script runs, but under an opaque origin: no cookie, no endpoint."""
        sandbox = self.directives()["sandbox"]
        self.assertEqual(sandbox, ["allow-scripts"])

    def test_it_cannot_fetch_post_or_be_framed(self):
        found = self.directives()
        self.assertEqual(found["connect-src"], ["'none'"])
        self.assertEqual(found["form-action"], ["'none'"])
        self.assertEqual(found["frame-ancestors"], ["'none'"])
        self.assertEqual(found["default-src"], ["'none'"])

    def test_only_the_resource_policy_is_relaxed_for_assets(self):
        self.assertEqual(designs.ASSET_HEADERS,
                         {"Cross-Origin-Resource-Policy": "cross-origin"})

    def test_only_a_font_is_readable_from_another_origin(self):
        # A sandboxed page has an opaque origin and fetches fonts in CORS mode, so
        # a font needs Access-Control-Allow-Origin. Nothing else gets it: the data
        # scripts beside the pages hold real notes and mail.
        self.assertEqual(designs.FONT_HEADERS,
                         {**designs.ASSET_HEADERS, "Access-Control-Allow-Origin": "*"})


class Served(unittest.TestCase):
    """The real `_design`, `_send` and `end_headers`, writing into a buffer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ui-design"
        tree(self.root)
        patcher = mock.patch.object(designs, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def handler(self):
        from sd_dashboard.server import Dashboard

        handler = object.__new__(Dashboard)
        handler.command = "GET"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "GET /designs/ HTTP/1.1"
        handler.client_address = ("127.0.0.1", 0)
        return handler

    def get(self, handler, tail):
        from sd_dashboard.server import Dashboard

        handler.wfile = io.BytesIO()
        handler._headers_buffer = []
        Dashboard._design(handler, tail)
        head, _, body = handler.wfile.getvalue().partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        sent = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            sent[name.strip()] = value.strip()
        return int(lines[0].split()[1]), sent, body

    def test_a_mockup_is_served_whole_under_the_sandbox_policy(self):
        from sd_dashboard.server import SECURITY_HEADERS

        status, sent, body = self.get(self.handler(), "products/system/designs/v1-today.html")
        self.assertEqual(status, 200)
        self.assertEqual(sent["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(sent["Content-Security-Policy"], designs.POLICY)
        self.assertEqual(sent["Cross-Origin-Resource-Policy"], dict(SECURITY_HEADERS)["Cross-Origin-Resource-Policy"])
        self.assertIn(b"tokens.css", body)

    def test_an_asset_relaxes_only_its_resource_policy(self):
        from sd_dashboard.server import CSP

        status, sent, _ = self.get(self.handler(), "foundation/tokens.css")
        self.assertEqual(status, 200)
        self.assertEqual(sent["Content-Type"], "text/css; charset=utf-8")
        self.assertEqual(sent["Cross-Origin-Resource-Policy"], "cross-origin")
        self.assertEqual(sent["Content-Security-Policy"], CSP)
        self.assertEqual(sent["X-Content-Type-Options"], "nosniff")

    def test_a_font_is_readable_by_the_sandboxed_page(self):
        status, sent, _ = self.get(self.handler(), "foundation/fonts/plex.woff2")
        self.assertEqual(status, 200)
        self.assertEqual(sent["Content-Type"], "font/woff2")
        self.assertEqual(sent["Access-Control-Allow-Origin"], "*")
        self.assertEqual(sent["Cross-Origin-Resource-Policy"], "cross-origin")

    def test_no_other_design_file_is_readable_cross_origin(self):
        for tail in ("foundation/tokens.css", "products/system/designs/v1-today.html",
                     "products/system/designs/shots/v1.png", "nope.html"):
            _, sent, _ = self.get(self.handler(), tail)
            self.assertNotIn("Access-Control-Allow-Origin", sent, tail)

    def test_the_next_response_does_not_inherit_the_font_header(self):
        handler = self.handler()
        self.get(handler, "foundation/fonts/plex.woff2")
        _, sent, _ = self.get(handler, "foundation/tokens.css")
        self.assertNotIn("Access-Control-Allow-Origin", sent)

    def test_the_ledger_the_v2_page_loads_is_answered_live(self):
        v2_tree(self.root)
        status, sent, body = self.get(self.handler(), designs.LIVE_LEDGER)
        self.assertEqual(status, 200)
        self.assertEqual(sent["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(sent["Cross-Origin-Resource-Policy"], "cross-origin")
        found = json.loads(body.decode("utf-8")[len("window.DESIGNS = "):].rstrip().rstrip(";"))
        self.assertIn(f"{V2}/today.html", [p["path"] for p in found["pages"]])
        self.assertNotIn(b"abc1234", body)  # Not the committed file's head.

    def test_any_other_data_script_is_served_as_committed(self):
        v2_tree(self.root)
        _, _, body = self.get(self.handler(), f"{V2}/data/commands.js")
        self.assertEqual(body, (self.root / V2 / "data" / "commands.js").read_bytes())

    def test_every_refusal_is_the_same_404(self):
        for tail in (".git/config.html", "products/system/README.md", "../x.html", "nope.html"):
            status, sent, _ = self.get(self.handler(), tail)
            self.assertEqual(status, 404, tail)

    def test_the_next_response_on_the_same_handler_does_not_inherit_the_override(self):
        from sd_dashboard.server import SECURITY_HEADERS

        handler = self.handler()
        self.get(handler, "foundation/tokens.css")
        _, sent, _ = self.get(handler, "nope.html")
        self.assertEqual(sent["Cross-Origin-Resource-Policy"],
                         dict(SECURITY_HEADERS)["Cross-Origin-Resource-Policy"])


V2 = "products/system/designs/v2"

#: A ledger with every field the hash drops and a float JSON.stringify writes as `2`.
LEDGER = (
    '// Generated by designs/tools/collect-designs.mjs. Do not edit.\n'
    'window.DESIGNS = {\n "read": "2026-09-29T19:14:37Z", "root": "~/ui", "head": "abc1234",'
    ' "branch": "main",\n "products": [{"name": "system", "title": "sd \u2014 caf\u00e9", "pages": 2.0}],\n'
    ' "pages": [{"path": "p.html", "bytes": 10, "dirty": true, "shots": ["a.png"],'
    ' "shotStale": [], "shotTimes": {"a.png": "x"}, "shotChanged": "x", "shotSha": "s",'
    ' "shotOldest": "a.png", "shotSize": {"a.png": [1, 2]}}]\n};\n'
)


def v2_tree(base: Path) -> None:
    """Two v2 pages whose inputs cover every rule in ui-design's tools/inputs.mjs.

    `EXPECTED` holds what `node inputs.mjs` computed for them; the Python port
    must give the same sixteen hex digits, or every recorded screenshot reads stale.
    """
    foundation = base / "foundation"
    (foundation / "fonts").mkdir(parents=True, exist_ok=True)
    (foundation / "tokens.css").write_text(
        '@import "fonts.css";\nbody { background: url(\'img/bg.png?v=2\'); }\n'
        '.a { background: url( "data:image/png;base64,AAAA" ); }\n'
        '.b { background: url(https://example.test/x.png); }\n', encoding="utf-8")
    (foundation / "fonts.css").write_text(
        '@font-face { src: url(fonts/plex.woff2) format("woff2"); }\n@import \'tokens.css\';\n',
        encoding="utf-8")
    # Not UTF-8: Node hashes the buffer as a string, each bad sequence one U+FFFD.
    (foundation / "fonts" / "plex.woff2").write_bytes(
        b"wOF2\x00\x80\xff\xc3(\xe2\x82 end \xf0\x9f\x98\x80 \xed\xa0\x80 \xf4\x90\x80\x80")
    data = base / V2 / "data"
    data.mkdir(parents=True)
    (data / "counts.js").write_text("window.COUNTS = {tasks: 3};\n", encoding="utf-8")
    (data / "commands.js").write_text(
        '// header\nwindow.COMMANDS = {\n  "read": "2026-09-29T10:00:00Z",\n  "rows": [1, 2]\n};\n',
        encoding="utf-8")
    (data / "designs-data.js").write_text(LEDGER, encoding="utf-8")
    (base / V2 / "shell.js").write_text("// shell \u2014 caf\u00e9\n", encoding="utf-8")
    (base / V2 / "today.html").write_text(
        '<!doctype html><title> Today \u2014 sd </title>\n'
        '<link rel="stylesheet" href="../../../../foundation/tokens.css">\n'
        '<script src="data/counts.js"></script><script src="data/commands.js"></script>\n'
        '<script\n  defer src="data/designs-data.js"></script>\n'
        '<script src="shell.js?x=1#y"></script><script src="shell.js"></script>\n'
        '<img alt="" src="https://example.test/a.png"><img src="${x}.png"><img src="#top">\n'
        '<iframe src="/frame.html"></iframe><SCRIPT src="upper.js"></SCRIPT>\n'
        '<source srcset="x.webp"><a href="designs.html">Designs</a>\n', encoding="utf-8")
    (base / V2 / "designs.html").write_text(
        '<title>Designs</title><script src="data/designs-data.js"></script>\n', encoding="utf-8")


#: `inputsHash(root, page)` from ui-design's tools/inputs.mjs over `v2_tree`.
EXPECTED = {
    f"{V2}/today.html": "326bca091ddc8e17",
    f"{V2}/designs.html": "a47477eba2c95d38",
}


class InputsHash(unittest.TestCase):
    """The port of tools/inputs.mjs: the recorded hashes are Node's, so the digits must match."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ui-design"
        v2_tree(self.root)

    def hash(self, page="today"):
        return designs.inputs_hash(self.root, f"{V2}/{page}.html")

    def test_the_hash_matches_node_byte_for_byte(self):
        for page, expected in EXPECTED.items():
            self.assertEqual(designs.inputs_hash(self.root, page), expected, page)

    def test_the_inputs_are_the_loaded_files_and_the_stylesheets_they_reach(self):
        self.assertEqual(designs.refs(self.root, f"{V2}/today.html"), [
            f"{V2}/today.html", "foundation/tokens.css", "foundation/fonts.css",
            "foundation/fonts/plex.woff2", "foundation/img/bg.png",
            f"{V2}/data/counts.js", f"{V2}/data/commands.js", f"{V2}/data/designs-data.js",
            f"{V2}/shell.js", f"{V2}/frame.html",
        ])

    def test_a_loaded_file_changes_the_hash(self):
        before = self.hash()
        (self.root / "foundation" / "fonts" / "plex.woff2").write_bytes(b"wOF2 other")
        self.assertNotEqual(self.hash(), before)

    def test_a_linked_page_does_not(self):
        before = self.hash()
        (self.root / V2 / "designs.html").write_text("<title>changed</title>", encoding="utf-8")
        self.assertEqual(self.hash(), before)

    def test_the_rail_counts_do_not(self):
        before = self.hash()
        (self.root / V2 / "data" / "counts.js").write_text("window.COUNTS = {};", encoding="utf-8")
        self.assertEqual(self.hash(), before)

    def test_the_commands_read_stamp_and_comments_do_not(self):
        before = self.hash()
        path = self.root / V2 / "data" / "commands.js"
        path.write_text(path.read_text(encoding="utf-8").replace("10:00:00", "11:00:00")
                        .replace("// header", "// other header"), encoding="utf-8")
        self.assertEqual(self.hash(), before)

    def test_a_retake_does_not_stale_the_designs_page(self):
        """The ledger's stamps and per-shot fields change on every collect; the hash leaves them out."""
        before = self.hash("designs")
        text = (LEDGER.replace('"abc1234"', '"def5678"').replace('"dirty": true', '"dirty": false')
                .replace('"shotChanged": "x"', '"shotChanged": "y"').replace("[1, 2]", "[3, 4]"))
        (self.root / V2 / "data" / "designs-data.js").write_text(text, encoding="utf-8")
        self.assertEqual(self.hash("designs"), before)

    def test_a_ledger_page_change_does(self):
        before = self.hash("designs")
        (self.root / V2 / "data" / "designs-data.js").write_text(
            LEDGER.replace('"bytes": 10', '"bytes": 11'), encoding="utf-8")
        self.assertNotEqual(self.hash("designs"), before)


class Ledger(unittest.TestCase):
    """`ledger()` reads a real git checkout the way collect-designs.mjs does."""

    WHEN = "2026-09-20T10:00:00+02:00"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outside = Path(self.tmp.name) / "outside"
        self.outside.mkdir()
        self.root = Path(self.tmp.name) / "ui-design"
        tree(self.root)
        v2_tree(self.root)
        (self.root / ".git" / "config.html").unlink()
        (self.root / ".git").rmdir()
        shots = self.root / "products" / "system" / "designs" / "shots"
        for name, width in (("v2-today-1440.png", 1440), ("v2-today-375.png", 375),
                            ("v2-today-capture-1440.png", 1440), ("v1-375x900.png", 375)):
            (shots / name).write_bytes(png(width, 900))
        (self.root / "products" / "system" / "README.md").write_text(
            "# system \u2014 design brief\n\n**Status:** v2 drawn.\n", encoding="utf-8")
        (self.root / "products" / "brand").mkdir()
        (self.root / "products" / "brand" / "README.md").write_text(
            "# brand brief\n\n**Status:** extracted; no redesign started.\n", encoding="utf-8")
        self.record({"v2-today-1440.png": self.hash("today"), "v2-today-375.png": self.hash("today")})
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Draw the v2 pages", when=self.WHEN)

    def git(self, *arguments, when=None):
        environment = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        if when:
            environment.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        return subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=Fixture", "-c", "user.email=f@example.test",
             "-c", "commit.gpgsign=false", *arguments],
            capture_output=True, text=True, check=True, timeout=30, env=environment).stdout.strip()

    def hash(self, page):
        return designs.inputs_hash(self.root, f"{V2}/{page}.html")

    def record(self, hashes):
        (self.root / designs.MANIFEST).write_text(json.dumps(hashes), encoding="utf-8")

    def page(self, found, path):
        return next(p for p in found["pages"] if p["path"] == path)

    def test_each_page_carries_kind_title_size_and_last_commit(self):
        found = designs.ledger(self.root)
        today = self.page(found, f"{V2}/today.html")
        self.assertEqual(today["kind"], "v2 mockup")
        self.assertEqual(today["title"], "Today \u2014 sd")
        self.assertEqual(today["bytes"], (self.root / V2 / "today.html").stat().st_size)
        self.assertEqual(today["sha"], self.git("rev-parse", "--short", "HEAD"))
        self.assertEqual(today["changed"], "2026-09-20T08:00:00Z")
        self.assertEqual(today["subject"], "Draw the v2 pages")
        self.assertFalse(today["dirty"])
        self.assertEqual(self.page(found, "products/system/designs/v1-today.html")["kind"], "v1 mockup")

    def test_a_page_takes_only_its_own_screenshots_with_their_sizes_and_times(self):
        today = self.page(designs.ledger(self.root), f"{V2}/today.html")
        shots = "products/system/designs/shots/"
        self.assertEqual(today["shots"], [shots + "v2-today-1440.png", shots + "v2-today-375.png"])
        self.assertEqual(today["shotSize"][shots + "v2-today-375.png"], [375, 900])
        self.assertEqual(today["shotTimes"][shots + "v2-today-1440.png"], "2026-09-20T08:00:00Z")
        self.assertEqual(today["shotOldest"], shots + "v2-today-1440.png")
        self.assertEqual(today["shotChanged"], "2026-09-20T08:00:00Z")
        self.assertEqual(today["shotStale"], [])

    def test_a_shot_is_stale_when_its_recorded_hash_differs_or_is_missing(self):
        self.record({"v2-today-1440.png": "0000000000000000"})
        today = self.page(designs.ledger(self.root), f"{V2}/today.html")
        self.assertEqual([posixpath.basename(f) for f in today["shotStale"]],
                         ["v2-today-1440.png", "v2-today-375.png"])

    def test_a_changed_input_stales_the_shot(self):
        (self.root / "foundation" / "tokens.css").write_text(":root{--x:1}", encoding="utf-8")
        today = self.page(designs.ledger(self.root), f"{V2}/today.html")
        self.assertEqual(len(today["shotStale"]), 2)

    def test_the_brief_gives_title_and_status_and_a_brief_only_product_is_listed(self):
        products = {p["name"]: p for p in designs.ledger(self.root)["products"]}
        self.assertEqual(products["brand"], {
            "name": "brand", "title": "brand brief", "brief": "products/brand/README.md",
            "status": "extracted; no redesign started.", "pages": 0})
        self.assertEqual(products["system"]["title"], "system \u2014 design brief")
        self.assertEqual(products["system"]["pages"], 3)
        self.assertEqual(products["empty"]["brief"], "")

    def test_caution_counts_uncommitted_unshot_and_stale_pages(self):
        found = designs.ledger(self.root)
        # designs.html has no screenshot; today.html is current; v1-today.html has its v1 shot.
        self.assertEqual(found["caution"], 1)
        (self.root / V2 / "today.html").write_text("<title>edited</title>", encoding="utf-8")
        found = designs.ledger(self.root)
        today = self.page(found, f"{V2}/today.html")
        self.assertTrue(today["dirty"])
        self.assertEqual(designs.caution(today), "uncommitted change in the working tree")
        self.assertEqual(found["caution"], 2)

    def test_an_untracked_page_is_dirty_and_dated_by_its_file(self):
        (self.root / V2 / "new.html").write_text("<title>New</title>", encoding="utf-8")
        new = self.page(designs.ledger(self.root), f"{V2}/new.html")
        self.assertTrue(new["dirty"])
        self.assertEqual(new["sha"], "")
        self.assertRegex(new["changed"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_nothing_listed_escapes_the_checkout(self):
        (self.outside / "v2-designs-1440.png").write_bytes(png(1440, 900))
        (self.outside / "README.md").write_text("# secret\n\n**Status:** secret\n", encoding="utf-8")
        shots = self.root / "products" / "system" / "designs" / "shots"
        (shots / "v2-designs-1440.png").symlink_to(self.outside / "v2-designs-1440.png")
        (self.root / "products" / "escape").symlink_to(self.outside, target_is_directory=True)
        found = designs.ledger(self.root)
        self.assertEqual(self.page(found, f"{V2}/designs.html")["shots"], [])
        self.assertNotIn("escape", [p["name"] for p in found["products"]])
        self.assertNotIn("secret", json.dumps(found))
        for entry in found["pages"]:
            for path in [entry["path"], *entry["shots"]]:
                self.assertIsNotNone(designs.resolve(path, self.root), path)

    def test_a_checkout_without_git_still_lists_its_pages(self):
        plain = Path(self.tmp.name) / "plain"
        tree(plain)
        found = designs.ledger(plain)
        self.assertEqual(found["head"], "")
        self.assertEqual([p["path"] for p in found["pages"]], ["products/system/designs/v1-today.html"])
        self.assertEqual(found["pages"][0]["sha"], "")

    def test_an_unparsable_ledger_stales_the_shot_instead_of_failing(self):
        shots = self.root / "products" / "system" / "designs" / "shots"
        (shots / "v2-designs-1440.png").write_bytes(png(1440, 900))
        self.record({"v2-designs-1440.png": self.hash("designs")})
        (self.root / designs.LIVE_LEDGER).write_text("window.DESIGNS = {", encoding="utf-8")
        page = self.page(designs.ledger(self.root), f"{V2}/designs.html")
        self.assertEqual(page["shotStale"], ["products/system/designs/shots/v2-designs-1440.png"])

    def test_the_script_is_what_the_page_loads(self):
        script = designs.ledger_script(self.root)
        self.assertTrue(script.startswith("window.DESIGNS = {"))
        self.assertEqual(json.loads(script[len("window.DESIGNS = "):].rstrip().rstrip(";"))["caution"], 1)


def png(width: int, height: int) -> bytes:
    """A PNG signature and IHDR chunk: enough for the size read."""
    return (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")


if __name__ == "__main__":
    unittest.main()
