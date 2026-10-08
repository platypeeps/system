-- `repo.lane_host`: the machine that runs this repository's merge lane
-- (sd:3075, design sd:3003).
--
-- The value is that machine's `hostname -s`, lower-cased, the per-host
-- folder rule of `local-cron-jobs`. NULL means the hub. Every row starts at
-- NULL, so nothing moves at migration: the hub keeps every lane.
-- `sd-db.sh repo lane-host PATH HOST|hub` sets it, as does the dashboard's
-- Move lane; `sd_db.ship.repository_lock` reads it and refuses on any other
-- machine. The CHECK repeats the setter's `[a-z0-9-]+` so no other writer
-- stores a name the lock never matches.
--
-- `ADD COLUMN` for 016's reason: SQLite adds a nullable column in place,
-- touches no other table and moves no row. Every existing row reads NULL.
-- The reverse, run by hand with the runner, the dashboard and the serve agent
-- stopped:
--
--   BEGIN;
--   ALTER TABLE repo DROP COLUMN lane_host;
--   PRAGMA user_version = 23;
--   COMMIT;

ALTER TABLE repo ADD COLUMN lane_host TEXT
    CHECK (lane_host IS NULL OR (lane_host <> '' AND lane_host NOT GLOB '*[^a-z0-9-]*'));
