-- `repo.satellite_gate`: whether the hub may merge on a satellite's gate
-- pass, `off` or `accept` (sd:2704, ruling Q1).
--
-- A satellite runs the full `sd-check` and writes an offload receipt; with
-- `accept`, the hub's merge lane compares that receipt and merges with no
-- second run. `off` keeps today's merge, where the hub runs the gate itself.
-- The grant is the operator's, per repository, outside the tree. The pack
-- reads the column; `sd-db.sh repo satellite-gate PATH off|accept` sets it,
-- and `repo list` prints it just before `runner_merge`, which stays the last
-- field.
--
-- The column only, and every row starts at `off`, which is what each
-- repository did before this migration. Nothing derives `accept`; the
-- operator sets each row by hand.
--
-- `ADD COLUMN` for 016's reason: SQLite adds a column with a constant default
-- and a CHECK in place, touches no other table and moves no row.
-- The reverse, run by hand with the runner, the dashboard and the serve agent
-- stopped:
--
--   BEGIN;
--   ALTER TABLE repo DROP COLUMN satellite_gate;
--   PRAGMA user_version = 19;
--   COMMIT;

ALTER TABLE repo ADD COLUMN satellite_gate TEXT NOT NULL DEFAULT 'off'
    CHECK (satellite_gate IN ('off', 'accept'));
