ALTER TABLE trip_understanding_sources
    ADD COLUMN IF NOT EXISTS execution_config_json JSONB
        CHECK (execution_config_json IS NULL OR jsonb_typeof(execution_config_json) = 'object');

ALTER TABLE trip_understanding_jobs
    ADD COLUMN IF NOT EXISTS inference_dispatched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS model_calls_json JSONB NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(model_calls_json) = 'object');

-- Existing attempts with an consumed allowance may have reached the provider.
UPDATE trip_understanding_jobs j SET inference_dispatched_at = COALESCE(j.started_at, j.created_at)
FROM trip_understanding_revisions r, trip_understanding_sources s
WHERE r.understanding_id=j.understanding_id AND r.revision=j.revision
  AND s.source_id=r.source_id AND s.inference_calls_remaining<31
  AND j.inference_dispatched_at IS NULL;
