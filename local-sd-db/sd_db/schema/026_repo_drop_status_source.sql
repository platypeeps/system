-- Drop `repo.status_source` (sd:3231).
--
-- The pack reads every `docs/work` item's status from its row (sd:3015), so
-- nothing reads who owns it. The `retire` verb that switched the column went
-- with it. 001 stays as applied; this file undoes its column.
-- `pieces_source` stays: the writing pieces keep their own owner.
--
-- `DROP COLUMN` in place, as 025 drops `satellite_gate`: SQLite rewrites
-- `repo` only, and every row keeps its other values. The column has no index,
-- view or trigger, which would make SQLite refuse the drop.
-- The reverse, run by hand with the runner, the dashboard and the serve agent
-- stopped. Every row reads `row`, the owner the rows have been since sd:3015;
-- a `file` or `retiring` value is not restored. It rebuilds `repo` rather than
-- adding the column at the end, so the column sits where 001 put it and a
-- backup restore compares the table to 25's shape. Foreign keys are off for
-- the rebuild, as `DROP TABLE repo` would check them.
--
--   PRAGMA foreign_keys = OFF;
--   BEGIN;
--   CREATE TABLE repo_at_25 (
--       path TEXT PRIMARY KEY, remote TEXT, mode TEXT,
--       runner_merge TEXT NOT NULL DEFAULT 'manual' CHECK (runner_merge IN ('manual', 'auto')),
--       status_source TEXT NOT NULL DEFAULT 'file' CHECK (status_source IN ('file', 'retiring', 'row')),
--       pieces_source TEXT NOT NULL DEFAULT 'file' CHECK (pieces_source IN ('file', 'retiring', 'row')),
--       created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
--       managed INTEGER NOT NULL DEFAULT 0 CHECK (managed IN (0, 1)),
--       ci TEXT NOT NULL DEFAULT 'github' CHECK (ci IN ('github', 'local')),
--       lane_host TEXT CHECK (lane_host IS NULL OR (lane_host <> '' AND lane_host NOT GLOB '*[^a-z0-9-]*')));
--   INSERT INTO repo_at_25 (path, remote, mode, runner_merge, status_source, pieces_source,
--                           created_at, updated_at, managed, ci, lane_host)
--       SELECT path, remote, mode, runner_merge, 'row', pieces_source,
--              created_at, updated_at, managed, ci, lane_host FROM repo;
--   DROP TABLE repo;
--   ALTER TABLE repo_at_25 RENAME TO repo;
--   PRAGMA user_version = 25;
--   COMMIT;
--   PRAGMA foreign_keys = ON;

-- The guard. `retiring` holds a repository whose restored rows are not yet
-- proven against its `docs/work` files; `sd restore reimport` under the 25
-- library proves them and clears it. The drop would lose that hold, so the
-- file aborts and the store stays at 25, untouched. 009's `item.kind` guard
-- cannot name the repositories; a temporary trigger's RAISE can, because its
-- message is an expression from SQLite 3.47. A `file` row passes: no `file`
-- repository held a `docs/work` item file once sd:3015 landed. `BEGIN` ends
-- the header line: `migrate` refuses a line that starts with it.
CREATE TEMP TRIGGER migration_026_refuses_a_retiring_repo
BEFORE UPDATE OF status_source ON repo FOR EACH ROW BEGIN
    SELECT RAISE(ABORT, 'migration 026 refuses: repo.status_source is retiring for '
        || (SELECT group_concat(path, ', ')
              FROM (SELECT path FROM repo WHERE status_source = 'retiring' ORDER BY path))
        || '; finish each with `sd restore reimport` under the schema 25 library, '
        || 'or restore a later snapshot, then migrate again');
END;
UPDATE repo SET status_source = status_source WHERE status_source = 'retiring';
DROP TRIGGER temp.migration_026_refuses_a_retiring_repo;

ALTER TABLE repo DROP COLUMN status_source;
