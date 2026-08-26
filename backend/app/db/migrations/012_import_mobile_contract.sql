-- Mobile-safe itinerary import state, discovery and idempotent apply commands.

ALTER TABLE itinerary_imports
    ADD COLUMN IF NOT EXISTS state_version INT NOT NULL DEFAULT 1
        CHECK (state_version > 0),
    ADD COLUMN IF NOT EXISTS applied_revision INT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE itinerary_imports
    DROP CONSTRAINT IF EXISTS itinerary_imports_applied_revision_check;

UPDATE itinerary_imports AS imported
SET applied_revision = revision.revision
FROM itinerary_revisions AS revision
WHERE imported.status = 'APPLIED'
  AND imported.applied_revision IS NULL
  AND revision.workspace_id = imported.workspace_id
  AND revision.source_type = 'IMPORT'
  AND revision.change_summary_json->>'import_id' = imported.import_id;

ALTER TABLE itinerary_imports
    ADD CONSTRAINT itinerary_imports_applied_revision_check
    CHECK (
        (status = 'APPLIED' AND applied_revision IS NOT NULL)
        OR (status <> 'APPLIED' AND applied_revision IS NULL)
    ) NOT VALID;

CREATE INDEX IF NOT EXISTS idx_itinerary_imports_workspace_updated
    ON itinerary_imports(workspace_id, updated_at DESC, import_id DESC);

CREATE TABLE IF NOT EXISTS itinerary_import_commands (
    command_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES itinerary_imports(import_id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    actor_user_id TEXT NOT NULL REFERENCES users(user_id),
    operation TEXT NOT NULL CHECK (operation IN ('APPLY_IMPORT')),
    request_hash CHAR(64) NOT NULL,
    idempotency_key TEXT NOT NULL,
    response_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (import_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_import_commands_workspace
    ON itinerary_import_commands(workspace_id, created_at DESC);
