"""Read-only port inventory from the system's existing candidates collector."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from .listing import Column, Listing
from .markup import join, tag


def _collectors():
    # The path is owned by this dashboard checkout, never supplied by a page.
    path = Path(__file__).resolve().parents[1] / "collectors.py"
    spec = importlib.util.spec_from_file_location("sd_dashboard_port_collectors", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("port collector is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OverBudget(Exception):
    """The collector stopped the scan at its own budget; this page refuses it.

    The collector's exception class belongs to a module loaded by path on every
    call, so the page cannot name it statically. `_collect` turns it into this
    one, and the reason travels with it.
    """


def _collect():
    # No timeout and no ceiling here: the budget is `collect_ports`' own, and a
    # caller that could set one could loosen it (sd:722).
    module = _collectors()
    try:
        return module.collect_ports()
    except module.OverBudget as error:
        raise OverBudget(str(error)) from None


def port_rows(snapshot):
    """Project only safe display fields; container state is not port health."""
    listeners = snapshot.get("listeners", {})
    rows = []
    for service in snapshot.get("services", []):
        ports = [port for port in service.get("ports", [])
                 if isinstance(port, str) and port.isdecimal() and 1 <= int(port) <= 65535]
        for port in ports or [None]:
            observed = listeners.get(port, {})
            state = observed.get("state", "unknown") if port else "unconfigured"
            if state not in ("listening", "not_listening", "unknown", "unconfigured"):
                state = "unknown"
            if state in ("listening", "not_listening") and observed.get("source") not in ("lsof", "lsof-legacy"):
                state = "unknown"
            holder = observed.get("holder") if state == "listening" else None
            pid = observed.get("pid") if state == "listening" else None
            process = str(holder)[:120] if isinstance(holder, str) else ""
            if type(pid) is int and pid > 0:
                process += f" (pid {pid})"
            conflicts = [hit.get("flag") for hit in service.get("conflicts", []) if hit.get("port") == port]
            container = service.get("state")
            if container not in ("running", "stopped"):
                container = "unknown"
            rows.append({"service": str(service.get("name", "Unknown service"))[:160],
                "profile": "In profile" if service.get("mark") == "+" else "Available" if service.get("mark") == "." else "Profile unknown",
                "port": port, "listener": state, "process": process,
                "container": container, "clash": "CLASH" in conflicts,
                "busy": "BUSY" in conflicts,
                "observed": observed.get("source") in ("lsof", "lsof-legacy") and state in ("listening", "not_listening")})
    # One row per port, even when several candidate services want it. Docker
    # identity stays separate from the processes lsof actually observed.
    grouped = {}
    for row in rows:
        key = row["port"] or "unconfigured:" + row["service"]
        if key not in grouped:
            grouped[key] = {**row, "configured": True, "candidates": [row]}
        else:
            grouped[key]["candidates"].append(row)
            grouped[key]["clash"] |= row["clash"]
            grouped[key]["busy"] |= row["busy"]
    observed = snapshot.get("observed", {}).get("ports", {})
    for port, owners in observed.items():
        if not isinstance(port, str) or not port.isdecimal() or not 1 <= int(port) <= 65535 or not owners:
            continue
        if port not in grouped:
            grouped[port] = {"service": "Observed only", "profile": "Outside the configured service inventory",
                "port": port, "container": "unmapped", "configured": False, "candidates": [],
                "clash": False, "busy": False}
        row = grouped[port]
        visible = []
        for owner in owners:
            if not isinstance(owner, dict) or type(owner.get("pid")) is not int or owner["pid"] <= 0:
                continue
            name = owner.get("command") or "Unknown process"
            value = f"{str(name)[:160]} (pid {owner['pid']}) · {str(owner.get('address', 'Address unavailable'))[:240]}"
            if value not in visible:
                visible.append(value)
        if visible:
            row.update(listener="listening", process="; ".join(visible), observed=True)
    for row in grouped.values():
        candidates = row["candidates"]
        if len(candidates) > 1:
            row["service"] = " · ".join(candidate["service"] for candidate in candidates)
            row["profile"] = "; ".join(candidate["service"] + ": " + candidate["profile"] for candidate in candidates)
            row["container_display"] = "; ".join(candidate["service"] + ": " + candidate["container"].capitalize() for candidate in candidates)
        else:
            row["container_display"] = row["container"].capitalize() if candidates else "Not mapped"
        row.setdefault("listener", "unknown")
        row.setdefault("process", "")
        row.setdefault("observed", False)
    return sorted(grouped.values(), key=lambda row: (int(row["port"] or 0), row["service"]))


LABELS = {"listening": "Listening", "not_listening": "No listener observed",
          "unknown": "Unknown", "unconfigured": "No port configured"}


def _listener(row):
    detail = "Observed TCP listener" if row["listener"] == "listening" and row["observed"] else (
        "No TCP listener visible to this user" if row["listener"] == "not_listening" and row["observed"] else
        "Listener inspection unavailable" if row["listener"] == "unknown" else "")
    return tag("div", tag("strong", LABELS[row["listener"]]),
               tag("p", detail, class_="hint") if detail else "",
               tag("p", row["process"], class_="hint") if row["process"] else "")


def _notes(row):
    notes = []
    if row["clash"]:
        notes.append("Shared by candidate services; overlap is configured, not an observed collision.")
    if row["busy"]:
        notes.append("Port occupied; the listener has not been matched to this container.")
    return " ".join(notes)


def ports_panel(connection=None, *, now, backend=None, parameters=None):
    """Compose one Operations panel; backend is a no-argument fixture seam."""
    try:
        snapshot = (backend or _collect)()
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("services"), list):
            raise ValueError("invalid collector output")
        rows = port_rows(snapshot)
        if not rows and not snapshot.get("complete"):
            raise ValueError("port inventory is unavailable")
    except OverBudget as refused:
        return tag("section", tag("h2", "Ports"),
            tag("p", "Port inventory exceeded its collection budget and was stopped rather than waited on.", role="status"),
            tag("p", str(refused)[:200], class_="hint"),
            tag("p", "No listener or service state is inferred from a refused inspection.", class_="hint"))
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return tag("section", tag("h2", "Ports"),
            tag("p", "Port inventory is unavailable. Refresh to try the read-only collector again.", role="status"),
            tag("p", "No listener or service state is inferred from a failed inspection.", class_="hint"))
    query, number, selected = Listing.read_query(parameters or {})
    listing = Listing("ports", (
        Column("service", "Service", lambda row: tag("div", tag("strong", row["service"]),
            tag("p", row["profile"], class_="hint")), text=lambda row: row["service"] + " " + row["profile"]),
        Column("port", "Port", lambda row: row["port"] or "—"),
        Column("listener", "Listener", _listener,
               text=lambda row: LABELS[row["listener"]] + " " + row["process"]),
        Column("container", "Container", lambda row: row["container_display"]),
        Column("notes", "Notes", _notes, hide_empty=True),
    ), rows, path="/operations", query=query, page_number=number, selected=selected,
        extra={"area": "ports"}, empty="No services match this filter." if query else "No service ports were reported.")
    unique = {row["port"]: row for row in rows if row["port"]}
    configured = sum(row["configured"] for row in unique.values())
    listening = sum(row["listener"] == "listening" for row in unique.values())
    unknown = sum(row["listener"] == "unknown" for row in unique.values())
    warnings = []
    if not snapshot.get("complete"):
        warnings.append("Inventory collection was incomplete; listed observations may be partial.")
    if unknown:
        warnings.append(f"Listener state is unknown for {unknown} configured ports.")
    if not snapshot.get("observed", {}).get("complete"):
        warnings.append("The wider TCP listener inventory is incomplete or unavailable; positive observations remain visible.")
    return tag("section", tag("h2", "Ports"),
        tag("p", f"{configured} configured ports · {listening} listening · {unknown} unknown", class_="hint"),
        join(tag("p", warning, role="status") for warning in warnings),
        listing.render(),
        tag("details", tag("summary", "Sources and scope"),
            tag("p", "Service candidates and effective host ports come from machine-setup's existing service inventory, including script defaults, exported port overrides and Compose declarations."),
            tag("p", "Profile membership is configuration. Container state comes from Docker; TCP listeners come from lsof and are limited to what this user can observe. A listening port does not prove application health or identify its intended owner."),
            tag("p", "Configured services are Docker candidates under system/local-*. Observed-only rows come from one local TCP listener inventory, including launchd services and other processes; no network scan is performed. Multiple visible owners and addresses are retained."),
            tag("p", "Collected for this request: ", now, class_="hint")))
