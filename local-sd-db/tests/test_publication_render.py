"""Heading normalization follows prose boundaries in publication HTML."""

import html
import re
import unittest
from pathlib import Path

from sd_db.publication_render import build
from sd_db.workflow import WorkflowError


class PublicationHeadings(unittest.TestCase):
    def render(self, markdown):
        snapshot = {
            "text": "## Draft\n" + markdown,
            "path": Path("piece/index.md"),
            "hashes": {},
        }
        return build(snapshot, {"title": "Article"}, ".")["html"]

    def headings(self, rendered):
        return re.findall(r'<h([1-6])(?: [^>]*)?>(.*?)</h\1>', rendered)

    def test_fenced_comments_do_not_change_real_heading_levels(self):
        for opening, closing in (
            ("```python", "```"),
            ("~~~python", "~~~"),
            ("````python", "````"),
            ("~~~~python", "~~~~~"),
        ):
            with self.subTest(opening=opening, closing=closing):
                code = "# comment\nprint('<value>')"
                rendered = self.render(
                    f"### Before\n\n{opening}\n{code}\n{closing}\n\n#### After"
                )
                self.assertEqual(self.headings(rendered), [
                    ("1", "Article"), ("2", "Before"), ("3", "After"),
                ])
                self.assertIn(f"<pre><code>{html.escape(code)}</code></pre>", rendered)

    def test_shorter_or_mismatched_fences_keep_comments_inside_code(self):
        for opening, inner, closing in (
            ("````text", "```", "````"),
            ("~~~~", "~~~", "~~~~~"),
            ("```", "~~~", "````"),
            ("~~~", "```", "~~~"),
            ("~~~", "~~~~ trailing text", "~~~"),
        ):
            with self.subTest(opening=opening, inner=inner):
                code = f"alpha\n{inner}\n# comment\nbeta"
                rendered = self.render(
                    f"### Before\n{opening}\n{code}\n{closing}\n#### After"
                )
                self.assertEqual(self.headings(rendered), [
                    ("1", "Article"), ("2", "Before"), ("3", "After"),
                ])
                self.assertIn(f"<pre><code>{html.escape(code)}</code></pre>", rendered)

    def test_real_heading_after_code_sets_the_global_normalization(self):
        rendered = self.render("#### Before\n```\n# comment\n```\n### After")
        self.assertEqual(self.headings(rendered), [
            ("1", "Article"), ("3", "Before"), ("2", "After"),
        ])

    def test_comment_only_document_has_no_prose_headings(self):
        rendered = self.render("~~~\n# comment\n~~~")
        self.assertEqual(self.headings(rendered), [("1", "Article")])
        self.assertIn("<pre><code># comment</code></pre>", rendered)

    def test_prose_only_heading_normalization_is_unchanged(self):
        for depth in (1, 2, 3, 4):
            with self.subTest(depth=depth):
                rendered = self.render("#" * depth + " Before\n" + "#" * (depth + 1) + " After")
                self.assertEqual(self.headings(rendered), [
                    ("1", "Article"), ("2", "Before"), ("3", "After"),
                ])

    def test_short_or_mismatched_fence_still_refuses_unclosed_code(self):
        for markdown in ("````\n# comment\n```", "~~~\n# comment\n```"):
            with self.subTest(markdown=markdown):
                with self.assertRaisesRegex(WorkflowError, "unclosed fenced code block"):
                    self.render("### Before\n" + markdown)
