from __future__ import annotations

import asyncio
import re
from typing import Protocol
from urllib.parse import quote_plus

from app.importing.confidence import candidate_confidence
from app.importing.models import (
    PlaceCandidate,
    RawStop,
    RejectedPlaceCandidate,
    ResolutionRejectionReason,
    ResolvedPlaceReceipt,
    ResolvedStop,
)
from app.itineraries.errors import ItineraryDomainError
from app.itineraries.models import ResolutionStatus
from app.schemas.place import Coordinates


class EntityProviderUnavailable(ItineraryDomainError):
    code = "EVIDENCE_PROVIDER_UNAVAILABLE"
    status_code = 503


class EntityCandidateProvider(Protocol):
    async def search(self, *, query: str, city: str) -> list[dict]: ...


def _normalized_city(value: str) -> str:
    normalized = re.sub(r"\s+", "", value).casefold()
    for suffix in ("特别行政区", "自治州", "地区", "盟", "市"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


class AmapEntityCandidateProvider:
    async def search(self, *, query: str, city: str) -> list[dict]:
        from app.config import settings

        fixture_allowed = settings.runtime_profile in {"demo", "test", "local_fixture"}
        if (settings.amap_mock or settings.demo_mode) and fixture_allowed:
            from app.agents.nodes.amap_search import _load_mock_entity_candidates

            places = _load_mock_entity_candidates(city, query)
            scored_places = sorted(
                places,
                key=lambda place: (
                    -candidate_confidence(
                        query,
                        place.model_dump(mode="json"),
                        city=city,
                    )[0],
                    place.place_id,
                ),
            )
            return [place.model_dump(mode="json") for place in scored_places[:8]]

        from app.tools.amap_tool import _run_amap_search

        try:
            places = await _run_amap_search(query=query, city=city)
        except Exception as exc:
            raise EntityProviderUnavailable("Amap entity search is unavailable") from exc
        return [place.model_dump(mode="json") for place in places[:8]]


class EntityResolver:
    def __init__(
        self,
        provider: EntityCandidateProvider,
        *,
        auto_match_threshold: float = 0.90,
        ambiguity_gap: float = 0.08,
    ):
        self.provider = provider
        self.auto_match_threshold = auto_match_threshold
        self.ambiguity_gap = ambiguity_gap

    async def resolve(self, raw_stop: RawStop, *, city: str) -> ResolvedStop:
        # Generic placeholders are not POI identities.  Asking a provider for
        # them can return an exact-looking but unrelated result and incorrectly
        # promote an unresolved draft to AUTO_MATCHED.
        normalized_name = re.sub(r"[\s，,。.!！?？:：;；、\-—_]+", "", raw_stop.raw_name).lower()
        if normalized_name in {
            "待定",
            "待确认",
            "暂定",
            "未知",
            "未知地点",
            "某景点",
            "某餐厅",
            "某酒店",
            "随便逛逛",
        }:
            return ResolvedStop(
                raw_stop_id=raw_stop.raw_stop_id,
                resolution_status=ResolutionStatus.NOT_FOUND,
                confidence=0,
            )
        raw_candidates = await self.provider.search(query=raw_stop.raw_name, city=city)
        if not raw_candidates:
            return ResolvedStop(
                raw_stop_id=raw_stop.raw_stop_id,
                resolution_status=ResolutionStatus.NOT_FOUND,
                confidence=0,
            )
        scored: list[PlaceCandidate] = []
        rejected_candidates: list[RejectedPlaceCandidate] = []
        for candidate in raw_candidates:
            score, reasons = candidate_confidence(raw_stop.raw_name, candidate, city=city)
            place_id = str(candidate.get("place_id") or "")
            name = str(candidate.get("name") or "")
            if not place_id or not name:
                continue
            candidate_city = str(candidate.get("city") or city)
            coords = None
            try:
                if isinstance(candidate.get("coords"), dict):
                    coords = Coordinates.model_validate(candidate["coords"])
            except (TypeError, ValueError):
                coords = None
            provider = str(candidate.get("retrieval_provider") or "") or None
            execution_mode_raw = candidate.get("execution_mode")
            execution_mode = (
                str(getattr(execution_mode_raw, "value", execution_mode_raw) or "") or None
            )
            response_hash = str(candidate.get("retrieval_response_hash") or "") or None
            request_hash = str(candidate.get("retrieval_request_hash") or "") or None
            observed_at = candidate.get("retrieval_observed_at")
            source_url = str(candidate.get("source_url") or "") or None
            if source_url is None and provider == "amap":
                source_url = (
                    "https://www.amap.com/search?query="
                    f"{quote_plus(raw_stop.raw_name)}&city={quote_plus(city)}"
                )
            receipt = None
            receipt_inputs_complete = bool(
                coords is not None
                and provider
                and execution_mode
                and response_hash
                and request_hash
                and re.fullmatch(r"[0-9a-f]{64}", request_hash)
                and re.fullmatch(r"[0-9a-f]{64}", response_hash)
                and observed_at is not None
            )
            if receipt_inputs_complete:
                try:
                    receipt = ResolvedPlaceReceipt(
                        canonical_place_id=place_id,
                        provider=provider,
                        provider_place_id=str(candidate.get("provider_place_id") or place_id),
                        name=name,
                        city=candidate_city,
                        district=candidate.get("district"),
                        address=candidate.get("address"),
                        category=str(candidate.get("category") or "") or None,
                        longitude=coords.lng,
                        latitude=coords.lat,
                        request_hash=request_hash,
                        response_hash=response_hash,
                        observed_at=observed_at,
                        execution_mode=execution_mode,
                        source_url=source_url,
                    )
                    if receipt.observed_at.tzinfo is None or receipt.observed_at.utcoffset() is None:
                        receipt = None
                except (TypeError, ValueError):
                    receipt = None
            place_candidate = PlaceCandidate(
                place_id=place_id,
                name=name,
                city=candidate_city,
                district=candidate.get("district"),
                address=candidate.get("address"),
                category=str(candidate.get("category") or "") or None,
                coords=coords,
                retrieval_provider=provider,
                execution_mode=execution_mode,
                retrieval_request_hash=request_hash,
                retrieval_response_hash=response_hash,
                retrieval_observed_at=observed_at,
                source_url=source_url,
                opening_hours=candidate.get("opening_hours"),
                phone=candidate.get("phone"),
                amap_rating=candidate.get("amap_rating"),
                amap_price=candidate.get("amap_price"),
                resolved_place_receipt=receipt,
                score=score,
                reasons=reasons,
            )
            if _normalized_city(candidate_city) != _normalized_city(city):
                # A wrong-city hit must never enter the user-confirmable list.
                # If the provider supplied a complete receipt, retain that
                # immutable observation so NOT_FOUND remains explainable.  An
                # incomplete provider result is dropped rather than padded
                # with invented audit facts.
                if receipt is not None:
                    rejected_candidates.append(
                        RejectedPlaceCandidate(
                            place_id=place_id,
                            name=name,
                            reason=ResolutionRejectionReason.WRONG_CITY,
                            target_city=city,
                            resolved_place_receipt=receipt,
                        )
                    )
                continue
            scored.append(place_candidate)
        scored.sort(key=lambda item: (-item.score, item.place_id))
        rejected_candidates.sort(key=lambda item: item.place_id)
        if not scored:
            return ResolvedStop(
                raw_stop_id=raw_stop.raw_stop_id,
                resolution_status=ResolutionStatus.NOT_FOUND,
                confidence=0,
                rejected_candidates=rejected_candidates,
            )
        top = scored[0]
        second_score = scored[1].score if len(scored) > 1 else 0.0
        auto_match = (
            top.resolved_place_receipt is not None
            and top.score >= self.auto_match_threshold
            and top.score - second_score >= self.ambiguity_gap
        )
        return ResolvedStop(
            raw_stop_id=raw_stop.raw_stop_id,
            canonical_place_id=top.place_id if auto_match else None,
            candidates=scored[:5],
            rejected_candidates=rejected_candidates,
            confidence=top.score,
            resolution_status=ResolutionStatus.AUTO_MATCHED if auto_match else ResolutionStatus.AMBIGUOUS,
        )

    async def resolve_all(self, raw_stops: list[RawStop], *, city: str) -> list[ResolvedStop]:
        semaphore = asyncio.Semaphore(3)

        async def resolve_bounded(raw_stop: RawStop) -> ResolvedStop:
            async with semaphore:
                return await self.resolve(raw_stop, city=city)

        return list(await asyncio.gather(*(resolve_bounded(raw_stop) for raw_stop in raw_stops)))
