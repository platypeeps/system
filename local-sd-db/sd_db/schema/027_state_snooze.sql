-- `snooze` joins the `state` kinds (sd:1896): a Today or Health row the
-- operator hid until a time. One row per snooze, keyed by the page and the
-- row's id (`today:job:nightly:1`), with the time and the row's fingerprint
-- (`seen`, what the operator saw) in the body; clearing it writes the same key
-- with no time. The latest row for a key is the answer,
-- read through 013's `state_by_kind_key`. Nothing expires a row: a reader
-- compares the time with its own clock.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt the way
-- 007 rebuilt it: renamed away, recreated, the rows copied with their ids,
-- the old one dropped, and all four indexes recreated -- `state_by_kind`
-- from 001, `runner_heartbeat` from 005, `runner_check` from 007 and
-- `state_by_kind_key` from 013. Nothing references `state` by foreign key,
-- so the rename rewrites no other table.
--
-- The reverse, run by hand with the runner, the dashboard and the serve agent
-- stopped. It drops the snooze rows, which the 26 shape cannot hold, so every
-- snoozed row shows again.
--
--   BEGIN;
--   DELETE FROM state WHERE kind = 'snooze';
--   ALTER TABLE state RENAME TO state_before_reverse;
--   CREATE TABLE state (
--       id              INTEGER PRIMARY KEY,
--       kind            TEXT NOT NULL
--                       CHECK (kind IN ('checkpoint', 'verified', 'restore',
--                                       'watermark', 'heartbeat', 'check')),
--       key             TEXT,
--       timestamp       TEXT NOT NULL,
--       body            TEXT,
--       resolved_at     TEXT
--   );
--   INSERT INTO state (id, kind, key, timestamp, body, resolved_at)
--       SELECT id, kind, key, timestamp, body, resolved_at FROM state_before_reverse;
--   DROP TABLE state_before_reverse;
--   CREATE INDEX state_by_kind ON state (kind, timestamp);
--   CREATE UNIQUE INDEX runner_heartbeat ON state(key) WHERE kind = 'heartbeat' AND key = 'runner';
--   CREATE UNIQUE INDEX runner_check ON state(key) WHERE kind = 'check';
--   CREATE INDEX state_by_kind_key ON state (kind, key, id);
--   PRAGMA user_version = 26;
--   COMMIT;

ALTER TABLE state RENAME TO state_before_027;
CREATE TABLE state (
    id              INTEGER PRIMARY KEY,
    -- The operational records that are not items. A kind not in this list
    -- has no home, which is the point: it is added here, in review, before
    -- anything writes it.
    kind            TEXT NOT NULL
                    CHECK (kind IN ('checkpoint', 'verified', 'restore',
                                    'watermark', 'heartbeat', 'check',
                                    'snooze')),
    key             TEXT,
    timestamp       TEXT NOT NULL,
    body            TEXT,
    resolved_at     TEXT
);
INSERT INTO state (id, kind, key, timestamp, body, resolved_at)
    SELECT id, kind, key, timestamp, body, resolved_at FROM state_before_027;
DROP TABLE state_before_027;
CREATE INDEX state_by_kind ON state (kind, timestamp);
CREATE UNIQUE INDEX runner_heartbeat ON state(key)
    WHERE kind = 'heartbeat' AND key = 'runner';
CREATE UNIQUE INDEX runner_check ON state(key)
    WHERE kind = 'check';
CREATE INDEX state_by_kind_key ON state (kind, key, id);
