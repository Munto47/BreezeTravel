-- G04 screenshot parity: short-lived owner-bound batches, ordered asset facts,
-- and append-only cleanup evidence. Raw pixels and client filenames are never
-- stored in PostgreSQL.

CREATE TABLE IF NOT EXISTS trip_understanding_screenshot_batches (
    batch_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    batch_ref_hash CHAR(64) NOT NULL UNIQUE
        CHECK (batch_ref_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key_hash CHAR(64) NOT NULL
        CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    request_hash CHAR(64) NOT NULL
        CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK (status IN (
        'PROCESSING', 'READY', 'PARTIAL', 'CONSUMED', 'FAILED',
        'CANCELLED', 'TIMED_OUT', 'EXPIRED', 'PRIVACY_BLOCKED'
    )),
    encrypted_source_document BYTEA,
    encryption_key_ref TEXT,
    source_document_hash CHAR(64)
        CHECK (source_document_hash IS NULL OR source_document_hash ~ '^[0-9a-f]{64}$'),
    semantic_text_hash CHAR(64)
        CHECK (semantic_text_hash IS NULL OR semantic_text_hash ~ '^[0-9a-f]{64}$'),
    image_count SMALLINT NOT NULL CHECK (image_count BETWEEN 0 AND 6),
    successful_image_count SMALLINT NOT NULL
        CHECK (successful_image_count BETWEEN 0 AND image_count),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    consumed_understanding_id TEXT
        REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    document_purged_at TIMESTAMPTZ,
    last_error_category TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (owner_user_id, idempotency_key_hash),
    CHECK ((encrypted_source_document IS NULL) = (encryption_key_ref IS NULL)),
    CHECK (
        (status IN ('READY', 'PARTIAL')
         AND encrypted_source_document IS NOT NULL
         AND source_document_hash IS NOT NULL
         AND semantic_text_hash IS NOT NULL)
        OR status NOT IN ('READY', 'PARTIAL')
    ),
    CHECK (
        (status = 'CONSUMED') =
        (consumed_at IS NOT NULL AND consumed_understanding_id IS NOT NULL)
    ),
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_trip_understanding_screenshot_batches_owner
    ON trip_understanding_screenshot_batches(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trip_understanding_screenshot_batches_expiry
    ON trip_understanding_screenshot_batches(expires_at, status)
    WHERE document_purged_at IS NULL;

CREATE TABLE IF NOT EXISTS trip_understanding_screenshot_assets (
    asset_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL
        REFERENCES trip_understanding_screenshot_batches(batch_id) ON DELETE CASCADE,
    upload_position SMALLINT NOT NULL CHECK (upload_position BETWEEN 0 AND 5),
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    media_type TEXT NOT NULL CHECK (media_type IN ('image/png', 'image/jpeg', 'image/webp')),
    byte_size BIGINT NOT NULL CHECK (byte_size BETWEEN 1 AND 10485760),
    storage_locator TEXT NOT NULL CHECK (length(storage_locator) BETWEEN 32 AND 256),
    ocr_status TEXT NOT NULL CHECK (ocr_status IN (
        'PENDING', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'NO_TEXT'
    )),
    cleanup_status TEXT NOT NULL CHECK (cleanup_status IN (
        'PENDING', 'CLEANED', 'CLEANUP_FAILED'
    )),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (batch_id, upload_position),
    UNIQUE (batch_id, storage_locator),
    UNIQUE (storage_locator)
);

CREATE TABLE IF NOT EXISTS trip_understanding_screenshot_cleanup_receipts (
    receipt_id TEXT PRIMARY KEY,
    -- These are immutable evidence bindings, not lifecycle-owned foreign keys.
    -- Parent removal must not cascade away the deletion proof.
    batch_id TEXT,
    asset_id TEXT,
    source_id TEXT,
    orphan_batch_locator_hash CHAR(64)
        CHECK (
            orphan_batch_locator_hash IS NULL
            OR orphan_batch_locator_hash ~ '^[0-9a-f]{64}$'
        ),
    orphan_asset_locator_hash CHAR(64)
        CHECK (
            orphan_asset_locator_hash IS NULL
            OR orphan_asset_locator_hash ~ '^[0-9a-f]{64}$'
        ),
    recovery_event_hash CHAR(64) UNIQUE
        CHECK (
            recovery_event_hash IS NULL
            OR recovery_event_hash ~ '^[0-9a-f]{64}$'
        ),
    upload_position SMALLINT CHECK (upload_position IS NULL OR upload_position BETWEEN 0 AND 5),
    asset_content_hash CHAR(64)
        CHECK (asset_content_hash IS NULL OR asset_content_hash ~ '^[0-9a-f]{64}$'),
    attempt_number SMALLINT NOT NULL CHECK (attempt_number BETWEEN 1 AND 3),
    terminal_reason TEXT NOT NULL CHECK (length(terminal_reason) BETWEEN 1 AND 80),
    cleanup_status TEXT NOT NULL CHECK (cleanup_status IN (
        'DELETED', 'ALREADY_ABSENT', 'DELETE_FAILED', 'CIPHERTEXT_PURGED'
    )),
    error_category TEXT,
    attempted_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        batch_id IS NOT NULL
        OR source_id IS NOT NULL
        OR orphan_batch_locator_hash IS NOT NULL
    ),
    CHECK (asset_id IS NULL OR batch_id IS NOT NULL),
    CHECK (
        orphan_asset_locator_hash IS NULL
        OR orphan_batch_locator_hash IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_trip_understanding_screenshot_cleanup_batch
    ON trip_understanding_screenshot_cleanup_receipts(batch_id, attempted_at);
CREATE INDEX IF NOT EXISTS idx_trip_understanding_screenshot_cleanup_source
    ON trip_understanding_screenshot_cleanup_receipts(source_id, attempted_at)
    WHERE source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trip_understanding_screenshot_cleanup_orphan
    ON trip_understanding_screenshot_cleanup_receipts(
        orphan_batch_locator_hash,
        attempted_at
    )
    WHERE orphan_batch_locator_hash IS NOT NULL;

DROP TRIGGER IF EXISTS trip_understanding_screenshot_cleanup_receipts_no_update
    ON trip_understanding_screenshot_cleanup_receipts;
CREATE TRIGGER trip_understanding_screenshot_cleanup_receipts_no_update
    BEFORE UPDATE OR DELETE ON trip_understanding_screenshot_cleanup_receipts
    FOR EACH ROW EXECUTE FUNCTION reject_trip_understanding_immutable_update();
