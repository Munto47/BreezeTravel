-- P1: authoritative trip workspace, append-only itinerary revisions and edit commands.

CREATE TABLE IF NOT EXISTS trip_workspaces (
    workspace_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    city TEXT NOT NULL CHECK (city IN ('北京', '上海', '杭州')),
    trip_start_date DATE NOT NULL,
    trip_end_date DATE NOT NULL,
    current_itinerary_revision INT,
    current_task_spec_revision INT,
    current_member_constraint_revision INT,
    current_report_id TEXT,
    status TEXT NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT', 'AUDITING', 'NEEDS_CONFIRMATION', 'CONFIRMED')),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (trip_end_date >= trip_start_date),
    UNIQUE (workspace_id, room_id)
);

CREATE INDEX IF NOT EXISTS idx_trip_workspaces_room
    ON trip_workspaces(room_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS itinerary_revisions (
    itinerary_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    revision INT NOT NULL CHECK (revision > 0),
    parent_revision INT,
    source_type TEXT NOT NULL
        CHECK (source_type IN ('IMPORT', 'TEMPLATE', 'MANUAL', 'REPAIR', 'PLANNER')),
    city TEXT NOT NULL CHECK (city IN ('北京', '上海', '杭州')),
    trip_start_date DATE NOT NULL,
    trip_end_date DATE NOT NULL,
    days_json JSONB NOT NULL,
    locked_commitments_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    change_summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_hash CHAR(64) NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (itinerary_id, revision),
    UNIQUE (workspace_id, revision),
    UNIQUE (workspace_id, content_hash, revision),
    CHECK (trip_end_date >= trip_start_date),
    CHECK (parent_revision IS NULL OR parent_revision < revision)
);

CREATE INDEX IF NOT EXISTS idx_itinerary_revisions_workspace
    ON itinerary_revisions(workspace_id, revision DESC);

CREATE TABLE IF NOT EXISTS itinerary_edit_commands (
    command_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    base_revision INT NOT NULL CHECK (base_revision >= 0),
    result_revision INT NOT NULL CHECK (result_revision > 0),
    actor_user_id TEXT NOT NULL REFERENCES users(user_id),
    operation TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    request_hash CHAR(64) NOT NULL,
    idempotency_key TEXT NOT NULL,
    client_timestamp TIMESTAMPTZ,
    response_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, idempotency_key),
    UNIQUE (workspace_id, result_revision)
);

CREATE INDEX IF NOT EXISTS idx_itinerary_commands_workspace
    ON itinerary_edit_commands(workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS itinerary_imports (
    import_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK (source_type IN ('AI_TEXT', 'MANUAL_TEXT')),
    raw_text TEXT NOT NULL,
    parse_version TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('PARSED', 'NEEDS_RESOLUTION', 'READY', 'APPLIED', 'FAILED')),
    parsed_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_itinerary_imports_workspace
    ON itinerary_imports(workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS itinerary_stop_resolutions (
    raw_stop_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES itinerary_imports(import_id) ON DELETE CASCADE,
    day_index INT,
    raw_name TEXT NOT NULL,
    raw_time TEXT,
    source_span JSONB NOT NULL,
    source_sentence TEXT NOT NULL,
    fixed_commitment BOOLEAN NOT NULL DEFAULT FALSE,
    canonical_place_id TEXT,
    candidates_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    resolution_status TEXT NOT NULL
        CHECK (resolution_status IN ('AUTO_MATCHED', 'USER_CONFIRMED', 'AMBIGUOUS', 'NOT_FOUND')),
    resolution_version INT NOT NULL DEFAULT 1 CHECK (resolution_version > 0),
    confirmed_by TEXT REFERENCES users(user_id),
    confirmed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_stop_resolutions_import
    ON itinerary_stop_resolutions(import_id, day_index, raw_stop_id);

CREATE TABLE IF NOT EXISTS traveler_profiles (
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    profile_json JSONB NOT NULL,
    confirmed_revision INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, member_id)
);

CREATE TABLE IF NOT EXISTS member_constraints (
    constraint_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    owner_member_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    constraint_type TEXT NOT NULL,
    operator TEXT NOT NULL,
    value_json JSONB NOT NULL,
    hardness TEXT NOT NULL CHECK (hardness IN ('HARD', 'SOFT')),
    priority INT NOT NULL DEFAULT 0,
    source TEXT NOT NULL
        CHECK (source IN ('MEMBER_EXPLICIT', 'ORGANIZER', 'ROOM_CONSENSUS', 'MEMORY', 'INFERRED')),
    confirmation_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (confirmation_status IN ('PENDING', 'CONFIRMED', 'REJECTED')),
    waivable_by TEXT[] NOT NULL DEFAULT '{}',
    revision INT NOT NULL CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (constraint_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_member_constraints_workspace
    ON member_constraints(workspace_id, owner_member_id, revision DESC);

