-- Trip Understanding v3: anonymous ownership, immutable understanding facts,
-- durable jobs/events, idempotency and strict public result pointers.
-- This migration is additive and deliberately does not alter legacy room data.

CREATE TABLE IF NOT EXISTS trip_understanding_anonymous_sessions (
    session_id TEXT PRIMARY KEY,
    capability_hash CHAR(64) NOT NULL UNIQUE
        CHECK (capability_hash ~ '^[0-9a-f]{64}$'),
    capability_version INT NOT NULL DEFAULT 1 CHECK (capability_version > 0),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    claimed_by TEXT REFERENCES users(user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_trip_understanding_anonymous_sessions_expiry
    ON trip_understanding_anonymous_sessions(expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS trip_understandings (
    understanding_id TEXT PRIMARY KEY,
    public_resource_id TEXT NOT NULL UNIQUE CHECK (length(public_resource_id) BETWEEN 20 AND 80),
    owner_user_id TEXT REFERENCES users(user_id),
    anonymous_session_id TEXT REFERENCES trip_understanding_anonymous_sessions(session_id),
    state TEXT NOT NULL CHECK (state IN (
        'PROCESSING', 'READY', 'PARTIAL', 'FAILED', 'DELETED'
    )),
    current_revision INT NOT NULL DEFAULT 1 CHECK (current_revision > 0),
    result_revision INT CHECK (result_revision IS NULL OR result_revision > 0),
    current_result_id TEXT,
    etag_nonce CHAR(64) NOT NULL CHECK (etag_nonce ~ '^[0-9a-f]{64}$'),
    source_expires_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((owner_user_id IS NULL) <> (anonymous_session_id IS NULL)),
    CHECK ((state = 'DELETED') = (deleted_at IS NOT NULL)),
    CHECK (source_expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_trip_understandings_anonymous_owner
    ON trip_understandings(anonymous_session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trip_understandings_user_owner
    ON trip_understandings(owner_user_id, created_at DESC)
    WHERE owner_user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS trip_understanding_sources (
    source_id TEXT PRIMARY KEY,
    understanding_id TEXT NOT NULL REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK (source_type IN ('FIXED_DEMO', 'TEXT', 'SCREENSHOT_OCR')),
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    encrypted_content BYTEA,
    encryption_key_ref TEXT,
    retention_until TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    deletion_receipt_hash CHAR(64)
        CHECK (deletion_receipt_hash IS NULL OR deletion_receipt_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (understanding_id, source_id),
    CHECK ((encrypted_content IS NULL) = (encryption_key_ref IS NULL)),
    CHECK ((deleted_at IS NULL) = (deletion_receipt_hash IS NULL))
);

CREATE TABLE IF NOT EXISTS trip_understanding_revisions (
    understanding_id TEXT NOT NULL REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    revision INT NOT NULL CHECK (revision > 0),
    parent_revision INT,
    source_id TEXT NOT NULL REFERENCES trip_understanding_sources(source_id),
    status TEXT NOT NULL CHECK (status IN ('DRAFT', 'PROCESSING', 'PARTIAL', 'READY', 'FAILED')),
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    destination_json JSONB NOT NULL CHECK (jsonb_typeof(destination_json) = 'object'),
    assumptions_json JSONB NOT NULL CHECK (jsonb_typeof(assumptions_json) = 'array'),
    proposal_json JSONB NOT NULL CHECK (jsonb_typeof(proposal_json) = 'object'),
    inference_binding_json JSONB NOT NULL CHECK (jsonb_typeof(inference_binding_json) = 'object'),
    compiler_receipt_json JSONB NOT NULL CHECK (jsonb_typeof(compiler_receipt_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (understanding_id, revision),
    FOREIGN KEY (understanding_id, parent_revision)
        REFERENCES trip_understanding_revisions(understanding_id, revision),
    CHECK (parent_revision IS NULL OR parent_revision < revision)
);

CREATE TABLE IF NOT EXISTS trip_understanding_activities (
    activity_id TEXT PRIMARY KEY,
    understanding_id TEXT NOT NULL,
    revision INT NOT NULL,
    public_activity_token TEXT NOT NULL UNIQUE
        CHECK (length(public_activity_token) BETWEEN 20 AND 80),
    day_index INT CHECK (day_index IS NULL OR day_index BETWEEN 1 AND 14),
    sequence_index INT NOT NULL CHECK (sequence_index >= 0),
    role TEXT NOT NULL CHECK (role IN (
        'PLANNED', 'OPTIONAL', 'REFERENCE', 'EXCLUDED', 'PASS_THROUGH'
    )),
    mention_text TEXT NOT NULL CHECK (length(mention_text) > 0),
    atomic_place_name TEXT,
    category_hint TEXT,
    time_hint TEXT,
    eligible_for_place_search BOOLEAN NOT NULL,
    resolution_status TEXT NOT NULL CHECK (resolution_status IN (
        'NOT_ELIGIBLE', 'UNRESOLVED', 'AUTO_MATCHED', 'NEEDS_CONFIRMATION'
    )),
    canonical_place_id TEXT,
    resolver_receipt_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(resolver_receipt_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (understanding_id, revision)
        REFERENCES trip_understanding_revisions(understanding_id, revision) ON DELETE CASCADE,
    UNIQUE (understanding_id, revision, day_index, sequence_index, activity_id),
    CHECK (
        NOT eligible_for_place_search
        OR (role = 'PLANNED' AND atomic_place_name IS NOT NULL AND day_index IS NOT NULL)
    ),
    CHECK (
        (resolution_status = 'AUTO_MATCHED' AND canonical_place_id IS NOT NULL)
        OR (resolution_status <> 'AUTO_MATCHED' AND canonical_place_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_trip_understanding_activities_day
    ON trip_understanding_activities(understanding_id, revision, day_index, sequence_index);

CREATE TABLE IF NOT EXISTS trip_understanding_source_claims (
    claim_id TEXT PRIMARY KEY,
    understanding_id TEXT NOT NULL,
    revision INT NOT NULL,
    source_id TEXT NOT NULL REFERENCES trip_understanding_sources(source_id),
    activity_id TEXT REFERENCES trip_understanding_activities(activity_id) ON DELETE CASCADE,
    claim_type TEXT NOT NULL CHECK (claim_type IN (
        'PLACE_MENTION', 'ROLE', 'DAY', 'TIME_HINT', 'ASSUMPTION', 'EXCLUSION'
    )),
    span_start INT NOT NULL CHECK (span_start >= 0),
    span_end INT NOT NULL CHECK (span_end > span_start),
    quote TEXT NOT NULL CHECK (length(quote) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (understanding_id, revision)
        REFERENCES trip_understanding_revisions(understanding_id, revision) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trip_understanding_source_claims_revision
    ON trip_understanding_source_claims(understanding_id, revision);

CREATE TABLE IF NOT EXISTS trip_understanding_jobs (
    job_id TEXT PRIMARY KEY,
    understanding_id TEXT NOT NULL REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    revision INT NOT NULL CHECK (revision > 0),
    job_type TEXT NOT NULL CHECK (job_type IN ('UNDERSTAND')),
    status TEXT NOT NULL CHECK (status IN (
        'QUEUED', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'PRIVACY_BLOCKED'
    )),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    attempt INT NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INT NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    input_hash CHAR(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    last_error_category TEXT,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (understanding_id, revision, job_type),
    FOREIGN KEY (understanding_id, revision)
        REFERENCES trip_understanding_revisions(understanding_id, revision),
    CHECK ((lease_owner IS NULL) = (lease_until IS NULL)),
    CHECK (status <> 'RUNNING' OR lease_owner IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_trip_understanding_jobs_claim
    ON trip_understanding_jobs(status, available_at, lease_until, created_at);

CREATE TABLE IF NOT EXISTS trip_understanding_events (
    understanding_id TEXT NOT NULL REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    event_id BIGINT GENERATED ALWAYS AS IDENTITY,
    event_key TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('progress', 'result_available')),
    public_payload_json JSONB NOT NULL CHECK (jsonb_typeof(public_payload_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (understanding_id, event_id),
    UNIQUE (understanding_id, event_key)
);

CREATE TABLE IF NOT EXISTS trip_understanding_results (
    result_id TEXT PRIMARY KEY,
    understanding_id TEXT NOT NULL,
    revision INT NOT NULL CHECK (revision > 0),
    public_json JSONB NOT NULL CHECK (jsonb_typeof(public_json) = 'object'),
    public_sha256 CHAR(64) NOT NULL CHECK (public_sha256 ~ '^[0-9a-f]{64}$'),
    opaque_etag TEXT NOT NULL UNIQUE CHECK (length(opaque_etag) BETWEEN 24 AND 120),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (understanding_id, revision),
    FOREIGN KEY (understanding_id, revision)
        REFERENCES trip_understanding_revisions(understanding_id, revision) ON DELETE CASCADE
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'trip_understandings_current_result_fk'
    ) THEN
        ALTER TABLE trip_understandings
            ADD CONSTRAINT trip_understandings_current_result_fk
            FOREIGN KEY (current_result_id) REFERENCES trip_understanding_results(result_id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS trip_understanding_idempotency_records (
    scope TEXT NOT NULL,
    key_hash CHAR(64) NOT NULL CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    state TEXT NOT NULL CHECK (state IN ('IN_PROGRESS', 'COMPLETED')),
    response_status SMALLINT,
    response_headers_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(response_headers_json) = 'object'),
    response_json JSONB,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (scope, key_hash),
    CHECK (
        (state = 'IN_PROGRESS' AND response_status IS NULL AND response_json IS NULL AND completed_at IS NULL)
        OR (state = 'COMPLETED' AND response_status IS NOT NULL AND response_json IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS trip_understanding_side_effect_receipts (
    receipt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES trip_understanding_jobs(job_id) ON DELETE CASCADE,
    effect_key TEXT NOT NULL UNIQUE,
    effect_type TEXT NOT NULL CHECK (effect_type IN ('FIXTURE_INFERENCE_RESOLUTION_PROJECTION')),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    response_hash CHAR(64) NOT NULL CHECK (response_hash ~ '^[0-9a-f]{64}$'),
    provider_binding_json JSONB NOT NULL CHECK (jsonb_typeof(provider_binding_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trip_understanding_resource_tombstones (
    public_resource_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL CHECK (reason IN ('CLAIMED', 'DELETED', 'EXPIRED')),
    replacement_public_resource_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (reason = 'CLAIMED' AND replacement_public_resource_id IS NOT NULL)
        OR (reason <> 'CLAIMED' AND replacement_public_resource_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS trip_understanding_claim_commands (
    command_id TEXT PRIMARY KEY,
    understanding_id TEXT NOT NULL REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    actor_user_id TEXT NOT NULL REFERENCES users(user_id),
    idempotency_key_hash CHAR(64) NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    old_public_resource_id TEXT NOT NULL,
    new_public_resource_id TEXT NOT NULL,
    response_json JSONB NOT NULL CHECK (jsonb_typeof(response_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (understanding_id, idempotency_key_hash)
);

CREATE TABLE IF NOT EXISTS trip_understanding_deletion_jobs (
    deletion_job_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('SOURCE', 'TRIP', 'ACCOUNT_TRAVEL_DATA')),
    understanding_id TEXT REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    owner_user_id TEXT REFERENCES users(user_id),
    status TEXT NOT NULL CHECK (status IN ('IN_PROGRESS', 'COMPLETED', 'RETRY_REQUIRED')),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    receipt_hash CHAR(64) CHECK (receipt_hash IS NULL OR receipt_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (understanding_id IS NOT NULL OR owner_user_id IS NOT NULL),
    CHECK ((status = 'COMPLETED') = (receipt_hash IS NOT NULL))
);

CREATE OR REPLACE FUNCTION reject_trip_understanding_immutable_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'trip understanding fact, event, result or receipt is immutable';
END;
$$;

DROP TRIGGER IF EXISTS trip_understanding_revisions_no_update ON trip_understanding_revisions;
CREATE TRIGGER trip_understanding_revisions_no_update
    BEFORE UPDATE ON trip_understanding_revisions
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_understanding_activities_no_update ON trip_understanding_activities;
CREATE TRIGGER trip_understanding_activities_no_update
    BEFORE UPDATE ON trip_understanding_activities
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_understanding_source_claims_no_update ON trip_understanding_source_claims;
CREATE TRIGGER trip_understanding_source_claims_no_update
    BEFORE UPDATE ON trip_understanding_source_claims
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_understanding_events_no_update ON trip_understanding_events;
CREATE TRIGGER trip_understanding_events_no_update
    BEFORE UPDATE ON trip_understanding_events
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_understanding_results_no_update ON trip_understanding_results;
CREATE TRIGGER trip_understanding_results_no_update
    BEFORE UPDATE ON trip_understanding_results
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_understanding_side_effect_receipts_no_update
    ON trip_understanding_side_effect_receipts;
CREATE TRIGGER trip_understanding_side_effect_receipts_no_update
    BEFORE UPDATE ON trip_understanding_side_effect_receipts
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();
