"""The finite command picker on v1 screens. Its history is in Activity, which lists the execution journal (sd:3402)."""

import json

from .markup import tag


def note_summary(note):
    """Ordinary Notes expose a command result, while storage keeps its descriptor."""
    try:
        value = json.loads(note["body"])
    except (TypeError, ValueError):
        value = {}
    name = value.get("command") if isinstance(value, dict) else None
    if not isinstance(name, str):
        name = "Command execution"
    state = f"Finished · exit {note['exit_code']}" if note["ended"] else "Unfinished · outcome pending"
    text = f"{name} · {state}"
    return tag("span", text, " · ", tag("a", "Open in Activity", href=f"/activity?kind=command&range=all&row=cmd:{note['id']}")), text


def dialog(screen):
    return tag("dialog",
        tag("div", tag("h2", "Commands", id="palette-title"),
            tag("button", "Close", type="button", data_palette_close=True), class_="dialog-header"),
        tag("p", "Choose an action here or run a registered command for an item.", class_="hint"),
        tag("label", "Filter commands", tag("input", type="search", data_palette_filter=True, autocomplete="off")),
        tag("div", data_palette_actions=True),
        tag("p", "Loading registered commands…", data_palette_message=True, role="status"),
        tag("form", tag("label", "Item", tag("select", name="item", required=True)),
            tag("label", "Command", tag("select", name="command", required=True)),
            tag("p", "Choose a registered command to see what it runs.", data_palette_cli=True, class_="command"),
            tag("p", data_palette_scope=True, class_="hint"),
            tag("div", data_palette_values=True),
            tag("button", "Run command", type="submit"), method="post", action="/api/palette/prepare",
            data_workflow_form=True, data_palette_form=True),
        tag("pre", data_palette_output=True, aria_live="polite", tabindex="0"),
        tag("a", "Command history in Activity", href="/activity?kind=command&range=all"),
        id="command-palette", data_screen=screen, aria_labelledby="palette-title")


