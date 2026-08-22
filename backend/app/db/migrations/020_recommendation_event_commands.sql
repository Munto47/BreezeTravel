-- Final 2.0 P5: public recommendation interactions are immutable,
-- idempotent commands and every set/candidate relationship is workspace-scoped.

-- Compatibility repair for databases that already applied migration 019.
ALTER TABLE suggestion_candidates
    ADD COLUMN IF NOT EXISTS workspace_id TEXT;

UPDATE suggestion_candidates AS candidate
SET workspace_id = suggestion_set.workspace_id
FROM suggestion_sets AS suggestion_set
WHERE candidate.suggestion_set_id = suggestion_set.suggestion_set_id
  AND candidate.workspace_id IS NULL;

ALTER TABLE suggestion_candidates
    ALTER COLUMN workspace_id SET NOT NULL;

DO $$ BEGIN
    ALTER TABLE suggestion_candidates
        ADD CONSTRAINT suggestion_candidates_workspace_set_candidate_key
        UNIQUE (workspace_id, suggestion_set_id, candidate_id);
-- PostgreSQL reports an already-existing UNIQUE constraint's backing index as
-- duplicate_table (42P07), not duplicate_object (42710), on fresh schemas where
-- migration 019 already created this exact constraint.
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE suggestion_candidates
        ADD CONSTRAINT suggestion_candidates_workspace_set_fkey
        FOREIGN KEY (workspace_id, suggestion_set_id)
        REFERENCES suggestion_sets(workspace_id, suggestion_set_id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE recommendation_events
    DROP CONSTRAINT IF EXISTS recommendation_events_suggestion_set_id_fkey;

ALTER TABLE recommendation_events
    ADD COLUMN IF NOT EXISTS session_id TEXT,
    ADD COLUMN IF NOT EXISTS context_hash CHAR(64),
    ADD COLUMN IF NOT EXISTS policy_version TEXT,
    ADD COLUMN IF NOT EXISTS provider_snapshot_id TEXT,
    ADD COLUMN IF NOT EXISTS rank_position INT,
    ADD COLUMN IF NOT EXISTS latency_ms INT,
    ADD COLUMN IF NOT EXISTS reason_code TEXT;

UPDATE recommendation_events
SET session_id = event_json->>'session_id',
    context_hash = event_json->>'context_hash',
    policy_version = event_json->>'policy_version',
    provider_snapshot_id = event_json->>'provider_snapshot_id',
    rank_position = NULLIF(event_json->>'rank_position', '')::INT,
    latency_ms = NULLIF(event_json->>'latency_ms', '')::INT,
    reason_code = event_json->>'reason_code'
WHERE session_id IS NULL;

ALTER TABLE recommendation_events
    ALTER COLUMN session_id SET NOT NULL;

DO $$ BEGIN
    ALTER TABLE recommendation_events
        ADD CONSTRAINT recommendation_events_workspace_set_fkey
        FOREIGN KEY (workspace_id, suggestion_set_id)
        REFERENCES suggestion_sets(workspace_id, suggestion_set_id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE recommendation_events
        ADD CONSTRAINT recommendation_events_workspace_set_candidate_fkey
        FOREIGN KEY (workspace_id, suggestion_set_id, candidate_id)
        REFERENCES suggestion_candidates(workspace_id, suggestion_set_id, candidate_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE recommendation_events
        ADD CONSTRAINT recommendation_events_json_authority_check CHECK (
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
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE recommendation_events
        ADD CONSTRAINT recommendation_events_frozen_context_check CHECK (
            length(session_id) > 0
            AND (context_hash IS NULL OR context_hash ~ '^[0-9a-f]{64}$')
            AND (rank_position IS NULL OR rank_position > 0)
            AND (latency_ms IS NULL OR latency_ms >= 0)
            AND (
                (suggestion_set_id IS NULL AND context_hash IS NULL AND policy_version IS NULL
                    AND provider_snapshot_id IS NULL AND rank_position IS NULL)
                OR (suggestion_set_id IS NOT NULL AND context_hash IS NOT NULL
                    AND policy_version IS NOT NULL AND provider_snapshot_id IS NOT NULL
                    AND (candidate_id IS NULL OR rank_position IS NOT NULL))
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE recommendation_events
        ADD CONSTRAINT recommendation_events_scope_by_type_check CHECK (
            (event_type = 'suggestion_failed' AND suggestion_set_id IS NULL AND candidate_id IS NULL)
            OR (event_type IN ('suggestions_shown', 'line_completed')
                AND suggestion_set_id IS NOT NULL AND candidate_id IS NULL)
            OR (event_type IN (
                'candidate_previewed', 'candidate_accepted', 'candidate_dismissed',
                'stop_undone', 'revision_conflict'
            ) AND suggestion_set_id IS NOT NULL AND candidate_id IS NOT NULL)
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS recommendation_event_commands (
    command_id TEXT PRIMARY KEY REFERENCES recommendation_events(event_id),
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    suggestion_set_id TEXT NOT NULL,
    candidate_id TEXT,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'candidate_previewed', 'candidate_dismissed', 'stop_undone', 'line_completed'
    )),
    actor_user_id TEXT NOT NULL REFERENCES users(user_id),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    response_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, idempotency_key),
    FOREIGN KEY (workspace_id, suggestion_set_id)
        REFERENCES suggestion_sets(workspace_id, suggestion_set_id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, suggestion_set_id, candidate_id)
        REFERENCES suggestion_candidates(workspace_id, suggestion_set_id, candidate_id),
    CHECK (response_json->'event'->>'event_id' = command_id),
    CHECK (response_json->'event'->>'workspace_id' = workspace_id),
    CHECK (response_json->'event'->>'suggestion_set_id' = suggestion_set_id),
    CHECK ((response_json->'event'->>'candidate_id') IS NOT DISTINCT FROM candidate_id),
    CHECK (response_json->'event'->>'event_type' = event_type),
    CHECK ((response_json->>'idempotent_replay')::BOOLEAN = FALSE)
);

CREATE INDEX IF NOT EXISTS idx_recommendation_event_commands_workspace
    ON recommendation_event_commands(workspace_id, created_at, command_id);

DROP TRIGGER IF EXISTS recommendation_event_commands_no_update ON recommendation_event_commands;
CREATE TRIGGER recommendation_event_commands_no_update
    BEFORE UPDATE ON recommendation_event_commands
    FOR EACH ROW EXECUTE FUNCTION reject_suggestion_fact_update();
