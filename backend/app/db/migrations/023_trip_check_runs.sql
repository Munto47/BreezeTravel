-- Trip Check V1: durable run state, stage attempts, events and side-effect receipts.

CREATE TABLE IF NOT EXISTS trip_check_runs (
    run_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    itinerary_revision INT NOT NULL CHECK (itinerary_revision > 0),
    brief_id TEXT NOT NULL,
    brief_revision INT NOT NULL CHECK (brief_revision > 0),
    stage TEXT NOT NULL CHECK (stage IN (
        'PARSE', 'WAIT_BRIEF_CONFIRMATION', 'RESOLVE_PLACES', 'COLLECT_EVIDENCE',
        'AUDIT', 'BUILD_ADVICE', 'WAIT_ADOPTION', 'POSTCHECK'
    )),
    stage_attempt INT NOT NULL DEFAULT 1 CHECK (stage_attempt > 0),
    status TEXT NOT NULL CHECK (status IN (
        'WAITING', 'RUNNING', 'PARTIAL', 'SUCCEEDED', 'FAILED',
        'PRIVACY_BLOCKED', 'CANCELLED'
    )),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    run_spec_json JSONB NOT NULL,
    config_hash CHAR(64) NOT NULL CHECK (config_hash ~ '^[0-9a-f]{64}$'),
    completed_stages_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    partial_failures_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_snapshot_id TEXT REFERENCES evidence_snapshots(snapshot_id),
    report_id TEXT REFERENCES audit_reports(report_id),
    advice_bundle_id TEXT,
    version INT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, run_id),
    FOREIGN KEY (workspace_id, itinerary_revision)
        REFERENCES itinerary_revisions(workspace_id, revision),
    FOREIGN KEY (brief_id, brief_revision)
        REFERENCES trip_brief_revisions(brief_id, revision),
    CHECK ((lease_owner IS NULL) = (lease_until IS NULL)),
    CHECK (jsonb_typeof(run_spec_json) = 'object'),
    CHECK (jsonb_typeof(completed_stages_json) = 'array'),
    CHECK (jsonb_typeof(partial_failures_json) = 'array'),
    CHECK (status <> 'PARTIAL' OR jsonb_array_length(partial_failures_json) > 0)
);

CREATE INDEX IF NOT EXISTS idx_trip_check_runs_workspace
    ON trip_check_runs(workspace_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_trip_check_runs_lease
    ON trip_check_runs(status, lease_until)
    WHERE status IN ('WAITING', 'RUNNING', 'PARTIAL');

CREATE TABLE IF NOT EXISTS trip_check_stage_attempts (
    run_id TEXT NOT NULL REFERENCES trip_check_runs(run_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    attempt INT NOT NULL CHECK (attempt > 0),
    state TEXT NOT NULL CHECK (state IN ('STARTED', 'SUCCEEDED', 'FAILED_RETRYABLE', 'FAILED_FINAL')),
    stage_input_hash CHAR(64) NOT NULL CHECK (stage_input_hash ~ '^[0-9a-f]{64}$'),
    stage_output_hash CHAR(64) CHECK (stage_output_hash IS NULL OR stage_output_hash ~ '^[0-9a-f]{64}$'),
    failure_category TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (run_id, stage, attempt),
    CHECK (
        (state = 'STARTED' AND finished_at IS NULL)
        OR (state <> 'STARTED' AND finished_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS trip_check_side_effect_receipts (
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES trip_check_runs(run_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    side_effect_key TEXT NOT NULL,
    effect_type TEXT NOT NULL,
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    response_hash CHAR(64) CHECK (response_hash IS NULL OR response_hash ~ '^[0-9a-f]{64}$'),
    provider TEXT,
    status TEXT NOT NULL CHECK (status IN ('SUCCEEDED', 'PARTIAL', 'FAILED')),
    receipt_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, side_effect_key),
    CHECK (jsonb_typeof(receipt_json) = 'object')
);

CREATE TABLE IF NOT EXISTS trip_check_run_events (
    run_id TEXT NOT NULL REFERENCES trip_check_runs(run_id) ON DELETE CASCADE,
    event_id BIGINT GENERATED ALWAYS AS IDENTITY,
    event_type TEXT NOT NULL,
    stage TEXT NOT NULL,
    run_version INT NOT NULL CHECK (run_version > 0),
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, event_id)
);

CREATE TABLE IF NOT EXISTS trip_check_commands (
    command_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    run_id TEXT REFERENCES trip_check_runs(run_id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK (operation IN ('CREATE_RUN', 'RESUME_RUN', 'CONFIRM_BRIEF', 'PATCH_BRIEF')),
    actor_user_id TEXT NOT NULL REFERENCES users(user_id),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    response_status SMALLINT NOT NULL,
    response_headers_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, idempotency_key)
);

ALTER TABLE trip_workspaces
    ADD COLUMN IF NOT EXISTS current_trip_check_run_id TEXT;

ALTER TABLE trip_workspaces
    ADD CONSTRAINT trip_workspaces_current_trip_check_run_fk
    FOREIGN KEY (current_trip_check_run_id)
    REFERENCES trip_check_runs(run_id);

CREATE OR REPLACE FUNCTION reject_trip_check_receipt_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'trip check receipts and events are immutable';
END;
$$;

DROP TRIGGER IF EXISTS trip_check_side_effect_receipts_no_update ON trip_check_side_effect_receipts;
CREATE TRIGGER trip_check_side_effect_receipts_no_update
    BEFORE UPDATE ON trip_check_side_effect_receipts
    FOR EACH ROW EXECUTE FUNCTION reject_trip_check_receipt_update();

DROP TRIGGER IF EXISTS trip_check_run_events_no_update ON trip_check_run_events;
CREATE TRIGGER trip_check_run_events_no_update
    BEFORE UPDATE ON trip_check_run_events
    FOR EACH ROW EXECUTE FUNCTION reject_trip_check_receipt_update();

DROP TRIGGER IF EXISTS trip_check_commands_no_update ON trip_check_commands;
CREATE TRIGGER trip_check_commands_no_update
    BEFORE UPDATE ON trip_check_commands
    FOR EACH ROW EXECUTE FUNCTION reject_trip_check_receipt_update();

CREATE OR REPLACE FUNCTION preserve_trip_check_run_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.workspace_id <> OLD.workspace_id
       OR NEW.itinerary_revision <> OLD.itinerary_revision
       OR NEW.brief_id <> OLD.brief_id
       OR NEW.brief_revision <> OLD.brief_revision
       OR NEW.run_spec_json <> OLD.run_spec_json
       OR NEW.config_hash <> OLD.config_hash
       OR NEW.created_by <> OLD.created_by
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'trip check run binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trip_check_runs_preserve_binding ON trip_check_runs;
CREATE TRIGGER trip_check_runs_preserve_binding
    BEFORE UPDATE ON trip_check_runs
    FOR EACH ROW EXECUTE FUNCTION preserve_trip_check_run_binding();
