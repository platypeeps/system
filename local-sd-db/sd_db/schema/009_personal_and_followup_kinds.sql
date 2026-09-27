-- Four kinds join `item.kind`, so a personal to-do, a followup, and an idea
-- that is not an article are all first-class rows rather than a convention in
-- free text.
--
-- `idea` is left exactly as it was. It is not the generic word it looks like:
-- `sd_db/writing.py` selects `kind = 'idea' AND piece IS NOT NULL`,
-- `sources/vault.py` imports Blog Ideas as `idea`, and the dashboard renders
-- writing controls on it. A personal or a work idea filed as `idea` would enter
-- the publishing pipeline, so each gets its own kind instead.
--
-- SQLite cannot alter a CHECK, and the usual rebuild is not available here.
-- Migration 7 could rename its table away because, as it records, "nothing
-- references `state` by foreign key". Four foreign keys reference `item(id)`,
-- and both of `publication_claim`'s carry no `ON DELETE` clause, so dropping
-- the old copy is refused rather than cascaded. Measured on 2026-09-13 against
-- sqlite 3.53.4: migration 7's pattern, a build-copy-drop-rename, and the same
-- with `defer_foreign_keys` all fail with "FOREIGN KEY constraint failed".
-- `PRAGMA foreign_keys` cannot rescue them, because it is a no-op inside a
-- transaction and `migrate` wraps every file in one. Rebuilding the referencing
-- tables instead would reach six tables, four indexes and a trigger to widen
-- one list.
--
-- So the CHECK text is edited in place. `replace` is deliberate: it rewrites
-- only the kind list and preserves whatever columns migrations 2 through 8
-- appended to this table, which a hand-written CREATE TABLE would silently drop
-- along with the data in them.

-- The guard, and it runs before `writable_schema` is switched on so a refusal
-- never leaves that pragma set. It aborts the whole transaction -- the invalid
-- kind violates the CHECK this migration is here to widen -- unless `item` is
-- in one of the two shapes this file knows how to leave correct: the one it
-- expects to find, or the one it produces. Both are accepted because the
-- restore path replays migrations onto a snapshot that may already carry the
-- new shape while claiming the older version (`backup.py`,
-- `_upgrade_restore_candidate`), and a replay must be a no-op and not a
-- refusal. On the already-migrated shape the `replace` below finds nothing and
-- changes nothing, which is the no-op. A third, unrecognised shape is a
-- database this file must not edit blind, and that is what aborts.
INSERT INTO item (kind, title, status, created_at, updated_at)
SELECT 'migration-009-refuses-an-unknown-item-shape', '', 'planning', '', ''
 WHERE NOT EXISTS (
     SELECT 1 FROM sqlite_master
      WHERE type = 'table' AND name = 'item'
        AND (sql GLOB '*''proposal'', ''skill-review'', ''dep''))*'
          OR sql GLOB '*''work-idea'', ''personal-idea''))*'));

PRAGMA writable_schema = ON;

UPDATE sqlite_master
   SET sql = replace(
       sql,
       '''proposal'', ''skill-review'', ''dep''))',
       '''proposal'', ''skill-review'', ''dep'',' || char(10) ||
       '                                    ''personal'', ''followup'',' || char(10) ||
       '                                    ''work-idea'', ''personal-idea''))')
 WHERE type = 'table' AND name = 'item';

PRAGMA writable_schema = RESET;

-- `writable_schema` leaves the schema cookie untouched, so a connection that is
-- already open would keep the old CHECK. `migrate` is documented to run with the
-- dashboard and the runner stopped, and this pair bumps the cookie anyway so
-- nothing depends on that promise alone. Both statements are in this migration
-- file, which is the only place a CREATE TABLE belongs, and the table does not
-- outlive the transaction that makes it.
CREATE TABLE migration_009_cookie (x INTEGER);
DROP TABLE migration_009_cookie;
