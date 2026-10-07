-- The context of a judgment call: five columns beside the decision (sd:2950).
--
-- `caller` and `stage` say which program asked and for which decision. These
-- say under what conditions, so a reader can split a stage's numbers by them:
--
-- - `location`: the caller's directory, the git toplevel of its working
--  directory, else that directory. A key column as `skill_use.cwd` is: a
--  path under `$HOME` is stored as `~/` plus the relative path.
-- - `threshold`: the cut-off the caller applied (`--gate`, `--unsure-below`),
--  from 0 to 1.
-- - `run_id`: an identifier grouping the calls of one run: one review, one
--  lint pass, one batch job.
-- - `prompt_hash`: 12 to 64 lowercase hex digits, a hash of the question
--  definition (instructions, criteria, levels), never the state.
-- - `load_avg`: the one-minute load average when the call was made.
--
-- `sd_db.judgment.record` holds each to its shape and stores NULL for a value
-- that fails it, so a bad context value costs the row that value and never
-- the row. Every existing row reads NULL in all five: no row recorded one.
--
-- `ADD COLUMN` for 016's reason: SQLite adds a nullable column in place,
-- touches no other table and moves no row.
-- The reverse, run by hand with the runner, the dashboard and the serve agent
-- stopped:
--
--   BEGIN;
--   ALTER TABLE judgment DROP COLUMN load_avg;
--   ALTER TABLE judgment DROP COLUMN prompt_hash;
--   ALTER TABLE judgment DROP COLUMN run_id;
--   ALTER TABLE judgment DROP COLUMN threshold;
--   ALTER TABLE judgment DROP COLUMN location;
--   PRAGMA user_version = 20;
--   COMMIT;

ALTER TABLE judgment ADD COLUMN location TEXT;
ALTER TABLE judgment ADD COLUMN threshold REAL;
ALTER TABLE judgment ADD COLUMN run_id TEXT;
ALTER TABLE judgment ADD COLUMN prompt_hash TEXT;
ALTER TABLE judgment ADD COLUMN load_avg REAL;
