-- `repo.managed`: 1 when the operator manages the repository, 0 otherwise.
--
-- Sessions named "the repositories the operator manages" by inferring it from
-- remotes and prose, and two inferences disagreed. The column makes it one
-- stored fact (sd:1619). `sd-db.sh repo managed PATH yes|no` sets it, and
-- `repo list --managed` reads it back.
--
-- The column only, and every row starts at 0. No rule derives the flag:
-- neither the GitHub owner nor anything else on the row says whether the
-- operator manages a repository, so the operator sets each one by hand with
-- `repo managed PATH yes` after this migration.
--
-- `ADD COLUMN` and not 007's rebuild: SQLite adds a column with a constant
-- default and a CHECK in place, touches no other table and moves no row, so
-- the foreign keys into `repo(path)` are never in question.
-- The reverse, run by hand with the runner and the dashboard stopped. The
-- CHECK is a column constraint, so SQLite drops it with the column. Run it
-- before 014's reverse, which expects a table at 14.
--
--   BEGIN;
--   ALTER TABLE repo DROP COLUMN managed;
--   PRAGMA user_version = 14;
--   COMMIT;

ALTER TABLE repo ADD COLUMN managed INTEGER NOT NULL DEFAULT 0
    CHECK (managed IN (0, 1));
