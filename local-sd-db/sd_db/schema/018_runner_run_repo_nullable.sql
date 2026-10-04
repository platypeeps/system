-- `runner_run.repo` may be NULL: a run detached from a removed repository
-- (sd:2581). `sd task edit N --belongs-to` moves an item to another
-- repository, and its finished runs keep naming the old one. `repo remove`
-- refused (P6) for as long as such a run existed, and no verb could move it.
-- The operator chose 2026-10-04 to keep the run and drop its link: the remove
-- sets a released run of a re-homed item to NULL, and its removal record names
-- the repository it had. The item keeps its run history; nothing is rewritten
-- to a repository the run never ran in.
--
-- `runner_run.detached_from` is that repository, written by the same UPDATE
-- that sets `repo` to NULL. The run's journal file keeps naming it and is
-- never rewritten (a file write cannot roll back with the remove), so the
-- journal and the row agree only through this column: `runner_journal.against`
-- reads a journal as detached only when its repository is the row's
-- `detached_from`. A row whose `repo` went NULL any other way has no
-- provenance, and still differs from its journal. No foreign key: the
-- repository row is gone by then. A journal written before this file has no
-- such field, and `runner_journal.canonical` reads it as NULL.
--
-- SQLite cannot drop a NOT NULL, and the rebuild is not available, for 009's
-- reason: `runner_lease.run` references `runner_run(id)` with no `ON DELETE`,
-- `migrate` wraps every file in a transaction, and `PRAGMA foreign_keys` is a
-- no-op inside one, so dropping the old copy fails on a foreign key. So the
-- column text is edited in place, as 009 edited `item`'s CHECK. Relaxing the
-- constraint cannot leave a stored row invalid, and no index or trigger names
-- the column's nullability. `runner_lease.repo` keeps its NOT NULL: a removed
-- repository's leases go with it (P4).
--
-- The guard runs before `writable_schema` is on, as in 009. It accepts the
-- shape 005 wrote and the nullable one; any other shape aborts the file
-- through `item.kind`'s CHECK, and the store stays at 17. GLOB, not LIKE:
-- `replace` is case-sensitive, so the guard is too. A store that already has
-- `detached_from` aborts on the `ADD COLUMN` instead, untouched: the restore
-- path migrates only a snapshot older than 18
-- (`backup._upgrade_restore_candidate`), so nothing replays this file.
--
-- The reverse, run by hand with the runner and the dashboard stopped. It
-- refuses while a detached run exists: put each one's repository back from
-- its removal record (`repo add` the path first), or delete the run.
--
--   BEGIN;
--   INSERT INTO item (kind, title, status, created_at, updated_at)
--   SELECT 'reverse-018-refuses-detached-runs', '', 'planning', '', ''
--    WHERE EXISTS (SELECT 1 FROM runner_run WHERE repo IS NULL);
--   PRAGMA writable_schema = ON;
--   UPDATE sqlite_master
--      SET sql = replace(sql, '    repo TEXT REFERENCES repo(path),',
--                             '    repo TEXT NOT NULL REFERENCES repo(path),')
--    WHERE type = 'table' AND name = 'runner_run';
--   PRAGMA writable_schema = RESET;
--   ALTER TABLE runner_run DROP COLUMN detached_from;
--   CREATE TABLE reverse_018_cookie (x INTEGER);
--   DROP TABLE reverse_018_cookie;
--   PRAGMA user_version = 17;
--   COMMIT;

INSERT INTO item (kind, title, status, created_at, updated_at)
SELECT 'migration-018-refuses-an-unknown-runner-run-shape', '', 'planning', '', ''
 WHERE NOT EXISTS (
     SELECT 1 FROM sqlite_master
      WHERE type = 'table' AND name = 'runner_run'
        AND (sql GLOB '*' || char(10) || '    repo TEXT NOT NULL REFERENCES repo(path),' || char(10) || '*'
          OR sql GLOB '*' || char(10) || '    repo TEXT REFERENCES repo(path),' || char(10) || '*'));

PRAGMA writable_schema = ON;

UPDATE sqlite_master
   SET sql = replace(sql, '    repo TEXT NOT NULL REFERENCES repo(path),',
                          '    repo TEXT REFERENCES repo(path),')
 WHERE type = 'table' AND name = 'runner_run';

PRAGMA writable_schema = RESET;

ALTER TABLE runner_run ADD COLUMN detached_from TEXT;

-- 009's cookie pair: `writable_schema` leaves the schema cookie alone, so an
-- open connection would keep the NOT NULL. `migrate` runs with the runner and
-- the dashboard stopped; this makes nothing depend on that alone.
CREATE TABLE migration_018_cookie (x INTEGER);
DROP TABLE migration_018_cookie;
