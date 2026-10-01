-- `judgment` gains the comparison arms of sd:2366: `kev`, a local System One
-- server answering the same request as Jev, and `haiku`, a frontier model
-- asked through a prompt adapter. Both only record; neither answer is used.
-- The arm names are roles in the comparison and not vendors: the transport
-- or the host is the row's `provider`, and the checkpoint is its `model`.
--
-- Two columns join the table. `server_ms` is the latency a model reported
-- beside the wall clock the caller measured. `probabilities` is the answer's
-- distribution as numbers in option order, which a Brier score needs.
-- `sd_db.judgment` shapes it as it shapes `ordering`: numbers and commas.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt the way
-- 007 rebuilt `state`: renamed away, recreated, the rows copied with their
-- ids, the old one dropped and the three indexes of 011 recreated. Nothing
-- references `judgment` by foreign key. The column comments of 011 still
-- describe every column they named.
--
-- The reverse, run by hand with every writer stopped. It drops the `kev`
-- and `haiku` rows, which the 016 shape cannot hold, and the two columns.
--
--   BEGIN;
--   ALTER TABLE judgment RENAME TO judgment_before_reverse;
--   CREATE TABLE judgment (
--       id              INTEGER PRIMARY KEY,
--       timestamp       TEXT NOT NULL,
--       caller          TEXT NOT NULL,
--       stage           TEXT NOT NULL,
--       arm             TEXT NOT NULL DEFAULT 'jev'
--                       CHECK (arm IN ('jev', 'baseline')),
--       pair            TEXT,
--       shadow          INTEGER NOT NULL DEFAULT 0 CHECK (shadow IN (0, 1)),
--       provider        TEXT NOT NULL,
--       model           TEXT,
--       primitive       TEXT NOT NULL,
--       question_id     TEXT,
--       questions       INTEGER,
--       outcome         TEXT NOT NULL
--                       CHECK (outcome IN ('ok', 'timeout', 'fallback',
--                                          'unavailable', 'invalid')),
--       cause           TEXT
--                       CHECK (cause IS NULL OR cause IN ('switched-off', 'unkeyed',
--                                                         'no-path', 'timeout',
--                                                         'invalid', 'unavailable',
--                                                         'budget')),
--       answer          TEXT,
--       confidence      REAL,
--       ordering        TEXT,
--       tokens_in       INTEGER,
--       tokens_out      INTEGER,
--       duration_ms     INTEGER,
--       usd             REAL,
--       changed         TEXT NOT NULL DEFAULT 'unknown'
--                       CHECK (changed IN ('yes', 'no', 'unknown')),
--       override        TEXT,
--       override_source TEXT,
--       override_at     TEXT
--   );
--   INSERT INTO judgment (id, timestamp, caller, stage, arm, pair, shadow, provider, model, primitive, question_id, questions, outcome, cause, answer, confidence, ordering, tokens_in, tokens_out, duration_ms, usd, changed, override, override_source, override_at)
--       SELECT id, timestamp, caller, stage, arm, pair, shadow, provider, model, primitive, question_id, questions, outcome, cause, answer, confidence, ordering, tokens_in, tokens_out, duration_ms, usd, changed, override, override_source, override_at
--       FROM judgment_before_reverse WHERE arm IN ('jev', 'baseline');
--   DROP TABLE judgment_before_reverse;
--   CREATE INDEX judgment_by_stage ON judgment (stage, arm, timestamp);
--   CREATE INDEX judgment_by_caller ON judgment (caller, timestamp);
--   CREATE INDEX judgment_by_pair ON judgment (pair);
--   PRAGMA user_version = 16;
--   COMMIT;

ALTER TABLE judgment RENAME TO judgment_before_017;
CREATE TABLE judgment (
    id              INTEGER PRIMARY KEY,
    timestamp       TEXT NOT NULL,
    caller          TEXT NOT NULL,
    stage           TEXT NOT NULL,
    arm             TEXT NOT NULL DEFAULT 'jev'
                    CHECK (arm IN ('jev', 'baseline', 'kev', 'haiku')),
    pair            TEXT,
    shadow          INTEGER NOT NULL DEFAULT 0 CHECK (shadow IN (0, 1)),
    provider        TEXT NOT NULL,
    model           TEXT,
    primitive       TEXT NOT NULL,
    question_id     TEXT,
    questions       INTEGER,
    outcome         TEXT NOT NULL
                    CHECK (outcome IN ('ok', 'timeout', 'fallback',
                                       'unavailable', 'invalid')),
    cause           TEXT
                    CHECK (cause IS NULL OR cause IN ('switched-off', 'unkeyed',
                                                      'no-path', 'timeout',
                                                      'invalid', 'unavailable',
                                                      'budget')),
    answer          TEXT,
    confidence      REAL,
    ordering        TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    duration_ms     INTEGER,
    usd             REAL,
    changed         TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (changed IN ('yes', 'no', 'unknown')),
    override        TEXT,
    override_source TEXT,
    override_at     TEXT,
    -- The latency the model reported, beside the wall clock in
    -- `duration_ms`: Kev's `latency_ms`, for one. NULL when the response
    -- carries none.
    server_ms       INTEGER,
    -- The answer's distribution as numbers in the caller's option order,
    -- `0.47,0.28,0.25`: numbers and commas, never the option keys.
    probabilities   TEXT
);
INSERT INTO judgment (id, timestamp, caller, stage, arm, pair, shadow, provider, model, primitive, question_id, questions, outcome, cause, answer, confidence, ordering, tokens_in, tokens_out, duration_ms, usd, changed, override, override_source, override_at)
    SELECT id, timestamp, caller, stage, arm, pair, shadow, provider, model, primitive, question_id, questions, outcome, cause, answer, confidence, ordering, tokens_in, tokens_out, duration_ms, usd, changed, override, override_source, override_at
    FROM judgment_before_017;
DROP TABLE judgment_before_017;
CREATE INDEX judgment_by_stage ON judgment (stage, arm, timestamp);
CREATE INDEX judgment_by_caller ON judgment (caller, timestamp);
CREATE INDEX judgment_by_pair ON judgment (pair);
