"""The finite command picker and a read-only execution history."""

import json

from sd_db import runner_exec

from .markup import join, tag


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
    return tag("span", text, " · ", tag("a", "Execution history", href=f"/operations?area=commands#execution-{note['id']}")), text


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
        tag("a", "Execution history", href="/operations?area=commands"),
        id="command-palette", data_screen=screen, aria_labelledby="palette-title")


def history(connection):
    rows = runner_exec.executions(connection)
    return tag("section", tag("h2", "Execution history"),
        tag("p", "Commands are recorded before they start. An unfinished response stays visible until its outcome is known.", class_="hint"),
        join(tag("article", tag("h3", row["command"]),
            tag("p", tag("a", f"#{row['item']} · {row['title']}", href=f"/item/{row['item']}")),
            tag("p", f"{row['started']} · {row['session']} · " +
                (f"Exit {row['exit_code']}" if row["ended"] else "Unfinished") +
                (" · output expired" if row.get("output_expired") else "")),
            tag("button", "Read output", type="button", data_execution=row["id"]),
            tag("button", "Reconcile outcome", type="button", data_execution_reconcile=row["id"]) if not row["ended"] else "",
            tag("pre", data_execution_output=row["id"], tabindex="0"), class_="execution-record", id=f"execution-{row['id']}") for row in rows)
        if rows else tag("p", "No palette commands have run yet."))
