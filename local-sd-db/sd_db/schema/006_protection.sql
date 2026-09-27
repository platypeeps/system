-- One row per registered repository: the branch protection its default
-- branch enforces, as last observed by `sd_db.protection.sync`. It is a
-- side observation of the tracker collector, not the tracker: a repository
-- whose protection could not be read is `unknown` with a `reason`, and never
-- `protected`. A registered repository with no row here has not been observed
-- yet, which the reader renders as `unknown` too.
CREATE TABLE repo_protection (
    repo            TEXT PRIMARY KEY REFERENCES repo(path),
    observed_at     TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('protected', 'unprotected', 'unknown')),
    default_branch  TEXT,
    -- Why `unknown`: the HTTP status, the budget, or a remote that is not
    -- GitHub. NULL on the two answers that were read.
    reason          TEXT,
    -- JSON: `gaps` [{id, gap}], `detail` {...} as sd-status builds it,
    -- `merge_settings` [{id, value, flagged, gap}], and `requests` used.
    body            TEXT
);
