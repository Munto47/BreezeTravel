-- Final 2.0 P5: bind every suggestion-derived Undo to its itinerary command,
-- emitted stop_undone event, and immutable candidate_accepted source event.

DO $$ BEGIN
    ALTER TABLE itinerary_edit_commands
        ADD CONSTRAINT itinerary_edit_commands_workspace_command_key
        UNIQUE (workspace_id, command_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE recommendation_events
        ADD CONSTRAINT recommendation_events_workspace_event_key
        UNIQUE (workspace_id, event_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS suggestion_undo_links (
    undo_command_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    source_accept_event_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    suggestion_set_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    accepted_revision INT NOT NULL CHECK (accepted_revision > 0),
    target_revision INT NOT NULL CHECK (target_revision > 0),
    result_revision INT NOT NULL CHECK (result_revision > accepted_revision),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (workspace_id, undo_command_id)
        REFERENCES itinerary_edit_commands(workspace_id, command_id),
    FOREIGN KEY (workspace_id, event_id)
        REFERENCES recommendation_events(workspace_id, event_id),
    FOREIGN KEY (workspace_id, source_accept_event_id)
        REFERENCES recommendation_events(workspace_id, event_id),
    FOREIGN KEY (workspace_id, suggestion_set_id, candidate_id)
        REFERENCES suggestion_candidates(workspace_id, suggestion_set_id, candidate_id),
    FOREIGN KEY (workspace_id, accepted_revision)
        REFERENCES itinerary_revisions(workspace_id, revision),
    FOREIGN KEY (workspace_id, target_revision)
        REFERENCES itinerary_revisions(workspace_id, revision),
    FOREIGN KEY (workspace_id, result_revision)
        REFERENCES itinerary_revisions(workspace_id, revision),
    CHECK (target_revision < accepted_revision)
);

CREATE INDEX IF NOT EXISTS idx_suggestion_undo_links_workspace
    ON suggestion_undo_links(workspace_id, result_revision, undo_command_id);

DROP TRIGGER IF EXISTS suggestion_undo_links_no_update ON suggestion_undo_links;
CREATE TRIGGER suggestion_undo_links_no_update
    BEFORE UPDATE ON suggestion_undo_links
    FOR EACH ROW EXECUTE FUNCTION reject_suggestion_fact_update();
