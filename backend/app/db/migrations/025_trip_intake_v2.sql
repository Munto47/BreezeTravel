-- Trip Intake v2: immutable pre-workspace extraction revisions and atomic materialization receipts.

CREATE TABLE IF NOT EXISTS trip_intake_revisions (
    intake_id TEXT NOT NULL,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    revision INT NOT NULL CHECK (revision > 0),
    parent_revision INT,
    schema_version TEXT NOT NULL CHECK (schema_version = 'trip-intake-revision-v2'),
    source_type TEXT NOT NULL CHECK (source_type IN ('AI_TEXT', 'MANUAL_TEXT', 'SCREENSHOT_OCR')),
    raw_text TEXT NOT NULL CHECK (length(raw_text) > 0),
    raw_text_sha256 CHAR(64) NOT NULL CHECK (raw_text_sha256 ~ '^[0-9a-f]{64}$'),
    sources_json JSONB NOT NULL CHECK (jsonb_typeof(sources_json) = 'array'),
    parser_binding_json JSONB NOT NULL CHECK (jsonb_typeof(parser_binding_json) = 'object'),
    extraction_json JSONB NOT NULL CHECK (jsonb_typeof(extraction_json) = 'object'),
    confirmed_fields_json JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(confirmed_fields_json) = 'array'),
    status TEXT NOT NULL CHECK (status IN ('NEEDS_CONFIRMATION', 'READY', 'EXTRACTION_FAILED')),
    content_json JSONB NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_by TEXT REFERENCES users(user_id),
    confirmed_at TIMESTAMPTZ,
    PRIMARY KEY (intake_id, revision),
    UNIQUE (room_id, intake_id),
    FOREIGN KEY (intake_id, parent_revision)
        REFERENCES trip_intake_revisions(intake_id, revision),
    CHECK (parent_revision IS NULL OR parent_revision < revision),
    CHECK (
        (status = 'READY' AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)
        OR (status <> 'READY' AND confirmed_by IS NULL AND confirmed_at IS NULL)
    ),
    CHECK (content_json->>'intake_id' = intake_id),
    CHECK ((content_json->>'revision')::INT = revision),
    CHECK (content_json->>'room_id' = room_id),
    CHECK (content_json->>'content_hash' = rtrim(content_hash))
);

CREATE INDEX IF NOT EXISTS idx_trip_intake_revisions_room
    ON trip_intake_revisions(room_id, created_at DESC, revision DESC);

CREATE TABLE IF NOT EXISTS trip_intake_field_sources (
    intake_id TEXT NOT NULL,
    intake_revision INT NOT NULL,
    field_path TEXT NOT NULL CHECK (length(field_path) > 0),
    source_index INT NOT NULL CHECK (source_index >= 0),
    source_id TEXT NOT NULL,
    span_start INT NOT NULL CHECK (span_start >= 0),
    span_end INT NOT NULL CHECK (span_end > span_start),
    quote TEXT NOT NULL CHECK (length(quote) > 0),
    confidence DOUBLE PRECISION CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0),
    PRIMARY KEY (intake_id, intake_revision, field_path, source_index),
    FOREIGN KEY (intake_id, intake_revision)
        REFERENCES trip_intake_revisions(intake_id, revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trip_intake_commands (
    command_id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL,
    base_revision INT NOT NULL CHECK (base_revision > 0),
    result_revision INT NOT NULL CHECK (result_revision > base_revision),
    operation TEXT NOT NULL CHECK (operation IN ('PATCH', 'CONFIRM')),
    actor_user_id TEXT NOT NULL REFERENCES users(user_id),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    response_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (intake_id, idempotency_key),
    UNIQUE (intake_id, result_revision),
    FOREIGN KEY (intake_id, base_revision)
        REFERENCES trip_intake_revisions(intake_id, revision),
    FOREIGN KEY (intake_id, result_revision)
        REFERENCES trip_intake_revisions(intake_id, revision)
);

CREATE TABLE IF NOT EXISTS trip_intake_materializations (
    materialization_id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL,
    intake_revision INT NOT NULL,
    actor_user_id TEXT NOT NULL REFERENCES users(user_id),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id),
    brief_id TEXT NOT NULL,
    brief_revision INT NOT NULL,
    import_id TEXT NOT NULL REFERENCES itinerary_imports(import_id),
    response_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (intake_id, idempotency_key),
    UNIQUE (intake_id, intake_revision),
    FOREIGN KEY (intake_id, intake_revision)
        REFERENCES trip_intake_revisions(intake_id, revision),
    FOREIGN KEY (brief_id, brief_revision)
        REFERENCES trip_brief_revisions(brief_id, revision)
);

ALTER TABLE trip_brief_revisions
    ADD COLUMN IF NOT EXISTS source_intake_id TEXT,
    ADD COLUMN IF NOT EXISTS source_intake_revision INT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'trip_brief_revisions_source_intake_fk'
    ) THEN
        ALTER TABLE trip_brief_revisions
            ADD CONSTRAINT trip_brief_revisions_source_intake_fk
            FOREIGN KEY (source_intake_id, source_intake_revision)
            REFERENCES trip_intake_revisions(intake_id, revision);
    END IF;
END $$;

-- Replace product-scope CHECKs without assuming PostgreSQL's generated constraint suffixes.
DO $$
DECLARE item RECORD;
BEGIN
    FOR item IN
        SELECT conrelid::regclass AS table_name, conname
        FROM pg_constraint
        WHERE contype = 'c'
          AND (
            (conrelid = 'trip_workspaces'::regclass AND pg_get_constraintdef(oid) LIKE '%北京%')
            OR (conrelid = 'itinerary_revisions'::regclass AND pg_get_constraintdef(oid) LIKE '%北京%')
            OR (conrelid = 'trip_brief_revisions'::regclass AND (
                pg_get_constraintdef(oid) LIKE '%北京%'
                OR pg_get_constraintdef(oid) LIKE '%traveler_count%BETWEEN 2 AND 5%'
                OR pg_get_constraintdef(oid) LIKE '%trip_end_date - trip_start_date%BETWEEN 1 AND 4%'
            ))
            OR (conrelid = 'suggestion_sets'::regclass
                AND pg_get_constraintdef(oid) LIKE '%day_index%BETWEEN 0 AND 4%')
          )
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', item.table_name, item.conname);
    END LOOP;
END $$;

ALTER TABLE trip_workspaces
    ADD CONSTRAINT trip_workspaces_city_nonblank_check CHECK (length(btrim(city)) > 0);
ALTER TABLE itinerary_revisions
    ADD CONSTRAINT itinerary_revisions_city_nonblank_check CHECK (length(btrim(city)) > 0);
ALTER TABLE trip_brief_revisions
    ADD CONSTRAINT trip_brief_revisions_city_nonblank_check CHECK (length(btrim(city)) > 0),
    ADD CONSTRAINT trip_brief_revisions_traveler_positive_check CHECK (traveler_count > 0);
ALTER TABLE suggestion_sets
    ADD CONSTRAINT suggestion_sets_day_index_nonnegative_check CHECK (day_index >= 0);

ALTER TABLE trip_brief_field_sources
    DROP CONSTRAINT IF EXISTS trip_brief_field_sources_origin_check;
ALTER TABLE trip_brief_field_sources
    ADD CONSTRAINT trip_brief_field_sources_origin_check CHECK (origin IN (
        'USER_TEXT', 'PARSER', 'USER_CONFIRMED', 'INFERRED',
        'DEFAULT_NO_PREFERENCE', 'UNSPECIFIED'
    ));

ALTER TABLE itinerary_imports
    DROP CONSTRAINT IF EXISTS itinerary_imports_source_type_check;
ALTER TABLE itinerary_imports
    ADD CONSTRAINT itinerary_imports_source_type_check
    CHECK (source_type IN ('AI_TEXT', 'MANUAL_TEXT', 'SCREENSHOT_OCR'));

CREATE OR REPLACE FUNCTION reject_trip_intake_fact_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'trip intake facts and receipts are immutable';
END;
$$;

DROP TRIGGER IF EXISTS trip_intake_revisions_no_update ON trip_intake_revisions;
CREATE TRIGGER trip_intake_revisions_no_update
    BEFORE UPDATE ON trip_intake_revisions
    FOR EACH ROW EXECUTE FUNCTION reject_trip_intake_fact_update();

DROP TRIGGER IF EXISTS trip_intake_field_sources_no_update ON trip_intake_field_sources;
CREATE TRIGGER trip_intake_field_sources_no_update
    BEFORE UPDATE ON trip_intake_field_sources
    FOR EACH ROW EXECUTE FUNCTION reject_trip_intake_fact_update();

DROP TRIGGER IF EXISTS trip_intake_commands_no_update ON trip_intake_commands;
CREATE TRIGGER trip_intake_commands_no_update
    BEFORE UPDATE ON trip_intake_commands
    FOR EACH ROW EXECUTE FUNCTION reject_trip_intake_fact_update();

DROP TRIGGER IF EXISTS trip_intake_materializations_no_update ON trip_intake_materializations;
CREATE TRIGGER trip_intake_materializations_no_update
    BEFORE UPDATE ON trip_intake_materializations
    FOR EACH ROW EXECUTE FUNCTION reject_trip_intake_fact_update();
