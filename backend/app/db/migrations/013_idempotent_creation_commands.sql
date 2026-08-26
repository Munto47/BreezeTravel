-- P1-P4: durable idempotency for creation-style commands.
-- Route-template migrations intentionally remain outside this number range.

ALTER TABLE trip_workspaces
    ADD COLUMN IF NOT EXISTS current_import_id TEXT;

ALTER TABLE itinerary_imports
    ADD CONSTRAINT itinerary_imports_workspace_import_unique
    UNIQUE (workspace_id, import_id);

WITH latest_import AS (
    SELECT DISTINCT ON (workspace_id) workspace_id, import_id
    FROM itinerary_imports
    ORDER BY workspace_id, updated_at DESC, import_id DESC
)
UPDATE trip_workspaces AS workspace
SET current_import_id = latest.import_id
FROM latest_import AS latest
WHERE workspace.workspace_id = latest.workspace_id
  AND workspace.current_import_id IS NULL;

ALTER TABLE trip_workspaces
    ADD CONSTRAINT trip_workspaces_current_import_fk
    FOREIGN KEY (workspace_id, current_import_id)
    REFERENCES itinerary_imports(workspace_id, import_id)
    ON DELETE SET NULL (current_import_id);

CREATE TABLE idempotent_creation_commands (
    command_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK (operation IN (
        'CREATE_IMPORT',
        'CREATE_AUDIT',
        'REFRESH_AUDIT',
        'PROPOSE_REPAIRS',
        'GENERATE_TIPS'
    )),
    target_id TEXT NOT NULL,
    actor_user_id TEXT NOT NULL REFERENCES users(user_id),
    idempotency_key TEXT NOT NULL CHECK (
        char_length(idempotency_key) BETWEEN 1 AND 200
    ),
    request_hash CHAR(64) NOT NULL,
    basis_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    state TEXT NOT NULL CHECK (state IN ('IN_PROGRESS', 'SUCCEEDED', 'FAILED_FINAL')),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    response_status SMALLINT,
    response_headers_json JSONB,
    response_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, idempotency_key),
    CHECK (
        (state = 'IN_PROGRESS' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR
        (state <> 'IN_PROGRESS' AND response_status IS NOT NULL AND response_json IS NOT NULL)
    )
);

CREATE INDEX idx_idempotent_creation_commands_target
    ON idempotent_creation_commands(operation, target_id, created_at DESC);

ALTER TABLE final_tips_artifacts
    ADD COLUMN generation_input_hash CHAR(64);

UPDATE final_tips_artifacts
SET generation_input_hash = artifact_hash
WHERE generation_input_hash IS NULL;

ALTER TABLE final_tips_artifacts
    ALTER COLUMN generation_input_hash SET NOT NULL;
