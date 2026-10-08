-- `judgment.error_class` and `judgment.error_detail`: why a call failed (sd:2973).
--
-- `cause` is the decline's class, one of seven, and `unavailable` covers an
-- endpoint that answered 503, a loopback port nothing listens on and a connect
-- a sandbox refused. Those repairs differ, so the row now says which: the
-- exception's class, `ConnectionRefusedError` or `HTTPError`, and its detail,
-- the errno name `ECONNREFUSED` or the status `503`. Never the message text,
-- the URL or a response body: either may carry the request back.
--
-- `ADD COLUMN` for 016's reason: SQLite adds a nullable column in place,
-- touches no other table and moves no row. Every existing row reads NULL.
-- The reverse, run by hand with the runner, the dashboard and the serve agent
-- stopped:
--
--   BEGIN;
--   ALTER TABLE judgment DROP COLUMN error_detail;
--   ALTER TABLE judgment DROP COLUMN error_class;
--   PRAGMA user_version = 22;
--   COMMIT;

ALTER TABLE judgment ADD COLUMN error_class TEXT;
ALTER TABLE judgment ADD COLUMN error_detail TEXT;
