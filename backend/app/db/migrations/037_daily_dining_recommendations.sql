-- Revision-bound, private daily recommendations. No source text or public tokens.
CREATE TABLE IF NOT EXISTS trip_daily_dining_jobs (
    understanding_id TEXT NOT NULL REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    revision INT NOT NULL CHECK (revision > 0),
    status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','BUILDING','READY','UNAVAILABLE')),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    attempts INT NOT NULL DEFAULT 0,
    request_key TEXT NOT NULL DEFAULT 'initial',
    payload_json JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(payload_json)='array'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (understanding_id, revision)
);
CREATE INDEX IF NOT EXISTS trip_daily_dining_jobs_queue ON trip_daily_dining_jobs(status, created_at);
