from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult, SpanExporter
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from app.itineraries.errors import ResourceNotFound
from app.itineraries.hash_service import sha256_canonical
from app.trip_check.models import TripCheckDomainTraceRecord, TripCheckRun, TripCheckStage
from app.trip_check.runs import TripCheckRunRepository


OTEL_ATTRIBUTE_ALLOWLIST = frozenset(
    {
        "bt.run_id",
        "bt.itinerary_revision",
        "bt.brief_revision",
        "bt.evidence_snapshot_id",
        "bt.config_hash",
        "bt.rule_set_version",
        "bt.provider_version",
        "bt.execution_mode",
        "bt.stage",
        "bt.stage_attempt",
        "bt.failure_category",
    }
)


def _span_attributes(run: TripCheckRun, *, stage: TripCheckStage | None = None) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "bt.run_id": run.run_id,
        "bt.itinerary_revision": run.itinerary_revision,
        "bt.brief_revision": run.brief_revision,
        "bt.config_hash": run.config_hash,
        "bt.rule_set_version": run.run_spec.rule_set_version,
        "bt.provider_version": run.run_spec.provider_version,
        "bt.execution_mode": run.run_spec.execution_mode,
        "bt.stage": (stage or run.stage).value,
        "bt.stage_attempt": run.stage_attempt,
    }
    if run.evidence_snapshot_id:
        attributes["bt.evidence_snapshot_id"] = run.evidence_snapshot_id
    return attributes


class TripCheckTelemetry:
    def __init__(self, tracer: Tracer | None = None):
        self.tracer = tracer or trace.get_tracer("breezetravel.trip_check")

    @contextmanager
    def run_span(self, run: TripCheckRun) -> Iterator[Span]:
        with self.tracer.start_as_current_span("trip_check.run", attributes=_span_attributes(run)) as span:
            yield span

    @contextmanager
    def stage_span(self, run: TripCheckRun) -> Iterator[Span]:
        with self.tracer.start_as_current_span(
            "trip_check.stage",
            attributes=_span_attributes(run, stage=run.stage),
        ) as span:
            yield span

    @contextmanager
    def provider_attempt_span(self, run: TripCheckRun, *, attempt: int) -> Iterator[Span]:
        attributes = _span_attributes(run, stage=TripCheckStage.COLLECT_EVIDENCE)
        attributes["bt.stage_attempt"] = attempt
        with self.tracer.start_as_current_span("trip_check.provider_attempt", attributes=attributes) as span:
            yield span

    @staticmethod
    def mark_failure(span: Span, category: str) -> None:
        span.set_attribute("bt.failure_category", category)
        span.set_status(Status(StatusCode.ERROR))


class RedactedJsonlSpanExporter(SpanExporter):
    """Synchronous local exporter used by the controlled Reliability runner."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()

    def export(self, spans: tuple[ReadableSpan, ...]) -> SpanExportResult:
        lines: list[str] = []
        for span in spans:
            attributes = {
                key: value
                for key, value in (span.attributes or {}).items()
                if key in OTEL_ATTRIBUTE_ALLOWLIST
            }
            context = span.context
            parent = span.parent
            lines.append(
                json.dumps(
                    {
                        "schema_version": "trip-check-otel-span-v1",
                        "name": span.name,
                        "trace_id": f"{context.trace_id:032x}" if context else None,
                        "span_id": f"{context.span_id:016x}" if context else None,
                        "parent_span_id": f"{parent.span_id:016x}" if parent else None,
                        "start_time_unix_nano": span.start_time,
                        "end_time_unix_nano": span.end_time,
                        "status": span.status.status_code.name,
                        "attributes": attributes,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        if lines:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n".join(lines) + "\n")
        return SpanExportResult.SUCCESS


class TripCheckDomainTraceAssembler:
    def __init__(self, repository: TripCheckRunRepository):
        self.repository = repository

    async def assemble(self, run_id: str) -> list[TripCheckDomainTraceRecord]:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise ResourceNotFound("trip check run does not exist")
        events = await self.repository.list_events(run_id)
        attempts = await self.repository.list_stage_attempts(run_id)
        receipts = await self.repository.list_receipts(run_id)
        pending: list[tuple[Any, int, dict[str, Any]]] = []

        common = {
            "run_id": run.run_id,
            "itinerary_revision": run.itinerary_revision,
            "brief_revision": run.brief_revision,
            "evidence_snapshot_id": run.evidence_snapshot_id,
            "config_hash": run.config_hash,
            "rule_set_version": run.run_spec.rule_set_version,
            "provider_version": run.run_spec.provider_version,
            "execution_mode": run.run_spec.execution_mode,
        }
        for event in events:
            pending.append(
                (
                    event.created_at,
                    0,
                    {
                        **common,
                        "record_type": "RUN_EVENT",
                        "stage": event.stage,
                        "stage_attempt": int(event.payload.get("attempt") or 1),
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "run_version": event.run_version,
                        "failure_category": event.payload.get("category"),
                        "payload_hash": sha256_canonical(event.payload),
                        "occurred_at": event.created_at,
                    },
                )
            )
        for attempt in attempts:
            payload = attempt.model_dump(mode="json")
            pending.append(
                (
                    attempt.started_at,
                    1,
                    {
                        **common,
                        "record_type": "STAGE_ATTEMPT",
                        "stage": attempt.stage,
                        "stage_attempt": attempt.attempt,
                        "attempt_state": attempt.state,
                        "failure_category": attempt.failure_category,
                        "payload_hash": sha256_canonical(payload),
                        "occurred_at": attempt.started_at,
                    },
                )
            )
        for receipt in receipts:
            pending.append(
                (
                    receipt.created_at,
                    2,
                    {
                        **common,
                        "record_type": "SIDE_EFFECT_RECEIPT",
                        "stage": receipt.stage,
                        "stage_attempt": 1,
                        "receipt_id": receipt.receipt_id,
                        "effect_type": receipt.effect_type,
                        "receipt_status": receipt.status,
                        "failure_category": receipt.receipt.get("failure_category"),
                        "payload_hash": sha256_canonical(receipt.receipt),
                        "occurred_at": receipt.created_at,
                    },
                )
            )
        pending.sort(key=lambda item: (item[0], item[1], sha256_canonical(item[2])))
        return [
            TripCheckDomainTraceRecord(sequence=index, **payload)
            for index, (_, _, payload) in enumerate(pending, start=1)
        ]
