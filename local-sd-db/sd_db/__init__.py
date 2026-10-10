"""The one database, and the fixture harness both faces test with.

Installed as a copy at a tag into the pack's virtualenv and the dashboard's:
`pip install` of this checkout, never `-e`, so a branch switch or a pull in
this checkout changes nothing a running process imports until the operator
installs the next tag and restarts what runs.

The package owns the schema, every write, the read queries the two faces
share, and the provider registry. Nothing outside it calls
`sqlite3.connect` -- that is requirement 2, and a grep of the pack, this
repository and the dashboard enforces it.

    from sd_db import connect, transition, read_registry

    connection = connect()
    transition(connection, item, "in_progress", who="sd-ship")

`sd_db.testing` ships beside it and is the harness, not the library: it is
imported by tests in both repositories and by nothing at runtime.
"""

from __future__ import annotations

from .backup import restore, run as backup
from .brief import Brief, note_brief
from .calls import CallRefused, CallResult, bound_for, call, estimate_tokens
from .database import connect, default_path, schema_version, tables, transaction
from .errors import (
    BackupError,
    RegistryError,
    SchemaTooNew,
    SchemaTooOld,
    SdDbError,
)
from .judgment import JudgmentRefused
from .judgment import by_stage as judgments_by_stage
from .judgment import record as record_judgment
from .ledger import LedgerRefused, Released, claim, exposure, lose, release_orphans, reserve, set_budget, settle
from .meter import MeterRefused, latest, sample
from .migrate import initialise, migrate
from .reads import (
    AgeBucket,
    Number,
    ScorecardRow,
    age_histogram,
    backlog_items,
    brief_items,
    brief_notes,
    item_by_id,
    item_notes,
    status_changes,
    today_items,
)
from .registry import Provider, Registry, ensure_seeded, read as read_registry, registry_path, seed
from .repos import Checkout, RepoRefusal
from .repos import add as add_repo
from .repos import registered as registered_repos
from .repos import seed as seed_repos
from .schema import SCHEMA_VERSION, TABLES
from .shadow_sync import TRACKERS, Collected, Synced, read_watermark
from .shadow_sync import sync as sync_shadow
from .sources import (
    Counts,
    Difference,
    Frozen,
    MigrationRefused,
    Record,
    Sitting,
)
from .sources import run as run_migration
from .sources import verify as verify_migration
from .writes import (
    STATE_KINDS,
    STATUSES,
    TransitionRefused,
    active_trials,
    add_note,
    create_assignment,
    create_item,
    end_trial,
    record_cost,
    record_skill_use,
    record_state,
    resolve_note,
    resolve_state,
    set_bill_cap,
    set_item_fields,
    set_provider_state,
    skill_use_since,
    snooze,
    snoozed,
    start_trial,
    trials,
    transition,
    unresolved_state,
    update_assignment,
    upsert_item,
    upsert_repo,
    upsert_shadow,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "active_trials",
    "age_histogram",
    "AgeBucket",

    "add_note",
    "add_repo",
    "backlog_items",
    "backup",
    "BackupError",
    "Brief",
    "brief_items",
    "brief_notes",
    "bound_for",
    "call",
    "CallRefused",
    "CallResult",
    "Checkout",
    "claim",
    "Collected",
    "connect",
    "Counts",
    "create_assignment",
    "create_item",
    "default_path",
    "Difference",
    "end_trial",
    "estimate_tokens",
    "exposure",
    "ensure_seeded",
    "Frozen",
    "initialise",
    "item_by_id",
    "item_notes",
    "latest",
    "LedgerRefused",
    "MeterRefused",
    "lose",
    "migrate",
    "MigrationRefused",
    "note_brief",
    "Number",
    "Provider",
    "read_registry",
    "read_watermark",
    "Record",
    "record_cost",
    "record_skill_use",
    "record_state",
    "registered_repos",
    "Registry",
    "registry_path",
    "release_orphans",
    "Released",
    "RegistryError",
    "RepoRefusal",
    "reserve",
    "resolve_note",
    "resolve_state",
    "restore",
    "run_migration",
    "sample",
    "SCHEMA_VERSION",
    "schema_version",
    "SchemaTooNew",
    "SchemaTooOld",
    "ScorecardRow",
    "SdDbError",
    "seed",
    "seed_repos",
    "set_budget",
    "set_bill_cap",
    "set_item_fields",
    "set_provider_state",
    "settle",
    "Sitting",
    "skill_use_since",
    "snooze",
    "snoozed",
    "start_trial",
    "STATE_KINDS",
    "status_changes",
    "STATUSES",
    "sync_shadow",
    "Synced",
    "TABLES",
    "tables",
    "today_items",
    "TRACKERS",
    "transaction",
    "transition",
    "TransitionRefused",
    "trials",
    "unresolved_state",
    "update_assignment",
    "upsert_item",
    "upsert_repo",
    "upsert_shadow",
    "verify_migration",
]
