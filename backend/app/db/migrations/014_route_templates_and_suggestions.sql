-- P5: versioned city route templates.  They are deliberately independent of
-- itinerary revisions; applying a template still has to create a revision.
CREATE TABLE city_route_templates (
    template_id TEXT PRIMARY KEY,
    city TEXT NOT NULL CHECK (city IN ('北京', '上海', '杭州')),
    name TEXT NOT NULL,
    template_version INTEGER NOT NULL CHECK (template_version >= 1),
    status TEXT NOT NULL CHECK (status IN ('DRAFT', 'REVIEWED', 'RETIRED')),
    provenance TEXT NOT NULL CHECK (provenance IN ('MODEL_GENERATED', 'HUMAN_CURATED')),
    last_verified_at TIMESTAMPTZ,
    template_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (NOT (status = 'REVIEWED' AND provenance = 'MODEL_GENERATED'))
);

CREATE INDEX idx_city_route_templates_city_status
    ON city_route_templates(city, status, template_version DESC);

-- The P1 creation-command ledger also guards template application.  This
-- replaces (rather than mutates) the original check so existing databases
-- accept the new operation after applying this migration.
ALTER TABLE idempotent_creation_commands
    DROP CONSTRAINT IF EXISTS idempotent_creation_commands_operation_check;
ALTER TABLE idempotent_creation_commands
    ADD CONSTRAINT idempotent_creation_commands_operation_check CHECK (operation IN (
        'CREATE_IMPORT', 'CREATE_AUDIT', 'REFRESH_AUDIT', 'PROPOSE_REPAIRS',
        'GENERATE_TIPS', 'APPLY_TEMPLATE'
    ));
