-- G03: bridge the public trip-understanding aggregate to immutable itinerary,
-- evidence, audit and change-preview records.  This stays inside PostgreSQL
-- and does not create a second runtime or a public room entry point.

INSERT INTO users (user_id, nickname)
VALUES ('__breezetravel_materializer__', '系统整理')
ON CONFLICT (user_id) DO NOTHING;

ALTER TABLE trip_workspaces
    ALTER COLUMN trip_start_date DROP NOT NULL,
    ALTER COLUMN trip_end_date DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS calendar_mode TEXT NOT NULL DEFAULT 'ABSOLUTE_DATES'
        CHECK (calendar_mode IN ('ABSOLUTE_DATES', 'DAY_INDEX_ONLY')),
    ADD COLUMN IF NOT EXISTS party_size INT NOT NULL DEFAULT 2
        CHECK (party_size BETWEEN 1 AND 50),
    ADD COLUMN IF NOT EXISTS party_size_source TEXT NOT NULL DEFAULT 'DEFAULT_TWO'
        CHECK (party_size_source IN ('USER_PROVIDED', 'DEFAULT_TWO')),
    ADD COLUMN IF NOT EXISTS current_plan_ref_id TEXT
        REFERENCES trip_plan_revision_refs(plan_ref_id);

ALTER TABLE trip_workspaces
    DROP CONSTRAINT IF EXISTS trip_workspaces_city_nonblank;
ALTER TABLE trip_workspaces
    ADD CONSTRAINT trip_workspaces_city_nonblank
    CHECK (length(btrim(city)) BETWEEN 1 AND 80);
ALTER TABLE trip_workspaces
    DROP CONSTRAINT IF EXISTS trip_workspaces_calendar_mode_dates;
ALTER TABLE trip_workspaces
    ADD CONSTRAINT trip_workspaces_calendar_mode_dates
    CHECK (
        (
            calendar_mode = 'ABSOLUTE_DATES'
            AND trip_start_date IS NOT NULL
            AND trip_end_date IS NOT NULL
            AND trip_end_date >= trip_start_date
        )
        OR (
            calendar_mode = 'DAY_INDEX_ONLY'
            AND trip_start_date IS NULL
            AND trip_end_date IS NULL
        )
    );

ALTER TABLE itinerary_revisions
    ALTER COLUMN trip_start_date DROP NOT NULL,
    ALTER COLUMN trip_end_date DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS calendar_mode TEXT NOT NULL DEFAULT 'ABSOLUTE_DATES'
        CHECK (calendar_mode IN ('ABSOLUTE_DATES', 'DAY_INDEX_ONLY')),
    ADD COLUMN IF NOT EXISTS party_size INT NOT NULL DEFAULT 2
        CHECK (party_size BETWEEN 1 AND 50),
    ADD COLUMN IF NOT EXISTS party_size_source TEXT NOT NULL DEFAULT 'DEFAULT_TWO'
        CHECK (party_size_source IN ('USER_PROVIDED', 'DEFAULT_TWO'));

ALTER TABLE itinerary_revisions
    DROP CONSTRAINT IF EXISTS itinerary_revisions_city_nonblank;
ALTER TABLE itinerary_revisions
    ADD CONSTRAINT itinerary_revisions_city_nonblank
    CHECK (length(btrim(city)) BETWEEN 1 AND 80);
ALTER TABLE itinerary_revisions
    DROP CONSTRAINT IF EXISTS itinerary_revisions_calendar_mode_dates;
ALTER TABLE itinerary_revisions
    ADD CONSTRAINT itinerary_revisions_calendar_mode_dates
    CHECK (
        (
            calendar_mode = 'ABSOLUTE_DATES'
            AND trip_start_date IS NOT NULL
            AND trip_end_date IS NOT NULL
            AND trip_end_date >= trip_start_date
        )
        OR (
            calendar_mode = 'DAY_INDEX_ONLY'
            AND trip_start_date IS NULL
            AND trip_end_date IS NULL
        )
    );

CREATE TABLE IF NOT EXISTS trip_materialized_trips (
    understanding_id TEXT PRIMARY KEY
        REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL UNIQUE
        REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    itinerary_id TEXT NOT NULL UNIQUE,
    current_understanding_revision INT NOT NULL CHECK (current_understanding_revision > 0),
    current_itinerary_revision INT NOT NULL CHECK (current_itinerary_revision > 0),
    current_plan_ref_id TEXT NOT NULL
        REFERENCES trip_plan_revision_refs(plan_ref_id),
    calendar_mode TEXT NOT NULL
        CHECK (calendar_mode IN ('ABSOLUTE_DATES', 'DAY_INDEX_ONLY')),
    party_size INT NOT NULL CHECK (party_size BETWEEN 1 AND 50),
    party_size_source TEXT NOT NULL
        CHECK (party_size_source IN ('USER_PROVIDED', 'DEFAULT_TWO')),
    public_projection_json JSONB NOT NULL
        CHECK (jsonb_typeof(public_projection_json) = 'object'),
    current_opaque_etag TEXT NOT NULL CHECK (length(current_opaque_etag) BETWEEN 20 AND 120),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (understanding_id, current_understanding_revision)
        REFERENCES trip_understanding_revisions(understanding_id, revision),
    FOREIGN KEY (workspace_id, current_itinerary_revision)
        REFERENCES itinerary_revisions(workspace_id, revision)
);

CREATE TABLE IF NOT EXISTS trip_materialization_lineage (
    lineage_id TEXT PRIMARY KEY,
    understanding_id TEXT NOT NULL
        REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    understanding_revision INT NOT NULL CHECK (understanding_revision > 0),
    workspace_id TEXT NOT NULL
        REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    itinerary_id TEXT NOT NULL,
    itinerary_revision INT NOT NULL CHECK (itinerary_revision > 0),
    plan_ref_id TEXT NOT NULL REFERENCES trip_plan_revision_refs(plan_ref_id),
    evidence_snapshot_id TEXT NOT NULL
        REFERENCES evidence_snapshots(snapshot_id),
    audit_report_id TEXT NOT NULL REFERENCES audit_reports(report_id),
    calendar_mode TEXT NOT NULL
        CHECK (calendar_mode IN ('ABSOLUTE_DATES', 'DAY_INDEX_ONLY')),
    party_size INT NOT NULL CHECK (party_size BETWEEN 1 AND 50),
    party_size_source TEXT NOT NULL
        CHECK (party_size_source IN ('USER_PROVIDED', 'DEFAULT_TWO')),
    stay_anchor_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(stay_anchor_json) = 'object'),
    public_projection_json JSONB NOT NULL
        CHECK (jsonb_typeof(public_projection_json) = 'object'),
    source_result_sha256 CHAR(64) NOT NULL
        CHECK (source_result_sha256 ~ '^[0-9a-f]{64}$'),
    postcheck_complete BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (understanding_id, understanding_revision),
    UNIQUE (workspace_id, itinerary_revision),
    FOREIGN KEY (understanding_id, understanding_revision)
        REFERENCES trip_understanding_revisions(understanding_id, revision),
    FOREIGN KEY (itinerary_id, itinerary_revision)
        REFERENCES itinerary_revisions(itinerary_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_trip_materialization_lineage_workspace
    ON trip_materialization_lineage(workspace_id, itinerary_revision DESC);

CREATE TABLE IF NOT EXISTS trip_g03_evidence_bindings (
    snapshot_id TEXT PRIMARY KEY REFERENCES evidence_snapshots(snapshot_id) ON DELETE CASCADE,
    candidate_set_sha256 CHAR(64) NOT NULL
        CHECK (candidate_set_sha256 ~ '^[0-9a-f]{64}$'),
    receipt_set_sha256 CHAR(64) NOT NULL
        CHECK (receipt_set_sha256 ~ '^[0-9a-f]{64}$'),
    receipt_binding_complete BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trip_public_check_tokens (
    check_token TEXT PRIMARY KEY CHECK (length(check_token) BETWEEN 20 AND 100),
    report_id TEXT NOT NULL REFERENCES audit_reports(report_id) ON DELETE CASCADE,
    finding_id TEXT NOT NULL UNIQUE REFERENCES audit_findings(finding_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (report_id, check_token)
);

CREATE TABLE IF NOT EXISTS trip_change_previews (
    preview_id TEXT PRIMARY KEY,
    change_token TEXT NOT NULL UNIQUE CHECK (length(change_token) BETWEEN 20 AND 100),
    understanding_id TEXT NOT NULL
        REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    base_understanding_revision INT NOT NULL CHECK (base_understanding_revision > 0),
    base_itinerary_revision INT NOT NULL CHECK (base_itinerary_revision > 0),
    source_report_id TEXT NOT NULL REFERENCES audit_reports(report_id) ON DELETE CASCADE,
    targeted_finding_id TEXT NOT NULL REFERENCES audit_findings(finding_id) ON DELETE CASCADE,
    command_json JSONB NOT NULL CHECK (jsonb_typeof(command_json) = 'object'),
    public_preview_json JSONB NOT NULL CHECK (jsonb_typeof(public_preview_json) = 'object'),
    status TEXT NOT NULL DEFAULT 'PROPOSED'
        CHECK (status IN ('PROPOSED', 'APPLIED', 'STALE')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_at TIMESTAMPTZ,
    FOREIGN KEY (understanding_id, base_understanding_revision)
        REFERENCES trip_understanding_revisions(understanding_id, revision),
    CHECK ((status = 'APPLIED') = (applied_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_trip_change_previews_current
    ON trip_change_previews(understanding_id, base_understanding_revision, status);

DROP TRIGGER IF EXISTS trip_materialization_lineage_no_update
    ON trip_materialization_lineage;
CREATE TRIGGER trip_materialization_lineage_no_update
    BEFORE UPDATE ON trip_materialization_lineage
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_g03_evidence_bindings_no_update
    ON trip_g03_evidence_bindings;
CREATE TRIGGER trip_g03_evidence_bindings_no_update
    BEFORE UPDATE ON trip_g03_evidence_bindings
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_public_check_tokens_no_update
    ON trip_public_check_tokens;
CREATE TRIGGER trip_public_check_tokens_no_update
    BEFORE UPDATE ON trip_public_check_tokens
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();
