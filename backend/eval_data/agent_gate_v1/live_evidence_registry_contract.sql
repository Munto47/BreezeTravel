-- Immutable G01 Agent Gate typed live-effect registry contract.
--
-- This is an evaluation authority contract, not a product migration. The
-- isolated Agent Gate materializes these exact tables in its disposable
-- PostgreSQL database and proves the fixed exporter queries against them. A
-- live runtime may write one row only after a successful external call; the
-- evidence exporter receives SELECT-only access. No table accepts a
-- preassembled receipt, signed bundle, aggregate metric, or generic artifact.

CREATE TABLE trip_g01_amap_provider_effects (
    evidence_run_id TEXT NOT NULL CHECK (
        evidence_run_id ~ '^G01-LIVE-[A-Z0-9-]{8,100}$'
    ),
    candidate_commit CHAR(40) NOT NULL CHECK (
        candidate_commit ~ '^[0-9a-f]{40}$'
    ),
    split TEXT NOT NULL CHECK (split IN ('dev', 'validation', 'frozen_blind')),
    effect_id TEXT NOT NULL CHECK (length(effect_id) BETWEEN 8 AND 160),
    effect_key_sha256 CHAR(64) NOT NULL CHECK (effect_key_sha256 ~ '^[0-9a-f]{64}$'),
    provider_binding_sha256 CHAR(64) NOT NULL CHECK (
        provider_binding_sha256 ~ '^[0-9a-f]{64}$'
    ),
    request_sha256 CHAR(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    response_sha256 CHAR(64) NOT NULL CHECK (response_sha256 ~ '^[0-9a-f]{64}$'),
    provider_request_id_sha256 CHAR(64) NOT NULL CHECK (
        provider_request_id_sha256 ~ '^[0-9a-f]{64}$'
    ),
    http_status SMALLINT NOT NULL CHECK (http_status BETWEEN 200 AND 299),
    resolution_status TEXT NOT NULL CHECK (
        resolution_status IN ('MATCHED', 'UNRESOLVED', 'AMBIGUOUS')
    ),
    accepted_source_name TEXT,
    place_id TEXT,
    place_name TEXT,
    city TEXT,
    category TEXT,
    accepted_source_names TEXT[] NOT NULL DEFAULT '{}',
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    raw_response_retained BOOLEAN NOT NULL DEFAULT FALSE CHECK (NOT raw_response_retained),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (evidence_run_id, candidate_commit, effect_id),
    UNIQUE (effect_key_sha256),
    CHECK (completed_at >= started_at),
    CHECK (
        (
            resolution_status = 'MATCHED'
            AND accepted_source_name IS NOT NULL
            AND place_id IS NOT NULL
            AND place_name IS NOT NULL
            AND city IS NOT NULL
            AND category IS NOT NULL
            AND cardinality(accepted_source_names) > 0
            AND accepted_source_name = ANY(accepted_source_names)
        )
        OR
        (
            resolution_status <> 'MATCHED'
            AND accepted_source_name IS NULL
            AND place_id IS NULL
            AND place_name IS NULL
            AND city IS NULL
            AND category IS NULL
            AND cardinality(accepted_source_names) = 0
        )
    )
);

CREATE TABLE trip_g01_qwen_inference_effects (
    evidence_run_id TEXT NOT NULL CHECK (
        evidence_run_id ~ '^G01-LIVE-[A-Z0-9-]{8,100}$'
    ),
    candidate_commit CHAR(40) NOT NULL CHECK (
        candidate_commit ~ '^[0-9a-f]{40}$'
    ),
    split TEXT NOT NULL CHECK (split IN ('dev', 'validation', 'frozen_blind')),
    effect_id TEXT NOT NULL CHECK (length(effect_id) BETWEEN 8 AND 160),
    case_id TEXT NOT NULL CHECK (case_id ~ '^G01-TC-[0-9]{3}$'),
    input_sha256 CHAR(64) NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    request_sha256 CHAR(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    response_sha256 CHAR(64) NOT NULL CHECK (response_sha256 ~ '^[0-9a-f]{64}$'),
    provider_request_id_sha256 CHAR(64) NOT NULL CHECK (
        provider_request_id_sha256 ~ '^[0-9a-f]{64}$'
    ),
    http_status SMALLINT NOT NULL CHECK (http_status BETWEEN 200 AND 299),
    output_sha256 CHAR(64) NOT NULL CHECK (output_sha256 ~ '^[0-9a-f]{64}$'),
    inference_output_json JSONB NOT NULL CHECK (
        jsonb_typeof(inference_output_json) = 'object'
        AND inference_output_json->>'schema_version' = 'g01-agent-inference-case-output-v2'
        AND inference_output_json->>'case_id' = case_id
    ),
    input_tokens INT NOT NULL CHECK (input_tokens >= 0),
    output_tokens INT NOT NULL CHECK (output_tokens >= 0),
    latency_ms DOUBLE PRECISION NOT NULL CHECK (latency_ms >= 0),
    repair_call_count INT NOT NULL CHECK (repair_call_count BETWEEN 0 AND 1),
    provider_binding_sha256 CHAR(64) NOT NULL CHECK (
        provider_binding_sha256 ~ '^[0-9a-f]{64}$'
    ),
    model_binding_sha256 CHAR(64) NOT NULL CHECK (
        model_binding_sha256 ~ '^[0-9a-f]{64}$'
    ),
    exact_model_id TEXT NOT NULL CHECK (length(exact_model_id) BETWEEN 2 AND 160),
    region TEXT NOT NULL CHECK (length(region) BETWEEN 2 AND 80),
    endpoint_sha256 CHAR(64) NOT NULL CHECK (endpoint_sha256 ~ '^[0-9a-f]{64}$'),
    prompt_sha256 CHAR(64) NOT NULL CHECK (prompt_sha256 ~ '^[0-9a-f]{64}$'),
    schema_sha256 CHAR(64) NOT NULL CHECK (schema_sha256 ~ '^[0-9a-f]{64}$'),
    config_sha256 CHAR(64) NOT NULL CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    raw_response_retained BOOLEAN NOT NULL DEFAULT FALSE CHECK (NOT raw_response_retained),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (evidence_run_id, candidate_commit, effect_id),
    UNIQUE (evidence_run_id, candidate_commit, case_id),
    CHECK (completed_at >= started_at)
);

CREATE OR REPLACE FUNCTION trip_g01_reject_live_effect_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'G01 live effect registries are append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER trip_g01_amap_effects_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON trip_g01_amap_provider_effects
FOR EACH STATEMENT EXECUTE FUNCTION trip_g01_reject_live_effect_mutation();

CREATE TRIGGER trip_g01_qwen_effects_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON trip_g01_qwen_inference_effects
FOR EACH STATEMENT EXECUTE FUNCTION trip_g01_reject_live_effect_mutation();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'breezetravel_g01_amap_effect_writer'
    ) THEN
        CREATE ROLE breezetravel_g01_amap_effect_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'breezetravel_g01_qwen_effect_writer'
    ) THEN
        CREATE ROLE breezetravel_g01_qwen_effect_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'breezetravel_g01_live_effect_exporter'
    ) THEN
        CREATE ROLE breezetravel_g01_live_effect_exporter NOLOGIN;
    END IF;
END;
$$;

ALTER ROLE breezetravel_g01_amap_effect_writer
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE breezetravel_g01_qwen_effect_writer
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE breezetravel_g01_live_effect_exporter
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

REVOKE ALL ON TABLE trip_g01_amap_provider_effects FROM PUBLIC;
REVOKE ALL ON TABLE trip_g01_qwen_inference_effects FROM PUBLIC;
REVOKE ALL ON TABLE trip_g01_amap_provider_effects
    FROM breezetravel_g01_amap_effect_writer,
         breezetravel_g01_qwen_effect_writer,
         breezetravel_g01_live_effect_exporter;
REVOKE ALL ON TABLE trip_g01_qwen_inference_effects
    FROM breezetravel_g01_amap_effect_writer,
         breezetravel_g01_qwen_effect_writer,
         breezetravel_g01_live_effect_exporter;
REVOKE ALL ON FUNCTION trip_g01_reject_live_effect_mutation() FROM PUBLIC;

GRANT USAGE ON SCHEMA public
    TO breezetravel_g01_amap_effect_writer,
       breezetravel_g01_qwen_effect_writer,
       breezetravel_g01_live_effect_exporter;
GRANT INSERT ON TABLE trip_g01_amap_provider_effects
    TO breezetravel_g01_amap_effect_writer;
GRANT INSERT ON TABLE trip_g01_qwen_inference_effects
    TO breezetravel_g01_qwen_effect_writer;
GRANT SELECT ON TABLE trip_g01_amap_provider_effects,
                      trip_g01_qwen_inference_effects
    TO breezetravel_g01_live_effect_exporter;

-- The table owner can still alter this contract and is inside the documented
-- host-admin threat boundary. Ordinary runtime/export roles cannot mutate or
-- cross-read effects; even accidental owner DML is rejected by the triggers.
