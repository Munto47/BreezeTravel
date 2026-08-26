-- MP1: privacy-preserving WeChat identity and atomic mini-program screenshot uploads.

CREATE TABLE IF NOT EXISTS wechat_identities (
    app_id TEXT NOT NULL,
    openid_hmac CHAR(64) NOT NULL CHECK (openid_hmac ~ '^[0-9a-f]{64}$'),
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (app_id, openid_hmac),
    UNIQUE (app_id, user_id)
);

CREATE TABLE IF NOT EXISTS screenshot_upload_batches (
    batch_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    actor_user_id TEXT NOT NULL REFERENCES users(user_id),
    expected_count SMALLINT NOT NULL CHECK (expected_count BETWEEN 1 AND 6),
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'PROCESSING', 'SUCCEEDED', 'FAILED',
        'PRIVACY_BLOCKED', 'CANCELLED', 'EXPIRED'
    )),
    version INT NOT NULL DEFAULT 1 CHECK (version > 0),
    result_import_id TEXT REFERENCES itinerary_imports(import_id),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (status = 'SUCCEEDED' AND result_import_id IS NOT NULL)
        OR (status <> 'SUCCEEDED' AND result_import_id IS NULL)
    )
);

ALTER TABLE trip_temporary_assets
    ADD COLUMN IF NOT EXISTS upload_batch_id TEXT
        REFERENCES screenshot_upload_batches(batch_id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS upload_position SMALLINT;

ALTER TABLE trip_temporary_assets
    DROP CONSTRAINT IF EXISTS trip_temporary_assets_upload_position_check;

ALTER TABLE trip_temporary_assets
    ADD CONSTRAINT trip_temporary_assets_upload_position_check
    CHECK (
        (upload_batch_id IS NULL AND upload_position IS NULL)
        OR (upload_batch_id IS NOT NULL AND upload_position BETWEEN 0 AND 5)
    );

CREATE UNIQUE INDEX IF NOT EXISTS idx_trip_temporary_assets_batch_position
    ON trip_temporary_assets(upload_batch_id, upload_position)
    WHERE upload_batch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_screenshot_upload_batches_expiry
    ON screenshot_upload_batches(status, expires_at)
    WHERE status IN ('PENDING', 'PROCESSING', 'PRIVACY_BLOCKED');

CREATE TABLE IF NOT EXISTS screenshot_upload_commands (
    command_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES screenshot_upload_batches(batch_id) ON DELETE CASCADE,
    actor_user_id TEXT NOT NULL REFERENCES users(user_id),
    operation TEXT NOT NULL CHECK (operation IN ('CREATE_BATCH', 'UPLOAD_FILE', 'COMMIT', 'CANCEL')),
    idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 200),
    request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    response_status INT NOT NULL CHECK (response_status BETWEEN 200 AND 599),
    response_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (batch_id, idempotency_key)
);
