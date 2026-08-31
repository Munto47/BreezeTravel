-- G06 explicit preference memory, purpose-specific consent, minimal feedback,
-- and revocable read-only sharing. Raw itinerary source, screenshots and chat
-- content are deliberately absent from this schema.

CREATE TABLE IF NOT EXISTS g06_data_consents (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    memory_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    feedback_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    training_eval_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS g06_preference_profiles (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    walking_tolerance_minutes INTEGER CHECK (
        walking_tolerance_minutes IS NULL
        OR walking_tolerance_minutes BETWEEN 5 AND 120
    ),
    preferred_start_time TEXT CHECK (
        preferred_start_time IS NULL
        OR preferred_start_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
    ),
    dining_preferences TEXT[] NOT NULL DEFAULT '{}',
    hotel_preferences TEXT[] NOT NULL DEFAULT '{}',
    intensity TEXT CHECK (
        intensity IS NULL OR intensity IN ('RELAXED', 'BALANCED', 'FULL')
    ),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (cardinality(dining_preferences) <= 3),
    CHECK (cardinality(hotel_preferences) <= 3),
    CHECK (dining_preferences <@ ARRAY[
        'LOCAL', 'VEGETARIAN', 'HALAL', 'NO_SPICY', 'QUICK'
    ]::TEXT[]),
    CHECK (hotel_preferences <@ ARRAY[
        'CHAIN', 'NEAR_TRANSIT', 'QUIET', 'CENTRAL'
    ]::TEXT[])
);

CREATE TABLE IF NOT EXISTS g06_feedback_events (
    feedback_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    understanding_id TEXT NOT NULL
        REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'CORRECTION', 'ADOPTED', 'REJECTED', 'VOLUNTARY'
    )),
    subject_type TEXT NOT NULL CHECK (subject_type IN (
        'TRIP', 'ACTIVITY', 'KNOWLEDGE_SUGGESTION'
    )),
    subject_ref_hash CHAR(64) CHECK (
        subject_ref_hash IS NULL OR subject_ref_hash ~ '^[0-9a-f]{64}$'
    ),
    key_hash CHAR(64) NOT NULL CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (owner_user_id, understanding_id, key_hash)
);

CREATE TABLE IF NOT EXISTS g06_share_links (
    share_ref TEXT PRIMARY KEY CHECK (length(share_ref) BETWEEN 24 AND 80),
    understanding_id TEXT NOT NULL
        REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    secret_hash CHAR(64) NOT NULL UNIQUE CHECK (secret_hash ~ '^[0-9a-f]{64}$'),
    projection_json JSONB NOT NULL CHECK (jsonb_typeof(projection_json) = 'object'),
    key_hash CHAR(64) NOT NULL CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (owner_user_id, understanding_id, key_hash),
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_g06_share_links_owner
    ON g06_share_links(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_g06_share_links_expiry
    ON g06_share_links(expires_at) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS g06_share_sessions (
    session_id TEXT PRIMARY KEY,
    share_ref TEXT NOT NULL REFERENCES g06_share_links(share_ref) ON DELETE CASCADE,
    capability_hash CHAR(64) NOT NULL UNIQUE
        CHECK (capability_hash ~ '^[0-9a-f]{64}$'),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_g06_share_sessions_expiry
    ON g06_share_sessions(expires_at);
