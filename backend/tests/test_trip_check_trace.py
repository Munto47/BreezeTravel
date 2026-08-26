from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.itineraries.hash_service import sha256_canonical
from app.trip_check.models import (
    RunBudget,
    RunSpec,
    SideEffectReceipt,
    TripCheckRun,
    TripCheckRunStatus,
    TripCheckStage,
)
from app.trip_check.runs import InMemoryTripCheckRunRepository, run_config_hash
from app.trip_check.trace import (
    OTEL_ATTRIBUTE_ALLOWLIST,
    RedactedJsonlSpanExporter,
    TripCheckDomainTraceAssembler,
    TripCheckTelemetry,
)


def _run() -> TripCheckRun:
    spec = RunSpec(
        commit_sha="8816975",
        prompt_version="none-p2",
        model_version="none-p2",
        provider_version="controlled-fixture-v2",
        rule_set_version="audit-v1",
        execution_mode="fixture",
        dataset_hash="a" * 64,
        snapshot_hash="b" * 64,
        fault_profile="none",
        random_seed=7,
        budget=RunBudget(timeout_seconds=30),
    )
    now = datetime(2026, 8, 23, tzinfo=timezone.utc)
    return TripCheckRun(
        run_id="trace-run",
        workspace_id="trace-workspace",
        itinerary_revision=1,
        brief_id="trace-brief",
        brief_revision=2,
        stage=TripCheckStage.COLLECT_EVIDENCE,
        run_spec=spec,
        config_hash=run_config_hash(spec),
        completed_stages=[
            TripCheckStage.PARSE,
            TripCheckStage.WAIT_BRIEF_CONFIRMATION,
            TripCheckStage.RESOLVE_PLACES,
        ],
        status=TripCheckRunStatus.WAITING,
        created_by="trace-user",
        created_at=now,
        updated_at=now,
    )


def test_domain_trace_is_deterministic_and_contains_only_hashed_payloads():
    async def scenario():
        repository = InMemoryTripCheckRunRepository()
        created = _run()
        await repository.create_run(created, idempotency_key="trace-create", request_hash="c" * 64)
        claimed = await repository.claim_for_execution(created.run_id, now=created.created_at)
        started = await repository.start_stage(
            created.run_id,
            lease_owner=claimed.lease_owner,
            stage_input_hash="d" * 64,
            now=created.created_at,
        )
        receipt = SideEffectReceipt(
            receipt_id="trace-receipt",
            run_id=created.run_id,
            stage=TripCheckStage.COLLECT_EVIDENCE,
            side_effect_key="trace-side-effect",
            effect_type="CONTROLLED_FIXTURE_EVIDENCE",
            request_hash="e" * 64,
            response_hash="f" * 64,
            provider="controlled_fixture",
            status="SUCCEEDED",
            receipt={"snapshot_id": "trace-snapshot", "raw_text": "must-not-be-exported"},
            created_at=created.created_at,
        )
        await repository.complete_stage(
            created.run_id,
            lease_owner=started.lease_owner,
            expected_stage=TripCheckStage.COLLECT_EVIDENCE,
            next_stage=TripCheckStage.AUDIT,
            status=TripCheckRunStatus.RUNNING,
            receipt=receipt,
            evidence_snapshot_id="trace-snapshot",
            now=created.created_at,
        )
        assembler = TripCheckDomainTraceAssembler(repository)
        first = await assembler.assemble(created.run_id)
        second = await assembler.assemble(created.run_id)
        assert [item.model_dump(mode="json") for item in first] == [
            item.model_dump(mode="json") for item in second
        ]
        assert {item.record_type for item in first} == {
            "RUN_EVENT",
            "STAGE_ATTEMPT",
            "SIDE_EFFECT_RECEIPT",
        }
        assert all(item.run_id == created.run_id for item in first)
        assert all(item.config_hash == created.config_hash for item in first)
        assert all(len(item.payload_hash) == 64 for item in first)
        serialized = json.dumps([item.model_dump(mode="json") for item in first], ensure_ascii=False)
        assert "must-not-be-exported" not in serialized
        assert sha256_canonical(receipt.receipt) in serialized

    asyncio.run(scenario())


def test_otel_span_hierarchy_and_exporter_attribute_allowlist(tmp_path):
    memory = InMemorySpanExporter()
    jsonl_path = tmp_path / "otel_spans.jsonl"
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    provider.add_span_processor(SimpleSpanProcessor(RedactedJsonlSpanExporter(jsonl_path)))
    telemetry = TripCheckTelemetry(provider.get_tracer("trip-check-test"))
    run = _run()

    with telemetry.run_span(run):
        with telemetry.stage_span(run):
            with telemetry.provider_attempt_span(run, attempt=1) as provider_span:
                provider_span.set_attribute("raw_text", "must-not-be-exported")

    spans = memory.get_finished_spans()
    by_name = {span.name: span for span in spans}
    assert set(by_name) == {"trip_check.run", "trip_check.stage", "trip_check.provider_attempt"}
    assert by_name["trip_check.stage"].parent.span_id == by_name["trip_check.run"].context.span_id
    assert by_name["trip_check.provider_attempt"].parent.span_id == by_name["trip_check.stage"].context.span_id

    exported = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
    assert len(exported) == 3
    assert all(set(item["attributes"]) <= OTEL_ATTRIBUTE_ALLOWLIST for item in exported)
    assert "must-not-be-exported" not in jsonl_path.read_text(encoding="utf-8")
