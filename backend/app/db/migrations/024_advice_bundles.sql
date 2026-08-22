-- Trip Check V1: immutable Advice bundles and postcheck lineage.

CREATE TABLE IF NOT EXISTS advice_bundles (
    advice_bundle_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES trip_workspaces(workspace_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL UNIQUE REFERENCES trip_check_runs(run_id) ON DELETE CASCADE,
    report_id TEXT NOT NULL UNIQUE REFERENCES audit_reports(report_id) ON DELETE CASCADE,
    itinerary_revision INT NOT NULL CHECK (itinerary_revision > 0),
    brief_id TEXT NOT NULL,
    brief_revision INT NOT NULL CHECK (brief_revision > 0),
    evidence_snapshot_id TEXT NOT NULL REFERENCES evidence_snapshots(snapshot_id),
    bundle_json JSONB NOT NULL,
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, content_hash),
    FOREIGN KEY (workspace_id, itinerary_revision)
        REFERENCES itinerary_revisions(workspace_id, revision),
    FOREIGN KEY (brief_id, brief_revision)
        REFERENCES trip_brief_revisions(brief_id, revision),
    CHECK (bundle_json->>'advice_bundle_id' = advice_bundle_id),
    CHECK (bundle_json->>'workspace_id' = workspace_id),
    CHECK (bundle_json->>'run_id' = run_id),
    CHECK (bundle_json->>'report_id' = report_id),
    CHECK (bundle_json->>'evidence_snapshot_id' = evidence_snapshot_id)
);

CREATE TABLE IF NOT EXISTS advice_actions (
    advice_bundle_id TEXT NOT NULL REFERENCES advice_bundles(advice_bundle_id) ON DELETE CASCADE,
    advice_id TEXT NOT NULL,
    finding_id TEXT NOT NULL REFERENCES audit_findings(finding_id) ON DELETE CASCADE,
    action_text TEXT NOT NULL,
    expected_impact TEXT NOT NULL,
    uncertainty TEXT NOT NULL,
    candidate_set_id TEXT REFERENCES suggestion_sets(suggestion_set_id),
    evidence_fact_ids TEXT[] NOT NULL DEFAULT '{}',
    provider_receipt_ids TEXT[] NOT NULL DEFAULT '{}',
    route_delta_json JSONB,
    repair_id TEXT REFERENCES repair_options(repair_id),
    tradeoffs_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (advice_bundle_id, advice_id),
    CHECK (jsonb_typeof(tradeoffs_json) = 'array')
);

CREATE TABLE IF NOT EXISTS trip_check_postcheck_lineage (
    lineage_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES trip_check_runs(run_id) ON DELETE CASCADE,
    advice_bundle_id TEXT NOT NULL REFERENCES advice_bundles(advice_bundle_id) ON DELETE CASCADE,
    repair_id TEXT NOT NULL REFERENCES repair_options(repair_id),
    source_report_id TEXT NOT NULL REFERENCES audit_reports(report_id),
    source_itinerary_revision INT NOT NULL CHECK (source_itinerary_revision > 0),
    result_itinerary_revision INT NOT NULL CHECK (result_itinerary_revision > source_itinerary_revision),
    postcheck_report_id TEXT NOT NULL REFERENCES audit_reports(report_id),
    postcheck_snapshot_id TEXT NOT NULL REFERENCES evidence_snapshots(snapshot_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, repair_id)
);

ALTER TABLE trip_check_runs
    ADD CONSTRAINT trip_check_runs_advice_bundle_fk
    FOREIGN KEY (advice_bundle_id)
    REFERENCES advice_bundles(advice_bundle_id);

CREATE OR REPLACE FUNCTION reject_advice_fact_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'advice and postcheck lineage facts are immutable';
END;
$$;

DROP TRIGGER IF EXISTS advice_bundles_no_update ON advice_bundles;
CREATE TRIGGER advice_bundles_no_update
    BEFORE UPDATE ON advice_bundles
    FOR EACH ROW EXECUTE FUNCTION reject_advice_fact_update();

DROP TRIGGER IF EXISTS advice_actions_no_update ON advice_actions;
CREATE TRIGGER advice_actions_no_update
    BEFORE UPDATE ON advice_actions
    FOR EACH ROW EXECUTE FUNCTION reject_advice_fact_update();

DROP TRIGGER IF EXISTS trip_check_postcheck_lineage_no_update ON trip_check_postcheck_lineage;
CREATE TRIGGER trip_check_postcheck_lineage_no_update
    BEFORE UPDATE ON trip_check_postcheck_lineage
    FOR EACH ROW EXECUTE FUNCTION reject_advice_fact_update();
