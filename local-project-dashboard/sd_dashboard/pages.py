"""The page chrome: one layout, one stylesheet, one script.

Requirement 5's "no build step" is a construction here, not a check. There is
no template engine, no framework and no bundler: a page is `markup.tag` calls,
the stylesheet is one hand-written file and the script is one hand-written
file, both served from disk. Criterion 12 asserts the folder holds no
`package.json`, no `node_modules` and no bundler config, and there is nothing
in this module that would want one.

The head carries no inline script and no inline handler. It cannot: `tag()`
would escape a handler's body into text, and the Content-Security-Policy the
server sets on every response refuses an inline script anyway. Two locks on
one door, which is the shape requirement 5 asks for -- a filter, and behind it
a policy the browser enforces whatever slipped the filter.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from html import unescape

from .markup import Markup, join, tag

__all__ = ["SECTIONS", "Section", "page", "tile", "command_hint"]


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    path: str
    #: Only working destinations appear in navigation.
    built: bool = True
    #: Item is a section and not a destination: it is one item, reached from a
    #: row. A nav link to `/item` would be a link to a 404, which is worse
    #: than no link.
    in_nav: bool = True


SECTIONS = (
    Section("today", "Today", "/"),
    Section("backlog", "Backlog", "/backlog"),
    Section("contributions", "Contributions", "/contributions"),
    Section("writing", "Writing", "/writing"),
    Section("operations", "Operations", "/operations"),
    Section("protection", "Protection", "/protection"),
    Section("item", "Item", "/item/<id>", in_nav=False),
    Section("skills", "Skills", "/skills"),
    Section("documents", "Documents", "/documents"),
    Section("designs", "Designs", "/designs"),
)


def command_hint(command: str) -> Markup:
    """A CLI equivalent, grouped below the primary controls for touch use."""
    return tag("code", command, class_="command")


def command_reference(command: str) -> Markup:
    return tag("span", hidden=True, data_cli=command)


def tile(label: str, value: object, *, detail: Markup | None = None, href: str | None = None) -> Markup:
    """A number, its label, and its inputs in a detail view.

    Criterion 15 offers "on hover or in a detail view" and requirement 5
    forbids a hover-only affordance, so this renders the detail view: a
    `details` element the operator taps open. There is no `title` attribute
    and no tooltip anywhere on this page.
    """
    body: list[object] = [
        tag("p", label, class_="tile-label"),
        tag("p", value, class_="tile-value"),
    ]
    if href:
        body.append(tag("p", tag("a", "Open", href=href), class_="tile-open"))
    if detail is not None:
        body.append(
            tag(
                "details",
                tag("summary", "Inputs"),
                detail,
                class_="tile-detail",
            )
        )
    return tag("div", join(body), class_="tile")


def navigation(current: str) -> Markup:
    links = []
    for section in SECTIONS:
        if not section.in_nav:
            continue
        if not section.built:
            continue
        links.append(
            tag(
                "a",
                section.label,
                href=section.path,
                class_="nav-current" if section.key == current else None,
                aria_current="page" if section.key == current else None,
            )
        )
    return tag("nav", join(links), class_="nav", aria_label="Sections")


def page(title: str, current: str, *body: object, subtitle: str | None = None,
         cli_equivalents: bool = True) -> str:
    """One page, whole. The return is a `str` because it goes down the wire."""
    head = tag(
        "head",
        tag("meta", charset="utf-8"),
        tag("meta", name="viewport", content="width=device-width, initial-scale=1, viewport-fit=cover"),
        tag("meta", name="color-scheme", content="light dark"),
        tag("title", f"{title} — sd"),
        tag("link", rel="stylesheet", href="/static/dashboard.css"),
        tag("script", src="/static/dashboard.js", defer=True),
    )
    header: list[object] = [tag("h1", title)]
    if subtitle:
        header.append(tag("p", subtitle, class_="subtitle"))
    rendered_body = join(body)
    commands = list(dict.fromkeys(unescape(value) for value in
                    re.findall(r'\bdata-cli="([^"]*)"', str(rendered_body))))
    equivalents = (tag("details", tag("summary", "CLI equivalents"),
        tag("p", "Commands run from the matching repository.", class_="hint"),
        tag("ul", join(tag("li", command_hint(command)) for command in commands)),
        class_="cli-equivalents") if commands and cli_equivalents else Markup(""))
    from .palette import dialog

    document = tag(
        "html",
        head,
        tag(
            "body",
            tag("header", join(header), navigation(current),
                tag("button", "Commands", type="button", data_palette_open=True, aria_keyshortcuts="p"), class_="page-header"),
            dialog(current),
            tag("main", rendered_body, equivalents, id="main"),
            tag(
                "footer",
                tag("p", "Your work, in one place. Changes save to the local workflow database."),
                tag("noscript", "Enable JavaScript to use task controls. Reading and filtering still work."),
                class_="page-footer",
            ),
        ),
        lang="en",
    )
    return "<!doctype html>" + str(document)


def error_page(status: int, message: str) -> str:
    """An error is a page too, and it carries the same policy the others do."""
    return page(
        f"{status}",
        "today",
        tag("p", message),
        tag("p", tag("a", "Back to Today", href="/")),
    )


def empty(message: str, next_step: str) -> Markup:
    """An empty state that says what to do next (requirement 5).

    No button: the next step is a command, because this screen writes nothing.
    """
    return tag(
        "div",
        tag("p", message),
        tag("p", "Next: ", command_hint(next_step)),
        class_="empty",
    )
