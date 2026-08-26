"""Immutable, scoped refreshes for the route edges introduced by one revision.

This is intentionally narrower than a general audit refresh.  It calls a
route provider only for edges whose endpoint binding changed between the
current revision and its parent.  Coordinates come exclusively from the
canonical revision/template projection; no place-name geocoding or city-centre
fallback is allowed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import aiohttp
from pydantic import BaseModel, Field

from app.audit.evidence_service import EvidenceObservation
from app.audit.models import AuditReport, EvidenceFreshness, EvidenceSnapshot, ProviderFailure
from app.audit.repositories import AuditRepository
from app.audit.service import AuditApplicationService
from app.config import settings
from app.constraints.geo_routes import RouteResult, fetch_amap_route
from app.itineraries.errors import ResourceNotFound, RevisionConflictError
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.map_projection import build_map_projection
from app.itineraries.models import ItineraryRevision, RevisionTransport
from app.itineraries.repositories import ItineraryRepository
from app.operations.models import CreationCommandResponse, CreationOperation
from app.operations.repositories import CreationCommandRepository
from app.schemas.place import Coordinates


class RouteEdgeRefreshResult(BaseModel):
    """Persisted result returned to the workspace after a scoped refresh."""

    workspace_id: str
    itinerary_revision: int = Field(gt=0)
    source_revision: int | None = Field(default=None, ge=1)
    report: AuditReport
    evidence_snapshot: EvidenceSnapshot
    route_delta: dict[str, Any]
    provider_failures: list[ProviderFailure] = Field(default_factory=list)
    idempotent_replay: bool = False


class RouteEvidenceProvider(Protocol):
    async def fetch(
        self,
        *,
        origin: Coordinates,
        destination: Coordinates,
        mode: str,
        city: str,
    ) -> RouteResult: ...


class AmapRouteEvidenceProvider:
    """The live adapter, safely disabled by the existing Amap configuration."""

    async def fetch(
        self,
        *,
        origin: Coordinates,
        destination: Coordinates,
        mode: str,
        city: str,
    ) -> RouteResult:
        # Avoid allocating a session at all for fixture/demo/missing-key runs.
        # The returned record is still persisted as an explicit unavailable
        # receipt by the service below.
        if settings.amap_mock or settings.demo_mode or not settings.amap_api_key:
            return RouteResult(
                status="unknown",
                source="route_unavailable",
                failure_reason="live_route_disabled",
            )
        async with aiohttp.ClientSession() as session:
            return await fetch_amap_route(session, origin, destination, mode, city)


@dataclass(frozen=True)
class _CurrentEdge:
    public_id: str
    evidence_id: str
    day_index: int
    from_stop_id: str
    to_stop_id: str
    from_place_id: str
    to_place_id: str
    transport: RevisionTransport | None


def _edges(revision: ItineraryRevision) -> dict[str, _CurrentEdge]:
    return {
        f"day:{day.day_index}:edge:{left.stop_id}->{right.stop_id}": _CurrentEdge(
            public_id=f"day:{day.day_index}:edge:{left.stop_id}->{right.stop_id}",
            evidence_id=f"{left.stop_id}->{right.stop_id}",
            day_index=day.day_index,
            from_stop_id=left.stop_id,
            to_stop_id=right.stop_id,
            from_place_id=left.place_id,
            to_place_id=right.place_id,
            transport=left.transport_to_next,
        )
        for day in revision.days
        for left, right in zip(day.stops, day.stops[1:])
    }


def _changed_current_edges(
    parent: ItineraryRevision | None,
    revision: ItineraryRevision,
) -> tuple[list[_CurrentEdge], list[str]]:
    """Return current refreshable edges plus removed-edge IDs for the delta."""

    current = _edges(revision)
    previous = _edges(parent) if parent is not None else {}
    changed_current: list[_CurrentEdge] = []
    for public_id, edge in current.items():
        before = previous.get(public_id)
        if before is None or (
            before.from_place_id,
            before.to_place_id,
        ) != (edge.from_place_id, edge.to_place_id):
            changed_current.append(edge)
    removed = sorted(set(previous) - set(current))
    return sorted(changed_current, key=lambda item: item.public_id), removed


async def _lineage(
    repository: ItineraryRepository,
    revision: ItineraryRevision,
) -> list[ItineraryRevision]:
    lineage = [revision]
    seen = {revision.revision}
    parent_revision = revision.parent_revision
    while parent_revision is not None and parent_revision not in seen:
        parent = await repository.get_revision(revision.workspace_id, parent_revision)
        if parent is None:
            break
        lineage.append(parent)
        seen.add(parent.revision)
        parent_revision = parent.parent_revision
    return lineage


def _failure(provider: str, category: str, detail: str | None) -> ProviderFailure:
    return ProviderFailure(provider=provider, error_category=category, retryable=False, detail=detail)


class ChangedRouteEdgeRefreshService:
    """Create an immutable audit bundle for exactly this revision's new edges."""

    def __init__(
        self,
        *,
        itinerary_repository: ItineraryRepository,
        audit_repository: AuditRepository,
        audit_service: AuditApplicationService | None = None,
        provider: RouteEvidenceProvider | None = None,
    ):
        self.itinerary_repository = itinerary_repository
        self.audit_repository = audit_repository
        self.audit_service = audit_service or AuditApplicationService(
            itinerary_repository=itinerary_repository,
            audit_repository=audit_repository,
        )
        self.provider = provider or AmapRouteEvidenceProvider()

    async def _collect(
        self,
        *,
        revision: ItineraryRevision,
        parent: ItineraryRevision | None,
        now: datetime,
    ) -> tuple[list[EvidenceObservation], list[ProviderFailure], dict[str, Any]]:
        edges, removed_edges = _changed_current_edges(parent, revision)
        projection = build_map_projection(revision, lineage=await _lineage(self.itinerary_repository, revision))
        projected = {item.stop_id: item for item in projection.stops}
        observations: list[EvidenceObservation] = []
        failures: list[ProviderFailure] = []
        refreshed: list[dict[str, Any]] = []
        previous = _edges(parent) if parent is not None else {}

        for edge in edges:
            source = projected.get(edge.from_stop_id)
            destination = projected.get(edge.to_stop_id)
            before = previous.get(edge.public_id)
            previous_minutes = before.transport.duration_minutes if before and before.transport else None
            base = {
                "edge_id": edge.public_id,
                "previous_minutes": previous_minutes,
                "from_place_id": edge.from_place_id,
                "to_place_id": edge.to_place_id,
            }
            if source is None or destination is None:
                reason = "REVISION_STOP_COORDINATES_UNAVAILABLE"
                observations.append(EvidenceObservation(
                    subject_type="ROUTE_EDGE", subject_id=edge.evidence_id, fact_type="ROUTE_TIME",
                    value={**base, "reason_code": reason}, provider="canonical_coordinate_projection",
                    observed_at=now, confidence=0, freshness_status=EvidenceFreshness.UNAVAILABLE,
                ))
                refreshed.append({
                    **base, "current_minutes": None, "freshness": "UNAVAILABLE",
                    "source": "canonical_coordinate_projection", "reason_code": reason,
                })
                continue
            try:
                route = await self.provider.fetch(
                    origin=source.coords, destination=destination.coords,
                    mode=(edge.transport.mode if edge.transport else "driving"), city=revision.city,
                )
            except Exception as exc:  # Provider adapters are boundary code.
                route = RouteResult(
                    status="unknown", source="route_provider", failure_reason=type(exc).__name__,
                    observed_at=now,
                )
            observed_at = route.observed_at or now
            if route.status != "ok" or route.duration_minutes is None:
                reason = route.failure_reason or "ROUTE_EVIDENCE_UNAVAILABLE"
                failures.append(_failure(route.source, "ROUTE_REFRESH_UNAVAILABLE", reason))
                observations.append(EvidenceObservation(
                    subject_type="ROUTE_EDGE", subject_id=edge.evidence_id, fact_type="ROUTE_TIME",
                    value={**base, "reason_code": reason}, provider=route.source,
                    observed_at=observed_at, confidence=0, freshness_status=EvidenceFreshness.UNAVAILABLE,
                ))
                refreshed.append({
                    **base, "current_minutes": None, "freshness": "UNAVAILABLE",
                    "source": route.source, "reason_code": reason,
                })
                continue
            value = {
                **base,
                "mode": edge.transport.mode if edge.transport else "driving",
                "duration_minutes": route.duration_minutes,
                "distance_km": route.distance_km,
                "distance_meters": round(route.distance_km * 1000) if route.distance_km is not None else None,
                "transfer_count": route.transfer_count,
                "route_response_hash": route.response_hash,
                "projection": {
                    "from_projection_revision": source.projection_revision,
                    "to_projection_revision": destination.projection_revision,
                    "from_coordinate_role": source.coordinate_role,
                    "to_coordinate_role": destination.coordinate_role,
                },
            }
            observations.append(EvidenceObservation(
                subject_type="ROUTE_EDGE", subject_id=edge.evidence_id, fact_type="ROUTE_TIME",
                value=value, provider=route.source, observed_at=observed_at, confidence=1,
            ))
            refreshed.append({
                **base, "current_minutes": route.duration_minutes, "freshness": "FRESH",
                "source": route.source, "reason_code": None,
            })

        unavailable = [item["edge_id"] for item in refreshed if item["freshness"] == "UNAVAILABLE"]
        known = [item for item in refreshed if item["freshness"] == "FRESH"]
        if unavailable:
            status = "PARTIAL" if known else "UNAVAILABLE"
        else:
            status = "AVAILABLE"
        delta = None
        previous_total = current_total = None
        if not unavailable:
            previous_total = sum(item["previous_minutes"] or 0 for item in refreshed)
            current_total = sum(item["current_minutes"] or 0 for item in refreshed)
            delta = current_total - previous_total
        route_delta = {
            "status": status,
            "previous_minutes": previous_total,
            "current_minutes": current_total,
            "delta_minutes": delta,
            "changed_edges": refreshed,
            "removed_edge_ids": removed_edges,
            "missing_edge_ids": unavailable,
            "async_route_refresh_required": bool(unavailable),
            "scope": "CURRENT_REVISION_CHANGED_EDGES_ONLY",
        }
        return observations, failures, route_delta

    async def run_idempotent(
        self,
        *,
        workspace_id: str,
        itinerary_revision: int,
        actor_user_id: str,
        idempotency_key: str,
        command_repository: CreationCommandRepository,
        now: datetime | None = None,
    ) -> tuple[RouteEdgeRefreshResult, bool]:
        workspace = await self.itinerary_repository.get_workspace(workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        if workspace.current_itinerary_revision != itinerary_revision:
            raise RevisionConflictError(
                "route evidence refresh requires the current itinerary revision",
                context={"expected_revision": itinerary_revision, "actual_revision": workspace.current_itinerary_revision},
            )
        revision = await self.itinerary_repository.get_revision(workspace_id, itinerary_revision)
        if revision is None:
            raise ResourceNotFound("itinerary revision does not exist")
        parent = (
            await self.itinerary_repository.get_revision(workspace_id, revision.parent_revision)
            if revision.parent_revision is not None else None
        )
        basis = self.audit_service._workspace_basis(workspace)
        request_hash = sha256_canonical({
            "schema_version": 1,
            "operation": CreationOperation.REFRESH_CHANGED_ROUTE_EDGES.value,
            "workspace_id": workspace_id,
            "itinerary_revision": itinerary_revision,
            "actor_user_id": actor_user_id,
        })
        claim = await command_repository.claim(
            workspace_id=workspace_id,
            operation=CreationOperation.REFRESH_CHANGED_ROUTE_EDGES,
            target_id=f"revision:{itinerary_revision}:changed-route-edges",
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            basis=basis,
        )
        if claim.replay is not None:
            return RouteEdgeRefreshResult.model_validate(claim.replay.body).model_copy(
                update={"idempotent_replay": True},
            ), True
        try:
            command_now = now or datetime.now(timezone.utc)
            observations, provider_failures, route_delta = await self._collect(
                revision=revision, parent=parent, now=command_now,
            )
            _, snapshot, report, prepared_basis = await self.audit_service._prepare_current_audit(
                workspace_id,
                provider_failures=provider_failures,
                extra_observations=observations,
                now=command_now,
            )
            if prepared_basis != claim.basis:
                from app.audit.errors import AuditInputStaleError
                raise AuditInputStaleError(
                    "workspace inputs changed before changed-edge refresh preparation",
                    context={"expected_basis": claim.basis, "actual_basis": prepared_basis},
                )

            async def finalize(conn, stored_basis):
                stored_report = await self.audit_repository.save_audit_bundle(
                    snapshot, report, basis=stored_basis, conn=conn,
                )
                result = RouteEdgeRefreshResult(
                    workspace_id=workspace_id,
                    itinerary_revision=itinerary_revision,
                    source_revision=parent.revision if parent else None,
                    report=stored_report,
                    evidence_snapshot=snapshot,
                    route_delta=route_delta,
                    provider_failures=provider_failures,
                )
                return CreationCommandResponse(
                    status_code=200, body=result.model_dump(mode="json"), headers={},
                )

            response = await command_repository.finalize(claim, finalize)
            return RouteEdgeRefreshResult.model_validate(response.body), response.idempotent_replay
        except Exception:
            await command_repository.abandon(claim)
            raise
