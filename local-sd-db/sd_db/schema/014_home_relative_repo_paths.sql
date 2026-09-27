-- Repository paths become home-relative keys (sd:1439).
--
-- A repository under `$HOME` is stored as `~/` plus its home-relative path,
-- so a second machine with another login resolves the same rows. A path
-- outside `$HOME` stays absolute. `sd_db.paths` owns the conversion, in the
-- caller's process; this file is the one place it runs inside SQL, because
-- `migrate` runs on the hub only.
--
-- `sd_home_relative` and `sd_home_absolute` are not SQLite functions. `migrate`
-- registers them on its connection with `paths.install`, and so does the
-- reference connection in `backup._check_restore`. Any other connection fails
-- this file closed with "no such function".
--
-- The order is insert, repoint, delete. `item.repo`, `repo_protection.repo`,
-- `runner_run.repo` and `runner_lease.repo` reference `repo(path)` without
-- `ON UPDATE CASCADE`, and 009 measured `defer_foreign_keys` failing inside
-- the transaction `migrate` wraps a file in. So a `~/` copy of each parent is
-- inserted first, the children move to it, and the absolute parent goes last.
-- The immediate foreign keys hold at every statement. The primary key refuses
-- a collision -- a `~/` row that already exists -- and aborts the file, which
-- leaves the store at 13.
--
-- Idempotent by construction. A value already `~/` is not under `$HOME` as a
-- string, so a replay changes nothing. `restore` replays migrations onto a
-- snapshot that may already have this shape.
--
-- History is not rewritten: `item.fields`, `item.body`, `note.body`,
-- `state.body`, `assignment.scope`, `publication_claim.payload` and the
-- journals keep what they said (prd R5). Hub-only paths stay absolute:
-- `note.output_path`, `runner_run.work_path` and `retained_path`.
--
-- The reverse, run by hand on a connection that has `paths.install`, with the
-- runner and the dashboard stopped. It is the same steps with
-- `sd_home_absolute`, and returns the key columns byte-identical. The
-- `item.path` step restores the `register` rows, all four of which were
-- absolute on the hub before this file.
--
--   BEGIN;
--   INSERT INTO repo (path, remote, mode, runner_merge, status_source,
--                     pieces_source, created_at, updated_at)
--       SELECT sd_home_absolute(path), remote, mode, runner_merge,
--              status_source, pieces_source, created_at, updated_at
--       FROM repo WHERE sd_home_absolute(path) != path;
--   UPDATE item SET repo = sd_home_absolute(repo)
--       WHERE repo IS NOT NULL AND sd_home_absolute(repo) != repo;
--   UPDATE repo_protection SET repo = sd_home_absolute(repo)
--       WHERE sd_home_absolute(repo) != repo;
--   UPDATE runner_run SET repo = sd_home_absolute(repo)
--       WHERE sd_home_absolute(repo) != repo;
--   UPDATE runner_lease SET repo = sd_home_absolute(repo)
--       WHERE sd_home_absolute(repo) != repo;
--   DELETE FROM repo WHERE sd_home_absolute(path) != path;
--   UPDATE item SET external_id =
--           sd_home_absolute(substr(external_id, 1, instr(external_id, '::') - 1))
--           || substr(external_id, instr(external_id, '::'))
--       WHERE source IN ('docs/work', 'writing-piece')
--         AND external_id LIKE '~%' AND instr(external_id, '::') > 0;
--   UPDATE state SET key = sd_home_absolute(substr(key, 1, length(key) - 14))
--           || substr(key, length(key) - 13)
--       WHERE kind = 'verified' AND key LIKE '~%'
--         AND (key LIKE '%:status_source' OR key LIKE '%:pieces_source');
--   UPDATE item SET path = repo || '/' || path
--       WHERE source = 'register' AND repo IS NOT NULL
--         AND path IS NOT NULL AND path NOT LIKE '/%';
--   UPDATE cost SET repo = sd_home_absolute(repo)
--       WHERE repo IS NOT NULL AND sd_home_absolute(repo) != repo;
--   UPDATE skill_use SET cwd = sd_home_absolute(cwd)
--       WHERE cwd IS NOT NULL AND sd_home_absolute(cwd) != cwd;
--   PRAGMA user_version = 13;
--   COMMIT;

-- Decision 3, first, while `item.repo` and `item.path` are both absolute:
-- the `register` rows landed before 2026-09-11 hold an absolute path, which
-- `git -C repo show commit:path` cannot follow. Every other row is
-- repo-relative already.
UPDATE item SET path = substr(path, length(repo) + 2)
    WHERE source = 'register' AND repo IS NOT NULL AND path IS NOT NULL
      AND substr(path, 1, length(repo) + 1) = repo || '/';

-- 1. A `~/` copy of every parent under `$HOME`, all columns carried.
INSERT INTO repo (path, remote, mode, runner_merge, status_source,
                  pieces_source, created_at, updated_at)
    SELECT sd_home_relative(path), remote, mode, runner_merge,
           status_source, pieces_source, created_at, updated_at
    FROM repo WHERE sd_home_relative(path) != path;

-- 2. The four children move to the copy.
UPDATE item SET repo = sd_home_relative(repo)
    WHERE repo IS NOT NULL AND sd_home_relative(repo) != repo;
UPDATE repo_protection SET repo = sd_home_relative(repo)
    WHERE sd_home_relative(repo) != repo;
UPDATE runner_run SET repo = sd_home_relative(repo)
    WHERE sd_home_relative(repo) != repo;
UPDATE runner_lease SET repo = sd_home_relative(repo)
    WHERE sd_home_relative(repo) != repo;

-- 3. The absolute parents, which no child names now.
DELETE FROM repo WHERE sd_home_relative(path) != path;

-- 4. The keys that embed a repository: `<repo>::<relative>` for `docs/work`
-- and writing pieces, and the `verified` receipt `<repo>:<column>` that
-- `recovery` and `writing` record. `item_by_external` refuses a collision.
UPDATE item SET external_id =
        sd_home_relative(substr(external_id, 1, instr(external_id, '::') - 1))
        || substr(external_id, instr(external_id, '::'))
    WHERE source IN ('docs/work', 'writing-piece')
      AND external_id LIKE '/%' AND instr(external_id, '::') > 0;
-- `:status_source` and `:pieces_source` are both 14 characters.
UPDATE state SET key = sd_home_relative(substr(key, 1, length(key) - 14))
        || substr(key, length(key) - 13)
    WHERE kind = 'verified' AND key LIKE '/%'
      AND (key LIKE '%:status_source' OR key LIKE '%:pieces_source');

-- 5. Decision 2: attribution and the skill log. Neither is a key, but
-- reports group by them.
UPDATE cost SET repo = sd_home_relative(repo)
    WHERE repo IS NOT NULL AND sd_home_relative(repo) != repo;
UPDATE skill_use SET cwd = sd_home_relative(cwd)
    WHERE cwd IS NOT NULL AND sd_home_relative(cwd) != cwd;
