-- P7: revocable capability links and append-only member acknowledgement evidence.
-- Raw bearer values are never persisted; token_hash is a SHA-256 digest.

CREATE TABLE IF NOT EXISTS trip_share_links (
    share_link_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    itinerary_revision INT NOT NULL CHECK (itinerary_revision > 0),
    report_id TEXT,
    token_hash CHAR(64) NOT NULL UNIQUE,
    scopes TEXT[] NOT NULL CHECK (cardinality(scopes) > 0),
    recipient_member_id TEXT REFERENCES users(user_id) ON DELETE RESTRICT,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (NOT ('CONSTRAINT_WRITE' = ANY(scopes) OR 'ACKNOWLEDGE' = ANY(scopes)) OR recipient_member_id IS NOT NULL),
    UNIQUE (share_link_id, workspace_id, itinerary_revision),
    FOREIGN KEY (workspace_id, itinerary_revision)
        REFERENCES itinerary_revisions(workspace_id, revision) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_trip_share_links_workspace_created ON trip_share_links(workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS trip_share_link_responses (
    response_id TEXT PRIMARY KEY,
    share_link_id TEXT NOT NULL REFERENCES trip_share_links(share_link_id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    itinerary_revision INT NOT NULL CHECK (itinerary_revision > 0),
    member_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('ACKNOWLEDGE', 'CONSTRAINT')),
    member_constraint_revision INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((action = 'ACKNOWLEDGE' AND member_constraint_revision IS NULL) OR (action = 'CONSTRAINT' AND member_constraint_revision IS NOT NULL)),
    FOREIGN KEY (share_link_id, workspace_id, itinerary_revision)
        REFERENCES trip_share_links(share_link_id, workspace_id, itinerary_revision) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trip_share_link_responses_workspace_member ON trip_share_link_responses(workspace_id, member_id, created_at DESC);
