-- Server-calculated shared routes are independent of each member's personal saves.
CREATE TABLE IF NOT EXISTS room_itinerary_revisions (
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    version BIGINT NOT NULL CHECK (version > 0),
    published_by_user_id TEXT REFERENCES users(user_id) ON DELETE SET NULL,
    request_id TEXT NOT NULL,
    selection_snapshot JSONB NOT NULL CHECK (jsonb_typeof(selection_snapshot) = 'object'),
    itinerary_data JSONB NOT NULL CHECK (jsonb_typeof(itinerary_data) = 'object'),
    total_distance_km DOUBLE PRECISION,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (room_id, version),
    UNIQUE (room_id, published_by_user_id, request_id)
);

-- A duplicate in-flight click must not start a second provider calculation.
-- Failed/interrupted attempts are never automatically retried with the same id.
CREATE TABLE IF NOT EXISTS room_itinerary_requests (
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    base_version BIGINT NOT NULL CHECK (base_version >= 0),
    selection_snapshot JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'PUBLISHED', 'FAILED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (room_id, user_id, request_id)
);
