-- Additive segment identity; existing snapshots remain readable as legacy segments.
ALTER TABLE trip_stay_candidates ADD COLUMN segment_key TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE trip_stay_selections ADD COLUMN segment_key TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE trip_stay_candidates DROP CONSTRAINT trip_stay_candidates_snapshot_id_canonical_place_id_key;
ALTER TABLE trip_stay_candidates ADD UNIQUE (snapshot_id, segment_key, canonical_place_id);
ALTER TABLE trip_stay_selections DROP CONSTRAINT trip_stay_selections_target_plan_ref_id_key;
ALTER TABLE trip_stay_selections ADD UNIQUE (target_plan_ref_id, segment_key);
ALTER TABLE trip_stay_candidates DROP CONSTRAINT trip_stay_candidates_rank_check;
ALTER TABLE trip_stay_candidates ADD CHECK (rank BETWEEN 1 AND 84);
ALTER TABLE trip_stay_recommendation_snapshots DROP CONSTRAINT trip_stay_recommendation_snapshots_candidate_count_check;
ALTER TABLE trip_stay_recommendation_snapshots ADD CHECK (candidate_count BETWEEN 0 AND 84);
