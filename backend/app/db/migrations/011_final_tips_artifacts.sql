-- P4: derived Tips are immutable presentation artifacts bound to one
-- canonical itinerary revision and its full AuditReport.

CREATE TABLE IF NOT EXISTS final_tips_artifacts (
    report_id TEXT PRIMARY KEY REFERENCES audit_reports(report_id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    itinerary_revision INTEGER NOT NULL,
    basis_content_hash TEXT NOT NULL,
    artifact_hash TEXT NOT NULL UNIQUE,
    itinerary_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (workspace_id, itinerary_revision)
        REFERENCES itinerary_revisions(workspace_id, revision) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_final_tips_workspace_revision
    ON final_tips_artifacts(workspace_id, itinerary_revision, created_at DESC);
