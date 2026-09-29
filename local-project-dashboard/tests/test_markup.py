"""Escaping, and the Markdown subset. Criterion 12's content clauses.

The fixture artifact the criterion names, carrying "a `script` tag, an image
with an `onerror` handler, a `javascript:` link, a `data:` link and a raw HTML
block", is rendered here and asserted to be text. The browser half of that
criterion -- the console showing the policy's refusal, the framed page
rendering nothing -- needs a browser and is not claimed by this suite; what is
claimed is that nothing reaches the browser for the policy to refuse.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from sd_dashboard.markup import Markup, escape, markdown, tag

HERE = Path(__file__).resolve().parents[1]

#: The v2 scripts' one HTML sink, as `git grep` names it from the repository root.
V2_SINK = "local-project-dashboard/sd_dashboard/v2/static/markup.js"

#: `prd.md:1330-1332`. Every construct in one artifact.
ARTIFACT = """\
# A heading

A paragraph with <script>window.pwned = 1</script> in it.

<img src=x onerror="window.pwned = 2">

[a link](javascript:window.pwned=3) and [another](data:text/html,<script>4</script>)

<div onclick="window.pwned = 5">a raw HTML block</div>

- a list item with `code` and **strong**
- [a real link](https://example.invalid/x)

```
a fenced block with <b>tags</b>
```
"""


class Escaping(unittest.TestCase):
    def test_text_is_escaped(self):
        self.assertEqual(escape("<b>&\"'"), "&lt;b&gt;&amp;&quot;&#x27;")

    def test_none_is_empty_not_the_word(self):
        self.assertEqual(escape(None), "")

    def test_children_are_escaped_and_markup_is_not(self):
        self.assertEqual(
            tag("p", "<b>", tag("em", "x")),
            "<p>&lt;b&gt;<em>x</em></p>",
        )

    def test_attributes_cannot_break_out(self):
        rendered = tag("a", "x", href='" onmouseover="alert(1)')
        self.assertIn("&quot; onmouseover=&quot;alert(1)", rendered)
        self.assertNotIn('" onmouseover="alert(1)"', rendered)

    def test_markup_is_a_str_so_it_composes(self):
        self.assertIsInstance(tag("p", "x"), Markup)
        self.assertIsInstance(tag("p", "x"), str)


class TheSubset(unittest.TestCase):
    def setUp(self):
        self.rendered = str(markdown(ARTIFACT))

    def test_no_script_survives(self):
        self.assertNotIn("<script", self.rendered)
        self.assertIn("&lt;script&gt;", self.rendered)

    def test_no_event_handler_survives(self):
        """The handler is *shown*, which is the difference that matters.

        `onerror=&quot;...&quot;` inside a `<p>` is four escaped characters and
        a word; what would run is `<img ... onerror="...">`, and the assertion
        is that no tag in the output carries an `on*` attribute at all.
        """
        import re as _re

        self.assertIsNone(_re.search(r"<[^>]+\son[a-z]+\s*=", self.rendered))
        self.assertIn("onerror=&quot;", self.rendered)

    def test_javascript_and_data_links_are_text(self):
        self.assertNotIn('href="javascript:', self.rendered)
        self.assertNotIn('href="data:', self.rendered)
        self.assertIn("[a link](javascript:", self.rendered.replace("&#x27;", "'"))

    def test_a_real_link_is_a_link(self):
        self.assertIn('<a href="https://example.invalid/x"', self.rendered)
        self.assertIn('rel="noopener noreferrer nofollow"', self.rendered)

    def test_raw_html_block_is_shown_not_run(self):
        self.assertIn("&lt;div", self.rendered)
        self.assertNotIn("<div", self.rendered)

    def test_the_constructs_that_are_allowed_are_rendered(self):
        self.assertIn("<h3>A heading</h3>", self.rendered)
        self.assertIn("<ul>", self.rendered)
        self.assertIn("<code>code</code>", self.rendered)
        self.assertIn("<strong>strong</strong>", self.rendered)
        self.assertIn("<pre><code>", self.rendered)

    def test_a_fenced_block_keeps_its_text_and_loses_its_tags(self):
        self.assertIn("a fenced block with &lt;b&gt;tags&lt;/b&gt;", self.rendered)

    def test_never_an_h1_because_the_page_has_one(self):
        self.assertNotIn("<h1>", self.rendered)

    def test_empty_and_none_are_empty(self):
        self.assertEqual(markdown(None), "")
        self.assertEqual(markdown(""), "")


class Shapes(unittest.TestCase):
    """SVG shapes are not HTML void elements, and the difference is a bug."""

    def test_svg_shapes_close_themselves(self):
        """Inside `<svg>` the parser is in foreign content: a start tag with no
        slash opens an element that stays open, so `<rect><rect>` nests the
        second bar inside the first, where a shape never draws its children.
        HTML void elements are the other rule and keep it."""
        from sd_dashboard.markup import tag

        for shape in ("rect", "line", "circle", "path", "use"):
            with self.subTest(shape):
                self.assertEqual(tag(shape, class_="x"), f'<{shape} class="x" />')
        self.assertEqual(tag("br"), "<br>")
        self.assertEqual(tag("img", src="/a.png"), '<img src="/a.png">')

    def test_shapes_stand_beside_each_other_and_do_not_nest(self):
        """The structural reason the rule exists. Two bars in one `svg` are
        siblings; unclosed they nest, and a shape never draws its children.
        Parsed strictly, the unclosed form is not well formed at all."""
        from xml.etree import ElementTree

        from sd_dashboard.markup import join, tag

        drawn = tag(
            "svg", join([tag("rect", x=1), tag("rect", x=2)]), viewBox="0 0 8 8"
        )
        parsed = ElementTree.fromstring(str(drawn))
        self.assertEqual(len(parsed.findall("rect")), 2)
        self.assertEqual([list(shape) for shape in parsed.findall("rect")], [[], []])


class TheGrep(unittest.TestCase):
    """Criterion 12: "a grep of the templates for `|safe`, `innerHTML` and
    `mark_safe` returns nothing"."""

    def grep(self, needle: str) -> list[str]:
        """The tracked files under the dashboard, and no others.

        **No `--untracked`.** A grep about what this pull request ships must
        read what the repository holds; with the flag, local scratch and build
        output fail the assertion for a reason that has nothing to do with the
        codebase.

        The return code is checked because `git grep` exits 1 when it matches
        nothing, which is the *passing* case here. Without the check, a real
        git failure -- a bad pathspec, a broken index -- returns empty stdout
        and every assertion below passes for the wrong reason.
        """
        found = subprocess.run(
            ["git", "grep", "-n", "-I", "-F", "-e", needle,
             "--", "local-project-dashboard"],
            cwd=str(HERE.parent), capture_output=True, text=True,
        )
        self.assertIn(found.returncode, (0, 1), found.stderr)
        return [line for line in found.stdout.splitlines() if line.strip()]

    def test_the_grep_itself_works(self):
        """A needle that is certainly there, so a grep matching nothing at all
        is told apart from a grep that cannot run."""
        self.assertTrue(self.grep("Markup"))

    def test_no_escape_hatch_anywhere_in_the_dashboard(self):
        """`v2/static/markup.js` is the v2 scripts' `markup.py`: it holds their
        one HTML sink, which takes only what its escaping `html` tag built.
        `test_v2_today.TheMarkup` pins that file to that one sink."""
        for needle in ("|safe", "innerHTML", "mark_safe", "outerHTML",
                       "document.write", "dangerouslySetInnerHTML",
                       "insertAdjacentHTML", "DOMParser", "createContextualFragment"):
            hits = [
                line for line in self.grep(needle)
                if "/tests/" not in line.split(":", 1)[0]
                and "markup.py" not in line.split(":", 1)[0]
                and line.split(":", 1)[0] != V2_SINK
            ]
            self.assertEqual(hits, [], f"{needle} is in the dashboard")

    def test_no_sqlite3_connect_in_the_dashboard(self):
        """Criterion 2: the library is the only thing that opens the database."""
        hits = [
            line for line in self.grep("sqlite3." + "connect(")
            if "/tests/" not in line.split(":", 1)[0]
        ]
        self.assertEqual(hits, [], "the dashboard opens a database of its own")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
