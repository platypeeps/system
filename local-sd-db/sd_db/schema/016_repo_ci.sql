-- `repo.ci`: where a repository's checks run, `github` or `local`.
--
-- `github` is GitHub Actions, which every repository used until now. `local`
-- is a repository that runs without Actions: `sd-ship` runs `sd-check` in a
-- clean worktree of the exact head and posts an `sd/local-gate` commit
-- status, which a protected branch may require (sd:1843). The pack reads the
-- column; `sd-db.sh repo ci PATH github|local` sets it, and `repo list`
-- prints it as the last field.
--
-- The column only, and every row starts at `github`, which is what each
-- repository did before this migration. Nothing derives `local`; the
-- operator sets each row by hand.
--
-- `ADD COLUMN` and not 007's rebuild, for 015's reason: SQLite adds a column
-- with a constant default and a CHECK in place, touches no other table and
-- moves no row, so the foreign keys into `repo(path)` are never in question.
-- The reverse, run by hand with the runner and the dashboard stopped. The
-- CHECK is a column constraint, so SQLite drops it with the column. Run it
-- before 015's reverse, which expects a table at 15.
--
--   BEGIN;
--   ALTER TABLE repo DROP COLUMN ci;
--   PRAGMA user_version = 15;
--   COMMIT;

ALTER TABLE repo ADD COLUMN ci TEXT NOT NULL DEFAULT 'github'
    CHECK (ci IN ('github', 'local'));
