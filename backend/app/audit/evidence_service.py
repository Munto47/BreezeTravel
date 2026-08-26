from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, Field

from app.audit.evidence_policy import EvidencePolicy
from app.audit.models import EvidenceFact, EvidenceFreshness, EvidenceSnapshot, ProviderFailure
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import ItineraryRevision


class EvidenceObservation(BaseModel):
    subject_type: str
    subject_id: str
    fact_type: str
    value: Any = None
    provider: str
    source_url: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    freshness_status: EvidenceFreshness | None = None


def _as_datetime(raw: Any, fallback: datetime) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return fallback
    return fallback


class EvidenceService:
    def __init__(self, policy: EvidencePolicy | None = None):
        self.policy = policy or EvidencePolicy()

    def observations_from_revision(
        self,
        revision: ItineraryRevision,
        place_records: dict[str, dict[str, Any]],
        *,
        now: datetime,
        target_itinerary_revision: int | None = None,
    ) -> list[EvidenceObservation]:
        if target_itinerary_revision is not None and target_itinerary_revision != revision.revision:
            raise ValueError("place records target revision does not match audited itinerary revision")
        observations: list[EvidenceObservation] = []
        seen_places: set[str] = set()
        for day in revision.days:
            for stop in day.stops:
                if stop.place_id in seen_places:
                    continue
                seen_places.add(stop.place_id)
                record = place_records.get(stop.place_id)
                if not record:
                    observations.extend([
                        EvidenceObservation(
                            subject_type="PLACE",
                            subject_id=stop.place_id,
                            fact_type="POI_IDENTITY",
                            provider="room_places",
                            confidence=0,
                            freshness_status=EvidenceFreshness.UNAVAILABLE,
                        ),
                        EvidenceObservation(
                            subject_type="PLACE",
                            subject_id=stop.place_id,
                            fact_type="OPENING_HOURS",
                            provider="amap_poi",
                            confidence=0,
                            freshness_status=EvidenceFreshness.UNAVAILABLE,
                        ),
                    ])
                    continue
                observed_at = _as_datetime(
                    record.get("retrieval_observed_at") or record.get("updated_at"),
                    now,
                )
                observations.append(EvidenceObservation(
                    subject_type="PLACE",
                    subject_id=stop.place_id,
                    fact_type="POI_IDENTITY",
                    value={
                        "place_id": stop.place_id,
                        "name": record.get("name") or stop.raw_name,
                        "city": record.get("city"),
                        "district": record.get("district"),
                        "address": record.get("address"),
                        "coords": record.get("coords"),
                        "category": record.get("category") or stop.category,
                    },
                    provider=str(record.get("provider") or record.get("source") or "room_places"),
                    observed_at=observed_at,
                    confidence=float(record.get("retrieval_confidence") or 1.0),
                ))
                opening_hours = record.get("opening_hours")
                observations.append(EvidenceObservation(
                    subject_type="PLACE",
                    subject_id=stop.place_id,
                    fact_type="OPENING_HOURS",
                    value=opening_hours,
                    provider=str(record.get("provider") or record.get("source") or "amap_poi"),
                    observed_at=observed_at,
                    confidence=0.7 if opening_hours else 0,
                    freshness_status=None if opening_hours else EvidenceFreshness.UNAVAILABLE,
                ))
                dietary_support = record.get("dietary_support")
                if dietary_support is None and isinstance(record.get("dietary_restrictions"), list):
                    dietary_support = {
                        "supported_restrictions": record["dietary_restrictions"],
                    }
                if dietary_support is not None:
                    observations.append(EvidenceObservation(
                        subject_type="PLACE",
                        subject_id=stop.place_id,
                        fact_type="DIETARY_SUPPORT",
                        value=dietary_support,
                        provider=str(record.get("provider") or record.get("source") or "room_places"),
                        observed_at=observed_at,
                        confidence=float(record.get("dietary_confidence") or 1.0),
                    ))
        observations.extend(self._route_observations_from_change_summary(revision, now=now))
        return observations

    @staticmethod
    def _route_observations_from_change_summary(
        revision: ItineraryRevision,
        *,
        now: datetime,
    ) -> list[EvidenceObservation]:
        """Expose accepted SuggestionSet receipts to the ordinary Audit path."""

        if revision.change_summary.get("operation") != "ACCEPT_SUGGESTION_CANDIDATE":
            return []
        route_delta = revision.change_summary.get("route_delta")
        projections = revision.change_summary.get("map_stop_projections")
        if not isinstance(route_delta, dict) or not isinstance(projections, dict) or len(projections) != 1:
            return []
        candidate_stop_id = next(iter(projections))
        candidate_stop = next(
            (stop for day in revision.days for stop in day.stops if stop.stop_id == candidate_stop_id),
            None,
        )
        if candidate_stop is None:
            return []
        target_day = revision.days[candidate_stop.day_index]
        by_place = {stop.place_id: stop.stop_id for stop in target_day.stops}
        output: list[EvidenceObservation] = []
        for raw in route_delta.get("route_receipts") or []:
            if not isinstance(raw, dict):
                continue
            leg = raw.get("leg")
            if leg == "PREVIOUS_TO_CANDIDATE":
                left = by_place.get(str(raw.get("origin_place_id") or ""))
                right = candidate_stop_id
            elif leg == "CANDIDATE_TO_NEXT":
                left = candidate_stop_id
                right = by_place.get(str(raw.get("destination_place_id") or ""))
            else:
                continue
            duration = raw.get("duration_minutes")
            observed_at = _as_datetime(raw.get("observed_at"), now)
            max_age = raw.get("max_age_seconds")
            if not left or not right or isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
                continue
            valid_until = (
                observed_at + timedelta(seconds=max_age)
                if isinstance(max_age, int) and max_age > 0
                else observed_at
            )
            output.append(EvidenceObservation(
                subject_type="ROUTE_EDGE",
                subject_id=f"{left}->{right}",
                fact_type="ROUTE_TIME",
                value={"mode": raw.get("transport_mode"), "duration_minutes": duration},
                provider=str(raw.get("provider") or "suggestion_route_receipt"),
                source_url=raw.get("source_url"),
                observed_at=observed_at,
                valid_until=valid_until,
                confidence=1.0,
            ))
        return output

    def create_snapshot(
        self,
        *,
        workspace_id: str,
        itinerary_revision: int,
        observations: list[EvidenceObservation],
        provider_failures: list[ProviderFailure] | None = None,
        supersedes_snapshot_id: str | None = None,
        snapshot_id: str | None = None,
        now: datetime | None = None,
    ) -> EvidenceSnapshot:
        now = now or datetime.now(timezone.utc)
        snapshot_id = snapshot_id or str(uuid4())
        conflicting_keys: set[tuple[str, str, str]] = set()
        grouped: dict[tuple[str, str, str], set[str]] = {}
        for index, observation in enumerate(observations):
            key = (observation.subject_type, observation.subject_id, observation.fact_type)
            if observation.freshness_status not in {EvidenceFreshness.UNAVAILABLE, EvidenceFreshness.STALE}:
                grouped.setdefault(key, set()).add(sha256_canonical(observation.value))
        conflicting_keys.update(key for key, values in grouped.items() if len(values) > 1)

        facts: list[EvidenceFact] = []
        for index, observation in enumerate(observations):
            key = (observation.subject_type, observation.subject_id, observation.fact_type)
            status = observation.freshness_status
            valid_until = observation.valid_until
            if key in conflicting_keys:
                status = EvidenceFreshness.CONFLICTING
            elif status is None:
                ttl = self.policy.ttl_for(observation.fact_type)
                if valid_until is None and ttl is not None:
                    valid_until = observation.observed_at + ttl
                status = EvidenceFreshness.STALE if valid_until is not None and valid_until < now else EvidenceFreshness.FRESH
            facts.append(EvidenceFact(
                fact_id=str(
                    uuid5(
                        NAMESPACE_URL,
                        (
                            f"breezetravel:{snapshot_id}:{index}:"
                            f"{observation.subject_type}:{observation.subject_id}:{observation.fact_type}"
                        ),
                    )
                ),
                snapshot_id=snapshot_id,
                subject_type=observation.subject_type,
                subject_id=observation.subject_id,
                fact_type=observation.fact_type,
                value=observation.value,
                provider=observation.provider,
                source_url=observation.source_url,
                observed_at=observation.observed_at,
                valid_from=observation.valid_from,
                valid_until=valid_until,
                response_hash=sha256_canonical({
                    "provider": observation.provider,
                    "subject_type": observation.subject_type,
                    "subject_id": observation.subject_id,
                    "fact_type": observation.fact_type,
                    "value": observation.value,
                    "observed_at": observation.observed_at,
                }),
                confidence=observation.confidence,
                freshness_status=status,
            ))
        providers = sorted({observation.provider for observation in observations})
        return EvidenceSnapshot(
            snapshot_id=snapshot_id,
            workspace_id=workspace_id,
            itinerary_revision=itinerary_revision,
            provider_set=providers,
            policy_version=self.policy.version,
            facts=facts,
            provider_failures=provider_failures or [],
            supersedes_snapshot_id=supersedes_snapshot_id,
            created_at=now,
        )

    def derive_snapshot_for_revision(
        self,
        source: EvidenceSnapshot,
        revision: ItineraryRevision,
        *,
        now: datetime | None = None,
    ) -> EvidenceSnapshot:
        """Rebind reusable facts and make newly-created route edges explicitly unavailable."""

        now = now or datetime.now(timezone.utc)
        snapshot_id = str(uuid4())
        place_ids = {stop.place_id for day in revision.days for stop in day.stops}
        day_ids = {str(day.day_index) for day in revision.days}
        edge_ids = {
            f"{left.stop_id}->{right.stop_id}"
            for day in revision.days
            for left, right in zip(day.stops, day.stops[1:])
        }
        copied: list[EvidenceFact] = []
        existing_route_ids: set[str] = set()
        for fact in source.facts:
            keep = (
                (fact.subject_type == "PLACE" and fact.subject_id in place_ids)
                or (fact.subject_type == "DAY" and fact.subject_id in day_ids)
                or (fact.subject_type == "ROUTE_EDGE" and fact.subject_id in edge_ids)
            )
            if not keep:
                continue
            status = fact.freshness_status
            if status == EvidenceFreshness.FRESH and fact.valid_until and fact.valid_until < now:
                status = EvidenceFreshness.STALE
            copied.append(fact.model_copy(update={
                "fact_id": str(uuid4()),
                "snapshot_id": snapshot_id,
                "freshness_status": status,
            }))
            if fact.subject_type == "ROUTE_EDGE" and fact.fact_type == "ROUTE_TIME":
                existing_route_ids.add(fact.subject_id)
        for edge_id in sorted(edge_ids - existing_route_ids):
            copied.append(EvidenceFact(
                fact_id=str(uuid4()),
                snapshot_id=snapshot_id,
                subject_type="ROUTE_EDGE",
                subject_id=edge_id,
                fact_type="ROUTE_TIME",
                value=None,
                provider="repair_postcheck",
                observed_at=now,
                response_hash=sha256_canonical({"edge_id": edge_id, "status": "UNAVAILABLE"}),
                confidence=0,
                freshness_status=EvidenceFreshness.UNAVAILABLE,
            ))
        return EvidenceSnapshot(
            snapshot_id=snapshot_id,
            workspace_id=source.workspace_id,
            itinerary_revision=revision.revision,
            provider_set=sorted({*source.provider_set, "repair_postcheck"}),
            policy_version=source.policy_version,
            facts=copied,
            provider_failures=source.provider_failures,
            created_at=now,
            supersedes_snapshot_id=source.snapshot_id,
        )
