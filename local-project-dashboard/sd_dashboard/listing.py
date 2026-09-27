"""The one list component: filter, selection and paging.

Requirement 5, `prd.md:847-861` and `prd.md:880-882`: "Every list has the same
three controls, rendered by one component." Six lists across five screens use
it -- Backlog in all three of its views, Item's notes, Skills, Providers,
Usage, the command log, Dependencies -- and the reason it lands with the first
screen rather than inside it is that three later screens assume it exists. A
component grown out of the first caller is three divergent copies by the
third.

The three controls, and what each one promises:

* **Filter.** One field, narrowing on every visible column and on the extra
  fields the section names. `/` focuses it, and *the URL carries it*, so a
  filtered view is a link the operator can send to themselves. The filter is
  applied by the server; the script does not hide rows, because a row hidden
  by a script is still in the page and still in the count.
* **Selection.** One tap per row, plus select-all-shown, and the bulk actions
  the section declares. A section with no bulk actions renders no selection
  control at all -- which is every list in this pull request, because this
  pull request has no writes. The code path is here and tested against a
  fixture section; what is missing is only the actions.
* **Paging.** Fifty rows a page with the total shown, once a list passes
  fifty. **The filter applies before the paging**, so the total under a
  filter is the filtered total and page two of a filtered list is page two of
  what the operator can see.

Nothing here is hover-only: the selection is a control that is always
rendered, the page links are links, and the filter is a field. Requirement 5
forbids a hover-only affordance and this is the component every list's
affordances come from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import urlencode

from .markup import Markup, escape, join, tag

__all__ = ["BulkAction", "Column", "Listing", "PAGE_SIZE", "SHOW_TOTAL_ABOVE"]

#: `prd.md:880-882`. Fifty, and the total once a list passes it.
PAGE_SIZE = 50
SHOW_TOTAL_ABOVE = 50


@dataclass(frozen=True)
class Column:
    """One visible column. `value` is what the filter matches and what shows."""

    key: str
    label: str
    value: Callable[[object], object]
    #: What the filter matches, when that is not what the cell draws. A column
    #: whose `value` returns `Markup` draws tags and attributes, and a filter
    #: reading those matches `span` and `class` on every row -- the reader
    #: typed a word they can see, so the filter has to read the words they can
    #: see. Columns whose value is already plain text leave this `None`.
    text: Callable[[object], object] | None = None
    css: str | None = None
    #: A column the filter reads but the table does not draw, for the fields a
    #: section names beyond its columns (`prd.md:848-850`).
    hidden: bool = False
    #: Omit a vacant display column while keeping its raw text searchable.
    hide_empty: bool = False
    short_label: str | None = None


@dataclass(frozen=True)
class BulkAction:
    """A bulk action, and the command that does the same thing.

    `command` is not decoration: criterion 12 walks the rendered pages and
    asserts no write control lacks a visible command beside it. An action
    constructed without one cannot be rendered.
    """

    name: str
    label: str
    command: str

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise ValueError(
                f"bulk action {self.name!r} has no command; criterion 12 requires "
                "the command beside the button"
            )


@dataclass(frozen=True)
class Page:
    number: int
    count: int
    total: int
    size: int = PAGE_SIZE

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.size))

    @property
    def first(self) -> int:
        return 0 if not self.total else (self.number - 1) * self.size + 1

    @property
    def last(self) -> int:
        return min(self.total, self.number * self.size)


@dataclass
class Listing:
    """One list's three controls over one row set."""

    name: str
    columns: Sequence[Column]
    rows: Sequence[object]
    #: Where the form submits and the page links point. The filter and the page
    #: ride in the query string of this path, which is what makes a filtered
    #: view a link.
    path: str
    query: str = ""
    page_number: int = 1
    selected: frozenset[str] = frozenset()
    actions: Sequence[BulkAction] = ()
    row_id: Callable[[object], str] | None = None
    row_href: Callable[[object], str] | None = None
    #: Extra query-string keys that must survive a filter or a page link --
    #: the Backlog's `view`, a kind or repo facet. Carried, never interpreted.
    extra: Mapping[str, str] = field(default_factory=dict)
    #: What this list's three controls are called in the query string, before
    #: `q`, `page` and `sel`. Empty for the one list on a page, which is every
    #: list but Sessions'. Two lists sharing a page shared those three names
    #: too, so filtering the processes also hid matching worktrees and paging
    #: one table paged the other (PR #427 review). A prefix gives each list
    #: its own; the sibling's state rides in `extra` so a filter or a page
    #: link carries it past unchanged. `dashboard.js` still writes a bare
    #: `sel`, which holds because a selection needs a bulk action and no
    #: prefixed list declares one; a prefixed list that does needs the script
    #: told its prefix.
    keys: str = ""
    empty: str = "Nothing here."
    empty_next: str | None = None

    # -- the filter --------------------------------------------------------

    def _haystack(self, row: object) -> str:
        return "\n".join(
            str((column.text or column.value)(row) or "").lower()
            for column in self.columns
        )

    def filtered(self) -> list[object]:
        """The rows the filter leaves. Every term must match; case is ignored."""
        terms = [term for term in self.query.lower().split() if term]
        if not terms:
            return list(self.rows)
        kept = []
        for row in self.rows:
            haystack = self._haystack(row)
            if all(term in haystack for term in terms):
                kept.append(row)
        return kept

    # -- the paging --------------------------------------------------------

    def page(self) -> tuple[list[object], Page]:
        """The filter first, then the page. That order is `prd.md:882`."""
        kept = self.filtered()
        total = len(kept)
        pages = max(1, -(-total // PAGE_SIZE))
        number = min(max(1, self.page_number), pages)
        start = (number - 1) * PAGE_SIZE
        window = kept[start : start + PAGE_SIZE]
        return window, Page(number=number, count=len(window), total=total)

    # -- the URL -----------------------------------------------------------

    def link(self, *, query: str | None = None, page: int | None = None, **facets: str) -> str:
        """A link to this list with some facets changed.

        A facet passed empty is *removed*, not ignored. That is what makes a
        "show every age" link expressible: without it, `extra` would carry the
        current age straight past a caller trying to drop it, and the way out
        of a filter would be the only link on the page that did not work.
        """
        parameters = dict(self.extra)
        for key, value in facets.items():
            if value:
                parameters[key] = value
            else:
                parameters.pop(key, None)
        text = self.query if query is None else query
        if text:
            parameters[f"{self.keys}q"] = text
        number = self.page_number if page is None else page
        if number and number > 1:
            parameters[f"{self.keys}page"] = str(number)
        if not parameters:
            return self.path
        return f"{self.path}?{urlencode(sorted(parameters.items()))}"

    @classmethod
    def read_query(cls, parameters: Mapping[str, Sequence[str]], *,
                   keys: str = "") -> tuple[str, int, frozenset[str]]:
        """The three controls' state, out of the query string.

        A page that is not a number is page one, not an error: a hand-edited
        URL should land the operator on the list, not on a stack trace.

        `keys` is the prefix the list's own `keys` field carries. Read once
        per list, so two lists on one page read two states.
        """
        query = (parameters.get(f"{keys}q") or [""])[0][:200]
        raw = (parameters.get(f"{keys}page") or ["1"])[0]
        try:
            page = max(1, int(raw))
        except ValueError:
            page = 1
        selected = frozenset(parameters.get(f"{keys}sel") or ())
        return query, page, selected

    @staticmethod
    def carried(keys: str, query: str, page: int) -> dict[str, str]:
        """One list's controls as query-string keys, for a sibling's `extra`.

        Only the values that are not the default: an empty filter and page one
        are what a missing key already means, and writing them would put
        `worktrees-page=1` in every link on the page. Taken as values rather
        than off a `Listing`, because each list needs the other's state before
        either one is built.
        """
        carried = {}
        if query:
            carried[f"{keys}q"] = query
        if page > 1:
            carried[f"{keys}page"] = str(page)
        return carried

    # -- the rendering -----------------------------------------------------

    @property
    def selectable(self) -> bool:
        """A selection exists only where a bulk action does.

        No actions, no checkboxes: a selection the operator cannot act on is a
        control that does nothing, and this pull request ships no writes.
        """
        return bool(self.actions)

    def controls(self, page: Page) -> Markup:
        """The filter field and the count, above the rows."""
        hidden = [
            tag("input", type="hidden", name=key, value=value)
            for key, value in sorted(self.extra.items())
        ]
        summary: list[object] = []
        if page.total > SHOW_TOTAL_ABOVE:
            summary.append(
                tag(
                    "p",
                    f"{page.first}-{page.last} of {page.total}",
                    class_="listing-count",
                )
            )
        elif page.total:
            summary.append(
                tag("p", f"{page.total} shown", class_="listing-count")
            )
        return tag(
            "div",
            tag(
                "form",
                tag(
                    "label",
                    tag("span", "Filter", class_="visually-hidden"),
                    tag(
                        "input",
                        type="search",
                        name=f"{self.keys}q",
                        value=self.query,
                        placeholder=f"Filter {self.name}",
                        data_listing_filter=self.name,
                        autocomplete="off",
                        enterkeyhint="search",
                    ),
                    class_="listing-filter",
                ),
                join(hidden),
                tag("button", "Filter", type="submit", class_="listing-go"),
                method="get",
                action=self.path,
                role="search",
                class_="listing-form",
            ),
            join(summary),
            class_="listing-controls",
        )

    def pager(self, page: Page) -> Markup:
        if page.pages <= 1:
            return Markup("")
        links: list[object] = []
        if page.number > 1:
            links.append(tag("a", "Previous", href=self.link(page=page.number - 1), rel="prev"))
        links.append(tag("span", f"Page {page.number} of {page.pages}"))
        if page.number < page.pages:
            links.append(tag("a", "Next", href=self.link(page=page.number + 1), rel="next"))
        return tag("nav", join(links), class_="listing-pager", aria_label=f"{self.name} pages")

    def table(self, window: Sequence[object]) -> Markup:
        columns = [column for column in self.columns if not column.hidden and
                   (not column.hide_empty or any((column.text or column.value)(row) for row in window))]
        head = []
        if self.selectable:
            head.append(tag("th", "Select", scope="col", class_="listing-select"))
        for column in columns:
            label = (join((tag("span", column.short_label, aria_hidden="true"),
                           tag("span", column.label, class_="visually-hidden")))
                     if column.short_label else column.label)
            head.append(tag("th", label, scope="col", class_=column.css))
        body = []
        for row in window:
            cells = []
            if self.selectable and self.row_id:
                identifier = self.row_id(row)
                cells.append(
                    tag(
                        "td",
                        tag(
                            "input",
                            type="checkbox",
                            name=f"{self.keys}sel",
                            value=identifier,
                            checked=identifier in self.selected,
                        ),
                        class_="listing-select",
                    )
                )
            for index, column in enumerate(columns):
                value = column.value(row)
                if index == 0 and self.row_href:
                    value = tag("a", value, href=self.row_href(row))
                cells.append(tag("td", value, class_=column.css))
            body.append(tag("tr", join(cells)))
        return tag(
            "table",
            tag("thead", tag("tr", join(head))),
            tag("tbody", join(body)),
            class_="listing-table",
        )

    def render(self) -> Markup:
        window, page = self.page()
        if not page.total:
            empty: list[object] = [tag("p", self.empty)]
            if self.empty_next:
                empty.append(tag("p", self.empty_next, class_="listing-next"))
            return tag(
                "section",
                self.controls(page),
                tag("div", join(empty), class_="listing-empty"),
                class_="listing",
                data_listing=self.name,
            )
        return tag(
            "section",
            self.controls(page),
            self.table(window),
            self.pager(page),
            class_="listing",
            data_listing=self.name,
        )


def columns_of(pairs: Iterable[tuple[str, str, Callable[[object], object]]]) -> list[Column]:
    return [Column(key=key, label=label, value=value) for key, label, value in pairs]
