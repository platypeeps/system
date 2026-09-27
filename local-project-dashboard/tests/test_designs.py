"""The Designs screen: what it lists, what it serves, and what it refuses.

The tab serves a checkout's tree under the dashboard's own origin, several
directories deep, so most of these tests are about addresses that must not
resolve, and about the headers that keep a mockup's script away from the
dashboard.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import designs


def tree(base: Path) -> None:
    """The ui-design layout in miniature, plus the things that must not be served."""
    (base / "foundation").mkdir(parents=True)
    (base / "foundation" / "tokens.css").write_text(":root{}", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
