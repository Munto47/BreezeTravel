-- Revision-bound map jobs and immutable walking/transit snapshots for v3.
-- Additive only: legacy route tables and existing trip data remain untouched.

CREATE TABLE IF NOT EXISTS trip_plan_revision_refs (
    plan_ref_id TEXT PRIMARY KEY,
    understanding_id TEXT NOT NULL
        REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    revision_kind TEXT NOT NULL CHECK (revision_kind IN ('UNDERSTANDING', 'ITINERARY')),
    aggregate_id TEXT NOT NULL,
    revision INT NOT NULL CHECK (revision > 0),
    stop_set_hash CHAR(64) NOT NULL CHECK (stop_set_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (understanding_id, revision_kind, aggregate_id, revision),
    UNIQUE (understanding_id, revision_kind, aggregate_id, revision, stop_set_hash)
);

CREATE TABLE IF NOT EXISTS trip_map_render_jobs (
    map_job_id TEXT PRIMARY KEY,
    plan_ref_id TEXT NOT NULL REFERENCES trip_plan_revision_refs(plan_ref_id) ON DELETE CASCADE,
    understanding_id TEXT NOT NULL
        REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    route_config_hash CHAR(64) NOT NULL CHECK (route_config_hash ~ '^[0-9a-f]{64}$'),
    logical_key_hash CHAR(64) NOT NULL UNIQUE CHECK (logical_key_hash ~ '^[0-9a-f]{64}$'),
    request_origin TEXT NOT NULL CHECK (request_origin IN ('INITIAL', 'MANUAL')),
    status TEXT NOT NULL CHECK (status IN (
        'QUEUED', 'BUILDING', 'READY', 'PARTIAL', 'UNAVAILABLE'
    )),
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
    UNIQUE (plan_ref_id, route_config_hash),
    CHECK ((lease_owner IS NULL) = (lease_until IS NULL)),
    CHECK (status <> 'BUILDING' OR lease_owner IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_trip_map_render_jobs_claim
    ON trip_map_render_jobs(status, available_at, lease_until, created_at);
CREATE INDEX IF NOT EXISTS idx_trip_map_render_jobs_understanding
    ON trip_map_render_jobs(understanding_id, created_at DESC);

CREATE TABLE IF NOT EXISTS trip_map_render_events (
    map_job_id TEXT NOT NULL REFERENCES trip_map_render_jobs(map_job_id) ON DELETE CASCADE,
    event_id BIGINT GENERATED ALWAYS AS IDENTITY,
    event_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'QUEUED', 'BUILDING', 'READY', 'PARTIAL', 'UNAVAILABLE'
    )),
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (map_job_id, event_id),
    UNIQUE (map_job_id, event_key)
);

CREATE TABLE IF NOT EXISTS trip_map_render_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    map_job_id TEXT NOT NULL UNIQUE
        REFERENCES trip_map_render_jobs(map_job_id) ON DELETE CASCADE,
    plan_ref_id TEXT NOT NULL REFERENCES trip_plan_revision_refs(plan_ref_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('READY', 'PARTIAL', 'UNAVAILABLE')),
    stop_count INT NOT NULL CHECK (stop_count >= 0),
    edge_count INT NOT NULL CHECK (edge_count >= 0),
    available_edge_count INT NOT NULL CHECK (
        available_edge_count >= 0 AND available_edge_count <= edge_count
    ),
    snapshot_sha256 CHAR(64) NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    provider_binding_json JSONB NOT NULL CHECK (jsonb_typeof(provider_binding_json) = 'object'),
    failure_json JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(failure_json) = 'object'),
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (finished_at >= started_at),
    CHECK (expires_at > observed_at),
    CHECK (
        (status = 'READY' AND edge_count > 0 AND available_edge_count = edge_count)
        OR (status = 'PARTIAL' AND available_edge_count > 0 AND available_edge_count < edge_count)
        OR (status = 'UNAVAILABLE' AND available_edge_count = 0)
    )
);

CREATE INDEX IF NOT EXISTS idx_trip_map_render_snapshots_plan
    ON trip_map_render_snapshots(plan_ref_id, finished_at DESC);

CREATE TABLE IF NOT EXISTS trip_map_route_edges (
    edge_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL
        REFERENCES trip_map_render_snapshots(snapshot_id) ON DELETE CASCADE,
    day_index INT NOT NULL CHECK (day_index BETWEEN 1 AND 14),
    sequence_index INT NOT NULL CHECK (sequence_index >= 0),
    origin_name TEXT NOT NULL,
    destination_name TEXT NOT NULL,
    selected_mode TEXT CHECK (selected_mode IN ('walking', 'transit')),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'UNAVAILABLE')),
    unavailable_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (snapshot_id, day_index, sequence_index),
    CHECK (
        (status = 'AVAILABLE' AND selected_mode IS NOT NULL AND unavailable_reason IS NULL)
        OR (status = 'UNAVAILABLE' AND selected_mode IS NULL AND unavailable_reason IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS trip_map_route_mode_facts (
    edge_id TEXT NOT NULL REFERENCES trip_map_route_edges(edge_id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK (mode IN ('walking', 'transit')),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'UNAVAILABLE')),
    duration_minutes INT CHECK (duration_minutes IS NULL OR duration_minutes > 0),
    distance_meters INT CHECK (distance_meters IS NULL OR distance_meters > 0),
    transfer_count INT CHECK (transfer_count IS NULL OR transfer_count >= 0),
    response_hash CHAR(64) NOT NULL CHECK (response_hash ~ '^[0-9a-f]{64}$'),
    geometry_ref TEXT,
    provider_receipt_json JSONB NOT NULL CHECK (jsonb_typeof(provider_receipt_json) = 'object'),
    observed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (edge_id, mode),
    CHECK (expires_at > observed_at),
    CHECK (
        (status = 'AVAILABLE' AND duration_minutes IS NOT NULL AND distance_meters IS NOT NULL)
        OR (status = 'UNAVAILABLE' AND duration_minutes IS NULL AND distance_meters IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS trip_map_provider_effect_receipts (
    receipt_id TEXT PRIMARY KEY,
    map_job_id TEXT NOT NULL REFERENCES trip_map_render_jobs(map_job_id) ON DELETE CASCADE,
    effect_key TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL CHECK (mode IN ('walking', 'transit')),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    response_hash CHAR(64) NOT NULL CHECK (response_hash ~ '^[0-9a-f]{64}$'),
    provider_binding_json JSONB NOT NULL CHECK (jsonb_typeof(provider_binding_json) = 'object'),
    external_call_count INT NOT NULL CHECK (external_call_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trip_plan_revision_refs_no_update ON trip_plan_revision_refs;
CREATE TRIGGER trip_plan_revision_refs_no_update
    BEFORE UPDATE ON trip_plan_revision_refs
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_map_render_events_no_update ON trip_map_render_events;
CREATE TRIGGER trip_map_render_events_no_update
    BEFORE UPDATE ON trip_map_render_events
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_map_render_snapshots_no_update ON trip_map_render_snapshots;
CREATE TRIGGER trip_map_render_snapshots_no_update
    BEFORE UPDATE ON trip_map_render_snapshots
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_map_route_edges_no_update ON trip_map_route_edges;
CREATE TRIGGER trip_map_route_edges_no_update
    BEFORE UPDATE ON trip_map_route_edges
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_map_route_mode_facts_no_update ON trip_map_route_mode_facts;
CREATE TRIGGER trip_map_route_mode_facts_no_update
    BEFORE UPDATE ON trip_map_route_mode_facts
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();

DROP TRIGGER IF EXISTS trip_map_provider_effect_receipts_no_update
    ON trip_map_provider_effect_receipts;
CREATE TRIGGER trip_map_provider_effect_receipts_no_update
    BEFORE UPDATE ON trip_map_provider_effect_receipts
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();
