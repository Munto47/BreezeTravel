-- Aggregate operational measurements only; no raw source, secrets or query payloads.
ALTER TABLE trip_daily_dining_jobs ADD COLUMN metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb;
