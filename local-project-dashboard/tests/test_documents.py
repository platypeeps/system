"""The Documents screen: what it lists, and what it refuses to serve.

Serving a file from disk under the dashboard's own origin is the part of this
screen that can go wrong quietly, so most of these tests are about addresses
that must not resolve rather than about the page that renders when one does.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path

from sd_dashboard import documents


class Roots(unittest.TestCase):
    """The config file's own half. `self.repos` is empty, so nothing is found."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conf = self.dir / "documents.conf"
        self.repos = self.dir / "repos"
        self.repos.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        self.conf.write_text(text, encoding="utf-8")
        return self.conf

    def read(self, text):
        return documents.roots(self.write(text), self.repos)

    def test_a_root_is_read(self):
        found = self.read("root|civic|Civic Reports|/tmp/reports\n")
        self.assertEqual([(r.key, r.label) for r in found], [("civic", "Civic Reports")])

    def test_comments_and_blank_lines_are_not_roots(self):
        found = self.read("# a note\n\nroot|a|A|/tmp/a\n")
        self.assertEqual([r.key for r in found], ["a"])

    def test_a_malformed_line_is_skipped_not_guessed(self):
        found = self.read("root|a|/tmp/a\nroot|b|B|/tmp/b\n")
        self.assertEqual([r.key for r in found], ["b"])

    def test_a_repeated_key_keeps_the_first(self):
        found = self.read("root|a|First|/tmp/1\nroot|a|Second|/tmp/2\n")
        self.assertEqual([(r.key, r.label) for r in found], [("a", "First")])

    def test_a_key_that_could_not_sit_in_a_url_is_refused(self):
        found = self.read("root|../etc|E|/tmp/e\nroot|ok|O|/tmp/o\n")
        self.assertEqual([r.key for r in found], ["ok"])

    def test_a_tilde_is_the_home_directory(self):
        found = self.read("root|a|A|~/reports\n")
        self.assertEqual(found[0].path, Path.home() / "reports")

    def test_a_missing_config_is_no_roots_not_a_crash(self):
        self.assertEqual(documents.roots(self.dir / "absent.conf", self.repos), [])


class Enumerated(unittest.TestCase):
    """The inventory comes off the disk. A list of repositories drifts; this cannot.

    Every assertion here is made against a tree with no config file at all, so
    a root that appears was found and not declared.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        self.repos = self.dir / "repos"
        self.absent = self.dir / "absent.conf"

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, relative, gitignored=False):
        """A checkout that publishes into docs/dashboard, as the real ones do."""
        repo = self.repos / relative
        (repo / "docs" / "dashboard").mkdir(parents=True)
        if gitignored:
            (repo / ".gitignore").write_text("docs/dashboard/\n", encoding="utf-8")
        return repo

    def link(self, relative, target):
        """A checkout whose `docs/dashboard` is a symlink to somewhere."""
        repo = self.repos / relative
        (repo / "docs").mkdir(parents=True)
        os.symlink(target, repo / "docs" / "dashboard")
        return repo

    def keys(self):
        return [r.key for r in documents.roots(self.absent, self.repos)]

    def test_a_repository_that_publishes_is_a_root_without_a_config_line(self):
        """The regression: onboarding is a directory, never a line somebody remembers."""
        self.publish("research/traces-research")
        self.publish("work/world-simulator")
        self.assertEqual(self.keys(), ["traces-research", "world-simulator"])

    def test_a_gitignored_directory_is_still_found(self):
        """`docs/dashboard` is built, not tracked. Git must not be consulted."""
        self.publish("research/aura-research", gitignored=True)
        self.assertEqual(self.keys(), ["aura-research"])

    def test_a_checkout_directly_under_the_root_is_found(self):
        self.publish("solo")
        self.assertEqual(self.keys(), ["solo"])

    def test_a_repository_without_the_directory_is_not_a_root(self):
        """trace-classifier has docs/ and no docs/dashboard. It is not onboarded."""
        (self.repos / "research" / "trace-classifier" / "docs").mkdir(parents=True)
        self.assertEqual(self.keys(), [])

    def test_a_file_named_like_the_directory_is_not_a_root(self):
        repo = self.repos / "research" / "odd" / "docs"
        repo.mkdir(parents=True)
        (repo / "dashboard").write_text("not a directory", encoding="utf-8")
        self.assertEqual(self.keys(), [])

    def test_a_label_comes_from_the_directory_name(self):
        self.publish("research/mcp-research")
        found = documents.roots(self.absent, self.repos)
        self.assertEqual(found[0].label, "Mcp Research")

    def test_a_config_line_overrides_a_found_root(self):
        """The one reason to write a line: a label the directory name cannot give."""
        self.publish("research/mcp-research")
        conf = self.dir / "documents.conf"
        conf.write_text("root|mcp-research|MCP|/tmp/elsewhere\n", encoding="utf-8")
        found = documents.roots(conf, self.repos)
        self.assertEqual([(r.key, r.label) for r in found], [("mcp-research", "MCP")])

    def test_a_label_line_renames_a_found_root(self):
        """The line most of them wanted: a better name, and no second path."""
        self.publish("research/mcp-research")
        conf = self.dir / "documents.conf"
        conf.write_text("label|mcp-research|MCP\n", encoding="utf-8")
        found = documents.roots(conf, self.repos)
        self.assertEqual([(r.key, r.label) for r in found], [("mcp-research", "MCP")])
        self.assertEqual(found[0].path,
                         self.repos / "research" / "mcp-research" / "docs" / "dashboard")

    def test_a_label_line_for_nothing_found_is_inert(self):
        """It renames a root. It never invents one, least of all a pathless one."""
        conf = self.dir / "documents.conf"
        conf.write_text("label|trace-classifier|Trace Classifier\n", encoding="utf-8")
        self.assertEqual(documents.roots(conf, self.repos), [])

    def test_a_label_line_does_not_settle_a_contest(self):
        """It says nothing about which directory is meant, which is the question."""
        self.publish("org-a/reports")
        self.publish("org-b/reports")
        conf = self.dir / "documents.conf"
        conf.write_text("label|reports|Reports\n", encoding="utf-8")
        self.assertEqual([r.key for r in documents.roots(conf, self.repos)], [])
        self.assertEqual(sorted(documents.unresolved(conf, self.repos)), ["reports"])

    def test_a_root_line_keeps_its_own_label(self):
        self.publish("research/mcp-research")
        conf = self.dir / "documents.conf"
        conf.write_text("root|mcp-research|From the root line|/tmp/elsewhere\n"
                        "label|mcp-research|From the label line\n", encoding="utf-8")
        found = documents.roots(conf, self.repos)
        self.assertEqual([(r.key, r.label) for r in found],
                         [("mcp-research", "From the root line")])

    def test_a_skip_line_withholds_a_found_root(self):
        self.publish("research/group-research")
        self.publish("research/aura-research")
        conf = self.dir / "documents.conf"
        conf.write_text("skip|group-research\n", encoding="utf-8")
        self.assertEqual([r.key for r in documents.roots(conf, self.repos)],
                         ["aura-research"])

    def test_an_absent_repo_root_is_no_roots_not_a_crash(self):
        self.assertEqual(documents.roots(self.absent, self.dir / "nowhere"), [])

    def test_two_checkouts_claiming_one_key_serve_neither(self):
        """The regression: a basename in two groups must not quietly pick one.

        First-wins made the winner depend on glob order, so a new checkout
        sorting earlier repointed an address that already worked.
        """
        self.publish("org-a/reports")
        self.publish("org-b/reports")
        self.assertEqual(self.keys(), [])
        contest = documents.unresolved(self.absent, self.repos)
        self.assertEqual(sorted(contest), ["reports"])
        self.assertEqual(contest["reports"], [
            self.repos / "org-a" / "reports" / "docs" / "dashboard",
            self.repos / "org-b" / "reports" / "docs" / "dashboard",
        ])

    def test_a_top_level_checkout_does_not_quietly_win_the_key(self):
        """`groups` puts the root first, so first-wins handed it the key."""
        self.publish("reports")
        self.publish("org-b/reports")
        self.assertEqual(self.keys(), [])
        self.assertEqual(sorted(documents.unresolved(self.absent, self.repos)),
                         ["reports"])

    def test_a_contest_leaves_the_other_roots_alone(self):
        self.publish("org-a/reports")
        self.publish("org-b/reports")
        self.publish("research/aura-research")
        self.assertEqual(self.keys(), ["aura-research"])

    def test_a_contested_key_is_named_on_the_page_not_dropped_in_silence(self):
        self.publish("org-a/reports")
        self.publish("org-b/reports")
        body = documents.render(None, config_path=self.absent, repo_root=self.repos)
        self.assertIn("claim the key reports", body)
        self.assertIn(str(self.repos / "org-a" / "reports" / "docs" / "dashboard"), body)
        self.assertIn(str(self.repos / "org-b" / "reports" / "docs" / "dashboard"), body)

    def test_a_root_line_settles_a_contest(self):
        self.publish("org-a/reports")
        self.publish("org-b/reports")
        conf = self.dir / "documents.conf"
        conf.write_text("root|reports|Reports|%s\n"
                        % (self.repos / "org-b" / "reports" / "docs" / "dashboard"),
                        encoding="utf-8")
        self.assertEqual([r.key for r in documents.roots(conf, self.repos)], ["reports"])
        self.assertEqual(documents.unresolved(conf, self.repos), {})

    def test_a_skip_line_settles_a_contest(self):
        self.publish("org-a/reports")
        self.publish("org-b/reports")
        conf = self.dir / "documents.conf"
        conf.write_text("skip|reports\n", encoding="utf-8")
        self.assertEqual([r.key for r in documents.roots(conf, self.repos)], [])
        self.assertEqual(documents.unresolved(conf, self.repos), {})

    def test_a_published_directory_symlinked_out_of_the_checkout_is_not_a_root(self):
        """The regression: enumeration must not grant what nobody asked for.

        Serving a directory outside a checkout is a thing somebody says with a
        `root|` line. A symlink is not somebody saying it, and `within()`
        cannot tell the difference -- it resolves the link and trusts the
        result, which is right for a declared root and no rule for a found one.
        """
        outside = self.dir / "private-reports"
        outside.mkdir()
        (outside / "secret.html").write_text("<h1>not yours</h1>", encoding="utf-8")
        self.link("research/leaky", outside)
        self.assertEqual(self.keys(), [])
        self.assertIsNone(
            documents.resolve("leaky", "secret.html", self.absent, self.repos))

    def test_a_published_directory_symlinked_inside_the_checkout_is_still_a_root(self):
        """The boundary is the checkout, not the literal directory."""
        repo = self.repos / "research" / "tidy"
        built = repo / "build"
        built.mkdir(parents=True)
        (built / "brief.html").write_text("<h1>hi</h1>", encoding="utf-8")
        self.link("research/tidy", built)
        self.assertEqual(self.keys(), ["tidy"])
        self.assertEqual(
            documents.resolve("tidy", "brief.html", self.absent, self.repos),
            (built / "brief.html").resolve())

    def test_a_declared_root_outside_any_checkout_still_resolves(self):
        """`civic` publishes into `reports`. The rule is about found roots only."""
        outside = self.dir / "reports"
        outside.mkdir()
        (outside / "brief.html").write_text("<h1>hi</h1>", encoding="utf-8")
        conf = self.dir / "documents.conf"
        conf.write_text("root|civic|Civic Reports|%s\n" % outside, encoding="utf-8")
        self.assertEqual([r.key for r in documents.roots(conf, self.repos)], ["civic"])
        self.assertEqual(documents.resolve("civic", "brief.html", conf, self.repos),
                         outside / "brief.html")

    def test_a_found_root_resolves_a_file(self):
        """Found and declared roots are the same shape, so serving is unchanged."""
        repo = self.publish("research/prism-research")
        published = repo / "docs" / "dashboard" / "brief.html"
        published.write_text("<h1>hi</h1>", encoding="utf-8")
        self.assertEqual(
            documents.resolve("prism-research", "brief.html", self.absent, self.repos),
            published)


class Shipped(unittest.TestCase):
    """The committed `documents.conf.example`, read through the real parser.

    `documents.conf` itself is gitignored; the example beside it shows a
    `root|vault` line. A vault is not a checkout under `REPO_ROOT`, so the
    enumeration never finds it, and this line is the only way it is served
    (sd:1674). The path has a `~` and spaces, which both have to survive.
    """

    def test_the_vault_is_a_configured_root(self):
        with tempfile.TemporaryDirectory() as empty:
            example = documents.CONFIG.with_name("documents.conf.example")
            found = {r.key: r for r in documents.roots(example, Path(empty))}
        self.assertIn("vault", found)
        self.assertEqual(found["vault"].label, "Vault")
        self.assertEqual(found["vault"].path,
                         Path.home() / "Documents" / "My Vault" / "docs" / "dashboard")


class Resolving(unittest.TestCase):
    """Every one of these must come back None. A None is a 404."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Resolved here because macOS makes /var a symlink to /private/var, and
        # the module compares resolved paths on purpose.
        self.dir = Path(self.tmp.name).resolve()
        self.reports = self.dir / "reports"
        self.reports.mkdir()
        (self.reports / "brief.html").write_text("<h1>hi</h1>", encoding="utf-8")
        self.outside = self.dir / "secret.html"
        self.outside.write_text("<h1>not yours</h1>", encoding="utf-8")
        self.conf = self.dir / "documents.conf"
        self.conf.write_text("root|civic|Civic|%s\n" % self.reports, encoding="utf-8")
        self.repos = self.dir / "repos"
        self.repos.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, key, name):
        return documents.resolve(key, name, self.conf, self.repos)

    def test_a_configured_file_resolves(self):
        self.assertEqual(self.get("civic", "brief.html"), self.reports / "brief.html")

    def test_an_unconfigured_root_does_not(self):
        self.assertIsNone(self.get("other", "brief.html"))

    def test_a_traversal_does_not(self):
        self.assertIsNone(self.get("civic", "../secret.html"))
        self.assertIsNone(self.get("civic", "..%2Fsecret.html"))

    def test_a_separator_in_the_name_does_not(self):
        self.assertIsNone(self.get("civic", "sub/brief.html"))
        self.assertIsNone(self.get("civic", "/etc/passwd"))

    def test_a_file_that_is_not_html_does_not(self):
        (self.reports / "notes.md").write_text("x", encoding="utf-8")
        self.assertIsNone(self.get("civic", "notes.md"))

    def test_a_dotfile_does_not(self):
        (self.reports / ".hidden.html").write_text("x", encoding="utf-8")
        self.assertIsNone(self.get("civic", ".hidden.html"))

    def test_a_directory_named_like_a_page_does_not(self):
        (self.reports / "folder.html").mkdir()
        self.assertIsNone(self.get("civic", "folder.html"))

    def test_a_symlink_out_of_the_root_does_not(self):
        """The name is clean and the parent is right until the link resolves."""
        os.symlink(self.outside, self.reports / "escape.html")
        self.assertIsNone(self.get("civic", "escape.html"))

    def test_an_absent_file_does_not(self):
        self.assertIsNone(self.get("civic", "nothing.html"))

    def test_an_empty_name_does_not(self):
        self.assertIsNone(self.get("civic", ""))


class Listing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        self.reports = self.dir / "reports"
        self.reports.mkdir()
        for name, age in (("old.html", 400), ("new.html", 10)):
            path = self.reports / name
            path.write_text("<h1>%s</h1>" % name, encoding="utf-8")
            stamp = os.stat(path).st_mtime - age
            os.utime(path, (stamp, stamp))
        (self.reports / "README.md").write_text("not html", encoding="utf-8")
        self.conf = self.dir / "documents.conf"
        self.conf.write_text("root|civic|Civic Reports|%s\n" % self.reports, encoding="utf-8")
        self.repos = self.dir / "repos"
        self.repos.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_newest_first(self):
        root = documents.roots(self.conf, self.repos)[0]
        self.assertEqual([d["name"] for d in documents.documents(root)],
                         ["new.html", "old.html"])

    def test_only_what_would_be_served_is_listed(self):
        """A listing that offers a link the server then refuses is a lie."""
        root = documents.roots(self.conf, self.repos)[0]
        self.assertNotIn("README.md", [d["name"] for d in documents.documents(root)])

    def test_an_unreadable_root_is_empty_not_an_exception(self):
        root = documents.Root("gone", "Gone", self.dir / "absent")
        self.assertEqual(documents.documents(root), [])

    def test_the_page_links_every_file(self):
        body = documents.render(None, config_path=self.conf, repo_root=self.repos)
        self.assertIn("/documents/civic/new.html", body)
        self.assertIn("/documents/civic/old.html", body)
        self.assertIn("Civic Reports", body)

    def test_no_roots_says_so_rather_than_rendering_an_empty_page(self):
        body = documents.render(None, config_path=self.dir / "absent.conf",
                                repo_root=self.repos)
        self.assertIn("No document roots are configured", body)

    def test_the_no_roots_page_names_the_tree_it_was_given(self):
        """Not the module default, which is a different machine's answer."""
        body = documents.render(None, config_path=self.dir / "absent.conf",
                                repo_root=self.repos)
        self.assertIn(str(self.repos), body)


class Policy(unittest.TestCase):
    def test_a_served_document_may_run_only_a_pinned_script(self):
        """The prohibition was absolute until a document needed a diagram (#471).

        An archify diagram page is interactive -- zoom, pan, PNG export -- and
        that is two inline scripts, byte-identical on every such page whatever
        the diagram. So the blanket `no script-src` became `script-src` naming
        those two digests and nothing else, which is a weaker rule than the one
        it replaced and a far stronger one than `'self'`: a document may run the
        script the renderer put there, and no other, because any edit to it
        changes the digest and the browser refuses it.

        Pinned rather than derived per file on purpose. Hashing whatever a
        served page happens to contain would let the page authorize its own
        script, which is the same as not having a policy. Re-measure the pins
        with `dashboard.sh docs --hashes` when archify's bundle changes.
        """
        self.assertIn("default-src 'none'", documents.POLICY)
        sources = [d for d in documents.POLICY.split(";")
                   if d.strip().startswith("script-src ")][0].split()[1:]
        self.assertEqual(sources,
                         [f"'{digest}'" for digest in documents.DIAGRAM_SCRIPTS])
        for source in sources:
            self.assertRegex(source, r"^'sha256-[A-Za-z0-9+/]{43}='$")

    def test_a_served_document_may_carry_its_own_stylesheet(self):
        """Without this the report renders unstyled, which is the whole point of it."""
        self.assertIn("style-src 'unsafe-inline'", documents.POLICY)

    def test_it_cannot_be_framed_or_post_anywhere(self):
        self.assertIn("frame-ancestors 'none'", documents.POLICY)
        self.assertIn("form-action 'none'", documents.POLICY)


class PerResponsePolicy(unittest.TestCase):
    """A policy belongs to one response, not to the connection that carried it.

    `BaseHTTPRequestHandler` reuses one instance for every request on a
    kept-alive connection. A policy left on `self` would then apply to the next
    page down the same socket: a report's policy governing the dashboard, and
    that policy forbids the dashboard's own script.

    These drive the real `_send` and the real `end_headers`, writing into a
    buffer in place of the socket, so what is asserted is the bytes a browser
    would receive rather than a restatement of the loop.
    """

    def headers(self, policy=None):
        from sd_dashboard.server import Dashboard

        handler = object.__new__(Dashboard)
        handler.command = "HEAD"
        handler.request_version = "HTTP/1.1"
        # `send_response` logs the request line; the handler silences the log
        # but the base class still reads the attribute.
        handler.requestline = "HEAD /documents/civic/brief.html HTTP/1.1"
        handler.client_address = ("127.0.0.1", 0)
        handler.wfile = io.BytesIO()
        handler._headers_buffer = []
        Dashboard._send(handler, 200, b"body", "text/html", policy=policy)
        sent = {}
        for line in handler.wfile.getvalue().decode("latin-1").split("\r\n"):
            name, sep, value = line.partition(":")
            if sep:
                sent[name.strip()] = value.strip()
        return sent

    def test_a_document_response_carries_the_document_policy(self):
        from sd_dashboard.documents import POLICY

        self.assertEqual(self.headers(POLICY)["Content-Security-Policy"], POLICY)

    def test_an_ordinary_response_carries_the_shared_policy(self):
        from sd_dashboard.server import CSP

        self.assertEqual(self.headers()["Content-Security-Policy"], CSP)

    def test_the_next_response_on_the_same_handler_does_not_inherit_it(self):
        from sd_dashboard.documents import POLICY
        from sd_dashboard.server import CSP, Dashboard

        handler = object.__new__(Dashboard)
        handler.command = "HEAD"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "GET / HTTP/1.1"
        handler.client_address = ("127.0.0.1", 0)
        for expected, policy in ((POLICY, POLICY), (CSP, None)):
            handler.wfile = io.BytesIO()
            handler._headers_buffer = []
            Dashboard._send(handler, 200, b"body", "text/html", policy=policy)
            self.assertIn(expected, handler.wfile.getvalue().decode("latin-1"))

    def test_the_other_security_headers_are_untouched(self):
        from sd_dashboard.documents import POLICY

        sent = self.headers(POLICY)
        self.assertEqual(sent["X-Frame-Options"], "DENY")
        self.assertEqual(sent["X-Content-Type-Options"], "nosniff")


if __name__ == "__main__":
    unittest.main()
