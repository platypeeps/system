-- Migration 1: the eleven tables, and no other.
--
-- The schema version lives in `PRAGMA user_version`, not in a table. A
-- `schema_version` table would be a twelfth table, and requirement 1 says
-- eleven and no other so that a record kind with no home is added to the
-- document before it is written rather than tucked into a table named for
-- something else.
--
-- `report` and `dep` are `item.kind` values, not tables.

CREATE TABLE repo (
    path            TEXT PRIMARY KEY,
    remote          TEXT,
    mode            TEXT,
    merge_policy    TEXT NOT NULL DEFAULT 'manual'
                    CHECK (merge_policy IN ('manual', 'auto')),
    -- `file` until the `docs/work` sitting, `retiring` while it runs, `row`
    -- after it. `pieces_source` is the same three values for item C's
    -- pieces; nothing in this item reads it but the restore.
    status_source   TEXT NOT NULL DEFAULT 'file'
                    CHECK (status_source IN ('file', 'retiring', 'row')),
    pieces_source   TEXT NOT NULL DEFAULT 'file'
                    CHECK (pieces_source IN ('file', 'retiring', 'row')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE item (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL
                    CHECK (kind IN ('work', 'idea', 'task', 'report',
                                    'proposal', 'skill-review', 'dep')),
    repo            TEXT REFERENCES repo(path) ON DELETE RESTRICT,
    branch          TEXT,
    path            TEXT,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('planning', 'ready', 'in_progress',
                                      'ready_to_send', 'blocked', 'done')),
    -- The kind's own word, where it has one: a writing `topic`'s ladder
    -- rung, an idea's stage. Free text, because the vocabulary belongs to
    -- the manifest that describes the kind and not to this schema.
    stage           TEXT,
    priority        INTEGER,
    due             TEXT,
    source          TEXT,
    external_id     TEXT,
    shipped_at      TEXT,
    -- The declared fields of a kind a pack's manifest describes, and the
    -- item's sections. JSON text, read by the manifest that declared them.
    fields          TEXT,
    body            TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX item_by_status ON item (status, kind);
CREATE INDEX item_by_repo ON item (repo, kind);
CREATE UNIQUE INDEX item_by_external ON item (source, external_id)
    WHERE external_id IS NOT NULL;

CREATE TABLE note (
    id              INTEGER PRIMARY KEY,
    item            INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    timestamp       TEXT NOT NULL,
    kind            TEXT NOT NULL
                    CHECK (kind IN ('followup', 'decision', 'proposal',
                                    'question', 'comment', 'exec',
                                    'status_change')),
    body            TEXT NOT NULL,
    session         TEXT,
    resolved_at     TEXT,
    -- An `exec` note carries the run; every other kind leaves these null.
    started         TEXT,
    ended           TEXT,
    exit_code       INTEGER,
    output_path     TEXT,
    CHECK (kind = 'exec' OR (started IS NULL AND ended IS NULL
                             AND exit_code IS NULL AND output_path IS NULL))
);

CREATE INDEX note_by_item ON note (item, timestamp);
CREATE INDEX note_by_kind ON note (kind, timestamp);

-- A tracker's cached view of external work. `shadow_sync.normalize` hands
-- `store` ten fields and this table holds eight of them. The two with no
-- column are named here, so that a reader finds the decision at the table
-- rather than finding the drop in `store`:
--
--   `why`         -- which search bucket found the row on this run. It is a
--                    property of a collect, not of the row, and it already
--                    has a durable home: `contribution_sync.refresh` reads
--                    it off the same collected rows, before `store` runs,
--                    and writes it onto the contribution's checkpoint,
--                    which `progress.tracker_items` reads back. That
--                    staging covers only the pulls the detail collector
--                    takes, so for an issue, or a pull nobody is tracking,
--                    `why` is gone after the run; `tracker_items` reports
--                    `[]` for those and its docstring says so. A column
--                    here would be a second copy of a fact whose first copy
--                    moves every night.
--   `updated_at`  -- when the tracker last changed the row. `last_seen` is
--                    when this collector last saw it, which is the question
--                    every reader of this table actually asks; the collect
--                    uses the tracker's own stamp to order its results and
--                    nothing reads it afterwards.
--
-- Adding either is a schema change and a decision, not a fix to `store`.
CREATE TABLE shadow (
    id              INTEGER PRIMARY KEY,
    tracker         TEXT NOT NULL,
    repo            TEXT,
    url             TEXT NOT NULL,
    number          INTEGER,
    kind            TEXT,
    title           TEXT,
    state           TEXT,
    author          TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

-- Migration 008 replaces this with a unique index on `(tracker, url)`.
CREATE UNIQUE INDEX shadow_by_url ON shadow (url);

CREATE TABLE assignment (
    id              INTEGER PRIMARY KEY,
    item            INTEGER REFERENCES item(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    provider        TEXT REFERENCES provider(name),
    status          TEXT NOT NULL,
    started         TEXT,
    ended           TEXT,
    cost            REAL,
    result          TEXT,
    -- The assignment this one waits on, and the one that spawned it.
    after           INTEGER REFERENCES assignment(id),
    parent          INTEGER REFERENCES assignment(id),
    lane            TEXT NOT NULL DEFAULT 'serial'
                    CHECK (lane IN ('serial', 'parallel')),
    budget_minutes  INTEGER,
    budget_usd      REAL,
    -- A `merge` row's last completed side effect, so a resumed merge knows
    -- what it already did.
    phase           TEXT
);

CREATE INDEX assignment_by_item ON assignment (item, status);
CREATE INDEX assignment_by_status ON assignment (status, lane);

CREATE TABLE skill_use (
    id              INTEGER PRIMARY KEY,
    timestamp       TEXT NOT NULL,
    skill           TEXT NOT NULL,
    surface         TEXT,
    mode            TEXT CHECK (mode IN ('direct', 'path')),
    cwd             TEXT
);

CREATE INDEX skill_use_by_skill ON skill_use (skill, timestamp);

CREATE TABLE trial (
    id              INTEGER PRIMARY KEY,
    skill           TEXT NOT NULL,
    started         TEXT NOT NULL,
    expires         TEXT NOT NULL
);

CREATE UNIQUE INDEX trial_by_skill ON trial (skill);

CREATE TABLE cost (
    id              INTEGER PRIMARY KEY,
    call_id         TEXT,
    timestamp       TEXT NOT NULL,
    provider        TEXT REFERENCES provider(name),
    bill            TEXT REFERENCES bill(name),
    role            TEXT,
    repo            TEXT,
    assignment      INTEGER REFERENCES assignment(id),
    pass            TEXT,
    owner_pid       INTEGER,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    usd             REAL,
    -- A `meter` row reports the vendor's own window rather than one call.
    window_minutes  INTEGER,
    used_percent    REAL,
    source          TEXT NOT NULL
                    CHECK (source IN ('reserved', 'sending', 'run',
                                      'bound', 'meter'))
);

CREATE INDEX cost_by_bill ON cost (bill, timestamp);
CREATE INDEX cost_by_assignment ON cost (assignment);

CREATE TABLE provider (
    name            TEXT PRIMARY KEY,
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    reason          TEXT,
    -- Rank within each role list. Null means the provider does not hold
    -- that role. The order is the dashboard's to change; the file seeds it.
    author_rank     INTEGER,
    reviewer_rank   INTEGER
);

CREATE TABLE bill (
    name            TEXT PRIMARY KEY,
    cost_basis      TEXT NOT NULL,
    cap_usd_month   REAL
);

CREATE TABLE state (
    id              INTEGER PRIMARY KEY,
    -- The operational records that are not items. A kind not in this list
    -- has no home, which is the point: it is added here, in review, before
    -- anything writes it.
    kind            TEXT NOT NULL
                    CHECK (kind IN ('checkpoint', 'verified', 'restore',
                                    'watermark', 'heartbeat')),
    key             TEXT,
    timestamp       TEXT NOT NULL,
    body            TEXT,
    resolved_at     TEXT
);

CREATE INDEX state_by_kind ON state (kind, timestamp);
