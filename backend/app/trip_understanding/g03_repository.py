from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from pydantic import TypeAdapter

from app.audit.models import (
    AuditReport,
    EvidenceFact,
    EvidenceFreshness,
    EvidenceSnapshot,
    ProviderFailure,
)
from app.audit.repositories import PostgresAuditRepository
from app.itineraries.models import ItineraryRevision, RevisionSource
from app.itineraries.repositories import _revision_from_row
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.errors import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    ResourceAccessDeniedError,
    ResourceGoneError,
    ResourceNotFoundError,
    ResourceNotReadyError,
    RevisionConflictError,
)
from app.trip_understanding.g03 import (
    G03_EVIDENCE_POLICY_VERSION,
    G03_SYSTEM_USER_ID,
    CalendarProfile,
    build_itinerary_revision,
    command_for_finding,
    preview_for_finding,
    public_checks,
    run_g03_audit,
)
from app.trip_understanding.models import (
    ChangeAdoptOutcome,
    ChangePreviewOutcome,
    MaterializationOutcome,
    MaterializedTripView,
    PublicChangeAdopted,
    PublicResourceRecord,
    PublicTripChecksView,
    TripUnderstandingCommand,
    UserFacingTripResult,
)
from app.trip_understanding.pipeline import canonical_sha256


_COMMAND_ADAPTER = TypeAdapter(TripUnderstandingCommand)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _route_stop_pairs(plan, itinerary: ItineraryRevision) -> dict:
    """Bind route occurrences through their plan positions and activity identities."""
    by_stop = {stop.stop_id: stop for day in itinerary.days for stop in day.stops}
    by_day: dict[int, list] = {}
    for stop in sorted(plan.stops, key=lambda item: (item.day_index, item.sequence_index)):
        by_day.setdefault(stop.day_index, []).append(stop)
    pairs = {}
    for day_index, stops in by_day.items():
        for sequence_index, (origin, destination) in enumerate(zip(stops, stops[1:])):
            left = by_stop.get(_stable_stop_id(origin.activity_token)) if origin.activity_token else None
            right = by_stop.get(_stable_stop_id(destination.activity_token)) if destination.activity_token else None
            if left is not None and right is not None:
                pairs[(day_index, sequence_index)] = (left, right)
    return pairs


def _materialized_view(profile: CalendarProfile) -> MaterializedTripView:
    return MaterializedTripView(
        message="行程已准备好，可以查看最值得处理的三项",
        calendar=profile.public_calendar,
        party_size=profile.party_size,
    )


class G03Repository(Protocol):
    async def materialize_trip(
        self,
        resource: PublicResourceRecord,
        *,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> MaterializationOutcome: ...

    async def get_trip_checks(
        self,
        resource: PublicResourceRecord,
    ) -> PublicTripChecksView: ...

    async def preview_trip_change(
        self,
        resource: PublicResourceRecord,
        *,
        check_token: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> ChangePreviewOutcome: ...

    async def adopt_trip_change(
        self,
        resource: PublicResourceRecord,
        *,
        change_token: str,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> ChangeAdoptOutcome: ...


class PostgresG03RepositoryMixin:
    async def _claim_g03_idempotency(
        self,
        conn: Any,
        *,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> Any | None:
        key_hash = _sha256_text(idempotency_key)
        claimed = await conn.fetchval(
            """
            INSERT INTO trip_understanding_idempotency_records (
                scope, key_hash, request_hash, state, lease_until, created_at
            ) VALUES ($1, $2, $3, 'IN_PROGRESS', $4, $5)
            ON CONFLICT (scope, key_hash) DO NOTHING
            RETURNING scope
            """,
            scope,
            key_hash,
            request_hash,
            now + timedelta(seconds=30),
            now,
        )
        if claimed is not None:
            return None
        existing = await conn.fetchrow(
            """
            SELECT request_hash, state, response_json, response_headers_json
            FROM trip_understanding_idempotency_records
            WHERE scope = $1 AND key_hash = $2
            """,
            scope,
            key_hash,
        )
        if existing is None:
            raise IdempotencyInProgressError("matching request is being claimed")
        if existing["request_hash"].strip() != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was already used with another request"
            )
        if existing["state"] != "COMPLETED":
            raise IdempotencyInProgressError("matching request is still in progress")
        return existing

    async def _complete_g03_idempotency(
        self,
        conn: Any,
        *,
        scope: str,
        idempotency_key: str,
        response_json: dict[str, Any],
        response_headers: dict[str, Any],
        now: datetime,
    ) -> None:
        await conn.execute(
            """
            UPDATE trip_understanding_idempotency_records
            SET state = 'COMPLETED', response_status = 200,
                response_json = $3::jsonb, response_headers_json = $4::jsonb,
                lease_until = NULL, completed_at = $5
            WHERE scope = $1 AND key_hash = $2
            """,
            scope,
            _sha256_text(idempotency_key),
            json.dumps(response_json, ensure_ascii=False),
            json.dumps(response_headers, ensure_ascii=False),
            now,
        )

    @staticmethod
    async def _lock_current_result(
        conn: Any,
        resource: PublicResourceRecord,
    ) -> Any:
        row = await conn.fetchrow(
            """
            SELECT * FROM trip_understandings
            WHERE understanding_id = $1
            FOR UPDATE
            """,
            resource.understanding_id,
        )
        if row is None:
            raise ResourceNotFoundError("trip resource does not exist")
        if row["state"] == "DELETED":
            raise ResourceGoneError("trip resource is no longer available")
        if row["public_resource_id"] != resource.public_resource_id:
            raise ResourceAccessDeniedError("trip resource binding changed")
        details = await conn.fetchrow(
            """
            SELECT r.public_json, r.opaque_etag, r.public_sha256,
                   ur.source_id, ur.destination_json, ur.assumptions_json
            FROM trip_understanding_results r
            JOIN trip_understanding_revisions ur
              ON ur.understanding_id = r.understanding_id
             AND ur.revision = r.revision
            WHERE r.result_id = $1
              AND r.understanding_id = $2
              AND r.revision = $3
            """,
            row["current_result_id"],
            resource.understanding_id,
            row["current_revision"],
        )
        if details is None or details["source_id"] is None:
            raise ResourceNotReadyError("trip cards are not ready")
        return {**dict(row), **dict(details)}

    @staticmethod
    async def _load_bindings(
        conn: Any,
        understanding_id: str,
        understanding_revision: int,
    ) -> dict[str, dict[str, Any]]:
        rows = await conn.fetch(
            """
            SELECT public_activity_token, canonical_place_id, resolution_status,
                   resolver_receipt_json
            FROM trip_understanding_activities
            WHERE understanding_id = $1 AND revision = $2 AND role = 'PLANNED'
            """,
            understanding_id,
            understanding_revision,
        )
        return {
            row["public_activity_token"]: {
                "canonical_place_id": row["canonical_place_id"],
                "resolution_status": row["resolution_status"],
                "resolver_receipt": _json(row["resolver_receipt_json"]),
            }
            for row in rows
        }

    @staticmethod
    async def _insert_itinerary_revision(
        conn: Any,
        revision: ItineraryRevision,
        profile: CalendarProfile,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO itinerary_revisions (
                itinerary_id, workspace_id, revision, parent_revision, source_type,
                city, trip_start_date, trip_end_date, days_json,
                locked_commitments_json, change_summary_json, content_hash,
                created_by, created_at, calendar_mode, party_size, party_size_source
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb,
                $10::jsonb, $11::jsonb, $12, $13, $14, $15, $16, $17
            )
            """,
            revision.itinerary_id,
            revision.workspace_id,
            revision.revision,
            revision.parent_revision,
            revision.source_type.value,
            revision.city,
            profile.start,
            profile.end,
            json.dumps(
                [day.model_dump(mode="json") for day in revision.days],
                ensure_ascii=False,
            ),
            json.dumps(revision.locked_commitments, ensure_ascii=False),
            json.dumps(revision.change_summary, ensure_ascii=False),
            revision.content_hash,
            revision.created_by,
            revision.created_at,
            profile.mode,
            profile.party_size,
            profile.party_size_source,
        )

    @staticmethod
    async def _ensure_itinerary_plan_ref(
        conn: Any,
        *,
        understanding_id: str,
        itinerary: ItineraryRevision,
        now: datetime,
    ) -> str:
        stop_set_hash = canonical_sha256(
            [
                {
                    "day_index": day.day_index,
                    "stops": [
                        {
                            "stop_id": stop.stop_id,
                            "place_id": stop.place_id,
                            "order_index": stop.order_index,
                        }
                        for stop in day.stops
                    ],
                }
                for day in itinerary.days
            ]
        )
        plan_ref_id = str(uuid4())
        await conn.execute(
            """
            INSERT INTO trip_plan_revision_refs (
                plan_ref_id, understanding_id, revision_kind, aggregate_id,
                revision, stop_set_hash, created_at
            ) VALUES ($1, $2, 'ITINERARY', $3, $4, $5, $6)
            ON CONFLICT (understanding_id, revision_kind, aggregate_id, revision)
            DO NOTHING
            """,
            plan_ref_id,
            understanding_id,
            itinerary.itinerary_id,
            itinerary.revision,
            stop_set_hash,
            now,
        )
        stored = await conn.fetchrow(
            """
            SELECT plan_ref_id, stop_set_hash
            FROM trip_plan_revision_refs
            WHERE understanding_id = $1 AND revision_kind = 'ITINERARY'
              AND aggregate_id = $2 AND revision = $3
            """,
            understanding_id,
            itinerary.itinerary_id,
            itinerary.revision,
        )
        if stored is None or stored["stop_set_hash"].strip() != stop_set_hash:
            raise IdempotencyConflictError("itinerary plan binding changed")
        return stored["plan_ref_id"]

    @staticmethod
    async def _stay_anchor(
        conn: Any,
        understanding_id: str,
        understanding_revision: int,
    ) -> dict[str, Any]:
        row = await conn.fetchrow(
            """
            SELECT s.selected_name, s.selected_brand, s.selected_address,
                   s.overnight_days
            FROM trip_stay_selections s
            JOIN trip_plan_revision_refs p ON p.plan_ref_id = s.target_plan_ref_id
            WHERE p.understanding_id = $1 AND p.revision_kind = 'UNDERSTANDING'
              AND p.aggregate_id = $1 AND p.revision = $2
            """,
            understanding_id,
            understanding_revision,
        )
        if row is None:
            return {}
        return {
            "name": row["selected_name"],
            "brand": row["selected_brand"],
            "area_or_address": row["selected_address"],
            "overnight_days": list(row["overnight_days"] or []),
        }

    async def _collect_g03_evidence(
        self,
        conn: Any,
        *,
        understanding_id: str,
        understanding_revision: int,
        itinerary: ItineraryRevision,
        result: UserFacingTripResult,
        bindings: dict[str, dict[str, Any]],
        now: datetime,
        supersedes_snapshot_id: str | None,
    ) -> tuple[EvidenceSnapshot, str, str, bool]:
        snapshot_id = str(uuid4())
        facts: list[EvidenceFact] = []
        provider_failures: list[ProviderFailure] = []
        candidate_values: list[dict[str, Any]] = []
        receipt_values: list[dict[str, Any]] = []

        for day, public_day in zip(itinerary.days, result.days, strict=True):
            for stop, card in zip(day.stops, public_day.activities, strict=True):
                binding = bindings.get(card.activity_token, {})
                receipt = dict(binding.get("resolver_receipt") or {})
                resolved = binding.get("resolution_status") == "AUTO_MATCHED"
                candidate_values.append(
                    {
                        "stop_id": stop.stop_id,
                        "canonical_place_id": binding.get("canonical_place_id"),
                        "resolution_status": binding.get("resolution_status"),
                    }
                )
                receipt_values.append(receipt)
                response_hash = canonical_sha256(receipt or {"status": "UNAVAILABLE"})
                facts.append(
                    EvidenceFact(
                        fact_id=str(uuid4()),
                        snapshot_id=snapshot_id,
                        subject_type="PLACE",
                        subject_id=stop.place_id,
                        fact_type="POI_IDENTITY",
                        value={
                            "name": card.name,
                            "category": card.category,
                            "city": itinerary.city,
                            "resolved": resolved,
                        },
                        provider=str(receipt.get("provider") or "trip-understanding"),
                        observed_at=now,
                        response_hash=response_hash,
                        confidence=1.0 if resolved else 0.0,
                        freshness_status=(
                            EvidenceFreshness.FRESH
                            if resolved or stop.category == "meal_break"
                            else EvidenceFreshness.UNAVAILABLE
                        ),
                    )
                )

        snapshot_row = await conn.fetchrow(
            """
            SELECT s.snapshot_id
            FROM trip_plan_revision_refs p
            JOIN trip_map_render_jobs j ON j.plan_ref_id = p.plan_ref_id
            JOIN trip_map_render_snapshots s ON s.map_job_id = j.map_job_id
            WHERE p.understanding_id = $1 AND p.revision_kind = 'UNDERSTANDING'
              AND p.aggregate_id = $1 AND p.revision = $2
            ORDER BY s.finished_at DESC LIMIT 1
            """,
            understanding_id,
            understanding_revision,
        )
        edge_rows: list[Any] = []
        if snapshot_row is not None:
            edge_rows = list(
                await conn.fetch(
                    """
                    SELECT e.day_index, e.sequence_index, e.origin_name,
                           e.destination_name, e.selected_mode, f.mode,
                           f.status, f.duration_minutes, f.distance_meters,
                           f.transfer_count, f.response_hash, f.observed_at
                    FROM trip_map_route_edges e
                    LEFT JOIN trip_map_route_mode_facts f ON f.edge_id = e.edge_id
                    WHERE e.snapshot_id = $1
                    ORDER BY e.day_index, e.sequence_index, f.mode
                    """,
                    snapshot_row["snapshot_id"],
                )
            )
        else:
            provider_failures.append(
                ProviderFailure(
                    provider="route",
                    error_category="CURRENT_ROUTE_NOT_RENDERED",
                    retryable=True,
                )
            )

        grouped_edges: dict[tuple[int, int], dict[str, Any]] = {}
        for row in edge_rows:
            key = (row["day_index"], row["sequence_index"])
            edge = grouped_edges.setdefault(
                key,
                {"selected_mode": row["selected_mode"], "modes": {}},
            )
            if row["mode"] is not None:
                edge["modes"][row["mode"]] = {
                    "status": row["status"],
                    "duration_minutes": row["duration_minutes"],
                    "distance_meters": row["distance_meters"],
                    "transfer_count": row["transfer_count"],
                    "response_hash": row["response_hash"].strip(),
                    "observed_at": row["observed_at"],
                }
        route_pairs = _route_stop_pairs(await self._read_map_plan(conn, understanding_id, understanding_revision), itinerary)
        for key, (left, right) in route_pairs.items():
            edge = grouped_edges.get(key)
            if edge is None:
                continue
            modes = edge["modes"]
            selected = modes.get(edge["selected_mode"])
            response_hashes = sorted(
                item["response_hash"] for item in modes.values()
            )
            facts.append(
                EvidenceFact(
                    fact_id=str(uuid4()),
                    snapshot_id=snapshot_id,
                    subject_type="ROUTE_EDGE",
                    subject_id=f"{left.stop_id}->{right.stop_id}",
                    fact_type="ROUTE_MODE_SET",
                    value={
                        "walking": modes.get("walking", {}).get(
                            "status", "UNAVAILABLE"
                        ),
                        "transit": modes.get("transit", {}).get(
                            "status", "UNAVAILABLE"
                        ),
                        "selected_mode": edge["selected_mode"],
                        "selected_duration_minutes": (
                            selected.get("duration_minutes") if selected else None
                        ),
                    },
                    provider="route",
                    observed_at=max(
                        (
                            item["observed_at"]
                            for item in modes.values()
                            if item.get("observed_at")
                        ),
                        default=now,
                    ),
                    response_hash=canonical_sha256(response_hashes),
                    confidence=1.0 if selected else 0.0,
                    freshness_status=(
                        EvidenceFreshness.FRESH
                        if selected
                        else EvidenceFreshness.UNAVAILABLE
                    ),
                )
            )
            receipt_values.extend(
                {
                    **item,
                    "mode": mode,
                    "observed_at": (
                        item["observed_at"].isoformat()
                        if item.get("observed_at")
                        else None
                    ),
                }
                for mode, item in sorted(modes.items())
            )
        stay = await conn.fetchrow(
            """
            SELECT c.max_single_leg_minutes, c.transfer_count,
                   c.provider_binding_json, s.selected_name
            FROM trip_stay_selections s
            JOIN trip_plan_revision_refs p ON p.plan_ref_id = s.target_plan_ref_id
            JOIN trip_stay_candidates c ON c.candidate_id = s.candidate_id
            WHERE p.understanding_id = $1 AND p.revision_kind = 'UNDERSTANDING'
              AND p.aggregate_id = $1 AND p.revision = $2
            """,
            understanding_id,
            understanding_revision,
        )
        if stay is not None:
            binding = dict(_json(stay["provider_binding_json"]) or {})
            facts.append(
                EvidenceFact(
                    fact_id=str(uuid4()),
                    snapshot_id=snapshot_id,
                    subject_type="STAY",
                    subject_id=itinerary.workspace_id,
                    fact_type="STAY_COMMUTE",
                    value={
                        "name": stay["selected_name"],
                        "max_single_leg_minutes": stay["max_single_leg_minutes"],
                        "transfer_count": stay["transfer_count"],
                    },
                    provider="route",
                    observed_at=now,
                    response_hash=canonical_sha256(binding),
                    confidence=1.0,
                    freshness_status=EvidenceFreshness.FRESH,
                )
            )
            receipt_values.append(binding)

        candidate_set_sha256 = canonical_sha256(candidate_values)
        receipt_set_sha256 = canonical_sha256(receipt_values)
        receipt_binding_complete = all(
            not item.get("canonical_place_id")
            or bool(receipt)
            for item, receipt in zip(candidate_values, receipt_values[: len(candidate_values)])
        )
        snapshot = EvidenceSnapshot(
            snapshot_id=snapshot_id,
            workspace_id=itinerary.workspace_id,
            itinerary_revision=itinerary.revision,
            provider_set=sorted(
                {fact.provider for fact in facts}
                | {failure.provider for failure in provider_failures}
            ),
            policy_version=G03_EVIDENCE_POLICY_VERSION,
            facts=facts,
            provider_failures=provider_failures,
            created_at=now,
            supersedes_snapshot_id=supersedes_snapshot_id,
        )
        return (
            snapshot,
            candidate_set_sha256,
            receipt_set_sha256,
            receipt_binding_complete,
        )

    @staticmethod
    async def _persist_check_tokens(
        conn: Any,
        report: AuditReport,
    ) -> dict[str, str]:
        tokens: dict[str, str] = {}
        for finding in report.findings:
            if finding.status.value not in {"VIOLATED", "UNKNOWN"}:
                continue
            token = secrets.token_urlsafe(24)
            await conn.execute(
                """
                INSERT INTO trip_public_check_tokens (
                    check_token, report_id, finding_id, created_at
                ) VALUES ($1, $2, $3, $4)
                """,
                token,
                report.report_id,
                finding.finding_id,
                report.created_at,
            )
            tokens[finding.finding_id] = token
        return tokens

    async def _audit_and_persist(
        self,
        conn: Any,
        *,
        understanding_id: str,
        understanding_revision: int,
        room_id: str,
        itinerary: ItineraryRevision,
        profile: CalendarProfile,
        result: UserFacingTripResult,
        bindings: dict[str, dict[str, Any]],
        previous_report_id: str | None,
        previous_snapshot_id: str | None,
        basis: dict[str, Any],
        now: datetime,
    ) -> tuple[EvidenceSnapshot, AuditReport, dict[str, str]]:
        (
            snapshot,
            candidate_set_sha256,
            receipt_set_sha256,
            binding_complete,
        ) = await self._collect_g03_evidence(
            conn,
            understanding_id=understanding_id,
            understanding_revision=understanding_revision,
            itinerary=itinerary,
            result=result,
            bindings=bindings,
            now=now,
            supersedes_snapshot_id=previous_snapshot_id,
        )
        report = run_g03_audit(
            revision=itinerary,
            profile=profile,
            room_id=room_id,
            snapshot=snapshot,
            supersedes_report_id=previous_report_id,
            now=now,
        )
        stored = await PostgresAuditRepository(self._pool).save_audit_bundle(
            snapshot,
            report,
            basis=basis,
            conn=conn,
        )
        await conn.execute(
            """
            INSERT INTO trip_g03_evidence_bindings (
                snapshot_id, candidate_set_sha256, receipt_set_sha256,
                receipt_binding_complete, created_at
            ) VALUES ($1, $2, $3, $4, $5)
            """,
            snapshot.snapshot_id,
            candidate_set_sha256,
            receipt_set_sha256,
            binding_complete,
            now,
        )
        tokens = await self._persist_check_tokens(conn, stored)
        return snapshot, stored, tokens

    @staticmethod
    async def _read_checks_with_conn(
        conn: Any,
        *,
        understanding_id: str,
        expected_understanding_revision: int | None = None,
    ) -> PublicTripChecksView:
        pointer = await conn.fetchrow(
            """
            SELECT mt.current_understanding_revision, tw.current_report_id AS audit_report_id,
                   ar.evidence_snapshot_id
            FROM trip_materialized_trips mt
            JOIN trip_workspaces tw ON tw.workspace_id = mt.workspace_id
            JOIN audit_reports ar ON ar.report_id = tw.current_report_id
            WHERE mt.understanding_id = $1
            """,
            understanding_id,
        )
        if pointer is None:
            raise ResourceNotReadyError("trip has not been materialized")
        if (
            expected_understanding_revision is not None
            and pointer["current_understanding_revision"]
            != expected_understanding_revision
        ):
            raise ResourceNotReadyError("trip checks need to be refreshed")
        audit_repository = PostgresAuditRepository()
        report = await audit_repository.get_report_with_connection(
            conn, pointer["audit_report_id"]
        )
        snapshot = await audit_repository.get_snapshot_with_conn(
            conn, pointer["evidence_snapshot_id"]
        )
        if report is None or snapshot is None:
            raise ResourceNotReadyError("trip checks are incomplete")
        token_rows = await conn.fetch(
            """
            SELECT finding_id, check_token
            FROM trip_public_check_tokens
            WHERE report_id = $1
            """,
            report.report_id,
        )
        public_json = await conn.fetchval("SELECT public_json FROM trip_understanding_results WHERE understanding_id=$1 AND revision=$2", understanding_id, pointer["current_understanding_revision"])
        result = UserFacingTripResult.model_validate(_json(public_json)) if public_json else None
        return public_checks(
            report,
            snapshot,
            result=result,
            check_tokens={
                row["finding_id"]: row["check_token"] for row in token_rows
            },
        )

    async def materialize_trip(
        self,
        resource: PublicResourceRecord,
        *,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> MaterializationOutcome:
        scope = f"understanding:{resource.understanding_id}:g03-materialize"
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            current = await self._lock_current_result(conn, resource)
            existing = await self._claim_g03_idempotency(
                conn,
                scope=scope,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                now=now,
            )
            if existing is not None:
                headers = _json(existing["response_headers_json"])
                return MaterializationOutcome(
                    view=MaterializedTripView.model_validate(
                        _json(existing["response_json"])
                    ),
                    opaque_etag=str(headers["ETag"]).strip('"'),
                    replayed=True,
                )
            if not hmac.compare_digest(current["opaque_etag"], expected_etag):
                raise RevisionConflictError(
                    "materialization precondition does not match current result"
                )
            understanding_revision = int(current["current_revision"])
            result = UserFacingTripResult.model_validate(_json(current["public_json"]))
            assumptions = list(_json(current["assumptions_json"]) or [])
            destination = dict(_json(current["destination_json"]) or {})
            city = str(destination.get("name") or "目的地待确认")
            bindings = await self._load_bindings(
                conn, resource.understanding_id, understanding_revision
            )
            pointer = await conn.fetchrow(
                """
                SELECT mt.*, tw.room_id, tw.current_report_id,
                       tw.trip_start_date, tw.trip_end_date
                FROM trip_materialized_trips mt
                JOIN trip_workspaces tw ON tw.workspace_id = mt.workspace_id
                WHERE mt.understanding_id = $1
                FOR UPDATE OF mt, tw
                """,
                resource.understanding_id,
            )
            if (
                pointer is not None
                and int(pointer["current_understanding_revision"])
                == understanding_revision
            ):
                profile = CalendarProfile(
                    mode=pointer["calendar_mode"],
                    start=pointer["trip_start_date"],
                    end=pointer["trip_end_date"],
                    party_size=int(pointer["party_size"]),
                    party_size_source=pointer["party_size_source"],
                )
                row = await conn.fetchrow("SELECT * FROM itinerary_revisions WHERE workspace_id=$1 AND revision=$2",
                    pointer["workspace_id"], pointer["current_itinerary_revision"])
                itinerary = _revision_from_row(row)
                previous_snapshot_id = await conn.fetchval("SELECT evidence_snapshot_id FROM audit_reports WHERE report_id=$1", pointer["current_report_id"])
                await self._audit_and_persist(conn, understanding_id=resource.understanding_id,
                    understanding_revision=understanding_revision, room_id=pointer["room_id"],
                    itinerary=itinerary, profile=profile, result=result, bindings=bindings,
                    previous_report_id=pointer["current_report_id"], previous_snapshot_id=previous_snapshot_id,
                    basis={"current_itinerary_revision": itinerary.revision, "current_task_spec_revision": 1,
                        "current_member_constraint_revision": None, "current_report_id": pointer["current_report_id"]}, now=now)
                await conn.execute("UPDATE trip_change_previews SET status='STALE' WHERE understanding_id=$1 AND status='PROPOSED'", resource.understanding_id)
                view = _materialized_view(profile)
                await self._complete_g03_idempotency(
                    conn,
                    scope=scope,
                    idempotency_key=idempotency_key,
                    response_json=view.model_dump(mode="json"),
                    response_headers={"ETag": f'"{current["opaque_etag"]}"'},
                    now=now,
                )
                return MaterializationOutcome(
                    view=view,
                    opaque_etag=current["opaque_etag"],
                )
            if pointer is None:
                room_id = f"g03-room-{uuid4()}"
                workspace_id = f"g03-workspace-{uuid4()}"
                itinerary_id = f"g03-itinerary-{uuid4()}"
                itinerary_revision = 1
                parent_revision = None
                previous_report_id = None
                previous_snapshot_id = None
                await conn.execute(
                    """
                    INSERT INTO rooms (
                        room_id, thread_id, trip_city, trip_days, phase,
                        created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, 'planned', $5, $5)
                    """,
                    room_id,
                    f"internal:{workspace_id}",
                    city,
                    len(result.days),
                    now,
                )
            else:
                room_id = pointer["room_id"]
                workspace_id = pointer["workspace_id"]
                itinerary_id = pointer["itinerary_id"]
                itinerary_revision = int(pointer["current_itinerary_revision"]) + 1
                parent_revision = int(pointer["current_itinerary_revision"])
                previous_report_id = pointer["current_report_id"]
                previous_snapshot_id = await conn.fetchval(
                    """
                    SELECT evidence_snapshot_id FROM audit_reports WHERE report_id=$1
                    """,
                    pointer["current_report_id"],
                )

            itinerary, profile = build_itinerary_revision(
                result=result,
                bindings=bindings,
                assumptions=assumptions,
                city=city,
                workspace_id=workspace_id,
                itinerary_id=itinerary_id,
                revision=itinerary_revision,
                parent_revision=parent_revision,
                source_type=(
                    RevisionSource.IMPORT
                    if itinerary_revision == 1
                    else RevisionSource.MANUAL
                ),
                created_at=now,
            )
            if pointer is None:
                await conn.execute(
                    """
                    INSERT INTO trip_workspaces (
                        workspace_id, room_id, city, trip_start_date, trip_end_date,
                        current_itinerary_revision, current_task_spec_revision,
                        current_member_constraint_revision, current_report_id,
                        status, created_by, created_at, updated_at, calendar_mode,
                        party_size, party_size_source
                    ) VALUES (
                        $1, $2, $3, $4, $5, NULL, 1, NULL, NULL,
                        'DRAFT', $6, $7, $7, $8, $9, $10
                    )
                    """,
                    workspace_id,
                    room_id,
                    itinerary.city,
                    profile.start,
                    profile.end,
                    G03_SYSTEM_USER_ID,
                    now,
                    profile.mode,
                    profile.party_size,
                    profile.party_size_source,
                )
            await self._insert_itinerary_revision(conn, itinerary, profile)
            plan_ref_id = await self._ensure_itinerary_plan_ref(
                conn,
                understanding_id=resource.understanding_id,
                itinerary=itinerary,
                now=now,
            )
            basis = {
                "current_itinerary_revision": itinerary.revision,
                "current_task_spec_revision": 1,
                "current_member_constraint_revision": None,
                "current_report_id": previous_report_id,
            }
            await conn.execute(
                """
                UPDATE trip_workspaces
                SET current_itinerary_revision = $2,
                    current_task_spec_revision = 1,
                    current_report_id = $3,
                    current_plan_ref_id = $4,
                    city = $5, trip_start_date = $6, trip_end_date = $7,
                    calendar_mode = $8, party_size = $9,
                    party_size_source = $10, status = 'AUDITING',
                    updated_at = $11
                WHERE workspace_id = $1
                """,
                workspace_id,
                itinerary.revision,
                previous_report_id,
                plan_ref_id,
                itinerary.city,
                profile.start,
                profile.end,
                profile.mode,
                profile.party_size,
                profile.party_size_source,
                now,
            )
            snapshot, report, _tokens = await self._audit_and_persist(
                conn,
                understanding_id=resource.understanding_id,
                understanding_revision=understanding_revision,
                room_id=room_id,
                itinerary=itinerary,
                profile=profile,
                result=result,
                bindings=bindings,
                previous_report_id=previous_report_id,
                previous_snapshot_id=previous_snapshot_id,
                basis=basis,
                now=now,
            )
            public_projection = result.model_dump(mode="json")
            stay_anchor = await self._stay_anchor(
                conn, resource.understanding_id, understanding_revision
            )
            await conn.execute(
                """
                INSERT INTO trip_materialization_lineage (
                    lineage_id, understanding_id, understanding_revision,
                    workspace_id, itinerary_id, itinerary_revision, plan_ref_id,
                    evidence_snapshot_id, audit_report_id, calendar_mode,
                    party_size, party_size_source, stay_anchor_json,
                    public_projection_json, source_result_sha256,
                    postcheck_complete, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13::jsonb, $14::jsonb, $15, TRUE, $16
                )
                """,
                str(uuid4()),
                resource.understanding_id,
                understanding_revision,
                workspace_id,
                itinerary_id,
                itinerary.revision,
                plan_ref_id,
                snapshot.snapshot_id,
                report.report_id,
                profile.mode,
                profile.party_size,
                profile.party_size_source,
                json.dumps(stay_anchor, ensure_ascii=False),
                json.dumps(public_projection, ensure_ascii=False),
                current["public_sha256"].strip(),
                now,
            )
            await conn.execute(
                """
                INSERT INTO trip_materialized_trips (
                    understanding_id, workspace_id, itinerary_id,
                    current_understanding_revision, current_itinerary_revision,
                    current_plan_ref_id, calendar_mode, party_size,
                    party_size_source, public_projection_json,
                    current_opaque_etag, created_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9,
                    $10::jsonb, $11, $12, $12
                )
                ON CONFLICT (understanding_id) DO UPDATE
                SET current_understanding_revision = EXCLUDED.current_understanding_revision,
                    current_itinerary_revision = EXCLUDED.current_itinerary_revision,
                    current_plan_ref_id = EXCLUDED.current_plan_ref_id,
                    calendar_mode = EXCLUDED.calendar_mode,
                    party_size = EXCLUDED.party_size,
                    party_size_source = EXCLUDED.party_size_source,
                    public_projection_json = EXCLUDED.public_projection_json,
                    current_opaque_etag = EXCLUDED.current_opaque_etag,
                    updated_at = EXCLUDED.updated_at
                """,
                resource.understanding_id,
                workspace_id,
                itinerary_id,
                understanding_revision,
                itinerary.revision,
                plan_ref_id,
                profile.mode,
                profile.party_size,
                profile.party_size_source,
                json.dumps(public_projection, ensure_ascii=False),
                current["opaque_etag"],
                now,
            )
            view = _materialized_view(profile)
            await self._complete_g03_idempotency(
                conn,
                scope=scope,
                idempotency_key=idempotency_key,
                response_json=view.model_dump(mode="json"),
                response_headers={"ETag": f'"{current["opaque_etag"]}"'},
                now=now,
            )
        return MaterializationOutcome(
            view=view,
            opaque_etag=current["opaque_etag"],
        )

    async def get_trip_checks(
        self,
        resource: PublicResourceRecord,
    ) -> PublicTripChecksView:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            aggregate = await conn.fetchrow(
                """
                SELECT public_resource_id, state, current_revision
                FROM trip_understandings WHERE understanding_id = $1
                """,
                resource.understanding_id,
            )
            if aggregate is None:
                raise ResourceNotFoundError("trip resource does not exist")
            if aggregate["state"] == "DELETED":
                raise ResourceGoneError("trip resource is no longer available")
            if aggregate["public_resource_id"] != resource.public_resource_id:
                raise ResourceAccessDeniedError("trip resource binding changed")
            return await self._read_checks_with_conn(
                conn,
                understanding_id=resource.understanding_id,
                expected_understanding_revision=int(aggregate["current_revision"]),
            )

    async def preview_trip_change(
        self,
        resource: PublicResourceRecord,
        *,
        check_token: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> ChangePreviewOutcome:
        scope = f"understanding:{resource.understanding_id}:g03-preview"
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            current = await self._lock_current_result(conn, resource)
            existing = await self._claim_g03_idempotency(
                conn,
                scope=scope,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                now=now,
            )
            if existing is not None:
                return ChangePreviewOutcome(
                    preview=_json(existing["response_json"]),
                    replayed=True,
                )
            pointer = await conn.fetchrow(
                """
                SELECT mt.current_understanding_revision,
                       mt.current_itinerary_revision, tw.current_report_id AS audit_report_id
                FROM trip_materialized_trips mt
                JOIN trip_workspaces tw ON tw.workspace_id = mt.workspace_id
                WHERE mt.understanding_id = $1
                """,
                resource.understanding_id,
            )
            token_row = await conn.fetchrow(
                """
                SELECT t.finding_id, t.report_id
                FROM trip_public_check_tokens t
                WHERE t.check_token = $1
                """,
                check_token,
            )
            if (
                pointer is None
                or pointer["current_understanding_revision"]
                != current["current_revision"]
                or token_row is None
                or token_row["report_id"] != pointer["audit_report_id"]
            ):
                raise ResourceNotReadyError("this check is no longer current")
            report = await PostgresAuditRepository(self._pool).get_report_with_connection(
                conn, pointer["audit_report_id"]
            )
            finding = next(
                (
                    item
                    for item in (report.findings if report else [])
                    if item.finding_id == token_row["finding_id"]
                ),
                None,
            )
            if finding is None or not finding.repairable:
                raise ResourceNotReadyError("this check needs a manual decision")
            change_token = secrets.token_urlsafe(24)
            result = UserFacingTripResult.model_validate(_json(current["public_json"]))
            command = command_for_finding(finding, result)
            preview = preview_for_finding(finding, change_token=change_token, result=result)
            await conn.execute(
                """
                INSERT INTO trip_change_previews (
                    preview_id, change_token, understanding_id,
                    base_understanding_revision, base_itinerary_revision,
                    source_report_id, targeted_finding_id, command_json,
                    public_preview_json, status, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8::jsonb,
                    $9::jsonb, 'PROPOSED', $10
                )
                """,
                str(uuid4()),
                change_token,
                resource.understanding_id,
                pointer["current_understanding_revision"],
                pointer["current_itinerary_revision"],
                pointer["audit_report_id"],
                finding.finding_id,
                json.dumps(command.model_dump(mode="json"), ensure_ascii=False),
                json.dumps(preview.model_dump(mode="json"), ensure_ascii=False),
                now,
            )
            await self._complete_g03_idempotency(
                conn,
                scope=scope,
                idempotency_key=idempotency_key,
                response_json=preview.model_dump(mode="json"),
                response_headers={},
                now=now,
            )
        return ChangePreviewOutcome(preview=preview)

    async def _persist_understanding_mutation(
        self,
        conn: Any,
        *,
        resource: PublicResourceRecord,
        current: Any,
        command: Any,
        request_hash: str,
        now: datetime,
    ) -> tuple[UserFacingTripResult, str, int, list[str]]:
        current_result = UserFacingTripResult.model_validate(_json(current["public_json"]))
        mutation = apply_public_command(current_result, command)
        public_payload = mutation.result.model_dump(mode="json")
        public_hash = canonical_sha256(public_payload)
        parent_revision = int(current["current_revision"])
        result_revision = parent_revision + 1
        terminal_state = (
            "READY" if mutation.result.status == "READY" else "PARTIAL"
        )
        destination = _json(current["destination_json"])
        assumptions = list(_json(current["assumptions_json"]) or [])
        await conn.execute(
            """
            INSERT INTO trip_understanding_revisions (
                understanding_id, revision, parent_revision, source_id, status,
                content_hash, destination_json, assumptions_json, proposal_json,
                inference_binding_json, compiler_receipt_json, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb,
                $10::jsonb, $11::jsonb, $12
            )
            """,
            resource.understanding_id,
            result_revision,
            parent_revision,
            current["source_id"],
            terminal_state,
            canonical_sha256(
                {
                    "parent_revision": parent_revision,
                    "command_hash": request_hash,
                    "public_hash": public_hash,
                }
            ),
            json.dumps(destination, ensure_ascii=False),
            json.dumps(assumptions, ensure_ascii=False),
            json.dumps(
                {
                    "kind": "USER_ADOPTED_CHANGE",
                    "command_type": command.command_type,
                    "source_quotes": "PARENT_REVISION_ONLY",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {"provider_calls": 0, "route_provider_calls": 0},
                ensure_ascii=False,
            ),
            json.dumps(
                {"kind": "USER_ADOPTED_CHANGE", "source_claims_copied": 0},
                ensure_ascii=False,
            ),
            now,
        )
        current_activities = await conn.fetch(
            """
            SELECT * FROM trip_understanding_activities
            WHERE understanding_id = $1 AND revision = $2
            ORDER BY day_index NULLS LAST, sequence_index, activity_id
            """,
            resource.understanding_id,
            parent_revision,
        )
        old_by_token = {
            row["public_activity_token"]: row for row in current_activities
        }
        old_token_by_new = {new: old for old, new in mutation.token_map.items()}
        for day_index, day in enumerate(mutation.result.days, start=1):
            for sequence_index, card in enumerate(day.activities):
                old_token = old_token_by_new.get(card.activity_token)
                old = old_by_token.get(old_token) if old_token else None
                preserve = old is not None
                receipt = (
                    _json(old["resolver_receipt_json"])
                    if preserve
                    else {
                        "status": "USER_SUGGESTION_NEEDS_CONFIRMATION",
                        "category": card.category,
                        "area_or_address": card.area_or_address,
                        "external_calls": 0,
                    }
                )
                await conn.execute(
                    """
                    INSERT INTO trip_understanding_activities (
                        activity_id, understanding_id, revision,
                        public_activity_token, day_index, sequence_index, role,
                        mention_text, atomic_place_name, category_hint, time_hint,
                        eligible_for_place_search, resolution_status,
                        canonical_place_id, resolver_receipt_json, created_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, 'PLANNED', $7, $7, $8, $9,
                        $10, $11, $12, $13::jsonb, $14
                    )
                    """,
                    str(uuid4()),
                    resource.understanding_id,
                    result_revision,
                    card.activity_token,
                    day_index,
                    sequence_index,
                    card.name,
                    card.category,
                    card.time_hint,
                    bool(old["eligible_for_place_search"]) if preserve else False,
                    old["resolution_status"] if preserve else "NEEDS_CONFIRMATION",
                    old["canonical_place_id"] if preserve else None,
                    json.dumps(receipt, ensure_ascii=False),
                    now,
                )
        for old in current_activities:
            if old["role"] == "PLANNED":
                continue
            await conn.execute(
                """
                INSERT INTO trip_understanding_activities (
                    activity_id, understanding_id, revision, public_activity_token,
                    day_index, sequence_index, role, mention_text,
                    atomic_place_name, category_hint, time_hint,
                    eligible_for_place_search, resolution_status,
                    canonical_place_id, resolver_receipt_json, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                    $12, $13, $14, $15::jsonb, $16
                )
                """,
                str(uuid4()),
                resource.understanding_id,
                result_revision,
                secrets.token_urlsafe(24),
                old["day_index"],
                old["sequence_index"],
                old["role"],
                old["mention_text"],
                old["atomic_place_name"],
                old["category_hint"],
                old["time_hint"],
                old["eligible_for_place_search"],
                old["resolution_status"],
                old["canonical_place_id"],
                json.dumps(_json(old["resolver_receipt_json"]), ensure_ascii=False),
                now,
            )
        result_id = str(uuid4())
        opaque_etag = f"tu3_{secrets.token_urlsafe(32)}"
        await conn.execute(
            """
            INSERT INTO trip_understanding_results (
                result_id, understanding_id, revision, public_json,
                public_sha256, opaque_etag, created_at
            ) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
            """,
            result_id,
            resource.understanding_id,
            result_revision,
            json.dumps(public_payload, ensure_ascii=False),
            public_hash,
            opaque_etag,
            now,
        )
        await conn.execute(
            """
            UPDATE trip_understandings
            SET state = $2, current_revision = $3, result_revision = $3,
                current_result_id = $4, updated_at = $5
            WHERE understanding_id = $1
            """,
            resource.understanding_id,
            terminal_state,
            result_revision,
            result_id,
            now,
        )
        await self._copy_stay_selection_to_revision(
            conn,
            resource.understanding_id,
            parent_revision,
            result_revision,
            now=now,
        )
        return mutation.result, opaque_etag, result_revision, mutation.changed_days

    async def adopt_trip_change(
        self,
        resource: PublicResourceRecord,
        *,
        change_token: str,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> ChangeAdoptOutcome:
        scope = f"understanding:{resource.understanding_id}:g03-adopt"
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            current = await self._lock_current_result(conn, resource)
            existing = await self._claim_g03_idempotency(
                conn,
                scope=scope,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                now=now,
            )
            if existing is not None:
                headers = _json(existing["response_headers_json"])
                return ChangeAdoptOutcome(
                    adopted=PublicChangeAdopted.model_validate(
                        _json(existing["response_json"])
                    ),
                    opaque_etag=str(headers["ETag"]).strip('"'),
                    replayed=True,
                )
            if not hmac.compare_digest(current["opaque_etag"], expected_etag):
                raise RevisionConflictError(
                    "change precondition does not match current result"
                )
            preview = await conn.fetchrow(
                """
                SELECT p.*, mt.workspace_id, mt.itinerary_id,
                       mt.current_understanding_revision,
                       mt.current_itinerary_revision, tw.room_id,
                       tw.current_report_id
                FROM trip_change_previews p
                JOIN trip_materialized_trips mt
                  ON mt.understanding_id = p.understanding_id
                JOIN trip_workspaces tw ON tw.workspace_id = mt.workspace_id
                WHERE p.change_token = $1 AND p.understanding_id = $2
                FOR UPDATE OF p, mt, tw
                """,
                change_token,
                resource.understanding_id,
            )
            if (
                preview is None
                or preview["status"] != "PROPOSED"
            or preview["created_at"] + timedelta(minutes=15) <= now
                or preview["base_understanding_revision"]
                != current["current_revision"]
                or preview["base_understanding_revision"]
                != preview["current_understanding_revision"]
                or preview["base_itinerary_revision"]
                != preview["current_itinerary_revision"]
                or preview["source_report_id"] != preview["current_report_id"]
            ):
                raise ResourceNotReadyError("this change preview is no longer current")
            command = _COMMAND_ADAPTER.validate_python(_json(preview["command_json"]))
            (
                next_result,
                opaque_etag,
                next_understanding_revision,
                changed_days,
            ) = await self._persist_understanding_mutation(
                conn,
                resource=resource,
                current=current,
                command=command,
                request_hash=request_hash,
                now=now,
            )
            next_bindings = await self._load_bindings(
                conn, resource.understanding_id, next_understanding_revision
            )
            assumptions = list(_json(current["assumptions_json"]) or [])
            destination = dict(_json(current["destination_json"]) or {})
            next_itinerary_revision = int(preview["current_itinerary_revision"]) + 1
            itinerary, profile = build_itinerary_revision(
                result=next_result,
                bindings=next_bindings,
                assumptions=assumptions,
                city=str(destination.get("name") or "目的地待确认"),
                workspace_id=preview["workspace_id"],
                itinerary_id=preview["itinerary_id"],
                revision=next_itinerary_revision,
                parent_revision=int(preview["current_itinerary_revision"]),
                source_type=RevisionSource.REPAIR,
                created_at=now,
            )
            await self._insert_itinerary_revision(conn, itinerary, profile)
            plan_ref_id = await self._ensure_itinerary_plan_ref(
                conn,
                understanding_id=resource.understanding_id,
                itinerary=itinerary,
                now=now,
            )
            previous_snapshot_id = await conn.fetchval(
                """
                SELECT evidence_snapshot_id FROM audit_reports WHERE report_id=$1
                """,
                preview["current_report_id"],
            )
            basis = {
                "current_itinerary_revision": itinerary.revision,
                "current_task_spec_revision": 1,
                "current_member_constraint_revision": None,
                "current_report_id": preview["current_report_id"],
            }
            await conn.execute(
                """
                UPDATE trip_workspaces
                SET current_itinerary_revision = $2,
                    current_plan_ref_id = $3,
                    current_report_id = $4,
                    trip_start_date = $5, trip_end_date = $6,
                    calendar_mode = $7, party_size = $8,
                    party_size_source = $9, status = 'AUDITING',
                    updated_at = $10
                WHERE workspace_id = $1
                """,
                preview["workspace_id"],
                itinerary.revision,
                plan_ref_id,
                preview["current_report_id"],
                profile.start,
                profile.end,
                profile.mode,
                profile.party_size,
                profile.party_size_source,
                now,
            )
            snapshot, report, _tokens = await self._audit_and_persist(
                conn,
                understanding_id=resource.understanding_id,
                understanding_revision=next_understanding_revision,
                room_id=preview["room_id"],
                itinerary=itinerary,
                profile=profile,
                result=next_result,
                bindings=next_bindings,
                previous_report_id=preview["current_report_id"],
                previous_snapshot_id=previous_snapshot_id,
                basis=basis,
                now=now,
            )
            public_projection = next_result.model_dump(mode="json")
            stay_anchor = await self._stay_anchor(
                conn, resource.understanding_id, next_understanding_revision
            )
            await conn.execute(
                """
                INSERT INTO trip_materialization_lineage (
                    lineage_id, understanding_id, understanding_revision,
                    workspace_id, itinerary_id, itinerary_revision, plan_ref_id,
                    evidence_snapshot_id, audit_report_id, calendar_mode,
                    party_size, party_size_source, stay_anchor_json,
                    public_projection_json, source_result_sha256,
                    postcheck_complete, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13::jsonb, $14::jsonb, $15, TRUE, $16
                )
                """,
                str(uuid4()),
                resource.understanding_id,
                next_understanding_revision,
                preview["workspace_id"],
                preview["itinerary_id"],
                itinerary.revision,
                plan_ref_id,
                snapshot.snapshot_id,
                report.report_id,
                profile.mode,
                profile.party_size,
                profile.party_size_source,
                json.dumps(stay_anchor, ensure_ascii=False),
                json.dumps(public_projection, ensure_ascii=False),
                canonical_sha256(public_projection),
                now,
            )
            await conn.execute(
                """
                UPDATE trip_materialized_trips
                SET current_understanding_revision = $2,
                    current_itinerary_revision = $3,
                    current_plan_ref_id = $4, calendar_mode = $5,
                    party_size = $6, party_size_source = $7,
                    public_projection_json = $8::jsonb,
                    current_opaque_etag = $9, updated_at = $10
                WHERE understanding_id = $1
                """,
                resource.understanding_id,
                next_understanding_revision,
                itinerary.revision,
                plan_ref_id,
                profile.mode,
                profile.party_size,
                profile.party_size_source,
                json.dumps(public_projection, ensure_ascii=False),
                opaque_etag,
                now,
            )
            await conn.execute(
                """
                UPDATE trip_change_previews
                SET status = CASE
                    WHEN change_token = $2 THEN 'APPLIED'
                    ELSE 'STALE'
                END,
                applied_at = CASE
                    WHEN change_token = $2 THEN $3::timestamptz
                    ELSE NULL::timestamptz
                END
                WHERE understanding_id = $1
                  AND base_understanding_revision = $4
                  AND status = 'PROPOSED'
                """,
                resource.understanding_id,
                change_token,
                now,
                preview["base_understanding_revision"],
            )
            checks = await self._read_checks_with_conn(
                conn,
                understanding_id=resource.understanding_id,
                expected_understanding_revision=next_understanding_revision,
            )
            adopted = PublicChangeAdopted(
                status=(
                    "STILL_NEEDS_CONFIRMATION"
                    if checks.status == "STILL_NEEDS_CONFIRMATION"
                    else "APPLIED"
                ),
                message=(
                    "改动已保存，完整复核后仍有内容需要确认"
                    if checks.status == "STILL_NEEDS_CONFIRMATION"
                    else "改动已保存并完成复核"
                ),
                changed_days=changed_days,
                checks=checks,
            )
            await self._complete_g03_idempotency(
                conn,
                scope=scope,
                idempotency_key=idempotency_key,
                response_json=adopted.model_dump(mode="json"),
                response_headers={"ETag": f'"{opaque_etag}"'},
                now=now,
            )
        return ChangeAdoptOutcome(
            adopted=adopted,
            opaque_etag=opaque_etag,
        )


class InMemoryG03RepositoryMixin:
    """Deterministic test/runtime twin for the public G03 mainline."""

    def _init_g03_store(self) -> None:
        self.g03_materialized: dict[str, dict[str, Any]] = {}
        self.g03_history: dict[str, list[dict[str, Any]]] = {}
        self.g03_previews: dict[str, dict[str, Any]] = {}
        self.g03_idempotency: dict[tuple[str, str], tuple[str, Any]] = {}
        self.g03_pipeline_inputs: dict[tuple[str, int], dict[str, Any]] = {}

    def _memory_g03_replay(
        self,
        *,
        scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> Any | None:
        existing = self.g03_idempotency.get((scope, _sha256_text(idempotency_key)))
        if existing is None:
            return None
        if existing[0] != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was already used with another request"
            )
        return existing[1]

    def _remember_g03_outcome(
        self,
        *,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        outcome: Any,
    ) -> None:
        self.g03_idempotency[(scope, _sha256_text(idempotency_key))] = (
            request_hash,
            outcome,
        )

    def _memory_g03_current(
        self,
        resource: PublicResourceRecord,
    ) -> tuple[dict[str, Any], Any]:
        public_id = self.resources_by_understanding.get(resource.understanding_id)
        if public_id is None:
            raise ResourceNotFoundError("trip resource does not exist")
        if public_id != resource.public_resource_id:
            raise ResourceAccessDeniedError("trip resource binding changed")
        aggregate = self.resources[public_id]
        if aggregate["state"] == "DELETED":
            raise ResourceGoneError("trip resource is no longer available")
        stored = self.results.get(aggregate.get("current_result_id") or "")
        if stored is None:
            raise ResourceNotReadyError("trip cards are not ready")
        return aggregate, stored

    @staticmethod
    def _memory_g03_bindings(result: UserFacingTripResult) -> dict[str, dict[str, Any]]:
        bindings: dict[str, dict[str, Any]] = {}
        for day in result.days:
            for card in day.activities:
                ready = card.status == "READY"
                bindings[card.activity_token] = {
                    "canonical_place_id": (
                        f"fixture:{canonical_sha256(card.name)[:20]}" if ready else None
                    ),
                    "resolution_status": (
                        "AUTO_MATCHED" if ready else "NEEDS_CONFIRMATION"
                    ),
                    "resolver_receipt": {
                        "provider": "controlled-fixture",
                        "status": "MATCHED" if ready else "UNAVAILABLE",
                        "external_calls": 0,
                    },
                }
        return bindings

    def _memory_g03_input(
        self,
        understanding_id: str,
        understanding_revision: int,
        result: UserFacingTripResult,
    ) -> tuple[list[dict[str, Any]], str, dict[str, dict[str, Any]]]:
        stored = self.g03_pipeline_inputs.get(
            (understanding_id, understanding_revision), {}
        )
        assumptions = list(stored.get("assumptions") or [])
        if not assumptions:
            assumptions = [
                {
                    "key": item.key,
                    "value": item.value,
                    "source": "SOFT_ASSUMPTION",
                }
                for item in result.assumptions
            ]
        destination = dict(stored.get("destination") or {})
        city = str(destination.get("name") or "")
        if not city:
            city = next(
                (
                    item.value.removeprefix("暂按 ")
                    for item in result.assumptions
                    if item.key == "destination"
                ),
                "目的地待确认",
            )
        bindings = dict(stored.get("bindings") or {})
        if not bindings:
            bindings = self._memory_g03_bindings(result)
        return assumptions, city, bindings

    def _memory_g03_evidence(
        self,
        *,
        understanding_id: str,
        understanding_revision: int,
        itinerary: ItineraryRevision,
        bindings: dict[str, dict[str, Any]],
        now: datetime,
        supersedes_snapshot_id: str | None,
    ) -> EvidenceSnapshot:
        snapshot_id = str(uuid4())
        facts: list[EvidenceFact] = []
        failures: list[ProviderFailure] = []
        for day in itinerary.days:
            for stop in day.stops:
                binding = next(
                    (
                        item
                        for token, item in bindings.items()
                        if _stable_stop_id(token) == stop.stop_id
                    ),
                    {},
                )
                resolved = binding.get("resolution_status") == "AUTO_MATCHED"
                receipt = dict(binding.get("resolver_receipt") or {})
                facts.append(
                    EvidenceFact(
                        fact_id=str(uuid4()),
                        snapshot_id=snapshot_id,
                        subject_type="PLACE",
                        subject_id=stop.place_id,
                        fact_type="POI_IDENTITY",
                        value={"resolved": resolved, "city": itinerary.city},
                        provider=str(receipt.get("provider") or "controlled-fixture"),
                        observed_at=now,
                        response_hash=canonical_sha256(
                            receipt or {"status": "UNAVAILABLE"}
                        ),
                        confidence=1.0 if resolved or stop.category == "meal_break" else 0.0,
                        freshness_status=(
                            EvidenceFreshness.FRESH
                            if resolved or stop.category == "meal_break"
                            else EvidenceFreshness.UNAVAILABLE
                        ),
                    )
                )

        output = next(
            (
                self.map_snapshots.get(item["map_job_id"])
                for item in self.map_jobs.values()
                if item["understanding_id"] == understanding_id
                and item["plan"].plan_ref.revision == understanding_revision
                and self.map_snapshots.get(item["map_job_id"]) is not None
            ),
            None,
        )
        if output is None:
            failures.append(
                ProviderFailure(
                    provider="route",
                    error_category="CURRENT_ROUTE_NOT_RENDERED",
                    retryable=True,
                )
            )
        else:
            route_pairs = _route_stop_pairs(self._memory_plan(understanding_id, understanding_revision), itinerary)
            for edge in output.edges:
                pair = route_pairs.get((edge.day_index, edge.sequence_index))
                if pair is None:
                    continue
                left, right = pair
                selected = (
                    edge.walking if edge.selected_mode == "walking" else edge.transit
                )
                facts.append(
                    EvidenceFact(
                        fact_id=str(uuid4()),
                        snapshot_id=snapshot_id,
                        subject_type="ROUTE_EDGE",
                        subject_id=f"{left.stop_id}->{right.stop_id}",
                        fact_type="ROUTE_MODE_SET",
                        value={
                            "walking": edge.walking.status,
                            "transit": edge.transit.status,
                            "selected_mode": edge.selected_mode,
                            "selected_duration_minutes": (
                                selected.duration_minutes if edge.selected_mode else None
                            ),
                        },
                        provider="route",
                        observed_at=output.observed_at,
                        response_hash=canonical_sha256(
                            [edge.walking.response_hash, edge.transit.response_hash]
                        ),
                        confidence=1.0 if edge.selected_mode else 0.0,
                        freshness_status=(
                            EvidenceFreshness.FRESH
                            if edge.selected_mode
                            else EvidenceFreshness.UNAVAILABLE
                        ),
                    )
                )
            if output.status != "READY":
                failures.append(
                    ProviderFailure(
                        provider="route",
                        error_category="PARTIAL_ROUTE_RESULT",
                        retryable=True,
                    )
                )

        selection = self.stay_selections.get(
            (understanding_id, understanding_revision)
        )
        if selection is not None:
            view = selection["view"]
            facts.append(
                EvidenceFact(
                    fact_id=str(uuid4()),
                    snapshot_id=snapshot_id,
                    subject_type="STAY",
                    subject_id=itinerary.workspace_id,
                    fact_type="STAY_COMMUTE",
                    value={
                        "max_single_leg_minutes": view.max_single_leg_minutes,
                        "transfer_count": view.transfer_count,
                    },
                    provider="route",
                    observed_at=now,
                    response_hash=canonical_sha256(view.model_dump(mode="json")),
                    confidence=1.0,
                    freshness_status=EvidenceFreshness.FRESH,
                )
            )
        return EvidenceSnapshot(
            snapshot_id=snapshot_id,
            workspace_id=itinerary.workspace_id,
            itinerary_revision=itinerary.revision,
            provider_set=sorted(
                {fact.provider for fact in facts}
                | {failure.provider for failure in failures}
            ),
            policy_version=G03_EVIDENCE_POLICY_VERSION,
            facts=facts,
            provider_failures=failures,
            created_at=now,
            supersedes_snapshot_id=supersedes_snapshot_id,
        )

    def _build_memory_g03_state(
        self,
        *,
        resource: PublicResourceRecord,
        aggregate: dict[str, Any],
        result: UserFacingTripResult,
        opaque_etag: str,
        now: datetime,
        source_type: RevisionSource,
        reuse_itinerary: bool = False,
    ) -> dict[str, Any]:
        understanding_revision = int(aggregate["current_revision"])
        previous = self.g03_materialized.get(resource.understanding_id)
        assumptions, city, bindings = self._memory_g03_input(
            resource.understanding_id, understanding_revision, result
        )
        workspace_id = (
            previous["itinerary"].workspace_id
            if previous
            else f"g03-workspace-{uuid4()}"
        )
        itinerary_id = (
            previous["itinerary"].itinerary_id
            if previous
            else f"g03-itinerary-{uuid4()}"
        )
        itinerary_revision = previous["itinerary"].revision + 1 if previous else 1
        itinerary, profile = build_itinerary_revision(
            result=result,
            bindings=bindings,
            assumptions=assumptions,
            city=city,
            workspace_id=workspace_id,
            itinerary_id=itinerary_id,
            revision=itinerary_revision,
            parent_revision=previous["itinerary"].revision if previous else None,
            source_type=source_type,
            created_at=now,
        )
        if reuse_itinerary and previous:
            itinerary, profile = previous["itinerary"], previous["profile"]
        snapshot = self._memory_g03_evidence(
            understanding_id=resource.understanding_id,
            understanding_revision=understanding_revision,
            itinerary=itinerary,
            bindings=bindings,
            now=now,
            supersedes_snapshot_id=(
                previous["snapshot"].snapshot_id if previous else None
            ),
        )
        report = run_g03_audit(
            revision=itinerary,
            profile=profile,
            room_id=f"g03-room:{resource.understanding_id}",
            snapshot=snapshot,
            supersedes_report_id=previous["report"].report_id if previous else None,
            now=now,
        )
        tokens = {
            finding.finding_id: secrets.token_urlsafe(24)
            for finding in report.findings
            if finding.status.value in {"VIOLATED", "UNKNOWN"}
        }
        state = {
            "understanding_revision": understanding_revision,
            "opaque_etag": opaque_etag,
            "itinerary": itinerary,
            "profile": profile,
            "snapshot": snapshot,
            "report": report,
            "tokens": tokens,
            "result": result,
        }
        self.g03_materialized[resource.understanding_id] = state
        self.g03_history.setdefault(resource.understanding_id, []).append(state)
        return state

    @staticmethod
    def _memory_checks(state: dict[str, Any]) -> PublicTripChecksView:
        return public_checks(
            state["report"],
            state["snapshot"],
            check_tokens=state["tokens"],
            result=state["result"],
        )

    async def materialize_trip(
        self,
        resource: PublicResourceRecord,
        *,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> MaterializationOutcome:
        scope = f"understanding:{resource.understanding_id}:g03-materialize"
        replay = self._memory_g03_replay(
            scope=scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay.model_copy(update={"replayed": True})
        aggregate, stored = self._memory_g03_current(resource)
        if not hmac.compare_digest(stored.opaque_etag, expected_etag):
            raise RevisionConflictError(
                "materialization precondition does not match current result"
            )
        state = self.g03_materialized.get(resource.understanding_id)
        same_itinerary = state is not None and state["understanding_revision"] == int(aggregate["current_revision"])
        state = self._build_memory_g03_state(
            resource=resource,
            aggregate=aggregate,
            result=stored.result,
            opaque_etag=stored.opaque_etag,
            now=now,
            source_type=RevisionSource.IMPORT,
            reuse_itinerary=same_itinerary,
        )
        for preview in self.g03_previews.values():
            if preview["understanding_id"] == resource.understanding_id and preview["status"] == "PROPOSED":
                preview["status"] = "STALE"
        outcome = MaterializationOutcome(
            view=_materialized_view(state["profile"]),
            opaque_etag=stored.opaque_etag,
        )
        self._remember_g03_outcome(
            scope=scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            outcome=outcome,
        )
        return outcome

    async def get_trip_checks(
        self,
        resource: PublicResourceRecord,
    ) -> PublicTripChecksView:
        aggregate, _stored = self._memory_g03_current(resource)
        state = self.g03_materialized.get(resource.understanding_id)
        if state is None or state["understanding_revision"] != int(
            aggregate["current_revision"]
        ):
            raise ResourceNotReadyError("trip checks need to be refreshed")
        return self._memory_checks(state)

    async def preview_trip_change(
        self,
        resource: PublicResourceRecord,
        *,
        check_token: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> ChangePreviewOutcome:
        scope = f"understanding:{resource.understanding_id}:g03-preview"
        replay = self._memory_g03_replay(
            scope=scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay.model_copy(update={"replayed": True})
        aggregate, _stored = self._memory_g03_current(resource)
        state = self.g03_materialized.get(resource.understanding_id)
        if state is None or state["understanding_revision"] != int(
            aggregate["current_revision"]
        ):
            raise ResourceNotReadyError("this check is no longer current")
        finding_id = next(
            (
                finding_id
                for finding_id, token in state["tokens"].items()
                if token == check_token
            ),
            None,
        )
        finding = next(
            (
                item
                for item in state["report"].findings
                if item.finding_id == finding_id
            ),
            None,
        )
        if finding is None or not finding.repairable:
            raise ResourceNotReadyError("this check needs a manual decision")
        change_token = secrets.token_urlsafe(24)
        command = command_for_finding(finding, state["result"])
        preview = preview_for_finding(finding, change_token=change_token, result=state["result"])
        self.g03_previews[change_token] = {
            "understanding_id": resource.understanding_id,
            "base_understanding_revision": state["understanding_revision"],
            "base_itinerary_revision": state["itinerary"].revision,
            "source_report_id": state["report"].report_id,
            "command": command,
            "status": "PROPOSED",
            "created_at": now,
        }
        outcome = ChangePreviewOutcome(preview=preview)
        self._remember_g03_outcome(
            scope=scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            outcome=outcome,
        )
        return outcome

    async def adopt_trip_change(
        self,
        resource: PublicResourceRecord,
        *,
        change_token: str,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> ChangeAdoptOutcome:
        scope = f"understanding:{resource.understanding_id}:g03-adopt"
        replay = self._memory_g03_replay(
            scope=scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay.model_copy(update={"replayed": True})
        aggregate, stored = self._memory_g03_current(resource)
        if not hmac.compare_digest(stored.opaque_etag, expected_etag):
            raise RevisionConflictError("change precondition does not match current result")
        preview = self.g03_previews.get(change_token)
        state = self.g03_materialized.get(resource.understanding_id)
        if (
            preview is None
            or state is None
            or preview["understanding_id"] != resource.understanding_id
            or preview["status"] != "PROPOSED"
                or preview["created_at"] + timedelta(minutes=15) <= now
            or preview["base_understanding_revision"] != int(aggregate["current_revision"])
            or preview["base_itinerary_revision"] != state["itinerary"].revision
            or preview["source_report_id"] != state["report"].report_id
        ):
            raise ResourceNotReadyError("this change preview is no longer current")
        command_outcome = await self.apply_command(
            resource,
            preview["command"],
            expected_etag=expected_etag,
            idempotency_key=f"g03-command:{change_token}",
            request_hash=canonical_sha256(
                {
                    "command": preview["command"].model_dump(mode="json"),
                    "change_token": change_token,
                }
            ),
            now=now,
        )
        next_aggregate, next_stored = self._memory_g03_current(resource)
        next_state = self._build_memory_g03_state(
            resource=resource,
            aggregate=next_aggregate,
            result=next_stored.result,
            opaque_etag=command_outcome.opaque_etag,
            now=now,
            source_type=RevisionSource.REPAIR,
        )
        for item in self.g03_previews.values():
            if (
                item["understanding_id"] == resource.understanding_id
                and item["base_understanding_revision"]
                == preview["base_understanding_revision"]
                and item["status"] == "PROPOSED"
            ):
                item["status"] = "APPLIED" if item is preview else "STALE"
        checks = self._memory_checks(next_state)
        adopted = PublicChangeAdopted(
            status=(
                "STILL_NEEDS_CONFIRMATION"
                if checks.status == "STILL_NEEDS_CONFIRMATION"
                else "APPLIED"
            ),
            message=(
                "改动已保存，完整复核后仍有内容需要确认"
                if checks.status == "STILL_NEEDS_CONFIRMATION"
                else "改动已保存并完成复核"
            ),
            changed_days=command_outcome.applied.changed_days,
            checks=checks,
        )
        outcome = ChangeAdoptOutcome(
            adopted=adopted,
            opaque_etag=command_outcome.opaque_etag,
        )
        self._remember_g03_outcome(
            scope=scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            outcome=outcome,
        )
        return outcome

    def _delete_g03_memory(self, understanding_id: str) -> None:
        self.g03_materialized.pop(understanding_id, None)
        self.g03_history.pop(understanding_id, None)
        for key in list(self.g03_pipeline_inputs):
            if key[0] == understanding_id:
                self.g03_pipeline_inputs.pop(key, None)
        for token, preview in list(self.g03_previews.items()):
            if preview["understanding_id"] == understanding_id:
                self.g03_previews.pop(token, None)
        prefix = f"understanding:{understanding_id}:g03-"
        for key in list(self.g03_idempotency):
            if key[0].startswith(prefix):
                self.g03_idempotency.pop(key, None)


def _stable_stop_id(activity_token: str) -> str:
    return f"stop_{canonical_sha256(activity_token)[:24]}"
