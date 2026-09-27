-- `judgment`: one row per decision a judgment model was asked to make, and
-- one per decision the mechanism it replaced made instead.
--
-- Roughly eight production callers route a decision through a judgment model
-- and keep their old mechanism as a fallback. Nothing measured any of it: the
-- response carried token counts that were read and dropped, no caller timed
-- the call, and no row anywhere said which stage spent what.
--
-- **Both arms, one table, one stage key.** A row for the model alone cannot
-- answer whether it helped. The old mechanism is still in the tree and still
-- runs whenever the model declines, times out or the machine is unkeyed, which
-- makes it the control arm. `arm` says which one a row is, `pair` ties the two
-- halves of one decision together, and both carry the same stage key, because
-- a fallback that cannot be counted against a judgment on the same stage
-- answers nothing.
--
-- **Not a `cost` row.** `cost` is money against a `bill`, and its `provider`
-- column is a foreign key into the registry's provider list. A judgment model
-- is on neither: writing judgments there would need a registry entry and a
-- bill invented to satisfy the key, and every sum the Usage screen takes over
-- `run` and `bound` rows would then have to learn to skip a source it never
-- asked about. This table carries its own counts instead, and `usd` stays NULL
-- until there is a price list to fill it.
--
-- **Identifiers and counts, and no submitted content.** No column here holds
-- prompt text, state text, a file path, a mail subject or a body. Two columns
-- could carry content by accident and neither is allowed to: `answer` is the
-- judgment -- a probability, a criterion key the caller itself named, or a
-- score -- and `ordering` is positions into the caller's own input, never the
-- things at those positions. `sd_db.judgment` caps and shapes both, and
-- refuses rather than truncates, because a truncated value is a prefix of
-- content, stored.
--
-- **Provider-neutral.** `provider`, `model` and `primitive` are required
-- fields with free values. No value in this table is required to name any
-- vendor, so a second judgment model records here without a migration.
CREATE TABLE judgment (
    id              INTEGER PRIMARY KEY,
    -- When the decision was made, in the one shape `writes.now` writes.
    timestamp       TEXT NOT NULL,
    -- The tool that asked, e.g. `local-mail-intake`.
    caller          TEXT NOT NULL,
    -- The decision it was making, e.g. `JEV_MAIL_INTAKE`. The report groups
    -- by this, and both arms of one decision carry the same value.
    stage           TEXT NOT NULL,
    -- Which arm of the comparison this row is: the model's judgment, or the
    -- mechanism that ran instead of it or beside it.
    arm             TEXT NOT NULL DEFAULT 'jev'
                    CHECK (arm IN ('jev', 'baseline')),
    -- Ties the two arms of one decision together. NULL on a row with no
    -- partner, which is most of them: a stage whose fallback never runs has
    -- nothing to pair with.
    pair            TEXT,
    -- A shadow run: both arms on the same input, the baseline's answer used
    -- and the model's recorded. It costs a real call, so the report counts it
    -- rather than letting it hide inside the call total.
    shadow          INTEGER NOT NULL DEFAULT 0 CHECK (shadow IN (0, 1)),
    -- What answered. For a baseline row this is the machine itself.
    provider        TEXT NOT NULL,
    -- The model as the response named it; NULL when nothing answered.
    model           TEXT,
    -- The kind of question: the provider's own word for it.
    primitive       TEXT NOT NULL,
    -- The caller's id for the question, which code sees and the model does
    -- not. NULL for a batch, which has one id per question and not one id.
    question_id     TEXT,
    -- How many questions the request carried, so cost per question is
    -- readable for a batch as well as for a single question.
    questions       INTEGER,
    -- How it ended. `fallback` means the old mechanism's answer was used
    -- instead of a judgment, and `cause` says why; the other four are what
    -- happened when there was no fallback to take.
    outcome         TEXT NOT NULL
                    CHECK (outcome IN ('ok', 'timeout', 'fallback',
                                       'unavailable', 'invalid')),
    -- Why the model was not used, as a value and not a boolean. `no-path` is
    -- its own word because it has already caused a silent outage here: every
    -- gate was on, the key worked and the probe answered, and every consumer
    -- skipped its step because `jev` was not linked onto PATH. `budget` is in
    -- the vocabulary before anything writes it, so that adding the budgets is
    -- a write and not a migration.
    cause           TEXT
                    CHECK (cause IS NULL OR cause IN ('switched-off', 'unkeyed',
                                                      'no-path', 'timeout',
                                                      'invalid', 'unavailable',
                                                      'budget')),
    -- The judgment itself, not the bytes the caller saw: a probability, the
    -- chosen criterion key, or the score. On a baseline row, what the old
    -- mechanism answered. NULL when nothing was judged.
    answer          TEXT,
    confidence      REAL,
    -- What a re-ranking stage produced, as positions into the caller's own
    -- input: `3,1,2`. Positions and never the things at them. Kept on both
    -- arms, because a lane where the old mechanism runs anyway and the model
    -- only reorders its output has no delta if only the final order is saved.
    ordering        TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    -- Wall clock from the first send to the final outcome, retries and their
    -- backoff included: what the caller waited, not what the server took. On
    -- a baseline row, what the old mechanism took.
    duration_ms     INTEGER,
    -- NULL until a price list exists. The counts above are the record.
    usd             REAL,
    -- Did this change what the caller did? `unknown` is the honest default
    -- and the common one: only a caller that also hands over what it would
    -- have done can be compared against.
    changed         TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (changed IN ('yes', 'no', 'unknown')),
    -- Room for a later human or authoritative-model answer over this one.
    -- Nothing writes these yet and no path exists to; the columns are here so
    -- that adding one is a write and not a migration.
    override        TEXT,
    override_source TEXT,
    override_at     TEXT
);

CREATE INDEX judgment_by_stage ON judgment (stage, arm, timestamp);
CREATE INDEX judgment_by_caller ON judgment (caller, timestamp);
CREATE INDEX judgment_by_pair ON judgment (pair);
