-- Large immutable publication payloads stay outside ordinary item reads.
CREATE TABLE publication_claim (
    id TEXT PRIMARY KEY,
    item INTEGER NOT NULL REFERENCES item(id),
    active_item INTEGER UNIQUE REFERENCES item(id),
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    state TEXT NOT NULL CHECK (json_valid(state)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (active_item IS NULL OR active_item = item)
);
CREATE INDEX publication_claim_item ON publication_claim(item);
CREATE TRIGGER publication_payload_immutable
BEFORE UPDATE OF id, item, payload, created_at ON publication_claim
FOR EACH ROW BEGIN
    SELECT RAISE(ABORT, 'publication claim payload is immutable');
END;
