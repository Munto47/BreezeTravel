-- Explicit refresh keeps older immutable snapshots and selections intact.
ALTER TABLE trip_stay_recommendation_jobs ADD COLUMN refresh_generation INT NOT NULL DEFAULT 0 CHECK (refresh_generation >= 0);
ALTER TABLE trip_stay_recommendation_jobs DROP CONSTRAINT trip_stay_recommendation_jobs_plan_ref_id_policy_hash_key;
ALTER TABLE trip_stay_recommendation_jobs ADD UNIQUE (plan_ref_id, policy_hash, refresh_generation);
