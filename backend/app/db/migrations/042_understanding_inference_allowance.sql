-- One allowance per source, shared across retries and later supplements.
-- 1 structure request + up to 14 days x 2 replies + 2 explicit supplements.
ALTER TABLE trip_understanding_sources
    ADD COLUMN IF NOT EXISTS inference_calls_remaining INTEGER NOT NULL DEFAULT 31
        CHECK (inference_calls_remaining >= 0),
    ADD COLUMN IF NOT EXISTS inference_deadline_at TIMESTAMPTZ;

-- NULL means no model request has been dispatched yet, including old records.
-- The first reservation starts a ten-minute deadline; later jobs cannot reset it.
