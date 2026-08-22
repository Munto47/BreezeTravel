-- P2/P4: immutable evidence snapshots, audit reports and repair proposals.
-- Legacy verification_reports remains unchanged and has no new write path.

CREATE TABLE IF NOT EXISTS evidence_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    itinerary_revision INT NOT NULL CHECK (itinerary_revision > 0),
    provider_set TEXT[] NOT NULL DEFAULT '{}',
    policy_version TEXT NOT NULL,
    provider_failures_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    supersedes_snapshot_id TEXT REFERENCES evidence_snapshots(snapshot_id) ON DELETE SET NULL,
    UNIQUE (workspace_id, snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_snapshots_workspace
    ON evidence_snapshots(workspace_id, itinerary_revision, created_at DESC);

CREATE TABLE IF NOT EXISTS evidence_facts (
    fact_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES evidence_snapshots(snapshot_id) ON DELETE CASCADE,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    value_json JSONB,
    provider TEXT NOT NULL,
    source_url TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    valid_from TIMESTAMPTZ,
    valid_until TIMESTAMPTZ,
    response_hash CHAR(64) NOT NULL,
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    freshness_status TEXT NOT NULL
        CHECK (freshness_status IN ('FRESH', 'STALE', 'UNAVAILABLE', 'CONFLICTING'))
);

CREATE INDEX IF NOT EXISTS idx_evidence_facts_snapshot
    ON evidence_facts(snapshot_id, subject_type, subject_id, fact_type);

CREATE TABLE IF NOT EXISTS audit_reports (
    report_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    itinerary_id TEXT NOT NULL,
    itinerary_revision INT NOT NULL CHECK (itinerary_revision > 0),
    task_id TEXT NOT NULL,
    task_revision INT NOT NULL CHECK (task_revision > 0),
    member_constraint_revision_set JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_snapshot_id TEXT NOT NULL REFERENCES evidence_snapshots(snapshot_id),
    audit_rule_set_version TEXT NOT NULL,
    report_input_hash CHAR(64) NOT NULL,
    overall_status TEXT NOT NULL CHECK (overall_status IN ('SATISFIED', 'VIOLATED', 'UNKNOWN')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    supersedes_report_id TEXT REFERENCES audit_reports(report_id) ON DELETE SET NULL,
    UNIQUE (workspace_id, report_input_hash)
);

CREATE INDEX IF NOT EXISTS idx_audit_reports_workspace
    ON audit_reports(workspace_id, itinerary_revision, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_findings (
    finding_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES audit_reports(report_id) ON DELETE CASCADE,
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('SATISFIED', 'VIOLATED', 'UNKNOWN')),
    severity TEXT NOT NULL CHECK (severity IN ('BLOCKER', 'HIGH', 'MEDIUM', 'LOW', 'INFO')),
    reason_code TEXT NOT NULL,
    message TEXT NOT NULL,
    input_values_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    affected_days INT[] NOT NULL DEFAULT '{}',
    affected_stop_ids TEXT[] NOT NULL DEFAULT '{}',
    affected_member_ids TEXT[] NOT NULL DEFAULT '{}',
    evidence_fact_ids TEXT[] NOT NULL DEFAULT '{}',
    repairable BOOLEAN NOT NULL DEFAULT FALSE,
    confirmation_action TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_findings_report
    ON audit_findings(report_id, severity, status);

CREATE TABLE IF NOT EXISTS repair_options (
    repair_id TEXT PRIMARY KEY,
    source_report_id TEXT NOT NULL REFERENCES audit_reports(report_id) ON DELETE CASCADE,
    base_itinerary_revision INT NOT NULL CHECK (base_itinerary_revision > 0),
    targeted_finding_ids TEXT[] NOT NULL,
    edit_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    risk_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    route_cost_delta DOUBLE PRECISION,
    new_unknown_count INT NOT NULL DEFAULT 0 CHECK (new_unknown_count >= 0),
    tradeoffs_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    affected_member_ids TEXT[] NOT NULL DEFAULT '{}',
    result_preview_json JSONB NOT NULL,
    postcheck_report_id TEXT REFERENCES audit_reports(report_id),
    status TEXT NOT NULL CHECK (status IN ('PROPOSED', 'APPLIED', 'REJECTED', 'STALE')),
    decided_by TEXT,
    decision_reason TEXT,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_repair_options_report
    ON repair_options(source_report_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS repair_operations (
    repair_id TEXT NOT NULL REFERENCES repair_options(repair_id) ON DELETE CASCADE,
    operation_index INT NOT NULL CHECK (operation_index >= 0),
    operation TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    PRIMARY KEY (repair_id, operation_index)
);
