-- Reuse understanding jobs; supplement rounds share the retained source budget.
ALTER TABLE trip_understanding_sources
    ADD COLUMN IF NOT EXISTS supplement_requests INTEGER NOT NULL DEFAULT 0
        CHECK (supplement_requests BETWEEN 0 AND 2);
ALTER TABLE trip_understanding_jobs
    DROP CONSTRAINT IF EXISTS trip_understanding_jobs_job_type_check;
ALTER TABLE trip_understanding_jobs ADD CONSTRAINT trip_understanding_jobs_job_type_check
    CHECK (job_type IN ('UNDERSTAND', 'SUPPLEMENT'));
ALTER TABLE trip_understanding_jobs
    ADD COLUMN IF NOT EXISTS supplement_round INTEGER NOT NULL DEFAULT 0 CHECK (supplement_round BETWEEN 0 AND 2),
    ADD COLUMN IF NOT EXISTS base_public_resource_id TEXT,
    ADD COLUMN IF NOT EXISTS base_etag TEXT,
    ADD COLUMN IF NOT EXISTS base_owner_user_id TEXT,
    ADD COLUMN IF NOT EXISTS base_anonymous_session_id TEXT,
    ADD COLUMN IF NOT EXISTS supplement_dispatched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS supplement_outcome_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE trip_understanding_jobs
    DROP CONSTRAINT IF EXISTS trip_understanding_jobs_understanding_id_revision_job_type_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_understanding_job_round
    ON trip_understanding_jobs(understanding_id, revision, job_type, supplement_round);
ALTER TABLE trip_understanding_jobs DROP CONSTRAINT IF EXISTS supplement_job_base;
ALTER TABLE trip_understanding_jobs ADD CONSTRAINT supplement_job_base CHECK (
    (job_type = 'UNDERSTAND' AND supplement_round = 0) OR
    (job_type = 'SUPPLEMENT' AND supplement_round IN (1,2) AND base_public_resource_id IS NOT NULL
     AND base_etag IS NOT NULL AND ((base_owner_user_id IS NULL) <> (base_anonymous_session_id IS NULL)))
);
