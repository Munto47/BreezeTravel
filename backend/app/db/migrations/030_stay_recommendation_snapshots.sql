-- Revision-bound whole-trip stay recommendations and selections for v3.
-- Additive only: no legacy stay or route tables are changed.

ALTER TABLE trip_plan_revision_refs
    ADD CONSTRAINT trip_plan_ref_owner_unique
    UNIQUE (plan_ref_id, understanding_id);

CREATE TABLE IF NOT EXISTS trip_stay_recommendation_jobs (
    stay_job_id TEXT PRIMARY KEY,
    plan_ref_id TEXT NOT NULL REFERENCES trip_plan_revision_refs(plan_ref_id) ON DELETE CASCADE,
    understanding_id TEXT NOT NULL REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    policy_hash CHAR(64) NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    logical_key_hash CHAR(64) NOT NULL UNIQUE CHECK (logical_key_hash ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK (status IN ('QUEUED', 'BUILDING', 'READY', 'PARTIAL', 'UNAVAILABLE')),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    attempt INT NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INT NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    last_error_category TEXT,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (plan_ref_id, policy_hash),
    UNIQUE (stay_job_id, plan_ref_id),
    FOREIGN KEY (plan_ref_id, understanding_id)
        REFERENCES trip_plan_revision_refs(plan_ref_id, understanding_id),
    CHECK ((lease_owner IS NULL) = (lease_until IS NULL)),
    CHECK (status <> 'BUILDING' OR lease_owner IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_trip_stay_jobs_claim
    ON trip_stay_recommendation_jobs(status, available_at, lease_until, created_at);
CREATE INDEX IF NOT EXISTS idx_trip_stay_jobs_understanding
    ON trip_stay_recommendation_jobs(understanding_id, created_at DESC);

CREATE TABLE IF NOT EXISTS trip_stay_recommendation_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    stay_job_id TEXT NOT NULL UNIQUE REFERENCES trip_stay_recommendation_jobs(stay_job_id) ON DELETE CASCADE,
    plan_ref_id TEXT NOT NULL REFERENCES trip_plan_revision_refs(plan_ref_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('READY', 'PARTIAL', 'UNAVAILABLE')),
    policy_hash CHAR(64) NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    area_summary TEXT NOT NULL,
    searched_scopes_json JSONB NOT NULL CHECK (jsonb_typeof(searched_scopes_json) = 'array'),
    candidate_count INT NOT NULL CHECK (candidate_count >= 0 AND candidate_count <= 12),
    snapshot_sha256 CHAR(64) NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    provider_binding_json JSONB NOT NULL CHECK (jsonb_typeof(provider_binding_json) = 'object'),
    failure_json JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(failure_json) = 'object'),
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (snapshot_id, plan_ref_id),
    FOREIGN KEY (stay_job_id, plan_ref_id)
        REFERENCES trip_stay_recommendation_jobs(stay_job_id, plan_ref_id),
    CHECK (finished_at >= started_at),
    CHECK ((status = 'UNAVAILABLE') = (candidate_count = 0))
);

CREATE TABLE IF NOT EXISTS trip_stay_candidates (
    candidate_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES trip_stay_recommendation_snapshots(snapshot_id) ON DELETE CASCADE,
    public_candidate_token TEXT NOT NULL UNIQUE CHECK (length(public_candidate_token) BETWEEN 20 AND 100),
    rank INT NOT NULL CHECK (rank BETWEEN 1 AND 12),
    canonical_place_id TEXT NOT NULL,
    name TEXT NOT NULL,
    brand TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category = '住宿'),
    area_or_address TEXT NOT NULL,
    city TEXT NOT NULL,
    longitude DOUBLE PRECISION NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    latitude DOUBLE PRECISION NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    search_radius_m INT CHECK (search_radius_m IS NULL OR search_radius_m IN (2000, 4000, 8000)),
    total_score DOUBLE PRECISION NOT NULL CHECK (total_score >= 0),
    max_single_leg_minutes INT NOT NULL CHECK (max_single_leg_minutes >= 0),
    transfer_count INT NOT NULL CHECK (transfer_count >= 0),
    missing_leg_count INT NOT NULL CHECK (missing_leg_count >= 0),
    evidence_penalty INT NOT NULL CHECK (evidence_penalty BETWEEN 0 AND 240),
    provider_binding_json JSONB NOT NULL CHECK (jsonb_typeof(provider_binding_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (snapshot_id, rank),
    UNIQUE (snapshot_id, canonical_place_id),
    UNIQUE (snapshot_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS trip_stay_commute_legs (
    leg_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES trip_stay_candidates(candidate_id) ON DELETE CASCADE,
    day_index INT NOT NULL CHECK (day_index BETWEEN 1 AND 14),
    direction TEXT NOT NULL CHECK (direction IN ('STAY_TO_FIRST', 'LAST_TO_STAY')),
    endpoint_name TEXT NOT NULL,
    selected_mode TEXT CHECK (selected_mode IN ('walking', 'transit')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (candidate_id, day_index, direction)
);

CREATE TABLE IF NOT EXISTS trip_stay_commute_mode_facts (
    leg_id TEXT NOT NULL REFERENCES trip_stay_commute_legs(leg_id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK (mode IN ('walking', 'transit')),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'UNAVAILABLE')),
    duration_minutes INT CHECK (duration_minutes IS NULL OR duration_minutes > 0),
    distance_meters INT CHECK (distance_meters IS NULL OR distance_meters > 0),
    transfer_count INT CHECK (transfer_count IS NULL OR transfer_count >= 0),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    response_hash CHAR(64) NOT NULL CHECK (response_hash ~ '^[0-9a-f]{64}$'),
    provider_receipt_json JSONB NOT NULL CHECK (jsonb_typeof(provider_receipt_json) = 'object'),
    external_call_count INT NOT NULL CHECK (external_call_count >= 0),
    observed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (leg_id, mode),
    CHECK (expires_at > observed_at),
    CHECK (
        (status = 'AVAILABLE' AND duration_minutes IS NOT NULL AND distance_meters IS NOT NULL)
        OR (status = 'UNAVAILABLE' AND duration_minutes IS NULL AND distance_meters IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS trip_stay_selections (
    selection_id TEXT PRIMARY KEY,
    understanding_id TEXT NOT NULL REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    source_snapshot_id TEXT NOT NULL REFERENCES trip_stay_recommendation_snapshots(snapshot_id),
    source_plan_ref_id TEXT NOT NULL REFERENCES trip_plan_revision_refs(plan_ref_id),
    target_plan_ref_id TEXT NOT NULL UNIQUE REFERENCES trip_plan_revision_refs(plan_ref_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES trip_stay_candidates(candidate_id),
    selected_place_id TEXT NOT NULL,
    selected_name TEXT NOT NULL,
    selected_brand TEXT NOT NULL,
    selected_address TEXT NOT NULL,
    selected_city TEXT NOT NULL,
    longitude DOUBLE PRECISION NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    latitude DOUBLE PRECISION NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    overnight_days INT[] NOT NULL,
    selection_request_hash CHAR(64) NOT NULL CHECK (selection_request_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (cardinality(overnight_days) > 0),
    FOREIGN KEY (source_snapshot_id, source_plan_ref_id)
        REFERENCES trip_stay_recommendation_snapshots(snapshot_id, plan_ref_id),
    FOREIGN KEY (source_snapshot_id, candidate_id)
        REFERENCES trip_stay_candidates(snapshot_id, candidate_id),
    FOREIGN KEY (target_plan_ref_id, understanding_id)
        REFERENCES trip_plan_revision_refs(plan_ref_id, understanding_id)
);

DROP TRIGGER IF EXISTS trip_stay_snapshots_no_update ON trip_stay_recommendation_snapshots;
CREATE TRIGGER trip_stay_snapshots_no_update
    BEFORE UPDATE ON trip_stay_recommendation_snapshots
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_stay_candidates_no_update ON trip_stay_candidates;
CREATE TRIGGER trip_stay_candidates_no_update
    BEFORE UPDATE ON trip_stay_candidates
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_stay_legs_no_update ON trip_stay_commute_legs;
CREATE TRIGGER trip_stay_legs_no_update
    BEFORE UPDATE ON trip_stay_commute_legs
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_stay_mode_facts_no_update ON trip_stay_commute_mode_facts;
CREATE TRIGGER trip_stay_mode_facts_no_update
    BEFORE UPDATE ON trip_stay_commute_mode_facts
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_stay_selections_no_update ON trip_stay_selections;
CREATE TRIGGER trip_stay_selections_no_update
    BEFORE UPDATE ON trip_stay_selections
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();
