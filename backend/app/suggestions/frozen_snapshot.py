from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.constraints.amap_types import typecodes_for_category
from app.importing.models import ResolvedPlaceReceipt
from app.schemas.place import Coordinates, PlaceCategory, RetrievalExecutionMode
from app.suggestions.models import CandidateCurrentFact, FrozenCanonicalPlace, SuggestionIntent
from app.suggestions.providers import (
    CandidateRouteSource,
    ProviderCandidate,
    ProviderCandidateBatch,
    ProviderCandidateQuery,
    ProviderCandidateSource,
    RouteTimes,
)
from app.suggestions.suitability import classify_provider_suitability


_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXPECTED_CLASS = "real_provider_local_authorized"
_LEGACY_SUBTYPE = "suggestion_live_candidate_and_walking_route_snapshot"
_CHAIN_SUBTYPE = "suggestion_live_chained_candidate_and_walking_route_snapshot"
_LEGACY_CLAIM_BOUNDARY = {
    "proves_local_authorized_live_entity": True,
    "proves_local_authorized_live_route": True,
    "proves_opening_hours": False,
    "proves_reservation": False,
    "proves_accessibility": False,
    "is_public_internet_e2e": False,
    "is_human_evidence": False,
    "is_release_approval": False,
}
_CHAIN_CLAIM_BOUNDARY = {
    "proves_local_authorized_live_entity": True,
    "proves_local_authorized_live_route": True,
    # Product replay is proved by the external G2 RunSpec receipt that binds
    # this immutable capture.  The Provider artifact must not self-attest the
    # outcome of a replay that can only happen after it is loaded.
    "proves_three_city_four_stop_snapshot_replay": False,
    "proves_current_live_state_at_replay": False,
    "proves_opening_hours": False,
    "proves_reservation": False,
    "proves_accessibility": False,
    "is_public_internet_e2e": False,
    "is_human_evidence": False,
    "is_release_approval": False,
}
_CAPTURED_INTENTS = (
    SuggestionIntent.NEARBY,
    SuggestionIntent.POPULAR,
    SuggestionIntent.FUN,
    SuggestionIntent.FOOD,
)
_INTENT_CATEGORIES = {
    SuggestionIntent.NEARBY: (PlaceCategory.ATTRACTION, PlaceCategory.FOOD),
    SuggestionIntent.POPULAR: (PlaceCategory.ATTRACTION, PlaceCategory.FOOD),
    SuggestionIntent.FUN: (PlaceCategory.ATTRACTION,),
    SuggestionIntent.FOOD: (PlaceCategory.FOOD,),
}
_INTENT_KEYWORDS = {
    SuggestionIntent.NEARBY: ("附近",),
    SuggestionIntent.POPULAR: ("热门", "口碑"),
    SuggestionIntent.FUN: ("景点", "好玩"),
    SuggestionIntent.FOOD: ("美食", "餐厅"),
}
_INTENT_RADIUS_M = {
    SuggestionIntent.NEARBY: 5_000,
    SuggestionIntent.POPULAR: 15_000,
    SuggestionIntent.FUN: 12_000,
    SuggestionIntent.FOOD: 5_000,
}


class FrozenSnapshotError(RuntimeError):
    """A configured snapshot cannot be trusted or cannot answer exactly."""


class FrozenSnapshotSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")

    def resolved_path(self, *, repo_root: Path = _REPO_ROOT) -> Path:
        raw = Path(self.path)
        if raw.is_absolute():
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_PATH_MUST_BE_REPOSITORY_RELATIVE")
        root = repo_root.resolve()
        resolved = (root / raw).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_PATH_ESCAPES_REPOSITORY") from exc
        return resolved


@dataclass(frozen=True)
class _CandidateRecord:
    matched_intents: tuple[SuggestionIntent, ...]
    candidate: ProviderCandidate
    route_times: RouteTimes


@dataclass(frozen=True)
class _CityRecord:
    city: str
    anchor_place_id: str
    anchor_name: str
    anchor_coords: Coordinates
    provider_snapshot_id: str
    query_hashes: Mapping[SuggestionIntent, str]
    query_retrieved_at: Mapping[SuggestionIntent, datetime]
    candidates: tuple[_CandidateRecord, ...]


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timezone_aware(value: Any, *, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise FrozenSnapshotError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FrozenSnapshotError(code)
    return parsed


def _require_hash(value: Any, *, code: str) -> str:
    candidate = str(value or "")
    if len(candidate) != 64 or any(char not in "0123456789abcdef" for char in candidate):
        raise FrozenSnapshotError(code)
    return candidate


def _expected_query(
    record: _CityRecord,
    intents: tuple[SuggestionIntent, ...],
) -> ProviderCandidateQuery:
    if len(intents) == 1:
        intent = intents[0]
        categories = _INTENT_CATEGORIES[intent]
        radius_m = _INTENT_RADIUS_M[intent]
        keywords = _INTENT_KEYWORDS[intent]
    elif intents == _CAPTURED_INTENTS:
        categories = (PlaceCategory.ATTRACTION, PlaceCategory.FOOD)
        radius_m = 15_000
        keywords = tuple(word for intent in _CAPTURED_INTENTS for word in _INTENT_KEYWORDS[intent])
    else:
        raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_INTENT_COMBINATION_NOT_CAPTURED")
    return ProviderCandidateQuery(
        city=record.city,
        intents=intents,
        typecodes=tuple(
            dict.fromkeys(
                code
                for category in categories
                for code in typecodes_for_category(category)
            )
        ),
        radius_m=radius_m,
        anchor_name=record.anchor_name,
        anchor_place_id=record.anchor_place_id,
        anchor_coords=record.anchor_coords,
        keywords=keywords,
        transport_mode="walking",
    )


class FrozenSuggestionSnapshot:
    """Byte-bound immutable view of the captured Suggestion Provider artifact."""

    def __init__(self, *, spec: FrozenSnapshotSpec, payload: Mapping[str, Any], cities: tuple[_CityRecord, ...]):
        self.spec = spec
        self.payload = payload
        self.cities = cities
        self._by_city: dict[str, tuple[_CityRecord, ...]] = {}
        for item in cities:
            self._by_city[item.city] = (*self._by_city.get(item.city, ()), item)

    @classmethod
    def load(cls, spec: FrozenSnapshotSpec, *, repo_root: Path = _REPO_ROOT) -> "FrozenSuggestionSnapshot":
        path = spec.resolved_path(repo_root=repo_root)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_FILE_UNAVAILABLE") from exc
        if hashlib.sha256(raw).hexdigest() != spec.file_sha256:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_FILE_HASH_MISMATCH")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_JSON_INVALID") from exc
        if not isinstance(payload, dict):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_ROOT_NOT_OBJECT")
        cls._validate_envelope(payload, spec)
        cities = cls._parse_cities(payload)
        return cls(spec=spec, payload=payload, cities=cities)

    @staticmethod
    def _validate_envelope(payload: Mapping[str, Any], spec: FrozenSnapshotSpec) -> None:
        schema_version = payload.get("schema_version")
        if schema_version not in {"1.0", "1.1"}:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_SCHEMA_UNSUPPORTED")
        if payload.get("evidence_class") != _EXPECTED_CLASS:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_EVIDENCE_CLASS_MISMATCH")
        expected_subtype = _LEGACY_SUBTYPE if schema_version == "1.0" else _CHAIN_SUBTYPE
        if payload.get("evidence_subtype") != expected_subtype:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_EVIDENCE_SUBTYPE_MISMATCH")
        expected_status = "passed" if schema_version == "1.0" else "PASSED"
        if payload.get("overall_status") != expected_status or payload.get("failure_receipt") is not None:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_STATUS_NOT_PASSED")
        expected_boundary = (
            _LEGACY_CLAIM_BOUNDARY if schema_version == "1.0" else _CHAIN_CLAIM_BOUNDARY
        )
        if payload.get("claim_boundary") != expected_boundary:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CLAIM_BOUNDARY_MISMATCH")
        runtime = payload.get("runtime")
        if not isinstance(runtime, dict) or runtime.get("authentication_material_persisted") is not False:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_SECRET_BOUNDARY_INVALID")
        if schema_version == "1.1" and (
            runtime.get("runtime_profile") != "local_real"
            or runtime.get("amap_mock") is not False
            or runtime.get("demo_mode") is not False
            or runtime.get("fixture_fallback_allowed") is not False
            or runtime.get("retry_count") != 0
        ):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_RUNTIME_BOUNDARY_INVALID")
        integrity = payload.get("integrity")
        if not isinstance(integrity, dict):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_INTEGRITY_MISSING")
        if integrity.get("passed") is not True or integrity.get("validation_errors") != []:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_INTEGRITY_NOT_PASSED")
        payload_hash = _canonical_hash({key: value for key, value in payload.items() if key != "integrity"})
        if integrity.get("artifact_payload_sha256") != payload_hash:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_PAYLOAD_HASH_MISMATCH")
        if payload_hash != spec.snapshot_id:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_ID_MISMATCH")

    @classmethod
    def _parse_cities(cls, payload: Mapping[str, Any]) -> tuple[_CityRecord, ...]:
        rows = payload.get("cities")
        if not isinstance(rows, list) or not rows:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CITIES_MISSING")
        if payload.get("schema_version") == "1.1":
            return cls._parse_chained_cities(rows)
        records = tuple(cls._parse_city(row) for row in rows)
        if len({item.city for item in records}) != len(records):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CITY_DUPLICATE")
        return records

    @classmethod
    def _parse_chained_cities(cls, rows: list[Any]) -> tuple[_CityRecord, ...]:
        output: list[_CityRecord] = []
        seen_cities: set[str] = set()
        seen_anchors: set[tuple[str, str]] = set()
        for city_row in rows:
            if not isinstance(city_row, dict):
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CITY_INVALID")
            city = str(city_row.get("city") or "")
            if not city or city in seen_cities:
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CITY_DUPLICATE")
            seen_cities.add(city)
            rounds = city_row.get("rounds")
            if (
                city_row.get("chain_status") != "COMPLETE"
                or city_row.get("required_rounds") != 3
                or city_row.get("completed_rounds") != 3
                or not isinstance(rounds, list)
                or len(rounds) != 3
            ):
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CHAIN_INCOMPLETE")
            initial = city_row.get("initial_anchor")
            if not isinstance(initial, dict) or initial.get("authority") != "fixed_canonical_anchor":
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_ANCHOR_INVALID")
            expected_anchor_id = str(initial.get("place_id") or "")
            selected_chain = [expected_anchor_id]
            for index, round_row in enumerate(rounds, 1):
                if not isinstance(round_row, dict) or round_row.get("round_index") != index:
                    raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CHAIN_ROUND_INVALID")
                anchor = round_row.get("anchor")
                if (
                    round_row.get("status") != "COMPLETE"
                    or not isinstance(anchor, dict)
                    or str(anchor.get("place_id") or "") != expected_anchor_id
                ):
                    raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CHAIN_CONTINUITY_INVALID")
                raw = {
                    "city": city,
                    "anchor": anchor,
                    "provider_snapshot_id": round_row.get("provider_snapshot_id"),
                    "query_receipts": round_row.get("query_receipts"),
                    "selection": round_row.get("selection"),
                    "candidates": round_row.get("candidates"),
                }
                query_rows = round_row.get("query_receipts")
                candidate_rows = round_row.get("candidates")
                if not isinstance(query_rows, list) or any(
                    not isinstance(item, dict)
                    or item.get("provider") != "amap"
                    or item.get("execution_mode") != "live"
                    or item.get("source_url") != "https://restapi.amap.com/v5/place/around"
                    for item in query_rows
                ):
                    raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_QUERY_RECEIPT_MODE_INVALID")
                if not isinstance(candidate_rows, list) or any(
                    not isinstance(item, dict)
                    or (item.get("provider_receipt") or {}).get("source_url")
                    != "https://restapi.amap.com/v5/place/around"
                    or any(
                        fact.get("source_url")
                        != "https://restapi.amap.com/v5/place/around"
                        for fact in (item.get("operational_evidence") or {}).get("facts") or []
                    )
                    or any(
                        receipt.get("source_url")
                        != "https://restapi.amap.com/v3/direction/walking"
                        for receipt in (item.get("route_times") or {}).get("route_receipts") or []
                    )
                    for item in candidate_rows
                ):
                    raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_RECEIPT_SOURCE_INVALID")
                record = cls._parse_city(raw)
                key = (city, record.anchor_place_id)
                if key in seen_anchors:
                    raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CHAIN_ANCHOR_DUPLICATE")
                seen_anchors.add(key)
                selected = str((round_row.get("selection") or {}).get("selected_candidate_place_id") or "")
                if selected not in {
                    candidate.candidate.canonical_place.place_id for candidate in record.candidates
                }:
                    raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CHAIN_SELECTION_INVALID")
                selected_chain.append(selected)
                expected_anchor_id = selected
                output.append(record)
            if city_row.get("selected_chain_place_ids") != selected_chain or len(set(selected_chain)) != 4:
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CHAIN_SELECTION_INVALID")
        return tuple(output)

    @classmethod
    def _parse_city(cls, raw: Any) -> _CityRecord:
        if not isinstance(raw, dict):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CITY_INVALID")
        city = str(raw.get("city") or "")
        anchor = raw.get("anchor")
        if (
            not city
            or not isinstance(anchor, dict)
            or anchor.get("authority")
            not in {"fixed_canonical_anchor", "prior_round_selected_canonical"}
        ):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_ANCHOR_INVALID")
        try:
            anchor_coords = Coordinates.model_validate(anchor["coords"])
        except (KeyError, ValueError, TypeError) as exc:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_ANCHOR_INVALID") from exc
        anchor_place_id = str(anchor.get("place_id") or "")
        anchor_name = str(anchor.get("name") or "")
        if not anchor_place_id or not anchor_name:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_ANCHOR_INVALID")

        query_rows = raw.get("query_receipts")
        if not isinstance(query_rows, list):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_QUERY_RECEIPTS_INVALID")
        query_hashes: dict[SuggestionIntent, str] = {}
        query_times: dict[SuggestionIntent, datetime] = {}
        provider_receipts: dict[SuggestionIntent, tuple[str, str]] = {}
        for row in query_rows:
            try:
                intent = SuggestionIntent(str(row["intent"]))
            except (KeyError, ValueError, TypeError) as exc:
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_QUERY_INTENT_INVALID") from exc
            if intent in query_hashes:
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_QUERY_INTENT_DUPLICATE")
            query_hashes[intent] = _require_hash(
                row.get("query_contract_sha256"), code="SUGGESTION_SNAPSHOT_QUERY_HASH_INVALID"
            )
            query_times[intent] = _timezone_aware(
                row.get("retrieved_at"), code="SUGGESTION_SNAPSHOT_QUERY_TIME_INVALID"
            )
            provider_receipts[intent] = (
                _require_hash(row.get("provider_request_hash"), code="SUGGESTION_SNAPSHOT_REQUEST_HASH_INVALID"),
                _require_hash(row.get("provider_response_hash"), code="SUGGESTION_SNAPSHOT_RESPONSE_HASH_INVALID"),
            )
        if tuple(query_hashes) != _CAPTURED_INTENTS:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_FOUR_INTENT_QUERIES_REQUIRED")

        provisional = _CityRecord(
            city=city,
            anchor_place_id=anchor_place_id,
            anchor_name=anchor_name,
            anchor_coords=anchor_coords,
            provider_snapshot_id=str(raw.get("provider_snapshot_id") or ""),
            query_hashes=query_hashes,
            query_retrieved_at=query_times,
            candidates=(),
        )
        for intent in _CAPTURED_INTENTS:
            expected_hash = _canonical_hash(_expected_query(provisional, (intent,)).model_dump(mode="json"))
            if query_hashes[intent] != expected_hash:
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_QUERY_CONTRACT_MISMATCH")
        if not provisional.provider_snapshot_id:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CITY_ID_MISSING")

        candidate_rows = raw.get("candidates")
        if not isinstance(candidate_rows, list) or not candidate_rows:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CANDIDATES_MISSING")
        candidates = tuple(
            cls._parse_candidate(
                item,
                city=provisional,
                provider_receipts=provider_receipts,
            )
            for item in candidate_rows
        )
        # The legacy/public workspace projection is only query geometry.  It
        # becomes usable for this snapshot because (a) the captured query hash
        # binds the exact anchor id/name/coords contract above and (b) at least
        # one original live route receipt binds that same id/coords as origin.
        # Client-supplied coordinates alone can therefore never authorize an
        # anchor or be used as a substitute Provider receipt.
        if not any(
            item.route_times.status == "AVAILABLE"
            and item.route_times.route_receipts
            and item.route_times.route_receipts[0].origin_place_id == anchor_place_id
            and item.route_times.route_receipts[0].origin_coords == anchor_coords
            and item.route_times.route_receipts[0].execution_mode is RetrievalExecutionMode.LIVE
            for item in candidates
        ):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_ANCHOR_PROVIDER_RECEIPT_MISSING")
        ids = [item.candidate.canonical_place.place_id for item in candidates]
        names = [item.candidate.canonical_place.name.strip().casefold() for item in candidates]
        if len(ids) != len(set(ids)) or len(names) != len(set(names)):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CANDIDATE_DUPLICATE")
        selection = raw.get("selection")
        if not isinstance(selection, dict) or selection.get("frozen_candidate_count") != len(candidates):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_SELECTION_COUNT_MISMATCH")
        return _CityRecord(**{**provisional.__dict__, "candidates": candidates})

    @staticmethod
    def _parse_candidate(
        raw: Any,
        *,
        city: _CityRecord,
        provider_receipts: Mapping[SuggestionIntent, tuple[str, str]],
    ) -> _CandidateRecord:
        if not isinstance(raw, dict):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CANDIDATE_INVALID")
        try:
            matched_intents = tuple(SuggestionIntent(str(value)) for value in raw["matched_intents"])
            place = FrozenCanonicalPlace.model_validate(raw["canonical_place"])
            receipt = ResolvedPlaceReceipt.model_validate(raw["provider_receipt"])
        except (KeyError, ValueError, TypeError) as exc:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CANDIDATE_INVALID") from exc
        if not matched_intents or len(set(matched_intents)) != len(matched_intents):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CANDIDATE_INTENTS_INVALID")
        if place.city != city.city or place.place_id == city.anchor_place_id:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CANDIDATE_CITY_OR_ANCHOR_INVALID")
        if receipt.provider != "amap" or receipt.execution_mode is not RetrievalExecutionMode.LIVE:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_ENTITY_RECEIPT_MODE_INVALID")
        if not any(
            (receipt.request_hash, receipt.response_hash) == provider_receipts[intent]
            for intent in matched_intents
        ):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_ENTITY_RECEIPT_NOT_QUERY_BOUND")

        operational = raw.get("operational_evidence")
        current_facts: tuple[CandidateCurrentFact, ...] = ()
        if not isinstance(operational, dict):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_OPERATIONAL_EVIDENCE_INVALID")
        if operational.get("status") == "PROVIDER_EXPLICIT":
            try:
                current_facts = tuple(
                    CandidateCurrentFact.model_validate(item) for item in operational.get("facts") or []
                )
            except (ValueError, TypeError) as exc:
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CURRENT_FACT_INVALID") from exc
            if not current_facts:
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_EXPLICIT_CURRENT_FACT_MISSING")
            if any(
                fact.request_hash != receipt.request_hash
                or fact.response_hash != receipt.response_hash
                or fact.execution_mode is not receipt.execution_mode
                for fact in current_facts
            ):
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CURRENT_FACT_NOT_ENTITY_BOUND")
        elif operational.get("status") != "UNKNOWN" or operational.get("facts"):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_OPERATIONAL_EVIDENCE_INVALID")

        signals = raw.get("provider_signals")
        if not isinstance(signals, dict):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_PROVIDER_SIGNALS_INVALID")
        try:
            suitability = classify_provider_suitability(
                name=place.name,
                provider_raw_type=receipt.provider_raw_type,
                provider_raw_typecode=receipt.provider_raw_typecode,
            )
            candidate = ProviderCandidate(
                canonical_place=place,
                provider_receipt=receipt,
                popularity=float(signals.get("popularity", 0.0)),
                diversity_tags=tuple(str(item) for item in signals.get("diversity_tags") or []),
                hard_block_codes=suitability.hard_block_codes,
                current_facts=current_facts,
            )
            route_times = RouteTimes.model_validate(raw.get("route_times"))
        except (ValueError, TypeError) as exc:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CANDIDATE_MODEL_INVALID") from exc
        if route_times.status == "AVAILABLE":
            if len(route_times.route_receipts) != 1:
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_ROUTE_SHAPE_INVALID")
            route = route_times.route_receipts[0]
            if (
                route.provider != "amap"
                or route.execution_mode is not RetrievalExecutionMode.LIVE
                or route.transport_mode != "walking"
                or route.origin_place_id != city.anchor_place_id
                or route.origin_coords != city.anchor_coords
                or route.destination_place_id != place.place_id
                or route.destination_coords != place.coords
            ):
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_ROUTE_ENDPOINT_OR_MODE_INVALID")
        elif route_times.route_receipts:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_UNKNOWN_ROUTE_HAS_RECEIPT")
        return _CandidateRecord(
            matched_intents=matched_intents,
            candidate=candidate,
            route_times=route_times,
        )

    def match_query(self, query: ProviderCandidateQuery) -> tuple[_CityRecord, tuple[SuggestionIntent, ...]]:
        records = self._by_city.get(query.city)
        if records is None:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CITY_NOT_CAPTURED")
        intents = tuple(query.intents)
        record = next(
            (
                item
                for item in records
                if query.model_dump(mode="json")
                == _expected_query(item, intents).model_dump(mode="json")
            ),
            None,
        )
        if record is None:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_QUERY_NOT_EXACT")
        if len(intents) == 1:
            query_hash = _canonical_hash(query.model_dump(mode="json"))
            if record.query_hashes.get(intents[0]) != query_hash:
                raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_QUERY_HASH_NOT_CAPTURED")
        return record, intents


class FrozenSnapshotCandidateSource(ProviderCandidateSource):
    def __init__(self, spec: FrozenSnapshotSpec, *, repo_root: Path = _REPO_ROOT):
        self.spec = spec
        self.repo_root = repo_root

    async def search(self, query: ProviderCandidateQuery) -> ProviderCandidateBatch:
        # Reload and hash on every request.  A file changed after startup cannot
        # be served from a previously trusted in-memory object.
        snapshot = FrozenSuggestionSnapshot.load(self.spec, repo_root=self.repo_root)
        city, intents = snapshot.match_query(query)
        requested = set(intents)
        candidates = tuple(
            record.candidate
            for record in city.candidates
            if requested.intersection(record.matched_intents)
        )
        retrieved_at = max(city.query_retrieved_at[intent] for intent in intents)
        return ProviderCandidateBatch(
            provider_snapshot_id=city.provider_snapshot_id,
            candidates=candidates,
            retrieved_at=retrieved_at,
        )


class FrozenSnapshotRouteSource(CandidateRouteSource):
    def __init__(self, spec: FrozenSnapshotSpec, *, repo_root: Path = _REPO_ROOT):
        self.spec = spec
        self.repo_root = repo_root

    async def route_times(self, query: ProviderCandidateQuery, candidate: ProviderCandidate) -> RouteTimes:
        snapshot = FrozenSuggestionSnapshot.load(self.spec, repo_root=self.repo_root)
        city, intents = snapshot.match_query(query)
        requested = set(intents)
        record = next(
            (
                item
                for item in city.candidates
                if item.candidate.canonical_place.place_id == candidate.canonical_place.place_id
                and item.candidate == candidate
                and requested.intersection(item.matched_intents)
            ),
            None,
        )
        if record is None:
            return RouteTimes(status="UNKNOWN", reason_code="SNAPSHOT_ROUTE_CANDIDATE_NOT_EXACT")
        if record.route_times.status != "AVAILABLE":
            return RouteTimes(
                status="UNKNOWN",
                reason_code=record.route_times.reason_code or "SNAPSHOT_ROUTE_NOT_CAPTURED",
            )
        route = record.route_times.route_receipts[0]
        if (
            query.previous_anchor is not None
            or query.next_anchor is not None
            or query.anchor_role != "PREVIOUS"
            or query.anchor_place_id != route.origin_place_id
            or query.anchor_coords != route.origin_coords
            or route.destination_place_id != candidate.canonical_place.place_id
            or route.destination_coords != candidate.canonical_place.coords
        ):
            return RouteTimes(status="UNKNOWN", reason_code="SNAPSHOT_ROUTE_ENDPOINT_NOT_EXACT")
        return record.route_times


def snapshot_spec_from_settings(settings: Settings) -> FrozenSnapshotSpec:
    try:
        return FrozenSnapshotSpec(
            path=settings.suggestion_snapshot_path,
            file_sha256=settings.suggestion_snapshot_sha256,
            snapshot_id=settings.suggestion_snapshot_id,
        )
    except ValueError as exc:
        raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CONFIGURATION_INCOMPLETE") from exc


def validate_suggestion_provider_configuration(settings: Settings) -> None:
    mode = settings.suggestion_provider_mode
    snapshot_values = (
        settings.suggestion_snapshot_path,
        settings.suggestion_snapshot_sha256,
        settings.suggestion_snapshot_id,
        settings.suggestion_snapshot_replay_at,
    )
    if mode == "frozen_snapshot":
        if not all(snapshot_values):
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_CONFIGURATION_INCOMPLETE")
        replay_at = settings.suggestion_snapshot_replay_at
        if replay_at is None or replay_at.tzinfo is None or replay_at.utcoffset() is None:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_REPLAY_CLOCK_INVALID")
        if settings.runtime_profile not in {"local_real", "test"}:
            raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_RUNTIME_PROFILE_FORBIDDEN")
        FrozenSuggestionSnapshot.load(snapshot_spec_from_settings(settings))
        return
    if any(snapshot_values):
        raise FrozenSnapshotError("SUGGESTION_SNAPSHOT_FIELDS_FORBIDDEN_OUTSIDE_SNAPSHOT_MODE")
    if mode == "live" and (settings.amap_mock or settings.demo_mode):
        raise FrozenSnapshotError("SUGGESTION_LIVE_MODE_CONFLICTS_WITH_FIXTURE_CONFIGURATION")
    if mode == "fixture" and settings.runtime_profile not in {"demo", "test", "local_fixture"}:
        raise FrozenSnapshotError("SUGGESTION_FIXTURE_RUNTIME_FORBIDDEN")


def suggestion_provider_health(settings: Settings) -> dict[str, Any]:
    result: dict[str, Any] = {"mode": settings.suggestion_provider_mode}
    if settings.suggestion_provider_mode == "frozen_snapshot":
        result.update(
            {
                "snapshot_id": settings.suggestion_snapshot_id,
                "snapshot_sha256": settings.suggestion_snapshot_sha256,
                "replay_at": (
                    settings.suggestion_snapshot_replay_at.isoformat()
                    if settings.suggestion_snapshot_replay_at is not None
                    else None
                ),
            }
        )
    return result
