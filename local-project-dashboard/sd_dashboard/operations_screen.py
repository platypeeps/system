"""Current operations and only the controls the shared backend can honor."""

import contextlib
import re
import signal
from pathlib import Path
from urllib.parse import quote, urlencode

from sd_db import operations, reads, registry, usage
from sd_db.errors import SdDbError

from . import job_failures
from .charts import age_histogram_svg
from .controls import field, form, select
from .listing import Column, Listing
from .markup import Markup, join, tag
from .pages import page, tile

#: A bill name the cap route (`/api/bills/<name>/cap`, `server.action_route`)
#: accepts in its path. The registry puts no rule on a bill name:
#: `registry.parse` takes every key of the `bills` mapping, and the loader's
#: key is whatever stands before the first unquoted colon (`_split_key` in
#: `local-sd-db/sd_db/yaml_lite.py`), so the bound is this route's own and
#: is the services route's, character for character, 200 at most (#435's
#: verification round: a 100 bound here left a name the services route
#: would take answering 404). `_bill_caps` renders a form only for a name
#: it matches, so no form posts to a path that answers 404.
BILL_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}"

AREAS = (("jobs", "Jobs"), ("services", "Services"), ("ports", "Ports"), ("progress", "Progress"),
         ("usage", "Usage"), ("reports", "Reports"), ("resources", "Resources"), ("trackers", "Trackers"),
         ("repos", "Repos"), ("sessions", "Sessions"), ("commands", "Commands"))


def _navigation(area):
    return tag("nav", join(tag("a", label, href=f"/operations?area={key}",
        class_="toggle-current" if area == key else None,
        aria_current="page" if area == key else None) for key, label in AREAS),
        class_="view-toggle", aria_label="Operations areas")


def _progress(connection, now):
    from sd_db import reporting

    observed = reporting.metrics(connection, now=now)
    rows = [row for row in reads.backlog_items(connection, now=now) if row["status"] != "done"]

    # Tasks reads the query v1 /backlog read: view, active, age and status (sd:2589).
    def tasks_link(bucket, series):
        filters = {"view": "list", "active": "1", "age": bucket.key}
        if series == "ready_to_send":
            filters["status"] = "ready_to_send"
        return "/tasks?" + urlencode(sorted(filters.items()))

    return tag("section", tag("h2", "Observed workflow activity"),
        tag("p", f"{observed['since']} through {observed['until']}", class_="hint"),
        tag("div", tile("requests recorded", observed["recorded_requests"]),
            tile("finished review attempts", observed["finished_review_attempts"]),
            tile("recorded deliveries", observed["recorded_deliveries"]), class_="tiles"),
        tag("p", observed["interpretation"], class_="hint"), tag("h2", "Age in status"),
        tag("p", "All active items across repositories, excluding completed and parked items. Task filters do not affect this chart.", class_="hint"),
        tag("p", "Select a bar to open that age bucket in Tasks.", class_="hint"),
        age_histogram_svg(reads.age_histogram(rows, now=now), link=tasks_link))


def _number_detail(number: reads.Number):
    return tag("dl", join(tag("div", tag("dt", label), tag("dd", value))
                          for label, value in number.inputs), class_="tile-inputs")


def _format(number: reads.Number):
    if number.value is None:
        return "—"
    if number.unit == "usd":
        return f"${number.value:,.2f}"
    if number.unit == "hours":
        return f"{number.value:,.1f} h"
    return f"{number.value:,.1f}" if number.value % 1 else f"{int(number.value)}"


@contextlib.contextmanager
def _one_snapshot(connection):
    """One deferred read transaction around the two store reads the page compares.

    `usage.bills` and `usage.read` each open their own when the connection
    is outside one, so the cost tile and the month card were two snapshots,
    and a cap committed between them put one cap on the tile and another on
    the card below it (PR #438 review). Both yield inside a caller's
    transaction instead (`usage._snapshot`), so the one opened here is the
    one they share. Deferred, as theirs is: the connection is read-only and
    `isolation_level=None`, and WAL keeps a reader's snapshot from its first
    read to its COMMIT.
    """
    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN")
    try:
        yield
    finally:
        connection.execute("COMMIT")


def _usage(connection, now, parameters):
    from .usage_screen import read_month

    # The snapshot covers these two reads and nothing else. The render below
    # runs `git` across every registered repository, twice, with a timeout
    # each, and a read transaction held across that keeps the WAL from
    # checkpointing past it for as long as the slowest checkout takes (Codex
    # review of PR #501). Under the registry's merged caps, as the month card
    # and `sd usage` are (`usage.caps`): a legacy row cap on a `start` bill
    # is no cap here.
    with _one_snapshot(connection):
        bills = usage.bills(connection, now=now)
        month, month_error = read_month(connection, now=now, parameters=parameters)
    return _usage_section(connection, now, parameters, bills, month, month_error)


def _usage_section(connection, now, parameters, bills, month, month_error):
    from .usage_screen import usage_panel

    cost_tile = tag("div", join(tag("p",
        f"{row['name']}: ${row['spent']:,.2f}"
        + (f" of ${row['cap_usd_month']:,.2f}" if row["cap_usd_month"] else "")
        + (f" (+${row['reserved']:,.2f} reserved)" if row["reserved"] else ""), class_="cost-line")
        for row in bills) or tag("p", "No bills yet.", class_="cost-line"), class_="cost-tile")
    numbers = reads.weekly_numbers(connection, now=now)
    tiles = [tile(number.label, _format(number), detail=_number_detail(number)) for number in numbers]
    scorecard = reads.scorecard(connection, now=now)
    scorecard_rows = tag("table", tag("thead", tag("tr", join(tag("th", label, scope="col")
        for label in ("Provider", "Author", "Reviewer", "Passes", "Blocking", "$/pass", "Skipped", "State")))),
        tag("tbody", join(tag("tr", tag("td", row.provider),
            tag("td", row.author_rank if row.author_rank is not None else "—"),
            tag("td", row.reviewer_rank if row.reviewer_rank is not None else "—"),
            tag("td", row.passes), tag("td", row.blocking),
            tag("td", "—" if row.usd_per_pass is None else f"${row.usd_per_pass:,.2f}"),
            tag("td", row.fallthrough), tag("td", "enabled" if row.enabled else f"disabled: {row.reason or '—'}"))
            for row in scorecard)), class_="listing-table")
    return tag("section", tag("h2", "Week, cost and provider details"),
        tag("h2", "The week"), tag("div", join(tiles), class_="tiles"),
        tag("h2", "Cost"), cost_tile,
        usage_panel(connection, now=now, parameters=parameters, month=month, error=month_error),
        tag("h2", "Providers this month"), scorecard_rows,
        _provider_controls(connection))


def _provider_controls(connection):
    from sd_db import provider_controls

    try:
        current = provider_controls.snapshot(connection)
    except (SdDbError, OSError, ValueError) as error:
        return tag("p", "Provider controls unavailable: " + str(error), class_="notice")
    enabled = [select(f"{provider['name']} · {provider['vendor']}", "enabled." + provider["name"],
                [("true", "Enabled"), ("false", "Disabled")], str(provider["enabled"]).lower(), id="provider-" + provider["name"])
               for provider in current["providers"]]
    return tag("details", tag("summary", "Configure providers"),
        tag("p", "Save enabled choices and both automatic role orders together. Omitted providers remain available for explicit requests when enabled. Each order needs an enabled provider. The first enabled author and reviewer must differ.", class_="hint"),
        form("/api/providers/configure", tag("div", join(enabled), class_="field-row"),
            join(select("First " + role, "first_" + role, [(name, name) for name in current["orders"][role]],
                 next((name for name in current["orders"][role] if any(entry["name"] == name and entry["enabled"] for entry in current["providers"])), ""),
                 id="first-" + role) for role in ("author", "reviewer")),
            field("Automatic author order (first choice first, comma-separated)", "author_order", ", ".join(current["orders"]["author"])),
            field("Automatic reviewer order (first choice first, comma-separated)", "reviewer_order", ", ".join(current["orders"]["reviewer"])),
            label="Save provider configuration", command="sd providers configure --file CONFIG.json", revision=current["revision"]),
        _bill_caps(connection, current),
        class_="control-panel")


def _bill_caps(connection, current):
    """One cap form per bill, the new number typed beside the old one (sd:234, prd
    "Providers": "raise or lower a cap in one action with the new number typed
    beside the old one"), posting to `/api/bills/<name>/cap` over
    `provider_controls.set_cap` with the snapshot's revision as the hidden field.

    A bill a `start` entry is billed to gets no number field: the library
    would refuse the cap with the reader's sentence (clause 15.15), so the
    screen says so instead of offering a save that cannot land. `snapshot`
    names each provider's transport and not its bill, so which bills those
    are is read from the registry, the read `snapshot` itself made.
    `snapshot`'s `warnings` -- a legacy row cap on such a bill, which `merge`
    reads past and leaves out of the merged view -- render above the forms,
    which is where slice 12a said they would, and the bill they name gets the
    one save its row can take, the repair the library's docstring names: a
    clear-only form posting `null`. The merged view hides that cap, and since
    slice 12h the cost tile prints the merged view too, so the row is asked
    directly through `reads.cost_by_bill` with no `caps`.

    The field starts at the current cap, so a save with nothing typed is the
    library's "unchanged" and writes no row; blank clears. No pack verb sets
    a cap yet, so the form carries no `data-cli` and the palette lists none.
    """
    spawned = {entry.bill for entry in registry.read(connection=connection).providers.values() if entry.kind == "start"}
    held = {row["name"]: row["cap_usd_month"] for row in reads.cost_by_bill(connection)}
    warnings = (tag("ul", join(tag("li", warning) for warning in current["warnings"]), class_="notice")
                if current["warnings"] else Markup(""))
    rows = []
    for bill in current["bills"]:
        name, cap = bill["name"], bill["cap_usd_month"]
        printed = f"${cap:,.2f} a month" if cap is not None else "no cap"
        if name in spawned and held.get(name) is not None and re.fullmatch(BILL_NAME, name):
            rows.append(form(f"/api/bills/{quote(name, safe='')}/cap",
                tag("p", tag("strong", name), f" · the row holds a cap of ${held[name]:,.2f} that nothing enforces (above); clearing it is the one write this bill takes.", class_="cost-line"),
                tag("input", type="hidden", name="cap_usd_month", value=""),
                label=f"Clear the legacy cap for {name}", command=None, revision=current["revision"], compact=True))
        elif name in spawned:
            rows.append(tag("p", f"{name}: no cap can be set here; a 'start' entry is billed to it.", class_="hint"))
        elif not re.fullmatch(BILL_NAME, name):
            rows.append(tag("p", f"{name}: its name is outside what the cap route accepts.", class_="hint"))
        else:
            rows.append(form(f"/api/bills/{quote(name, safe='')}/cap",
                tag("p", tag("strong", name), f" · current cap: {printed}", class_="cost-line"),
                # `str`, because `field` reads a falsy value as blank and a cap of zero is a cap.
                field("New cap (USD a month; blank clears it)", "cap_usd_month", "" if cap is None else str(cap),
                      type="number", min=0, step="any", inputmode="decimal", id=f"cap-{name}"),
                label=f"Save cap for {name}", command=None, revision=current["revision"], compact=True))
    return tag("div", tag("h3", "Bill caps"),
        tag("p", "A cap is the number the ledger refuses a reservation past, per bill and month; it is written as the bill's row and never to the file.", class_="hint"),
        warnings, join(rows), class_="bill-caps")


def _action(entity, identifier, action, capability, revision, label):
    if capability["allowed"]:
        return form(f"/api/{entity}/{identifier}/{action}", label=label,
                    revision=revision, compact=True,
                    command=f"sd {'jobs' if entity == 'jobs' else 'assignments'} {action} {identifier}")
    return tag("p", label, ": ", capability["reason"], class_="hint")


def _schedule(schedule):
    if not schedule:
        return "No calendar schedule"
    descriptions = []
    weekdays = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    for entry in schedule:
        hour, minute = entry.get("Hour"), entry.get("Minute")
        if hour is not None and minute is not None:
            timing = f"At {hour:02d}:{minute:02d}"
        elif minute is not None:
            timing = f"Every hour at :{minute:02d}"
        elif hour is not None:
            timing = f"Every minute during the {hour:02d}:00 hour"
        else:
            timing = "Every minute"
        weekday = entry.get("Weekday")
        if weekday is not None:
            timing += " on " + (weekdays[weekday] if 0 <= weekday < len(weekdays) else f"weekday {weekday}")
        if "Day" in entry:
            timing += f", day {entry['Day']}"
        if "Month" in entry:
            timing += f", month {entry['Month']}"
        descriptions.append(timing)
    return "; ".join(descriptions) + " · Mac local time"


def _job(job, cron_root=None):
    request = job["last_request"]
    previous = (tag("div", tag("strong", "Last request: "),
        f"{request['action']} · {request['status']}",
        tag("p", request["message"]), class_="operation-request") if request else "")
    return tag("article", tag("h3", job["name"].replace("-", " ").replace("_", " ")),
        tag("p", tag("strong", job["state"].capitalize()),
            f" · Last exit {job['last_exit']}" if job["last_exit"] is not None else "",
            # A last run that ended in a signal has no exit code; launchd records the
            # signal instead. .get(): the served sd_db may predate the field (sd:1344).
            f" · Last signal {_signal_name(job['last_signal'])}" if job.get("last_signal") is not None else ""),
        _triage(job, cron_root), tag("p", _schedule(job["schedule"]), class_="hint"), previous,
        tag("div", _action("jobs", job["name"], "retry", job["capabilities"]["retry"], job["revision"], "Retry job"),
            _action("jobs", job["name"], "cancel", job["capabilities"]["cancel"], job["revision"], "Stop job"),
            class_="operation-actions"), class_="operation-card")


def _triage(job, cron_root):
    """A failed job's rule-based class (sd:1166): a label only, and nothing acts on it."""
    if job["state"] != "failed":
        return ""
    found = job_failures.classify(job, None if cron_root is None else cron_root / "logs" / f"{job['name']}.log")
    return tag("p", f"Triage: {found['class']}, {found['why']}. Advisory only; nothing retries it.", class_="hint")


def _assignment(assignment, connection):
    from sd_db import runner
    from .runner_screen import assignment_controls

    title = assignment["title"] or f"Assignment #{assignment['id']}"
    heading = tag("a", title, href=f"/item/{assignment['item']}") if assignment["item"] is not None else title
    return tag("article", tag("h3", heading),
        tag("p", tag("strong", assignment["status"].replace("_", " ").capitalize()),
            f" · {assignment['role']} · {assignment['provider']}"),
        assignment_controls(runner.queue_state(connection, assignment["id"])), class_="operation-card")


def _jobs(connection, backend):
    from .runner_screen import jobs_panel

    backend = backend or operations.LaunchdBackend()
    root = getattr(backend, "cron_root", None)
    cron_root = Path(root) if isinstance(root, (str, Path)) else None
    current = operations.inventory(connection, backend=backend)
    jobs = current["jobs"]
    needs_attention = [job for job in jobs if job["state"] in ("failed", "interrupted", "unknown", "unloaded")]
    running = [job for job in jobs if job["state"] == "running"]
    other = [job for job in jobs if job not in needs_attention and job not in running]
    assignments = current["assignments"]
    active = [assignment for assignment in assignments if assignment["status"] in ("queued", "running", "ending")]
    history = [assignment for assignment in assignments if assignment not in active]
    return join((
        jobs_panel(connection),
        tag("p", "Job state is observed from this Mac. An accepted request is not proof that a job finished.", class_="hint"),
        tag("p", tag("a", "Refresh observations", href="/operations?area=jobs")),
        tag("section", tag("h2", "Jobs needing attention"),
            tag("div", join(_job(job, cron_root) for job in needs_attention), class_="operations-grid")
            if needs_attention else tag("p", "No installed jobs need attention.")),
        tag("section", tag("h2", "Running jobs"),
            tag("div", join(_job(job) for job in running), class_="operations-grid")
            if running else tag("p", "No installed jobs are running.")),
        tag("section", tag("h2", "Active assignments"),
            tag("div", join(_assignment(row, connection) for row in active), class_="operations-grid")
            if active else tag("p", "No queued or running assignments.")),
        tag("details", tag("summary", f"Other jobs ({len(other)})"),
            tag("div", join(_job(job) for job in other), class_="operations-grid")),
        tag("details", tag("summary", f"Assignment history ({len(history)})"),
            tag("div", join(_assignment(row, connection) for row in history), class_="operations-grid")),
    ))


def _signal_name(number):
    """`SIGTERM (15)` when Python knows the number, the bare number otherwise."""
    try:
        return f"{signal.Signals(number).name} ({number})"
    except ValueError:
        return str(number)


def _service(entry):
    request = entry["last_request"]
    previous = (tag("div", tag("strong", "Last request: "),
        f"{request['action']} · {request['status']}", tag("p", request["message"]),
        class_="operation-request") if request else "")
    actions, reasons = [], []
    for action, label in (("start", "Start service"), ("restart", "Restart service"), ("stop", "Stop service")):
        capability = entry["capabilities"][action]
        if capability["allowed"]:
            actions.append(form(f"/api/services/{quote(entry['label'], safe='')}/{action}",
                label=label, command=f"sd services {action} {entry['label']}",
                revision=entry["revision"], compact=True))
        elif capability["reason"] and capability["reason"] not in reasons:
            reasons.append(capability["reason"])
    scope = "System daemon" if entry["scope"] == "system" else "User LaunchAgent"
    return tag("article", tag("h3", entry["name"]),
        tag("p", entry["label"], class_="hint") if entry["label"] != entry["name"] else "",
        tag("p", f"{scope} · {entry['domain']} · {entry['category']}", class_="hint"),
        tag("p", tag("strong", entry["state"].capitalize()),
            f" · PID {entry['pid']}" if entry["pid"] is not None else "",
            f" · Last exit {entry['last_exit']}" if entry["last_exit"] is not None else "",
            # A last run that ended in a signal has no exit code; launchd records the
            # signal instead. .get(): the served sd_db may predate the field (sd:1331).
            f" · Last signal {_signal_name(entry['last_signal'])}" if entry.get("last_signal") is not None else ""),
        previous,
        tag("div", join(actions), class_="operation-actions") if actions else "",
        join(tag("p", reason, class_="hint") for reason in reasons), class_="operation-card")


def _services(connection, parameters, backend):
    from sd_db import services

    current = services.inventory(connection, backend=backend)
    query, number, selected = Listing.read_query(parameters)
    listing = Listing("services", (
        Column("name", "Service", lambda row: row["name"]),
        Column("label", "Label", lambda row: row["label"]),
        Column("scope", "Scope", lambda row: row["scope"]),
        Column("domain", "Domain", lambda row: row["domain"]),
        Column("category", "Category", lambda row: row["category"]),
        Column("state", "State", lambda row: row["state"]),
    ), current["services"], path="/operations", query=query, page_number=number,
        selected=selected, extra={"area": "services"})
    shown, paged = listing.page()
    return tag("section", tag("h2", "Launchd services"),
        tag("p", "User LaunchAgents and third-party system daemons installed on this Mac. System daemons and protected services are read-only.", class_="hint"),
        tag("p", "Stop unloads a user service for this login. Its plist stays installed and may load again at the next login. An accepted request is not proof of application health.", class_="hint"),
        tag("p", tag("a", "Refresh observations", href=listing.link())),
        listing.controls(paged),
        tag("div", join(_service(entry) for entry in shown), class_="operations-grid") if shown else
            tag("p", "No services match this filter." if query else "No installed services were found."),
        listing.pager(paged), class_="listing", data_listing="services")


def operations_page(connection, *, parameters, now, backend=None, services_backend=None, ports_backend=None):
    area = (parameters.get("area") or ["jobs"])[0]
    if area not in dict(AREAS):
        area = "jobs"
    if area == "progress":
        content = _progress(connection, now)
    elif area == "usage":
        content = _usage(connection, now, parameters)
    elif area == "jobs":
        content = _jobs(connection, backend)
    elif area == "services":
        content = _services(connection, parameters, services_backend)
    elif area == "ports":
        from .ports_screen import ports_panel

        content = ports_panel(connection, now=now, parameters=parameters, backend=ports_backend)
    elif area == "reports":
        from .reports_screen import reports_panel

        content = reports_panel(connection, parameters, now=now)
    elif area == "resources":
        from .reports_screen import resources

        content = resources(parameters)
    elif area == "trackers":
        from .trackers_screen import trackers_panel

        content = trackers_panel(connection, parameters, now=now)
    elif area == "repos":
        from .repos_screen import repos_panel

        content = repos_panel(parameters)
    elif area == "sessions":
        from .sessions_screen import sessions_panel

        content = sessions_panel(parameters)
    elif area == "commands":
        from .palette import history

        content = history(connection)
    return page("Operations", "operations", _navigation(area), content,
                subtitle=_subtitle())


def _subtitle():
    """The areas, from `AREAS`, so the summary names every tab the nav draws.

    A written sentence named ten of eleven: `commands` joined `AREAS` and not
    the sentence (PR #427 review). `Progress` reads as "workflow progress"
    here, which is what the tab holds.
    """
    words = ["workflow progress" if key == "progress" else label.lower() for key, label in AREAS]
    words[0] = words[0].capitalize()
    return ", ".join(words[:-1]) + " and " + words[-1]
