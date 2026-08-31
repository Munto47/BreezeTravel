-- G05 three-city sourced knowledge. Source and claim versions are append-only;
-- withdrawal is represented by separate immutable records. No source page body
-- or screenshot is stored in these tables.

CREATE TABLE IF NOT EXISTS knowledge_source_versions (
    source_version_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL CHECK (length(source_key) BETWEEN 3 AND 160),
    version INTEGER NOT NULL CHECK (version > 0),
    publisher_name TEXT NOT NULL CHECK (length(publisher_name) BETWEEN 2 AND 200),
    source_tier TEXT NOT NULL CHECK (source_tier IN ('GOVERNMENT', 'OFFICIAL_OPERATOR')),
    canonical_url TEXT NOT NULL CHECK (canonical_url ~ '^https://'),
    access_method TEXT NOT NULL CHECK (access_method IN ('MANUAL_REVIEWED_PUBLIC_HTTPS', 'OFFICIAL_API')),
    terms_url TEXT NOT NULL CHECK (terms_url ~ '^https://'),
    license_status TEXT NOT NULL CHECK (license_status IN (
        'FACTS_ONLY_WITH_ATTRIBUTION', 'OPEN_DATA_REUSE', 'REUSE_SCOPE_UNCLEAR'
    )),
    storage_policy TEXT NOT NULL CHECK (storage_policy IN ('NORMALIZED_FACTS_ONLY', 'NO_STORAGE')),
    admission_status TEXT NOT NULL CHECK (admission_status IN ('ADMITTED', 'NOT_READY')),
    observed_at TIMESTAMPTZ NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    reviewer TEXT NOT NULL CHECK (length(reviewer) BETWEEN 3 AND 160),
    withdrawal_method TEXT NOT NULL CHECK (length(withdrawal_method) BETWEEN 3 AND 500),
    version_note TEXT NOT NULL CHECK (length(version_note) BETWEEN 3 AND 1000),
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_key, version),
    CHECK (observed_at <= reviewed_at AND reviewed_at < expires_at),
    CHECK (
        (admission_status = 'ADMITTED'
         AND license_status IN ('FACTS_ONLY_WITH_ATTRIBUTION', 'OPEN_DATA_REUSE')
         AND storage_policy = 'NORMALIZED_FACTS_ONLY')
        OR
        (admission_status = 'NOT_READY' AND storage_policy = 'NO_STORAGE')
    )
);

CREATE INDEX IF NOT EXISTS idx_knowledge_source_versions_latest
    ON knowledge_source_versions(source_key, version DESC);

CREATE TABLE IF NOT EXISTS knowledge_source_withdrawals (
    withdrawal_id TEXT PRIMARY KEY,
    source_version_id TEXT NOT NULL UNIQUE
        REFERENCES knowledge_source_versions(source_version_id),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 3 AND 1000),
    reviewer TEXT NOT NULL CHECK (length(reviewer) BETWEEN 3 AND 160),
    withdrawn_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS knowledge_claim_versions (
    claim_revision_id TEXT PRIMARY KEY,
    claim_key TEXT NOT NULL CHECK (length(claim_key) BETWEEN 3 AND 160),
    version INTEGER NOT NULL CHECK (version > 0),
    source_version_id TEXT NOT NULL REFERENCES knowledge_source_versions(source_version_id),
    canonical_place_id TEXT NOT NULL CHECK (length(canonical_place_id) BETWEEN 3 AND 200),
    city TEXT NOT NULL CHECK (city IN ('北京', '上海', '杭州')),
    claim_type TEXT NOT NULL CHECK (claim_type IN (
        'TYPICAL_DURATION', 'SUITABLE_TIME', 'NIGHT_VIEW', 'SEASON', 'RESERVATION_ADVICE'
    )),
    conditions_json JSONB NOT NULL CHECK (jsonb_typeof(conditions_json) = 'object'),
    conditions_hash CHAR(64) NOT NULL CHECK (conditions_hash ~ '^[0-9a-f]{64}$'),
    suggestion_text TEXT NOT NULL CHECK (length(suggestion_text) BETWEEN 3 AND 300),
    short_evidence TEXT NOT NULL CHECK (length(short_evidence) BETWEEN 3 AND 500),
    effective_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    reviewer TEXT NOT NULL CHECK (length(reviewer) BETWEEN 3 AND 160),
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    supersedes_claim_revision_id TEXT REFERENCES knowledge_claim_versions(claim_revision_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (claim_key, version),
    CHECK (effective_at < expires_at)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_claim_versions_place
    ON knowledge_claim_versions(canonical_place_id, claim_type, version DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_claim_versions_expiry
    ON knowledge_claim_versions(expires_at);

CREATE TABLE IF NOT EXISTS knowledge_claim_withdrawals (
    withdrawal_id TEXT PRIMARY KEY,
    claim_revision_id TEXT NOT NULL UNIQUE
        REFERENCES knowledge_claim_versions(claim_revision_id),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 3 AND 1000),
    reviewer TEXT NOT NULL CHECK (length(reviewer) BETWEEN 3 AND 160),
    withdrawn_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS knowledge_import_receipts (
    import_receipt_id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL UNIQUE CHECK (length(bundle_id) BETWEEN 3 AND 160),
    bundle_hash CHAR(64) NOT NULL UNIQUE CHECK (bundle_hash ~ '^[0-9a-f]{64}$'),
    source_version_count INTEGER NOT NULL CHECK (source_version_count >= 0),
    claim_version_count INTEGER NOT NULL CHECK (claim_version_count >= 0),
    reviewer TEXT NOT NULL CHECK (length(reviewer) BETWEEN 3 AND 160),
    imported_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS knowledge_usage_receipts (
    usage_receipt_id TEXT PRIMARY KEY,
    understanding_id TEXT NOT NULL
        REFERENCES trip_understandings(understanding_id) ON DELETE CASCADE,
    result_revision INTEGER NOT NULL CHECK (result_revision > 0),
    projection_hash CHAR(64) NOT NULL CHECK (projection_hash ~ '^[0-9a-f]{64}$'),
    claim_revision_ids JSONB NOT NULL CHECK (jsonb_typeof(claim_revision_ids) = 'array'),
    selected_count INTEGER NOT NULL CHECK (selected_count > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (understanding_id, result_revision, projection_hash)
);

CREATE OR REPLACE FUNCTION g05_reject_knowledge_record_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'knowledge records are append-only and immutable';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS knowledge_source_versions_immutable ON knowledge_source_versions;
CREATE TRIGGER knowledge_source_versions_immutable
    BEFORE UPDATE OR DELETE ON knowledge_source_versions
    FOR EACH ROW EXECUTE FUNCTION g05_reject_knowledge_record_mutation();

DROP TRIGGER IF EXISTS knowledge_source_withdrawals_immutable ON knowledge_source_withdrawals;
CREATE TRIGGER knowledge_source_withdrawals_immutable
    BEFORE UPDATE OR DELETE ON knowledge_source_withdrawals
    FOR EACH ROW EXECUTE FUNCTION g05_reject_knowledge_record_mutation();

DROP TRIGGER IF EXISTS knowledge_claim_versions_immutable ON knowledge_claim_versions;
CREATE TRIGGER knowledge_claim_versions_immutable
    BEFORE UPDATE OR DELETE ON knowledge_claim_versions
    FOR EACH ROW EXECUTE FUNCTION g05_reject_knowledge_record_mutation();

DROP TRIGGER IF EXISTS knowledge_claim_withdrawals_immutable ON knowledge_claim_withdrawals;
CREATE TRIGGER knowledge_claim_withdrawals_immutable
    BEFORE UPDATE OR DELETE ON knowledge_claim_withdrawals
    FOR EACH ROW EXECUTE FUNCTION g05_reject_knowledge_record_mutation();

DROP TRIGGER IF EXISTS knowledge_import_receipts_immutable ON knowledge_import_receipts;
CREATE TRIGGER knowledge_import_receipts_immutable
    BEFORE UPDATE OR DELETE ON knowledge_import_receipts
    FOR EACH ROW EXECUTE FUNCTION g05_reject_knowledge_record_mutation();

DROP TRIGGER IF EXISTS knowledge_usage_receipts_immutable ON knowledge_usage_receipts;
CREATE TRIGGER knowledge_usage_receipts_immutable
    -- A receipt cannot be rewritten while its trip exists. Deletion remains
    -- available only so the owning trip's privacy cascade is not blocked.
    BEFORE UPDATE ON knowledge_usage_receipts
    FOR EACH ROW EXECUTE FUNCTION g05_reject_knowledge_record_mutation();
