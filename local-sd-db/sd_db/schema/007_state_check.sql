-- `check` joins the `state` kinds: the repository check the runner ran in
-- the clone after an author session, one row per run keyed by the run id,
-- with the tree hash it checked in the body. It exists for sd-review to
-- read (sd:495): `sd-ship prepare` runs the same `sd-check --json` on the
-- same tree again, and a record keyed on the tree is what lets it skip
-- that. Nothing reads it yet.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt: the old
-- one is renamed away, the rows are copied with their ids, and both
-- indexes -- `state_by_kind` from 001 and the heartbeat's partial unique
-- index from 005 -- are recreated. A third partial index makes the run id
-- unique among `check` rows, which is what the writer's upsert conflicts
-- on: a re-run replaces its record. Nothing references `state` by foreign
-- key, so the rename rewrites no other table.
ALTER TABLE state RENAME TO state_before_007;
CREATE TABLE state (
    id              INTEGER PRIMARY KEY,
    -- The operational records that are not items. A kind not in this list
    -- has no home, which is the point: it is added here, in review, before
    -- anything writes it.
    kind            TEXT NOT NULL
                    CHECK (kind IN ('checkpoint', 'verified', 'restore',
                                    'watermark', 'heartbeat', 'check')),
    key             TEXT,
    timestamp       TEXT NOT NULL,
    body            TEXT,
    resolved_at     TEXT
);
INSERT INTO state (id, kind, key, timestamp, body, resolved_at)
    SELECT id, kind, key, timestamp, body, resolved_at FROM state_before_007;
DROP TABLE state_before_007;
CREATE INDEX state_by_kind ON state (kind, timestamp);
CREATE UNIQUE INDEX runner_heartbeat ON state(key)
    WHERE kind = 'heartbeat' AND key = 'runner';
CREATE UNIQUE INDEX runner_check ON state(key)
    WHERE kind = 'check';
