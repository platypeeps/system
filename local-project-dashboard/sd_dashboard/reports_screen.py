"""Database reports and bounded, read-only legacy resource views."""

import importlib.util
import json
import subprocess
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from sd_db import reporting, workflow

from .controls import field, form
from .listing import Column, Listing
from .markup import Markup, escape, join, tag

# Queues joined this tuple rather than taking a screen of its own (sd:719 step
# 2). `collectors.py` has no `collect_queues` to call, but `sd_tile.py queues`
# already turns `DBS` and `db_rows` into the same document the other four
# return, so the view needed a name here and nothing else: the same budget,
# the same markup filter, and the same refusal on the page when the vault
# cannot be read. A screen of its own would have imported the collectors
# in-process, as Ports does, and read the vault inside the server process
# rather than in a child that the budget can kill.
VIEWS = (("toolbox", "Toolbox"), ("briefs", "Briefs"), ("vault", "Vault"), ("research", "Research"),
         ("queues", "Queues"))
# The child each view runs: one plugin tile, owned by this dashboard checkout
# and never supplied by a page.
TILE = Path(__file__).resolve().parents[1] / "sd_tile.py"
# Each view's time ceiling, applied by `collectors.Budget` together with the
# shared 64 KB while the tile's output is read (sd:758). Five seconds was the
# retired plugin contract's figure, and these tabs were built for it
# (`sd_tile.py`, `TILE_SECONDS`).
# Measured on 2026-09-13 through this interpreter at load average ~14, ten runs
# each: toolbox, briefs and vault 0.08s worst, research 0.53s worst. Queues,
# measured when it joined through `python3 -I sd_tile.py queues` at load
# average ~4, five runs: 0.13s worst and 6,242 bytes. Read those as the quiet
# case: on 2026-09-14, with this machine at load average 11 to 45, Toolbox,
# Vault, Research and Queues were each seen refused at this ceiling, and Briefs
# and Vault answered in 0.19-0.50s a few minutes later at load 42. The scan did
# not grow; the machine was busy. A view that outgrows its ceiling is
# refused with the reason, not waited on; file it as sd:756 was filed for Ports
# rather than raise the number here.
VIEW_SECONDS = {"toolbox": 5.0, "briefs": 5.0, "vault": 5.0, "research": 5.0, "queues": 5.0}


class SafeFragment(HTMLParser):
    """Keep structural text/table markup; never trust returned active content."""
    allowed = frozenset({"h3", "p", "table", "thead", "tbody", "tr", "th", "td", "dl", "dt", "dd", "ul", "li", "a"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []

    def handle_starttag(self, name, attrs):
        if name not in self.allowed:
            return
        attributes = dict(attrs)
        extra = ' class="listing-table"' if name == "table" else ""
        if name == "a" and str(attributes.get("href", "")).startswith(("https://", "http://")):
            extra = f' href="{escape(attributes["href"])}" rel="noopener noreferrer"'
        self.output.append(f"<{name}{extra}>")

    def handle_endtag(self, name):
        if name in self.allowed:
            self.output.append(f"</{name}>")

    def handle_data(self, data):
        self.output.append(str(escape(data)))


def _collectors():
    # `Budget` is defined once, in the collectors this dashboard owns, and read
    # by path as `ports_screen.py` reads it.
    path = Path(__file__).resolve().parents[1] / "collectors.py"
    spec = importlib.util.spec_from_file_location("sd_dashboard_resource_collectors", path)
    if spec is None or spec.loader is None:
        raise ValueError("resource budget is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect(area):
    if area not in dict(VIEWS):
        raise ValueError("unknown resource view")
    module = _collectors()
    budget = module.Budget(VIEW_SECONDS[area])
    try:
        # Both ceilings apply while reading: past either one the tile's process
        # group is killed then, not after it exits (sd:758). A cut of the
        # tile's stderr is named for the tab, not the interpreter (sd:834).
        process = budget.run([sys.executable, "-I", str(TILE), area], label=area)
    except module.OverBudget as error:
        raise ValueError(f"resource inspection was stopped at its budget: {error}") from None
    if process.returncode:
        raise ValueError(process.stderr.strip() or f"resource collector exited {process.returncode} without a reason")
    raw = process.stdout
    result = json.loads(raw)
    if not isinstance(result, dict) or not isinstance(result.get("html"), str):
        raise ValueError("resource collector returned incomplete output")  # noqa: TRY004 - external document validation
    rows = result.get("rows", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict)
            or not isinstance(row.get("what"), str) or not isinstance(row.get("detail"), str) for row in rows):
        raise ValueError("resource collector returned incomplete observations")  # noqa: TRY004 - external document validation
    parser = SafeFragment()
    parser.feed(result["html"])
    notices = tag("section", tag("h3", "Needs attention"),
        tag("dl", join(tag("div", tag("dt", row["what"]), tag("dd", row["detail"])) for row in rows)),
        class_="notice") if rows else ""
    return join((Markup("".join(parser.output)), notices))


def resources(parameters, *, backend=None):
    area = (parameters.get("resource") or ["toolbox"])[0]
    if area not in dict(VIEWS):
        area = "toolbox"
    navigation = tag("nav", join(tag("a", label, href=f"/operations?area=resources&resource={key}",
        aria_current="page" if key == area else None) for key, label in VIEWS), class_="view-toggle", aria_label="Resources")
    try:
        content = (backend or collect)(area)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        content = tag("p", "This resource could not be observed: " + str(error), class_="notice")
    return tag("section", tag("h2", "Resources"), navigation,
        tag("p", "Read-only observations from the existing local collectors. Nothing here starts jobs or edits vault notes.", class_="hint"), content)


#: How many rows of each preview list the page renders; the counts are whole.
PREVIEW_ROWS = 200


def _preview_list(heading, rows, describe):
    """One of the two preview lists: its whole count in the heading, its
    first `PREVIEW_ROWS` rows, and how many the page does not show."""
    shown = rows[:PREVIEW_ROWS]
    return tag("section", tag("h3", f"{heading} ({len(rows)})"),
        tag("ul", join(tag("li", tag("a", f"#{row['id']}", href=f"/item/{row['id']}"), " ", describe(row))
                       for row in shown)) if shown else tag("p", "None.", class_="hint"),
        tag("p", f"and {len(rows) - len(shown)} more", class_="hint") if len(rows) > len(shown) else "")


def _clean_preview(connection, parameters, *, now):
    """sd:755. Step one of the bulk acknowledge: a GET that writes nothing.

    The date is asked for with a plain GET form, outside the form script, so
    the page can be reloaded to the same preview. With `clean_before` the
    panel runs the same `clean_reports` the CLI runs, at the stamp `cutoff`
    makes of the date, and renders what it would move and what it declines.
    The apply form carries that stamp and the plan in hidden fields; a
    person types `who`. A date `cutoff` refuses, or one that has not begun
    at `now`, is a notice and no form; so is a selection with no plan, empty
    or above `MAX_BATCH`, which the apply would refuse as stale anyway.
    """
    requested = parameters.get("clean_before")
    asked = tag("form", tag("input", type="hidden", name="area", value="reports"),
        tag("div", tag("label", "Created before (00:00 UTC on)", for_="clean-before"),
            tag("input", type="date", id="clean-before", name="clean_before", required=True,
                value=requested[0] if requested else ""), class_="field"),
        tag("div", tag("button", "Preview clean reports", type="submit", class_="primary"), class_="form-actions"),
        method="get", action="/operations", class_="workflow-form compact-form")
    body = ""
    if requested:
        try:
            before = reporting.cutoff(requested[0])
            # The dashboard clock is a `...Z` string; `clean_reports` takes
            # an aware datetime, and refuses a cutoff later than it.
            preview = reporting.clean_reports(connection, before=before, now=datetime.fromisoformat(now))
        except workflow.WorkflowError as error:
            body = tag("p", str(error), class_="notice")
        else:
            count, plan = preview["count"], preview["plan"]
            if plan is None:
                reason = (f"Nothing is clean before {before}." if count == 0 else
                          f"That is more than {preview['max_batch']} clean reports, and one acknowledge moves at most "
                          f"{preview['max_batch']}. Choose an earlier date.")
                apply = tag("p", reason, class_="notice")
            else:
                apply = form("/api/reports/acknowledge-clean",
                    tag("input", type="hidden", name="before", value=before),
                    tag("input", type="hidden", name="plan", value=plan),
                    field("Acknowledged by", "who", required=True, maxlength=200, autocomplete="off"),
                    label=f"Acknowledge {count} report{'' if count == 1 else 's'}",
                    command=f"sd reports acknowledge --all-clean --before {before} --apply --if-plan {plan} --who NAME",
                    reload_label="Preview again")
            body = join((
                _preview_list("Would acknowledge", preview["selected"],
                              lambda row: f"{row['job'] or 'unknown job'}, recorded {row['created_at']}"),
                _preview_list("Declined", preview["declined"], lambda row: row["why"]),
                apply))
    return tag("section", tag("h2", "Acknowledge clean reports"),
        tag("p", "Preview the reports retention would settle, then acknowledge them in one attributed batch. "
                 "The preview writes nothing; the batch is recorded as a report of its own.", class_="hint"),
        asked, body, class_="control-panel")


def reports_panel(connection, parameters, *, now):
    rows = reporting.reports(connection)
    query, number, selected = Listing.read_query(parameters)
    listing = Listing("reports", [Column("title", "Report", lambda row: row["title"]),
        Column("created_at", "Recorded", lambda row: row["created_at"]),
        Column("status", "Status", lambda row: row["status"])], rows, path="/operations",
        extra={"area": "reports"}, query=query, page_number=number, selected=selected,
        row_href=lambda row: f"/item/{row['id']}", empty="No reports have been recorded yet.")
    return tag("section", tag("h2", "Reports"),
        tag("p", "Scheduled job output, with its source and findings. Existing emails keep going. Open a report to follow up, assign work or acknowledge it.", class_="hint"),
        _clean_preview(connection, parameters, now=now),
        tag("p", "Showing up to the newest 200 reports.", class_="hint"), listing.render())


def report_controls(connection, row, revision):
    if row["kind"] != "report":
        return ""
    try:
        # The library's parse, so the verdict is the one retention and the
        # bulk clean give (`reporting.UNREADABLE_FIELDS`): NULL, '', text that
        # is not JSON, `NaN`, and nesting past SQLite's depth are one case.
        # Before sd:873 the parse was `json.loads(row["fields"] or "{}")`,
        # which turned NULL and '' into an empty document: every provenance
        # field read "Not recorded" and the acknowledge form was offered.
        fields = reporting.fields_document(connection, row["fields"])
    except reporting.UnreadableFields:
        # sd:775. `item.fields` has no `json_valid` check on it, so a row the
        # page cannot parse is a row the store permits; unguarded, the parse
        # raised through `screens.item` and `do_GET` -- which catches
        # `MissingItem`, `NotFound` and the database errors and not this --
        # and the reader got no response at all, not even an error page.
        # Fail closed the way the same column is treated elsewhere: one
        # meaning for NULL, '' and text that is not JSON, the
        # `reporting.UNREADABLE_FIELDS` predicate retention and the bulk
        # clean read (sd:754 I11, sd:755, sd:873). Such a report is for a
        # person to look at, and this page is that person's surface: it
        # renders, the reason is plain, and the acknowledge form is withheld
        # rather than offered beside a source nothing could read, because
        # one click here would close unexamined the row those two declined.
        # `reporting.acknowledge` never parses `fields`, so the CLI, whose
        # acknowledge takes a named `who`, still finishes such a report and
        # the notice names it.
        return tag("section", tag("h2", "Report source"),
            tag("p", "This report's stored fields are missing or not valid JSON, so its source cannot be "
                     "read and acknowledging it here is disabled. Acknowledge it from the command line: ",
                tag("code", f"sd reports acknowledge {row['id']}"), ".", class_="notice"),
            class_="control-panel")
    provenance = fields.get("report", {}) if isinstance(fields, dict) else {}
    if not isinstance(provenance, dict):
        provenance = {}
    # sd:920: a job failing the same way on a schedule folds into one report,
    # and `fields.repeats` carries the count and the last run. One row, only
    # when the key exists; a single failure's page is unchanged.
    repeats = fields.get("repeats") if isinstance(fields, dict) else None
    repeated = (tag("div", tag("dt", "Repeats"),
                    tag("dd", f"{repeats.get('count', '?')} runs, last {repeats.get('last_ended', 'Not recorded')}"))
                if isinstance(repeats, dict) else "")
    return tag("section", tag("h2", "Report source"),
        tag("dl", join(tag("div", tag("dt", label), tag("dd", provenance.get(key, "Not recorded")))
            for key, label in (("job", "Job"), ("started", "Started"), ("ended", "Ended"), ("exit_code", "Exit code"),
                               ("source_path", "Source log"), ("attention_basis", "Attention evidence"))), repeated),
        tag("p", "Output was truncated. The complete source remains in the log.", class_="notice") if provenance.get("truncated") else "",
        form(f"/api/reports/{row['id']}/acknowledge", label="Acknowledge report", command=f"sd reports acknowledge {row['id']}", revision=revision)
        if row["status"] != "done" else tag("p", "Acknowledged"), class_="control-panel")
