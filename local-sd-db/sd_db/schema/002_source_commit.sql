-- Migration 2: `item.source_commit`, the commit a migrated row was read from.
--
-- Requirement 2's `docs/work` migration reads committed trees and never a
-- working copy, and an item lives on its branch until its merge -- so a row
-- can be landed from a branch the default has never seen. The commit it was
-- read from is what makes that row recoverable: `sd restore reimport` takes a
-- restored row back to its lines by reading this commit, and without the
-- column the only way back is rerunning the whole sitting.
--
-- Null everywhere the row did not come from a tree. A `shadow` row has a
-- tracker, an `idea` row has a file in the vault, and neither has a commit.
--
-- No table is added: this is the eleven tables with one more column, and
-- requirement 1's count is unchanged.

ALTER TABLE item ADD COLUMN source_commit TEXT;
