-- `request_outcome`: one row per write transaction a remote session committed
-- that changed something (sd:1335, step 5 of the second-machine plan).
--
-- Over the wire, the hub can commit and its answer can still be lost, so the
-- satellite cannot tell a committed transaction from a rolled-back one. Each
-- remote write transaction carries a request id, a ULID the client makes at
-- `BEGIN IMMEDIATE`. `sd_db.serve` inserts the row on the same connection
-- just before it forwards `COMMIT`, so the row and the application writes
-- commit together or not at all. A client whose answer was lost asks the hub
-- for the id: a row means the transaction committed. A transaction that
-- changed nothing gets no row; committed or not, it wrote nothing.
--
-- `id` is the request id; `committed_at` is the hub's clock (`writes.now`).
-- A local connection writes no row. No row is ever deleted: the operator
-- ruled on 2026-10-04 (Q1 = B) to build the protocol without the prune, so
-- a missing row always means the transaction wrote nothing.
--
-- A new table, so no row moves and no other table is touched.
-- The reverse, run by hand with the runner, the dashboard and the serve agent
-- stopped:
--
--   BEGIN;
--   DROP TABLE request_outcome;
--   PRAGMA user_version = 18;
--   COMMIT;

CREATE TABLE request_outcome (
    id TEXT NOT NULL PRIMARY KEY,
    committed_at TEXT NOT NULL
);
