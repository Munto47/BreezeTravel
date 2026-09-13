-- Explicit manual refresh creates a new job/snapshot for the same trip version.
-- Old rows remain generation zero; no snapshot or business version is rewritten.
-- Older writers using ON CONFLICT(plan_ref_id, route_config_hash) are incompatible.
ALTER TABLE trip_map_render_jobs
    ADD COLUMN IF NOT EXISTS render_generation INT NOT NULL DEFAULT 0
        CHECK (render_generation >= 0);

ALTER TABLE trip_map_render_jobs
    DROP CONSTRAINT IF EXISTS trip_map_render_jobs_plan_ref_id_route_config_hash_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_trip_map_render_jobs_generation
    ON trip_map_render_jobs(plan_ref_id, route_config_hash, render_generation);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trip_map_render_jobs_active
    ON trip_map_render_jobs(plan_ref_id, route_config_hash)
    WHERE status IN ('QUEUED', 'BUILDING');
