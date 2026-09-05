-- Add explicit cancellation terminals without changing existing v3 URLs or facts.

ALTER TABLE trip_understandings
    DROP CONSTRAINT IF EXISTS trip_understandings_state_check;
ALTER TABLE trip_understandings
    ADD CONSTRAINT trip_understandings_state_check CHECK (state IN (
        'PROCESSING', 'READY', 'PARTIAL', 'CANCELLED', 'FAILED', 'DELETED'
    ));

ALTER TABLE trip_understanding_revisions
    DROP CONSTRAINT IF EXISTS trip_understanding_revisions_status_check;
ALTER TABLE trip_understanding_revisions
    ADD CONSTRAINT trip_understanding_revisions_status_check CHECK (status IN (
        'DRAFT', 'PROCESSING', 'PARTIAL', 'READY', 'CANCELLED', 'FAILED'
    ));

ALTER TABLE trip_understanding_jobs
    DROP CONSTRAINT IF EXISTS trip_understanding_jobs_status_check;
ALTER TABLE trip_understanding_jobs
    ADD CONSTRAINT trip_understanding_jobs_status_check CHECK (status IN (
        'QUEUED', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'CANCELLED', 'FAILED',
        'PRIVACY_BLOCKED'
    ));

-- Progressive public snapshots stay strictly redacted. Provider accounting
-- needed for cancellation audit is stored in a separate, non-public column.
ALTER TABLE trip_understanding_events
    ADD COLUMN IF NOT EXISTS internal_binding_json JSONB NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(internal_binding_json) = 'object');
