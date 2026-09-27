"""Atomic operator controls over registry state; provider identity stays in YAML.

`path` names the registry file. Omitted, it is the file beside the database
the connection is open on (`registry.beside`), so a caller under test that
forgets it reads the fixture's registry or gets a `RegistryError` naming the
fixture's path -- not the operator's `~/.local/share/sd/providers.yaml`
because the process's `$HOME` is the real one.
"""

import hashlib
import json
from dataclasses import replace

from . import ledger, registry, writes
from .database import transaction
from .workflow import StaleItem, WorkflowError


def snapshot(connection, *, path=None):
    current = registry.read(path, connection=connection)
    providers = [{"name": entry.name, "vendor": entry.vendor, "enabled": entry.enabled,
                  "reason": entry.reason or "", "roles": list(entry.roles), "ranks": entry.ranks,
                  "transport": entry.kind, "model": entry.model}
                 for entry in current.providers.values()]
    orders = {role: [entry.name for entry in sorted(current.providers.values(),
              key=lambda entry: (entry.ranks.get(role, 999999), entry.name)) if role in entry.ranks]
              for role in registry.ROLES}
    bills = [{"name": bill.name, "cost_basis": bill.cost_basis, "cap_usd_month": bill.cap_usd_month}
             for bill in current.bills.values()]
    # The revision hashes the bills and the warnings too, so a cap written
    # since the read is a stale `expected_revision` to `configure` and to
    # `set_cap` alike, and so is a legacy cap cleared since.
    data = {"providers": providers, "orders": orders, "bills": bills, "warnings": list(current.warnings),
            "order_policy": registry.order_policy(connection),
            "configuration_sha256": hashlib.sha256(current.path.read_bytes()).hexdigest()}
    data["revision"] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return data


def configure(connection, *, enabled, orders, expected_revision, path=None, who):
    """Validate both role lists and every enabled flag before any state write."""
    with transaction(connection):
        current = snapshot(connection, path=path)
        if expected_revision != current["revision"]:
            raise StaleItem("provider configuration changed; reload before saving")
        source = registry.read(path, connection=connection)
        if not isinstance(enabled, dict) or set(enabled) != set(source.providers) or any(type(value) is not bool for value in enabled.values()):
            raise WorkflowError("provide an enabled choice for every known provider")
        if not isinstance(orders, dict) or set(orders) != set(registry.ROLES):
            raise WorkflowError("provide both author and reviewer orders together")
        for role, names in orders.items():
            if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
                raise WorkflowError(f"{role} order must list distinct provider names and cannot be empty")
            invalid = [name for name in names if name not in source.providers or role not in source.providers[name].roles]
            if invalid:
                raise WorkflowError(f"{role} order requires capable providers: {', '.join(invalid)}")
        entries = {name: replace(entry, enabled=enabled[name], ranks={role: names.index(name)
                   for role, names in orders.items() if name in names}) for name, entry in source.providers.items()}
        proposed = replace(source, providers=entries)
        for role in registry.ROLES:
            proposed.resolve(role)
        registry._refuse_one_provider_for_both_roles(proposed)
        previous_policy = current["order_policy"]
        previous_names = previous_policy["explicit_providers"] if previous_policy else []
        policy = {"schema_version": 1, "explicit_providers": sorted(set(previous_names) | set(entries))}
        if (enabled == {entry["name"]: entry["enabled"] for entry in current["providers"]}
                and orders == current["orders"] and policy == previous_policy):
            return current
        registry.seed(connection, source)
        for name, entry in entries.items():
            connection.execute("UPDATE provider SET author_rank=?,reviewer_rank=? WHERE name=?",
                (entry.ranks.get("author"), entry.ranks.get("reviewer"), name))
            if entry.enabled != source.providers[name].enabled:
                connection.execute("UPDATE provider SET enabled=?,reason=? WHERE name=?",
                    (int(entry.enabled), None if entry.enabled else f"Disabled by {who}", name))
        if policy != previous_policy:
            writes.record_state(connection, "checkpoint", key=registry.ORDER_POLICY_KEY, body=policy)
        return snapshot(connection, path=path)


def set_cap(connection, bill, cap_usd_month, *, expected_revision, path=None, who):
    """Raise, lower or clear one bill's monthly cap as its `bill` row; the file is never written.

    Refused before any write, in this order: a stale revision; a bill the
    registry does not name; a number the ledger would not hold (`ledger._money`,
    the rule `reserve` checks a bound by, so a cap is what a bound is measured
    against); and a cap on a bill any `start` entry is billed to, with the
    reader's own sentence (`registry.refuse_capped_start_entries` over the
    registry as it would read after the write), which is clause 15.15's
    refusal from the dashboard. `None` clears the cap, and clears it from
    the row itself: a legacy cap the merge reads past (`snapshot`'s
    `warnings`) is not in the merged view, so "unchanged" is judged against
    the row and not the view, and `set_cap(..., None)` is the repair. `who`
    names the operator as every write verb must (sd:749); the `bill` row has
    no column to hold it, so the caller's name reaches no row from here.
    """
    with transaction(connection):
        current = snapshot(connection, path=path)
        if expected_revision != current["revision"]:
            raise StaleItem("provider configuration changed; reload before saving")
        source = registry.read(path, connection=connection)
        if bill not in source.bills:
            raise WorkflowError(f"no bill {bill!r}; the registry names {', '.join(source.bills)}")
        if cap_usd_month is not None:
            cap_usd_month = ledger._money(cap_usd_month, "a cap", scope="bill", name=bill)
        proposed = replace(source, bills={**source.bills, bill: replace(source.bills[bill], cap_usd_month=cap_usd_month)})
        registry.refuse_capped_start_entries(proposed)
        registry.seed(connection, source)
        held = connection.execute("SELECT cap_usd_month FROM bill WHERE name = ?", (bill,)).fetchone()[0]
        if cap_usd_month == held:
            return current
        writes.set_bill_cap(connection, bill, cap_usd_month)
        return snapshot(connection, path=path)
