-- Final 2.0 P5: immutable ranked suggestion sets, atomic candidate acceptance,
-- and a replayable recommendation-event ledger.

CREATE TABLE IF NOT EXISTS suggestion_sets (
    suggestion_set_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    base_revision INT NOT NULL CHECK (base_revision > 0),
    day_index INT NOT NULL CHECK (day_index BETWEEN 0 AND 4),
    insert_after_stop_id TEXT,
    insert_before_stop_id TEXT,
    intents_json JSONB NOT NULL,
    context_hash CHAR(64) NOT NULL CHECK (context_hash ~ '^[0-9a-f]{64}$'),
    policy_version TEXT NOT NULL CHECK (length(policy_version) > 0),
    provider_snapshot_id TEXT NOT NULL CHECK (
        length(provider_snapshot_id) > 0
        AND lower(provider_snapshot_id) NOT IN ('live', 'latest', 'current', 'unknown')
    ),
    expires_at TIMESTAMPTZ NOT NULL,
    session_id TEXT NOT NULL CHECK (length(session_id) > 0),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    result_status TEXT NOT NULL DEFAULT 'COMPLETE' CHECK (result_status IN ('COMPLETE', 'PARTIAL')),
    shortage_reason_codes_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    excluded_counts_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (workspace_id, suggestion_set_id),
    FOREIGN KEY (workspace_id, base_revision)
        REFERENCES itinerary_revisions(workspace_id, revision) ON DELETE CASCADE,
    CHECK (jsonb_typeof(intents_json) = 'array' AND jsonb_array_length(intents_json) > 0),
    CHECK (expires_at > created_at),
    CHECK (jsonb_typeof(shortage_reason_codes_json) = 'array'),
    CHECK (jsonb_typeof(excluded_counts_json) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_suggestion_sets_workspace_revision
    ON suggestion_sets(workspace_id, base_revision, created_at DESC);

CREATE TABLE IF NOT EXISTS suggestion_candidates (
    workspace_id TEXT NOT NULL,
    suggestion_set_id TEXT NOT NULL REFERENCES suggestion_sets(suggestion_set_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    canonical_place_id TEXT NOT NULL,
    provider_receipt_id TEXT NOT NULL,
    rank_position INT NOT NULL CHECK (rank_position > 0),
    hard_gate_passed BOOLEAN NOT NULL,
    candidate_json JSONB NOT NULL,
    PRIMARY KEY (suggestion_set_id, candidate_id),
    CONSTRAINT suggestion_candidates_workspace_set_candidate_key
        UNIQUE (workspace_id, suggestion_set_id, candidate_id),
    UNIQUE (suggestion_set_id, rank_position),
    CONSTRAINT suggestion_candidates_workspace_set_fkey
        FOREIGN KEY (workspace_id, suggestion_set_id)
        REFERENCES suggestion_sets(workspace_id, suggestion_set_id) ON DELETE CASCADE,
    CHECK (candidate_json->>'suggestion_set_id' = suggestion_set_id),
    CHECK (candidate_json->>'candidate_id' = candidate_id),
    CHECK (candidate_json->'canonical_place'->>'place_id' = canonical_place_id),
    CHECK (candidate_json->>'provider_receipt_id' = provider_receipt_id),
    CHECK ((candidate_json->'provider_receipt'->>'canonical_place_id') = canonical_place_id),
    CHECK ((candidate_json->'hard_gate'->>'passed')::boolean = hard_gate_passed)
);

CREATE TABLE IF NOT EXISTS recommendation_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL CHECK (length(session_id) > 0),
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL REFERENCES users(user_id),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'suggestions_shown', 'candidate_previewed', 'candidate_accepted',
        'candidate_dismissed', 'stop_undone', 'line_completed',
        'suggestion_failed', 'revision_conflict'
    )),
    suggestion_set_id TEXT,
    candidate_id TEXT,
    revision_before INT,
    revision_after INT,
    context_hash CHAR(64) CHECK (context_hash IS NULL OR context_hash ~ '^[0-9a-f]{64}$'),
    policy_version TEXT,
    provider_snapshot_id TEXT,
    rank_position INT CHECK (rank_position IS NULL OR rank_position > 0),
    latency_ms INT CHECK (latency_ms IS NULL OR latency_ms >= 0),
    reason_code TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    event_json JSONB NOT NULL,
    CHECK (revision_before IS NULL OR revision_before > 0),
    CHECK (revision_after IS NULL OR revision_after > 0),
    CHECK (event_json->>'event_id' = event_id),
    CHECK (event_json->>'event_type' = event_type),
    CONSTRAINT recommendation_events_json_authority_check CHECK (
        event_json->>'workspace_id' = workspace_id
        AND event_json->>'session_id' = session_id
        AND event_json->>'actor_id' = actor_id
        AND (event_json->>'suggestion_set_id') IS NOT DISTINCT FROM suggestion_set_id
        AND (event_json->>'candidate_id') IS NOT DISTINCT FROM candidate_id
        AND (NULLIF(event_json->>'revision_before', '')::INT) IS NOT DISTINCT FROM revision_before
        AND (NULLIF(event_json->>'revision_after', '')::INT) IS NOT DISTINCT FROM revision_after
        AND (event_json->>'context_hash') IS NOT DISTINCT FROM rtrim(context_hash)
        AND (event_json->>'policy_version') IS NOT DISTINCT FROM policy_version
        AND (event_json->>'provider_snapshot_id') IS NOT DISTINCT FROM provider_snapshot_id
        AND (NULLIF(event_json->>'rank_position', '')::INT) IS NOT DISTINCT FROM rank_position
        AND (NULLIF(event_json->>'latency_ms', '')::INT) IS NOT DISTINCT FROM latency_ms
        AND (event_json->>'reason_code') IS NOT DISTINCT FROM reason_code
        AND (event_json->>'occurred_at')::TIMESTAMPTZ = occurred_at
    ),
    CONSTRAINT recommendation_events_workspace_set_fkey
        FOREIGN KEY (workspace_id, suggestion_set_id)
        REFERENCES suggestion_sets(workspace_id, suggestion_set_id) ON DELETE CASCADE,
    CONSTRAINT recommendation_events_workspace_set_candidate_fkey
        FOREIGN KEY (workspace_id, suggestion_set_id, candidate_id)
        REFERENCES suggestion_candidates(workspace_id, suggestion_set_id, candidate_id),
    CONSTRAINT recommendation_events_scope_by_type_check CHECK (
        (event_type = 'suggestion_failed' AND suggestion_set_id IS NULL AND candidate_id IS NULL)
        OR (event_type IN ('suggestions_shown', 'line_completed') AND suggestion_set_id IS NOT NULL AND candidate_id IS NULL)
        OR (event_type IN (
            'candidate_previewed', 'candidate_accepted', 'candidate_dismissed',
            'stop_undone', 'revision_conflict'
        ) AND suggestion_set_id IS NOT NULL AND candidate_id IS NOT NULL)
    ),
    CONSTRAINT recommendation_events_frozen_context_check CHECK (
        (suggestion_set_id IS NULL AND context_hash IS NULL AND policy_version IS NULL
            AND provider_snapshot_id IS NULL AND rank_position IS NULL)
        OR (suggestion_set_id IS NOT NULL AND context_hash IS NOT NULL
            AND policy_version IS NOT NULL AND provider_snapshot_id IS NOT NULL
            AND (candidate_id IS NULL OR rank_position IS NOT NULL))
    )
);

CREATE INDEX IF NOT EXISTS idx_recommendation_events_workspace
    ON recommendation_events(workspace_id, occurred_at, event_id);

CREATE TABLE IF NOT EXISTS suggestion_accept_commands (
    command_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    suggestion_set_id TEXT NOT NULL REFERENCES suggestion_sets(suggestion_set_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    base_revision INT NOT NULL CHECK (base_revision > 0),
    result_revision INT NOT NULL CHECK (result_revision > base_revision),
    actor_user_id TEXT NOT NULL REFERENCES users(user_id),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    response_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, idempotency_key),
    UNIQUE (workspace_id, result_revision),
    FOREIGN KEY (suggestion_set_id, candidate_id)
        REFERENCES suggestion_candidates(suggestion_set_id, candidate_id),
    FOREIGN KEY (workspace_id, result_revision)
        REFERENCES itinerary_revisions(workspace_id, revision)
);

-- Suggestion facts are frozen.  Cascaded deletion with the owning workspace is
-- still allowed, while in-place rewriting is rejected.
CREATE OR REPLACE FUNCTION reject_suggestion_fact_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'suggestion facts are immutable';
END;
$$;

DROP TRIGGER IF EXISTS suggestion_sets_no_update ON suggestion_sets;
CREATE TRIGGER suggestion_sets_no_update
    BEFORE UPDATE ON suggestion_sets
    FOR EACH ROW EXECUTE FUNCTION reject_suggestion_fact_update();

DROP TRIGGER IF EXISTS suggestion_candidates_no_update ON suggestion_candidates;
CREATE TRIGGER suggestion_candidates_no_update
    BEFORE UPDATE ON suggestion_candidates
    FOR EACH ROW EXECUTE FUNCTION reject_suggestion_fact_update();

DROP TRIGGER IF EXISTS recommendation_events_no_update ON recommendation_events;
CREATE TRIGGER recommendation_events_no_update
    BEFORE UPDATE ON recommendation_events
    FOR EACH ROW EXECUTE FUNCTION reject_suggestion_fact_update();
