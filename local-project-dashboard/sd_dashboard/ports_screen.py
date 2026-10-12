"""Read-only port inventory from the system's existing candidates collector."""

from __future__ import annotations

import importlib.util
from pathlib import Path


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


