-- R0-R2 contracts: task revisions, verification, room authorization and governed memory.

ALTER TABLE room_members ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'member';

ALTER TABLE user_preferences
    ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    ADD COLUMN IF NOT EXISTS source_message_ids TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS content_hash TEXT,
    ADD COLUMN IF NOT EXISTS supersedes_id UUID REFERENCES user_preferences(id) ON DELETE SET NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_prefs_active_hash
    ON user_preferences(user_id, content_hash) WHERE active = TRUE AND content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_user_prefs_expiry
    ON user_preferences(expires_at) WHERE active = TRUE;

CREATE TABLE IF NOT EXISTS user_memory_settings (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS memory_audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    preference_id UUID,
    action TEXT NOT NULL,
    reason_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trip_task_specs (
    task_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    task_revision INT NOT NULL CHECK (task_revision > 0),
    spec_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(room_id, task_revision)
);

CREATE TABLE IF NOT EXISTS verification_reports (
    report_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES trip_task_specs(task_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    planning_input_hash TEXT NOT NULL,
    report_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS room_snapshots (
    room_id TEXT PRIMARY KEY REFERENCES rooms(room_id) ON DELETE CASCADE,
    snapshot_json JSONB NOT NULL,
    planning_input_hash TEXT NOT NULL,
    snapshot_version BIGINT NOT NULL DEFAULT 1,
    updated_by TEXT NOT NULL REFERENCES users(user_id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tool_receipts (
    call_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    room_id TEXT REFERENCES rooms(room_id) ON DELETE SET NULL,
    actor_user_id TEXT,
    tool TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INT NOT NULL,
    result_count INT NOT NULL DEFAULT 0,
    error_category TEXT,
    degraded BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
