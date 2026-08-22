-- P0 import fact continuity: immutable, revision-scoped place receipts.
-- room_places remains the collaborative/Yjs projection and may be updated by
-- clients; this table is the append-only authority used for audit readback.

CREATE TABLE IF NOT EXISTS itinerary_place_receipts (
    workspace_id TEXT NOT NULL,
    itinerary_revision INT NOT NULL CHECK (itinerary_revision > 0),
    stop_id TEXT NOT NULL,
    place_id TEXT NOT NULL,
    receipt_hash CHAR(64) NOT NULL,
    receipt_json JSONB NOT NULL,
    place_data_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, itinerary_revision, stop_id),
    FOREIGN KEY (workspace_id, itinerary_revision)
        REFERENCES itinerary_revisions(workspace_id, revision) ON DELETE CASCADE,
    CHECK (receipt_hash ~ '^[0-9a-f]{64}$'),
    CHECK (receipt_json->>'canonical_place_id' = place_id),
    CHECK (receipt_json = place_data_json->'resolved_place_receipt')
);

CREATE INDEX IF NOT EXISTS idx_itinerary_place_receipts_place
    ON itinerary_place_receipts(workspace_id, itinerary_revision, place_id);
