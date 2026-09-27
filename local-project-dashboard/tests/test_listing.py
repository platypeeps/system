"""The one list component: filter, selection, paging, and the order of them.

This is the component three later screens assume exists. The assertions here
are the promises those screens will be built against, which is why they are
written before the third caller rather than after it.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from sd_dashboard.listing import PAGE_SIZE, BulkAction, Column, Listing
from sd_dashboard.markup import tag


def rows(count: int):
    return [
        {"id": index, "title": f"item {index}", "repo": "alpha" if index % 2 else "beta"}
        for index in range(1, count + 1)
    ]


def listing(count: int, **columns) -> Listing:
    return Listing(
        name="fixture",
        path="/fixture",
        rows=rows(count),
        row_id=lambda row: str(row["id"]),
        row_href=lambda row: f"/item/{row['id']}",
        columns=[
            Column("title", "Item", lambda row: row["title"]),
            Column("repo", "Repository", lambda row: row["repo"]),
        ],
        **columns,
    )


class TheFilter(unittest.TestCase):
    def test_narrows_on_any_visible_column(self):
        self.assertEqual(len(listing(10, query="alpha").filtered()), 5)
        self.assertEqual(len(listing(10, query="item 3").filtered()), 1)

    def test_every_term_must_match(self):
        self.assertEqual(len(listing(10, query="alpha item 3").filtered()), 1)
        self.assertEqual(len(listing(10, query="alpha beta").filtered()), 0)

    def test_case_is_ignored(self):
        self.assertEqual(len(listing(10, query="ALPHA").filtered()), 5)

    def test_reads_hidden_columns_too(self):
        """A section names fields beyond its columns (`prd.md:848-850`)."""
        one = Listing(
            name="fixture", path="/fixture", rows=rows(4), query="secret",
            columns=[
                Column("title", "Item", lambda row: row["title"]),
                Column("hidden", "Hidden", lambda row: "secret", hidden=True),
            ],
        )
        self.assertEqual(len(one.filtered()), 4)

    def test_it_matches_what_the_reader_can_see_and_not_the_markup(self):
        """A column that draws `Markup` draws tags and attributes. The reader
        types a word off the screen, so the filter reads the screen: `span`
        and `class` are in the cell's markup and match nothing."""
        one = Listing(
            name="fixture", path="/fixture", rows=rows(4),
            columns=[
                Column("title", "Item", lambda row: row["title"]),
                Column(
                    "status", "Status",
                    lambda row: tag("span", "ready", class_="status status-ready"),
                    text=lambda row: "ready",
                ),
            ],
        )
        for invisible in ("span", "class", "status-ready"):
            with self.subTest(invisible):
                narrowed = replace(one, query=invisible)
                self.assertEqual(
                    len(narrowed.filtered()), 0,
                    f"the filter matched {invisible!r}, which is markup and not text",
                )
        self.assertEqual(len(replace(one, query="ready").filtered()), 4)

    def test_the_url_carries_it(self):
        one = listing(10, query="alpha")
        self.assertEqual(one.link(), "/fixture?q=alpha")
        self.assertIn('value="alpha"', one.render())
        self.assertIn('name="q"', one.render())

    def test_the_field_is_focusable_by_the_script(self):
        self.assertIn("data-listing-filter", listing(3).render())


class ThePaging(unittest.TestCase):
    def test_fifty_a_page(self):
        window, page = listing(120).page()
        self.assertEqual(len(window), PAGE_SIZE)
        self.assertEqual(page.pages, 3)
        self.assertEqual((page.first, page.last), (1, 50))

    def test_a_short_list_is_one_page_with_no_pager(self):
        rendered = listing(10).render()
        self.assertNotIn("listing-pager", rendered)

    def test_the_total_shows_once_the_list_passes_fifty(self):
        self.assertIn("1-50 of 120", listing(120).render())
        self.assertNotIn(" of 10", listing(10).render())

    def test_the_filter_applies_before_the_paging(self):
        """`prd.md:882`. The total under a filter is the filtered total."""
        one = listing(120, query="alpha")
        window, page = one.page()
        self.assertEqual(page.total, 60)
        self.assertEqual(page.pages, 2)
        self.assertTrue(all(row["repo"] == "alpha" for row in window))

    def test_a_page_link_carries_the_filter(self):
        one = listing(120, query="alpha")
        self.assertEqual(one.link(page=2), "/fixture?page=2&q=alpha")

    def test_a_page_past_the_end_lands_on_the_last_page(self):
        _, page = listing(60, page_number=99).page()
        self.assertEqual(page.number, 2)

    def test_a_page_that_is_not_a_number_is_page_one(self):
        query, page, selected = Listing.read_query({"page": ["../etc"], "q": ["x"]})
        self.assertEqual((query, page, selected), ("x", 1, frozenset()))


class TheSelection(unittest.TestCase):
    ACTIONS = (BulkAction("status", "Change status", "sd status <id> ready"),)

    def test_no_actions_means_no_selection_control(self):
        """Every list in this pull request. A selection nothing can act on is
        a control that does nothing."""
        one = listing(5)
        self.assertFalse(one.selectable)
        self.assertNotIn('type="checkbox"', one.render())

    def test_actions_bring_the_selection(self):
        one = listing(5, actions=self.ACTIONS)
        self.assertTrue(one.selectable)
        rendered = one.render()
        self.assertEqual(rendered.count('type="checkbox"'), 5)
        self.assertIn('name="sel"', rendered)

    def test_the_url_carries_the_selection(self):
        query, page, selected = Listing.read_query({"sel": ["3", "7"]})
        self.assertEqual(selected, frozenset({"3", "7"}))
        one = listing(10, actions=self.ACTIONS, selected=frozenset({"3"}))
        self.assertIn('value="3" checked', one.render())

    def test_a_bulk_action_must_carry_its_command(self):
        """Criterion 12: no write control without a visible command beside it.

        Enforced in the constructor, so a control with no command cannot be
        built rather than being caught by a walk of the rendered page.
        """
        with self.assertRaises(ValueError):
            BulkAction("status", "Change status", "   ")


class TheRendering(unittest.TestCase):
    def test_the_first_column_is_the_link_to_the_row(self):
        self.assertIn('<a href="/item/1">item 1</a>', listing(3).render())

    def test_values_are_escaped(self):
        one = Listing(
            name="fixture", path="/fixture",
            rows=[{"title": "<script>x</script>"}],
            columns=[Column("title", "Item", lambda row: row["title"])],
        )
        rendered = one.render()
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_an_empty_list_says_what_to_do_next(self):
        one = Listing(
            name="fixture", path="/fixture", rows=[],
            columns=[Column("title", "Item", lambda row: row["title"])],
            empty="Nothing open.", empty_next="File one.",
        )
        rendered = one.render()
        self.assertIn("Nothing open.", rendered)
        self.assertIn("File one.", rendered)

    def test_extra_facets_survive_a_filter_and_a_page(self):
        one = listing(120, query="alpha", extra={"view": "board"})
        self.assertIn("view=board", one.link(page=2))
        self.assertIn('name="view"', one.render())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
