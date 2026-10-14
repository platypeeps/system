"""The page chrome for the server-drawn screens: the v2 shell around them (sd:3586), and one script.

Requirement 5's "no build step" is a construction here, not a check. There is
no template engine, no framework and no bundler: a page is `markup.tag` calls,
styled by the shell's hand-written `/ui/` stylesheets, with one hand-written
script beside the shell's, all served from disk. Criterion 12 asserts the folder holds no
`package.json`, no `node_modules` and no bundler config, and there is nothing
in this module that would want one.

The head carries no inline script and no inline handler. It cannot: `tag()`
would escape a handler's body into text, and the Content-Security-Policy the
server sets on every response refuses an inline script anyway. Two locks on
one door, which is the shape requirement 5 asks for -- a filter, and behind it
a policy the browser enforces whatever slipped the filter.
"""

from __future__ import annotations

import re
from html import unescape

from . import caching
from .caching import STATIC_VERSIONS
from .markup import Markup, join, tag
from .v2 import VERSIONS as UI_VERSIONS

__all__ = ["shell_page", "tile", "command_hint"]


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


def _equivalents(rendered_body: Markup) -> Markup:
    """The CLI line of every control in `rendered_body`, folded below them; nothing when no control names one."""
    commands = list(dict.fromkeys(unescape(value) for value in
                    re.findall(r'\bdata-cli="([^"]*)"', str(rendered_body))))
    return (tag("details", tag("summary", "CLI equivalents"),
        tag("p", "Commands run from the matching repository.", class_="hint"),
        tag("ul", join(tag("li", command_hint(command)) for command in commands)),
        class_="cli-equivalents") if commands else Markup(""))


def shell_page(title: str, section: str, *body: object, subtitle: str, note: str, screen: str,
               cli_equivalents: bool = True) -> str:
    """A classic screen inside the v2 shell (sd:3586): the rail, pane and keys are shell.js's, the tokens the foundation's.

    `section` is the rail entry the screen belongs to; `note` fills the Details pane; `screen` names the palette scope,
    one of `runner_exec.SCREENS` (sd:4002).
    `cli_equivalents=False` leaves out the folded list of the body's commands.
    The body keeps its server-drawn forms, which `/static/dashboard.js` submits as before; it leaves the keys to the
    shell but `/` and `p`. `/ui/classic.css` styles that markup.
    """
    from .palette import dialog

    rendered_body = join(body)
    head = tag(
        "head",
        tag("meta", charset="utf-8"),
        tag("meta", name="viewport", content="width=device-width, initial-scale=1, viewport-fit=cover"),
        tag("title", f"{title} · system"),
        tag("link", rel="stylesheet", href="/ui/fonts.css"),
        tag("link", rel="stylesheet", href="/ui/tokens.css"),
        tag("script", src="/ui/theme.js"),
        tag("script", src="/ui/markup.js"),
        tag("link", rel="stylesheet", href="/ui/shell.css"),
        tag("link", rel="stylesheet", href="/ui/classic.css"),
    )
    main = tag(
        "main",
        tag("div", tag("div", tag("h1", title), tag("p", subtitle)),
            tag("button", "Registered commands", type="button", class_="btn quiet", data_palette_open=True,
                aria_keyshortcuts="p"), class_="head"),
        tag("div", id="state", hidden=True),
        dialog(screen),
        rendered_body,
        _equivalents(rendered_body) if cli_equivalents else Markup(""),
        tag("noscript", tag("p", "Enable JavaScript to use these controls. Reading still works.", class_="hint")),
        id="main", class_="classic",
    )
    document = tag(
        "html",
        head,
        tag(
            "body",
            tag("script", src="/ui/icons.js"),
            tag("div", main, tag("div", tag("p", note, class_="note"), class_="details", id="details"), class_="shell"),
            tag("script", src="/ui/sections.js"),
            tag("script", src=f"/static/dashboard.js?v={STATIC_VERSIONS['dashboard.js']}"),
            tag("script", src="/ui/shell.js"),
            data_page=section, data_scope=section.lower(),
        ),
        lang="en",
    )
    return "<!doctype html>" + caching.link(str(document), "/ui/", UI_VERSIONS)


def error_page(status: int, message: str) -> str:
    """An error is a page too, and it carries the same policy the others do: a shell page under Today (sd:3586)."""
    return shell_page(f"{status}", "Today", tag("p", tag("a", "Back to Today", href="/")), subtitle=message,
                      note="The dashboard answered this address with an error; the rail still opens every page.",
                      screen="today")


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
