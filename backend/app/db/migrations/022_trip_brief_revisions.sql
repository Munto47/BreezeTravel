-- Trip Check V1: immutable, field-provenance-aware TripBrief revisions.

CREATE TABLE IF NOT EXISTS trip_brief_revisions (
    brief_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    revision INT NOT NULL CHECK (revision > 0),
    parent_revision INT,
    status TEXT NOT NULL CHECK (status IN ('DRAFT', 'NEEDS_CONFIRMATION', 'CONFIRMED')),
    city TEXT NOT NULL CHECK (city IN ('北京', '上海', '杭州')),
    trip_start_date DATE NOT NULL,
    trip_end_date DATE NOT NULL,
    traveler_count INT NOT NULL CHECK (traveler_count BETWEEN 2 AND 5),
    content_json JSONB NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_by TEXT REFERENCES users(user_id),
    confirmed_at TIMESTAMPTZ,
    PRIMARY KEY (brief_id, revision),
    UNIQUE (workspace_id, revision),
    UNIQUE (workspace_id, content_hash, revision),
    FOREIGN KEY (brief_id, parent_revision)
        REFERENCES trip_brief_revisions(brief_id, revision),
    CHECK (trip_end_date >= trip_start_date),
    CHECK (trip_end_date - trip_start_date BETWEEN 1 AND 4),
    CHECK (parent_revision IS NULL OR parent_revision < revision),
    CHECK (
        (status = 'CONFIRMED' AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)
        OR (status <> 'CONFIRMED' AND confirmed_by IS NULL AND confirmed_at IS NULL)
    ),
    CHECK (content_json->>'brief_id' = brief_id),
    CHECK ((content_json->>'revision')::INT = revision),
    CHECK (content_json->>'workspace_id' = workspace_id),
    CHECK (content_json->>'content_hash' = rtrim(content_hash))
);

CREATE INDEX IF NOT EXISTS idx_trip_brief_revisions_workspace
    ON trip_brief_revisions(workspace_id, revision DESC);

CREATE TABLE IF NOT EXISTS trip_brief_field_sources (
    brief_id TEXT NOT NULL,
    brief_revision INT NOT NULL,
    field_path TEXT NOT NULL CHECK (length(field_path) > 0),
    source_index INT NOT NULL CHECK (source_index >= 0),
    source_id TEXT,
    span_start INT CHECK (span_start IS NULL OR span_start >= 0),
    span_end INT CHECK (span_end IS NULL OR span_end > span_start),
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    origin TEXT NOT NULL CHECK (origin IN (
        'USER_TEXT', 'PARSER', 'USER_CONFIRMED', 'INFERRED', 'DEFAULT_NO_PREFERENCE'
    )),
    confirmation_status TEXT NOT NULL CHECK (confirmation_status IN ('UNCONFIRMED', 'CONFIRMED')),
    hardness TEXT NOT NULL CHECK (hardness IN ('HARD', 'SOFT', 'NO_PREFERENCE')),
    PRIMARY KEY (brief_id, brief_revision, field_path, source_index),
    FOREIGN KEY (brief_id, brief_revision)
        REFERENCES trip_brief_revisions(brief_id, revision) ON DELETE CASCADE,
    CHECK (origin <> 'INFERRED' OR hardness <> 'HARD'),
    CHECK (origin <> 'DEFAULT_NO_PREFERENCE' OR hardness = 'NO_PREFERENCE'),
    CHECK (
        (source_id IS NULL AND span_start IS NULL AND span_end IS NULL)
        OR (source_id IS NOT NULL AND span_start IS NOT NULL AND span_end IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS trip_temporary_assets (
    asset_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    media_type TEXT NOT NULL,
    byte_size BIGINT NOT NULL CHECK (byte_size > 0),
    storage_locator TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'PROCESSING', 'CLEANED', 'CLEANUP_FAILED')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS temporary_asset_cleanup_receipts (
    receipt_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL UNIQUE REFERENCES trip_temporary_assets(asset_id) ON DELETE CASCADE,
    terminal_reason TEXT NOT NULL CHECK (terminal_reason IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT')),
    cleanup_status TEXT NOT NULL CHECK (cleanup_status IN ('DELETED', 'DELETE_FAILED')),
    asset_hash CHAR(64) NOT NULL CHECK (asset_hash ~ '^[0-9a-f]{64}$'),
    cleanup_attempted_at TIMESTAMPTZ NOT NULL,
    cleanup_error_category TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (cleanup_status = 'DELETED' AND cleanup_error_category IS NULL)
        OR (cleanup_status = 'DELETE_FAILED' AND cleanup_error_category IS NOT NULL)
    )
);

ALTER TABLE trip_workspaces
    ADD COLUMN IF NOT EXISTS current_brief_id TEXT,
    ADD COLUMN IF NOT EXISTS current_trip_brief_revision INT;

ALTER TABLE trip_workspaces
    ADD CONSTRAINT trip_workspaces_current_trip_brief_fk
    FOREIGN KEY (current_brief_id, current_trip_brief_revision)
    REFERENCES trip_brief_revisions(brief_id, revision);

CREATE OR REPLACE FUNCTION reject_trip_brief_fact_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'trip brief revision facts are immutable';
END;
$$;

DROP TRIGGER IF EXISTS trip_brief_revisions_no_update ON trip_brief_revisions;
CREATE TRIGGER trip_brief_revisions_no_update
    BEFORE UPDATE ON trip_brief_revisions
    FOR EACH ROW EXECUTE FUNCTION reject_trip_brief_fact_update();

DROP TRIGGER IF EXISTS trip_brief_field_sources_no_update ON trip_brief_field_sources;
CREATE TRIGGER trip_brief_field_sources_no_update
    BEFORE UPDATE ON trip_brief_field_sources
    FOR EACH ROW EXECUTE FUNCTION reject_trip_brief_fact_update();

DROP TRIGGER IF EXISTS temporary_asset_cleanup_receipts_no_update ON temporary_asset_cleanup_receipts;
CREATE TRIGGER temporary_asset_cleanup_receipts_no_update
    BEFORE UPDATE ON temporary_asset_cleanup_receipts
    FOR EACH ROW EXECUTE FUNCTION reject_trip_brief_fact_update();
