-- The assignment is the requested work; each attempt has a durable identity.
ALTER TABLE assignment ADD COLUMN scope TEXT NOT NULL DEFAULT 'item';
ALTER TABLE assignment ADD COLUMN run_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE assignment ADD COLUMN queued_at TEXT;
UPDATE assignment SET queued_at = COALESCE(started, '') WHERE queued_at IS NULL;

CREATE TABLE runner_run (
    id TEXT PRIMARY KEY,
    assignment INTEGER NOT NULL REFERENCES assignment(id),
    run INTEGER NOT NULL,
    repo TEXT NOT NULL REFERENCES repo(path),
    branch TEXT NOT NULL,
    owner TEXT NOT NULL,
    journal_version INTEGER NOT NULL DEFAULT 0,
    start_step TEXT NOT NULL DEFAULT 'claimed',
    end_step TEXT,
    end_action TEXT,
    outcome TEXT,
    detail TEXT,
    work_path TEXT NOT NULL,
    retained_path TEXT NOT NULL,
    supervisor_pid INTEGER,
    supervisor_pgid INTEGER,
    supervisor_start TEXT,
    provider TEXT,
    vendor TEXT,
    base_head TEXT,
    authored_head TEXT,
    reviewed_head TEXT,
    delivery_proof TEXT,
    ignored_manifest TEXT,
    cancel_requested TEXT,
    quarantine TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    released_at TEXT,
    UNIQUE(assignment, run)
);
CREATE TABLE runner_lease (
    run TEXT PRIMARY KEY REFERENCES runner_run(id),
    repo TEXT NOT NULL REFERENCES repo(path),
    branch TEXT NOT NULL,
    exclusive INTEGER NOT NULL CHECK(exclusive IN (0, 1)),
    acquired_at TEXT NOT NULL,
    released_at TEXT
);
CREATE UNIQUE INDEX runner_active_branch ON runner_lease(repo, branch)
    WHERE released_at IS NULL;
CREATE INDEX runner_active_repo ON runner_lease(repo, released_at);
CREATE INDEX runner_by_assignment ON runner_run(assignment, run);
-- Old builds appended ticks. Only this derived health key is compacted.
DELETE FROM state WHERE kind = 'heartbeat' AND key = 'runner' AND id NOT IN
    (SELECT MAX(id) FROM state WHERE kind = 'heartbeat' AND key = 'runner');
CREATE UNIQUE INDEX runner_heartbeat ON state(key)
    WHERE kind = 'heartbeat' AND key = 'runner';
