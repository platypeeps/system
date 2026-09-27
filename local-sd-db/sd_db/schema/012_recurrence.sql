-- Tasks that recur (sd:1099, `docs/work/2026-09-19-tasks-that-recur/`).
--
-- `recurrence` holds an RRULE string in the stdlib subset `sd_db.recurrence`
-- accepts -- FREQ, INTERVAL, BYMONTH, BYMONTHDAY -- in its canonical
-- spelling. `recurrence_anchor` is `schedule` or `completion`: whether the
-- next occurrence counts from the completed row's `due` or from the day it
-- was completed. Both are NULL on a row that does not recur.
--
-- **No CHECK on either column.** SQLite cannot `ALTER` a CHECK, and this
-- table cannot be rebuilt and renamed because four foreign keys reference
-- `item(id)`; migration 009 had to edit `sqlite_master` to widen one. The
-- library validates both instead, as it validates `due`.
--
-- A completion moves both values to the row it creates and clears them on
-- the completed row, so the rows that recur are `WHERE recurrence IS NOT NULL`.
ALTER TABLE item ADD COLUMN recurrence TEXT;
ALTER TABLE item ADD COLUMN recurrence_anchor TEXT;
