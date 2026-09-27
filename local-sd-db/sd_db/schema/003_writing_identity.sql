-- Writing lifecycle state belongs to the item. Existing rows remain unchanged.
ALTER TABLE item ADD COLUMN piece TEXT;
ALTER TABLE item ADD COLUMN parked_at TEXT;
ALTER TABLE item ADD COLUMN gate_generation INTEGER NOT NULL DEFAULT 0
    CHECK (gate_generation >= 0);
ALTER TABLE item ADD COLUMN ready_digest TEXT;
CREATE UNIQUE INDEX item_by_piece ON item(repo, piece) WHERE piece IS NOT NULL;
