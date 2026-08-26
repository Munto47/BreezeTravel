from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import aiohttp
from pydantic import BaseModel, ConfigDict, Field

from app.audit.evidence_service import EvidenceObservation
from app.audit.models import EvidenceFreshness, ProviderFailure
from app.config import Settings, get_settings
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import ItineraryRevision
from app.schemas.place import Coordinates
from app.trip_check.models import RunPartialFailure, TripCheckRun, TripCheckStage


ROUTE_MODES = ("walking", "transit", "bicycling", "driving")
AMAP_ROUTE_ENDPOINTS = {
    "walking": "https://restapi.amap.com/v5/direction/walking",
    "transit": "https://restapi.amap.com/v5/direction/transit/integrated",
    "bicycling": "https://restapi.amap.com/v5/direction/bicycling",
    "driving": "https://restapi.amap.com/v5/direction/driving",
}
AMAP_CITY_CODES = {"北京": "010", "上海": "021", "杭州": "0571"}
DEFAULT_SNAPSHOT_PATH = Path(__file__).parents[2] / "evals" / "fixtures" / "trip_check_provider_integrity_v1.json"


class ProviderCallReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    receipt_id: str
    provider: str
    operation: str
    execution_mode: Literal["fixture", "snapshot", "live"]
    status: Literal["SUCCEEDED", "PARTIAL", "UNAVAILABLE"]
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    source_url: str
    affected_fields: list[str] = Field(default_factory=list)
    failure_category: str | None = None


class ProviderCollectionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    observations: list[EvidenceObservation]
    provider_failures: list[ProviderFailure]
    partial_failures: list[RunPartialFailure]
    provider_receipts: list[ProviderCallReceipt]
    provider_attempt_count: int = Field(ge=0)


class ProviderSnapshotMismatchError(RuntimeError):
    pass


class ProviderQueryBudgetExceededError(RuntimeError):
    pass


def provider_snapshot_sha256(path: Path = DEFAULT_SNAPSHOT_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: Any) -> str:
    return sha256_canonical(value)


def _coords(value: Any) -> Coordinates | None:
    if isinstance(value, Coordinates):
        return value
    if isinstance(value, dict):
        try:
            return Coordinates.model_validate(value)
        except ValueError:
            return None
    return None


def _place_coordinates(
    revision: ItineraryRevision,
    place_records: dict[str, dict[str, Any]],
) -> dict[str, Coordinates]:
    result: dict[str, Coordinates] = {}
    projections = dict(revision.change_summary.get("map_stop_projections") or {})
    for day in revision.days:
        for stop in day.stops:
            record = place_records.get(stop.place_id) or {}
            value = _coords(record.get("coords")) or _coords((projections.get(stop.stop_id) or {}).get("coords"))
            if value is not None:
                result[stop.stop_id] = value
    return result


def _source_tier(url: str) -> str:
    lowered = url.casefold()
    if ".gov.cn" in lowered or lowered.endswith(".gov.cn"):
        return "GOVERNMENT"
    if any(token in lowered for token in ("amap.com", "qweather.com", "12306.cn")):
        return "OPERATOR"
    return "MEDIA"


def _qweather_headers(settings: Settings) -> dict[str, str]:
    if settings.qweather_auth_type != "jwt":
        return {"X-QW-Api-Key": settings.qweather_api_key}
    import jwt as pyjwt

    private_key = settings.qweather_private_key
    if "BEGIN PRIVATE KEY" not in private_key:
        private_key = f"-----BEGIN PRIVATE KEY-----\n{private_key}\n-----END PRIVATE KEY-----"
    token = pyjwt.encode(
        {
            "sub": settings.qweather_project_id,
            "iat": int(time.time()) - 30,
            "exp": int(time.time()) + 900,
        },
        private_key,
        algorithm="EdDSA",
        headers={"kid": settings.qweather_key_id},
    )
    return {"Authorization": f"Bearer {token}"}


class TripCheckProviderIntegrityCollector:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        snapshot_path: Path = DEFAULT_SNAPSHOT_PATH,
        session_factory: Any = aiohttp.ClientSession,
        max_live_calls: int | None = None,
    ):
        if max_live_calls is not None and max_live_calls < 1:
            raise ValueError("max_live_calls must be positive when configured")
        self.settings = settings or get_settings()
        self.snapshot_path = snapshot_path
        self.session_factory = session_factory
        self.max_live_calls = max_live_calls
        self.live_call_count = 0

    def _consume_live_call(self) -> None:
        if self.max_live_calls is not None and self.live_call_count >= self.max_live_calls:
            raise ProviderQueryBudgetExceededError(
                f"live Provider call budget exhausted at {self.max_live_calls} calls"
            )
        self.live_call_count += 1

    def _load_snapshot(self, run: TripCheckRun) -> dict[str, Any]:
        raw = self.snapshot_path.read_bytes()
        actual_hash = hashlib.sha256(raw).hexdigest()
        if run.run_spec.execution_mode == "snapshot" and run.run_spec.snapshot_hash != actual_hash:
            raise ProviderSnapshotMismatchError(
                f"provider snapshot hash mismatch: expected={run.run_spec.snapshot_hash} actual={actual_hash}"
            )
        payload = json.loads(raw)
        if payload.get("schema_version") != "trip-check-provider-snapshot-v1":
            raise ProviderSnapshotMismatchError("provider snapshot schema is not supported")
        return payload

    @staticmethod
    def _receipt(
        *,
        run: TripCheckRun,
        provider: str,
        operation: str,
        execution_mode: str,
        status: str,
        request: Any,
        response: Any | None,
        observed_at: datetime,
        source_url: str,
        affected_fields: list[str] | None = None,
        failure_category: str | None = None,
    ) -> ProviderCallReceipt:
        request_hash = _canonical_hash(request)
        return ProviderCallReceipt(
            receipt_id=_canonical_hash(
                {
                    "run_id": run.run_id,
                    "provider": provider,
                    "operation": operation,
                    "request_hash": request_hash,
                }
            ),
            provider=provider,
            operation=operation,
            execution_mode=execution_mode,
            status=status,
            request_hash=request_hash,
            response_hash=_canonical_hash(response) if response is not None else None,
            observed_at=observed_at,
            source_url=source_url,
            affected_fields=list(affected_fields or []),
            failure_category=failure_category,
        )

    @staticmethod
    def _partial(
        *,
        provider: str,
        category: str,
        fields: list[str],
        retryable: bool,
    ) -> tuple[ProviderFailure, RunPartialFailure]:
        return (
            ProviderFailure(
                provider=provider,
                error_category=category,
                retryable=retryable,
                detail="provider fields remain UNKNOWN/UNAVAILABLE",
            ),
            RunPartialFailure(
                stage=TripCheckStage.COLLECT_EVIDENCE,
                provider=provider,
                category=category,
                affected_fields=fields,
                retryable=retryable,
            ),
        )

    @staticmethod
    def _route_observations(
        *,
        edge_id: str,
        selected_mode: str,
        mode: str,
        value: dict[str, Any],
        provider: str,
        observed_at: datetime,
        unavailable_category: str | None = None,
    ) -> list[EvidenceObservation]:
        freshness = EvidenceFreshness.UNAVAILABLE if unavailable_category else None
        normalized = {
            "mode": mode,
            "duration_minutes": value.get("duration_minutes"),
            "distance_km": value.get("distance_km"),
            "transfer_count": value.get("transfer_count"),
            **({"reason_code": unavailable_category} if unavailable_category else {}),
        }
        observations = [
            EvidenceObservation(
                subject_type="ROUTE_OPTION",
                subject_id=f"{edge_id}:{mode}",
                fact_type="ROUTE_OPTION",
                value=normalized,
                provider=provider,
                observed_at=observed_at,
                valid_until=observed_at + timedelta(minutes=15) if not unavailable_category else None,
                confidence=0 if unavailable_category else 1,
                freshness_status=freshness,
            )
        ]
        if mode == selected_mode:
            observations.append(
                EvidenceObservation(
                    subject_type="ROUTE_EDGE",
                    subject_id=edge_id,
                    fact_type="ROUTE_TIME",
                    value=normalized,
                    provider=provider,
                    observed_at=observed_at,
                    valid_until=observed_at + timedelta(minutes=15) if not unavailable_category else None,
                    confidence=0 if unavailable_category else 1,
                    freshness_status=freshness,
                )
            )
        return observations

    async def _collect_snapshot(
        self,
        run: TripCheckRun,
        revision: ItineraryRevision,
    ) -> ProviderCollectionResult:
        payload = self._load_snapshot(run)
        execution_mode = run.run_spec.execution_mode
        provider = "controlled_provider_fixture_v1" if execution_mode == "fixture" else "frozen_provider_snapshot_v1"
        source_prefix = "fixture" if execution_mode == "fixture" else "snapshot"
        observations: list[EvidenceObservation] = []
        failures: list[ProviderFailure] = []
        partials: list[RunPartialFailure] = []
        receipts: list[ProviderCallReceipt] = []
        route_failure_used = False
        city_routes = payload["routes"][revision.city]
        for day in revision.days:
            for left, right in zip(day.stops, day.stops[1:]):
                edge_id = f"{left.stop_id}->{right.stop_id}"
                selected = left.transport_to_next.mode if left.transport_to_next else "driving"
                selected = selected if selected in ROUTE_MODES else "driving"
                for mode in ROUTE_MODES:
                    request = {"city": revision.city, "edge_id": edge_id, "mode": mode}
                    field = f"route_edges.{edge_id}.{mode}"
                    inject = run.run_spec.fault_profile == "route_mode_unavailable" and not route_failure_used
                    if inject:
                        route_failure_used = True
                        category = "PROVIDER_ROUTE_MODE_UNAVAILABLE"
                        observations.extend(
                            self._route_observations(
                                edge_id=edge_id,
                                selected_mode=selected,
                                mode=mode,
                                value={},
                                provider=provider,
                                observed_at=run.created_at,
                                unavailable_category=category,
                            )
                        )
                        failure, partial = self._partial(
                            provider=provider,
                            category=category,
                            fields=[field],
                            retryable=False,
                        )
                        failures.append(failure)
                        partials.append(partial)
                        receipts.append(
                            self._receipt(
                                run=run,
                                provider=provider,
                                operation=f"route.{mode}",
                                execution_mode=execution_mode,
                                status="UNAVAILABLE",
                                request=request,
                                response=None,
                                observed_at=run.created_at,
                                source_url=f"{source_prefix}://route/{revision.city}/{mode}",
                                affected_fields=[field],
                                failure_category=category,
                            )
                        )
                        continue
                    value = dict(city_routes[mode])
                    observations.extend(
                        self._route_observations(
                            edge_id=edge_id,
                            selected_mode=selected,
                            mode=mode,
                            value=value,
                            provider=provider,
                            observed_at=run.created_at,
                        )
                    )
                    receipts.append(
                        self._receipt(
                            run=run,
                            provider=provider,
                            operation=f"route.{mode}",
                            execution_mode=execution_mode,
                            status="SUCCEEDED",
                            request=request,
                            response=value,
                            observed_at=run.created_at,
                            source_url=f"{source_prefix}://route/{revision.city}/{mode}",
                        )
                    )

        weather_value = {
            **dict(payload["weather"][revision.city]),
            "evidence_class": "DETERMINISTIC_PROVIDER_FACT",
        }
        weather_fields = [f"days.{day.day_index}.weather" for day in revision.days]
        weather_failed = run.run_spec.fault_profile == "weather_unavailable"
        for day in revision.days:
            observations.append(
                EvidenceObservation(
                    subject_type="DAY",
                    subject_id=str(day.day_index),
                    fact_type="WEATHER",
                    value=None if weather_failed else weather_value,
                    provider=provider,
                    observed_at=run.created_at,
                    valid_until=run.created_at + timedelta(hours=6) if not weather_failed else None,
                    confidence=0 if weather_failed else 1,
                    freshness_status=EvidenceFreshness.UNAVAILABLE if weather_failed else None,
                )
            )
        weather_category = "PROVIDER_WEATHER_UNAVAILABLE" if weather_failed else None
        if weather_category:
            failure, partial = self._partial(
                provider=provider,
                category=weather_category,
                fields=weather_fields,
                retryable=False,
            )
            failures.append(failure)
            partials.append(partial)
        receipts.append(
            self._receipt(
                run=run,
                provider=provider,
                operation="weather.daily",
                execution_mode=execution_mode,
                status="UNAVAILABLE" if weather_failed else "SUCCEEDED",
                request={"city": revision.city, "dates": [str(day.date) for day in revision.days]},
                response=None if weather_failed else weather_value,
                observed_at=run.created_at,
                source_url=f"{source_prefix}://weather/{revision.city}",
                affected_fields=weather_fields if weather_failed else [],
                failure_category=weather_category,
            )
        )

        risk_failed = run.run_spec.fault_profile == "risk_unavailable"
        risk_values = list(payload["risk"][revision.city])[:5]
        if risk_failed:
            observations.append(
                EvidenceObservation(
                    subject_type="TRIP",
                    subject_id=run.workspace_id,
                    fact_type="RISK_SOURCE",
                    value=None,
                    provider=provider,
                    observed_at=run.created_at,
                    confidence=0,
                    freshness_status=EvidenceFreshness.UNAVAILABLE,
                )
            )
            category = "PROVIDER_RISK_UNAVAILABLE"
            failure, partial = self._partial(
                provider=provider,
                category=category,
                fields=["risk.sources"],
                retryable=False,
            )
            failures.append(failure)
            partials.append(partial)
        else:
            for index, value in enumerate(risk_values):
                observations.append(
                    EvidenceObservation(
                        subject_type="TRIP",
                        subject_id=f"{run.workspace_id}:risk:{index}",
                        fact_type="RISK_SOURCE",
                        value={
                            **value,
                            "retrieved_at": run.created_at.isoformat(),
                            "evidence_class": "ADVISORY_SOURCE",
                        },
                        provider=provider,
                        source_url=value["url"],
                        observed_at=run.created_at,
                        valid_until=run.created_at + timedelta(hours=24),
                        confidence=1,
                    )
                )
        receipts.append(
            self._receipt(
                run=run,
                provider=provider,
                operation="risk.news_search",
                execution_mode=execution_mode,
                status="UNAVAILABLE" if risk_failed else "SUCCEEDED",
                request={"city": revision.city, "query_profile": "trip_risk_v1", "count": 5},
                response=None if risk_failed else risk_values,
                observed_at=run.created_at,
                source_url=f"{source_prefix}://risk/{revision.city}",
                affected_fields=["risk.sources"] if risk_failed else [],
                failure_category="PROVIDER_RISK_UNAVAILABLE" if risk_failed else None,
            )
        )
        return ProviderCollectionResult(
            observations=observations,
            provider_failures=failures,
            partial_failures=partials,
            provider_receipts=receipts,
            provider_attempt_count=len(receipts),
        )

    async def _live_route(
        self,
        session: aiohttp.ClientSession,
        *,
        run: TripCheckRun,
        city: str,
        edge_id: str,
        mode: str,
        origin: Coordinates | None,
        destination: Coordinates | None,
    ) -> tuple[list[EvidenceObservation], ProviderCallReceipt, ProviderFailure | None, RunPartialFailure | None]:
        field = f"route_edges.{edge_id}.{mode}"
        endpoint = AMAP_ROUTE_ENDPOINTS[mode]
        request = {
            "origin": origin.model_dump(mode="json") if origin else None,
            "destination": destination.model_dump(mode="json") if destination else None,
            "city": city,
            "mode": mode,
        }
        selected_mode = mode
        category: str | None = None
        response_payload: Any | None = None
        route_value: dict[str, Any] = {}
        if not self.settings.amap_api_key:
            category = "AMAP_CREDENTIALS_MISSING"
        elif origin is None or destination is None:
            category = "ROUTE_COORDINATES_MISSING"
        else:
            params = {
                "key": self.settings.amap_api_key,
                "origin": f"{origin.lng:.6f},{origin.lat:.6f}",
                "destination": f"{destination.lng:.6f},{destination.lat:.6f}",
                "output": "json",
            }
            if mode == "transit":
                params["city1"] = AMAP_CITY_CODES[city]
                params["city2"] = AMAP_CITY_CODES[city]
            try:
                self._consume_live_call()
                async with session.get(endpoint, params=params, timeout=aiohttp.ClientTimeout(total=8)) as response:
                    response.raise_for_status()
                    response_payload = await response.json()
                if response_payload.get("status") != "1":
                    category = "AMAP_PROVIDER_STATUS"
                else:
                    route = response_payload.get("route") or {}
                    options = route.get("transits") if mode == "transit" else route.get("paths")
                    first = (options or [None])[0]
                    if not isinstance(first, dict):
                        category = "AMAP_ROUTE_EMPTY"
                    else:
                        route_value = {
                            "duration_minutes": max(1, round(float(first.get("duration") or 0) / 60)),
                            "distance_km": round(float(first.get("distance") or 0) / 1000, 3),
                            "transfer_count": None,
                        }
            except Exception as exc:
                category = f"AMAP_{type(exc).__name__.upper()}"
        observed_at = datetime.now(timezone.utc)
        observations = self._route_observations(
            edge_id=edge_id,
            selected_mode=selected_mode,
            mode=mode,
            value=route_value,
            provider="amap",
            observed_at=observed_at,
            unavailable_category=category,
        )
        receipt = self._receipt(
            run=run,
            provider="amap",
            operation=f"route.{mode}",
            execution_mode="live",
            status="UNAVAILABLE" if category else "SUCCEEDED",
            request=request,
            response=response_payload,
            observed_at=observed_at,
            source_url=endpoint,
            affected_fields=[field] if category else [],
            failure_category=category,
        )
        if not category:
            return observations, receipt, None, None
        failure, partial = self._partial(provider="amap", category=category, fields=[field], retryable=True)
        return observations, receipt, failure, partial

    async def _collect_live(
        self,
        run: TripCheckRun,
        revision: ItineraryRevision,
        place_records: dict[str, dict[str, Any]],
    ) -> ProviderCollectionResult:
        observations: list[EvidenceObservation] = []
        failures: list[ProviderFailure] = []
        partials: list[RunPartialFailure] = []
        receipts: list[ProviderCallReceipt] = []
        coords = _place_coordinates(revision, place_records)
        async with self.session_factory() as session:
            for day in revision.days:
                for left, right in zip(day.stops, day.stops[1:]):
                    edge_id = f"{left.stop_id}->{right.stop_id}"
                    selected = left.transport_to_next.mode if left.transport_to_next else "driving"
                    selected = selected if selected in ROUTE_MODES else "driving"
                    for mode in ROUTE_MODES:
                        route_observations, receipt, failure, partial = await self._live_route(
                            session,
                            run=run,
                            city=revision.city,
                            edge_id=edge_id,
                            mode=mode,
                            origin=coords.get(left.stop_id),
                            destination=coords.get(right.stop_id),
                        )
                        if mode != selected:
                            route_observations = [item for item in route_observations if item.subject_type != "ROUTE_EDGE"]
                        observations.extend(route_observations)
                        receipts.append(receipt)
                        if failure and partial:
                            failures.append(failure)
                            partials.append(partial)

            weather_fields = [f"days.{day.day_index}.weather" for day in revision.days]
            anchor = next(iter(coords.values()), None)
            weather_request = {
                "city": revision.city,
                "location": anchor.model_dump(mode="json") if anchor else None,
                "days": 7,
            }
            weather_payload: Any | None = None
            weather_category: str | None = None
            weather_missing_fields: list[str] = []
            credentials = bool(
                self.settings.qweather_api_key
                or (
                    self.settings.qweather_private_key
                    and self.settings.qweather_key_id
                    and self.settings.qweather_project_id
                )
            )
            if not credentials:
                weather_category = "QWEATHER_CREDENTIALS_MISSING"
            elif anchor is None:
                weather_category = "WEATHER_COORDINATES_MISSING"
            else:
                try:
                    host = self.settings.qweather_api_host.removeprefix("https://").removeprefix("http://").rstrip("/")
                    url = f"https://{host}/v7/weather/7d"
                    headers = _qweather_headers(self.settings)
                    self._consume_live_call()
                    async with session.get(
                        url,
                        params={"location": f"{anchor.lng:.2f},{anchor.lat:.2f}", "lang": "zh"},
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as response:
                        response.raise_for_status()
                        weather_payload = await response.json()
                    if weather_payload.get("code") != "200":
                        weather_category = "QWEATHER_PROVIDER_STATUS"
                    else:
                        by_date = {item.get("fxDate"): item for item in weather_payload.get("daily") or []}
                        for day in revision.days:
                            item = by_date.get(str(day.date))
                            if item is None:
                                weather_missing_fields.append(f"days.{day.day_index}.weather")
                                observations.append(
                                    EvidenceObservation(
                                        subject_type="DAY",
                                        subject_id=str(day.day_index),
                                        fact_type="WEATHER",
                                        provider="qweather",
                                        observed_at=datetime.now(timezone.utc),
                                        confidence=0,
                                        freshness_status=EvidenceFreshness.UNAVAILABLE,
                                    )
                                )
                                continue
                            observations.append(
                                EvidenceObservation(
                                    subject_type="DAY",
                                    subject_id=str(day.day_index),
                                    fact_type="WEATHER",
                                    value={
                                        "condition": str(item.get("textDay") or "未知"),
                                        "temp_high": int(float(item.get("tempMax") or 0)),
                                        "temp_low": int(float(item.get("tempMin") or 0)),
                                        "precip_mm": float(item.get("precip") or 0),
                                        "suggestion": "根据 Provider 天气事实核对户外安排",
                                        "evidence_class": "DETERMINISTIC_PROVIDER_FACT",
                                    },
                                    provider="qweather",
                                    observed_at=datetime.now(timezone.utc),
                                    valid_until=datetime.now(timezone.utc) + timedelta(hours=6),
                                    confidence=1,
                                )
                            )
                except Exception as exc:
                    weather_category = f"QWEATHER_{type(exc).__name__.upper()}"
            if weather_category:
                for day in revision.days:
                    observations.append(
                        EvidenceObservation(
                            subject_type="DAY",
                            subject_id=str(day.day_index),
                            fact_type="WEATHER",
                            provider="qweather",
                            observed_at=datetime.now(timezone.utc),
                            confidence=0,
                            freshness_status=EvidenceFreshness.UNAVAILABLE,
                        )
                    )
                failure, partial = self._partial(
                    provider="qweather",
                    category=weather_category,
                    fields=weather_fields,
                    retryable=True,
                )
                failures.append(failure)
                partials.append(partial)
            elif weather_missing_fields:
                weather_category = "QWEATHER_PARTIAL_DATES"
                failure, partial = self._partial(
                    provider="qweather",
                    category=weather_category,
                    fields=weather_missing_fields,
                    retryable=True,
                )
                failures.append(failure)
                partials.append(partial)
            receipts.append(
                self._receipt(
                    run=run,
                    provider="qweather",
                    operation="weather.daily",
                    execution_mode="live",
                    status=(
                        "PARTIAL"
                        if weather_category == "QWEATHER_PARTIAL_DATES"
                        else "UNAVAILABLE"
                        if weather_category
                        else "SUCCEEDED"
                    ),
                    request=weather_request,
                    response=weather_payload,
                    observed_at=datetime.now(timezone.utc),
                    source_url=f"https://{self.settings.qweather_api_host.removeprefix('https://')}/v7/weather/7d",
                    affected_fields=(weather_missing_fields if weather_missing_fields else weather_fields)
                    if weather_category
                    else [],
                    failure_category=weather_category,
                )
            )

            risk_request = {
                "city": revision.city,
                "location": anchor.model_dump(mode="json") if anchor else None,
                "risk_scope": "ACTIVE_WEATHER_ALERTS_ONLY",
                "local_time": True,
            }
            risk_payload: Any | None = None
            risk_category: str | None = None
            risk_endpoint = None
            if not credentials:
                risk_category = "QWEATHER_CREDENTIALS_MISSING"
            elif anchor is None:
                risk_category = "WEATHER_ALERT_COORDINATES_MISSING"
            else:
                try:
                    host = self.settings.qweather_api_host.removeprefix("https://").removeprefix("http://").rstrip("/")
                    risk_endpoint = (
                        f"https://{host}/weatheralert/v1/current/"
                        f"{anchor.lat:.2f}/{anchor.lng:.2f}"
                    )
                    self._consume_live_call()
                    async with session.get(
                        risk_endpoint,
                        params={"lang": "zh", "localTime": "true"},
                        headers=_qweather_headers(self.settings),
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as response:
                        response.raise_for_status()
                        risk_payload = await response.json()
                    metadata = risk_payload.get("metadata") or {}
                    alerts = list(risk_payload.get("alerts") or [])
                    zero_result = metadata.get("zeroResult") is True
                    attributions = list(metadata.get("attributions") or [])
                    if not zero_result and not alerts:
                        risk_category = "QWEATHER_ALERT_RESPONSE_INVALID"
                    elif zero_result:
                        observations.append(
                            EvidenceObservation(
                                subject_type="TRIP",
                                subject_id=f"{run.workspace_id}:weather-alert-status",
                                fact_type="RISK_SOURCE",
                                value={
                                    "risk_type": "WEATHER_ALERT",
                                    "status": "NONE_REPORTED",
                                    "scope": "ACTIVE_WEATHER_ALERTS_ONLY",
                                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                                    "source_tier": "OPERATOR",
                                    "evidence_class": "DETERMINISTIC_PROVIDER_FACT",
                                    "attributions": attributions,
                                },
                                provider="qweather_alert",
                                source_url=risk_endpoint,
                                observed_at=datetime.now(timezone.utc),
                                valid_until=datetime.now(timezone.utc) + timedelta(hours=1),
                                confidence=1,
                            )
                        )
                    else:
                        for index, item in enumerate(alerts[:5]):
                            event_type = item.get("eventType") or {}
                            observations.append(
                                EvidenceObservation(
                                    subject_type="TRIP",
                                    subject_id=f"{run.workspace_id}:weather-alert:{index}",
                                    fact_type="RISK_SOURCE",
                                    value={
                                        "risk_type": "WEATHER_ALERT",
                                        "status": "ACTIVE",
                                        "scope": "ACTIVE_WEATHER_ALERTS_ONLY",
                                        "alert_id": str(item.get("id") or ""),
                                        "sender_name": str(item.get("senderName") or "")[:200],
                                        "event_name": str(event_type.get("name") or "")[:100],
                                        "event_code": str(event_type.get("code") or "")[:50],
                                        "severity": item.get("severity"),
                                        "urgency": item.get("urgency"),
                                        "certainty": item.get("certainty"),
                                        "headline": str(item.get("headline") or "")[:300],
                                        "description": str(item.get("description") or "")[:1000],
                                        "issued_at": item.get("issuedTime"),
                                        "effective_at": item.get("effectiveTime"),
                                        "expires_at": item.get("expireTime"),
                                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                                        "source_tier": "OPERATOR",
                                        "evidence_class": "DETERMINISTIC_PROVIDER_FACT",
                                        "attributions": attributions,
                                    },
                                    provider="qweather_alert",
                                    source_url=risk_endpoint,
                                    observed_at=datetime.now(timezone.utc),
                                    valid_until=datetime.now(timezone.utc) + timedelta(hours=1),
                                    confidence=1,
                                )
                            )
                except Exception as exc:
                    risk_category = f"QWEATHER_ALERT_{type(exc).__name__.upper()}"
            if risk_category:
                observations.append(
                    EvidenceObservation(
                        subject_type="TRIP",
                        subject_id=run.workspace_id,
                        fact_type="RISK_SOURCE",
                        provider="qweather_alert",
                        observed_at=datetime.now(timezone.utc),
                        confidence=0,
                        freshness_status=EvidenceFreshness.UNAVAILABLE,
                    )
                )
                failure, partial = self._partial(
                    provider="qweather_alert",
                    category=risk_category,
                    fields=["risk.sources"],
                    retryable=True,
                )
                failures.append(failure)
                partials.append(partial)
            receipts.append(
                self._receipt(
                    run=run,
                    provider="qweather_alert",
                    operation="risk.weather_alert",
                    execution_mode="live",
                    status="UNAVAILABLE" if risk_category else "SUCCEEDED",
                    request=risk_request,
                    response=risk_payload,
                    observed_at=datetime.now(timezone.utc),
                    source_url=risk_endpoint or "qweather://weatheralert/current",
                    affected_fields=["risk.sources"] if risk_category else [],
                    failure_category=risk_category,
                )
            )
        return ProviderCollectionResult(
            observations=observations,
            provider_failures=failures,
            partial_failures=partials,
            provider_receipts=receipts,
            provider_attempt_count=len(receipts),
        )

    async def collect(
        self,
        run: TripCheckRun,
        revision: ItineraryRevision,
        place_records: dict[str, dict[str, Any]],
    ) -> ProviderCollectionResult:
        route_edge_count = sum(max(0, len(day.stops) - 1) for day in revision.days)
        required_queries = route_edge_count * len(ROUTE_MODES) + 2
        if run.run_spec.budget.max_provider_queries < required_queries:
            raise ProviderQueryBudgetExceededError(
                f"P3 provider collection requires {required_queries} queries but budget allows "
                f"{run.run_spec.budget.max_provider_queries}"
            )
        if run.run_spec.execution_mode in {"fixture", "snapshot"}:
            return await self._collect_snapshot(run, revision)
        if run.run_spec.execution_mode == "live":
            return await self._collect_live(run, revision, place_records)
        raise ValueError(f"unsupported P3 provider execution mode: {run.run_spec.execution_mode}")
