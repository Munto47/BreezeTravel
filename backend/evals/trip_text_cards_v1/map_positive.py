from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.trip_understanding.map_render import (
    ROUTE_CONFIG_SHA256,
    InternalRouteModeFact,
    MapRenderJobRecord,
    MapRenderOutput,
    MapRenderPlan,
    MapRenderer,
    MapStop,
    PlanRevisionRef,
)
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.pipeline import canonical_sha256


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RouteModeFixture(StrictModel):
    duration_minutes: int = Field(gt=0)
    distance_meters: int = Field(gt=0)
    transfer_count: int = Field(ge=0)


class RouteEdgeFixture(StrictModel):
    edge_id: str = Field(pattern=r"^G01-MAP-E\d{3}$")
    origin: str
    destination: str
    walking: RouteModeFixture
    transit: RouteModeFixture


class RouteStopFixture(StrictModel):
    canonical_place_id: str
    name: str


class RoutePlanFixture(StrictModel):
    plan_id: str = Field(pattern=r"^G01-MAP-P\d{3}$")
    city: Literal["北京", "上海", "杭州"]
    stops: list[RouteStopFixture] = Field(min_length=5, max_length=5)
    edges: list[RouteEdgeFixture] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def edges_match_stops(self) -> "RoutePlanFixture":
        expected = [(left.name, right.name) for left, right in zip(self.stops, self.stops[1:])]
        actual = [(edge.origin, edge.destination) for edge in self.edges]
        if actual != expected:
            raise ValueError("fixture edges must match adjacent stops in order")
        return self


class MapPositiveFixture(StrictModel):
    schema_version: Literal["g01-map-positive-fixture-v1"]
    dataset_version: Literal["g01-map-positive-v1"]
    execution_mode: Literal["CONTROLLED_FIXTURE"]
    authority: Literal["NON_LIVE_SYNTHETIC_ROUTE_FACTS"]
    selection_policy: Literal["walking-within-10-minutes-v1"]
    observed_at: datetime
    external_calls: Literal[0]
    plans: list[RoutePlanFixture] = Field(min_length=30, max_length=30)


class MapPositiveValidationError(ValueError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_and_validate_fixture(data_root: Path) -> tuple[MapPositiveFixture, dict[str, Any]]:
    data_root = data_root.resolve(strict=True)
    fixture_path = data_root / "fixture.json"
    contract = json.loads((data_root / "dataset_contract.json").read_text(encoding="utf-8"))
    if contract.get("schema_version") != "g01-map-positive-dataset-contract-v1":
        raise MapPositiveValidationError("unexpected map fixture contract")
    if contract.get("fixture_sha256") != _sha256(fixture_path):
        raise MapPositiveValidationError("map fixture byte binding mismatch")
    backend_root = data_root.parents[1]
    generator = backend_root / "scripts" / "generate_g01_map_positive_fixture.py"
    if contract.get("generator_sha256") != _sha256(generator):
        raise MapPositiveValidationError("map fixture generator binding mismatch")
    fixture = MapPositiveFixture.model_validate_json(fixture_path.read_text(encoding="utf-8"))
    plan_ids = [plan.plan_id for plan in fixture.plans]
    edge_ids = [edge.edge_id for plan in fixture.plans for edge in plan.edges]
    route_pairs = [(edge.origin, edge.destination) for plan in fixture.plans for edge in plan.edges]
    if plan_ids != [f"G01-MAP-P{index:03d}" for index in range(1, 31)]:
        raise MapPositiveValidationError("plan IDs must be continuous")
    if edge_ids != [f"G01-MAP-E{index:03d}" for index in range(1, 121)]:
        raise MapPositiveValidationError("edge IDs must be continuous")
    if len(set(route_pairs)) != 120:
        raise MapPositiveValidationError("every positive edge must have a unique directed pair")
    city_counts = Counter(plan.city for plan in fixture.plans)
    if dict(city_counts) != {"北京": 10, "上海": 10, "杭州": 10}:
        raise MapPositiveValidationError("positive fixture must have 10 plans per deep city")
    return fixture, {
        "schema_version": "g01-map-positive-validation-receipt-v1",
        "valid": True,
        "plan_count": len(fixture.plans),
        "edge_count": len(edge_ids),
        "city_plan_counts": dict(city_counts),
        "unique_directed_edges": len(set(route_pairs)),
        "external_calls": 0,
        "live_provider_claim": "NOT_RUN",
    }


class PositiveFixtureRouteProvider:
    def __init__(self, fixture: MapPositiveFixture, fixture_sha256: str) -> None:
        self.fixture_sha256 = fixture_sha256
        self.routes = {
            (edge.origin, edge.destination): edge
            for plan in fixture.plans
            for edge in plan.edges
        }
        self.requests: list[tuple[str, str, str]] = []

    async def route(
        self,
        origin: MapStop,
        destination: MapStop,
        mode: Literal["walking", "transit"],
        *,
        observed_at: datetime,
    ) -> InternalRouteModeFact:
        key = (origin.name, destination.name)
        record = self.routes[key]
        value = record.walking if mode == "walking" else record.transit
        self.requests.append((origin.name, destination.name, mode))
        request = {
            "origin": origin.name,
            "destination": destination.name,
            "mode": mode,
            "route_config_hash": ROUTE_CONFIG_SHA256,
        }
        response = {"status": "AVAILABLE", **value.model_dump(mode="json")}
        return InternalRouteModeFact(
            mode=mode,
            status="AVAILABLE",
            duration_minutes=value.duration_minutes,
            distance_meters=value.distance_meters,
            transfer_count=value.transfer_count,
            response_hash=canonical_sha256(response),
            request_hash=canonical_sha256(request),
            provider_binding={
                "execution_mode": "controlled_fixture",
                "fixture_sha256": self.fixture_sha256,
                "authority": "NON_LIVE_SYNTHETIC_ROUTE_FACTS",
            },
            external_call_count=0,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(hours=24),
        )


class MatrixMapRepository:
    def __init__(self, plans: list[MapRenderPlan], observed_at: datetime) -> None:
        self._plans = {plan.understanding_id: plan for plan in plans}
        self._queue = deque(plan.understanding_id for plan in plans)
        self._jobs: dict[str, MapRenderJobRecord] = {}
        self.outputs: dict[str, MapRenderOutput] = {}
        self.failures: list[str] = []
        self.observed_at = observed_at

    async def claim_next_map(self, *, worker_id: str, now: datetime, lease_seconds: int):
        if not self._queue:
            return None
        understanding_id = self._queue.popleft()
        plan = self._plans[understanding_id]
        job = MapRenderJobRecord(
            map_job_id=f"job-{understanding_id}",
            understanding_id=understanding_id,
            plan_ref_id=f"ref-{understanding_id}",
            plan_ref=plan.plan_ref,
            route_config_hash=plan.route_config_hash,
            status="BUILDING",
            lease_owner=worker_id,
            lease_until=now + timedelta(seconds=lease_seconds),
            attempt=1,
            max_attempts=3,
            started_at=now,
        )
        self._jobs[job.map_job_id] = job
        return job

    async def load_map_plan(self, job: MapRenderJobRecord) -> MapRenderPlan:
        return self._plans[job.understanding_id]

    async def complete_map_job(self, job: MapRenderJobRecord, output: MapRenderOutput, *, now: datetime) -> bool:
        if output.plan_ref != job.plan_ref:
            raise ValueError("matrix output plan binding mismatch")
        self.outputs[job.understanding_id] = output
        return True

    async def fail_map_job(self, job: MapRenderJobRecord, *, category: str, now: datetime) -> None:
        self.failures.append(f"{job.map_job_id}:{category}")


def _plan(value: RoutePlanFixture) -> MapRenderPlan:
    stops = [
        MapStop(
            day_index=1,
            day_label="Day 1",
            sequence_index=index,
            name=stop.name,
            canonical_place_id=stop.canonical_place_id,
            resolution_status="AUTO_MATCHED",
        )
        for index, stop in enumerate(value.stops)
    ]
    return MapRenderPlan(
        understanding_id=value.plan_id,
        plan_ref=PlanRevisionRef(
            kind="UNDERSTANDING",
            aggregate_id=value.plan_id,
            revision=1,
            stop_set_hash=canonical_sha256([stop.model_dump(mode="json") for stop in stops]),
        ),
        route_config_hash=ROUTE_CONFIG_SHA256,
        stops=stops,
    )


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


async def run_positive_matrix(data_root: Path) -> dict[str, Any]:
    fixture, validation = load_and_validate_fixture(data_root)
    fixture_sha256 = _sha256(data_root / "fixture.json")
    provider = PositiveFixtureRouteProvider(fixture, fixture_sha256)
    plans = [_plan(value) for value in fixture.plans]
    observed_at = fixture.observed_at.astimezone(timezone.utc)
    repository = MatrixMapRepository(plans, observed_at)
    worker = MapRenderWorker(repository, renderer=MapRenderer(provider), lease_seconds=30)
    durations: list[float] = []
    for index in range(30):
        started = time.perf_counter()
        processed = await worker.run_once(f"g01-map-matrix-{index:02d}", now=observed_at)
        durations.append((time.perf_counter() - started) * 1000)
        if not processed:
            raise MapPositiveValidationError("worker stopped before processing all positive plans")
    if await worker.run_once("g01-map-matrix-drain", now=observed_at):
        raise MapPositiveValidationError("worker processed an unexpected extra job")
    outputs = list(repository.outputs.values())
    edges = [edge for output in outputs for edge in output.edges]
    usable_edges = [edge for edge in edges if edge.available]
    request_counts = Counter(provider.requests)
    duplicate_requests = sum(count - 1 for count in request_counts.values() if count > 1)
    selected_modes = Counter(edge.selected_mode for edge in usable_edges)
    return {
        "schema_version": "g01-map-positive-matrix-receipt-v1",
        "dataset_version": fixture.dataset_version,
        "validation": validation,
        "execution_scope": "IN_MEMORY_MAP_WORKER_CONTROLLED_FIXTURE",
        "plan_count": len(outputs),
        "edge_count": len(edges),
        "usable_edge_count": len(usable_edges),
        "usable_coverage": len(usable_edges) / len(edges),
        "ready_snapshot_count": sum(output.status == "READY" for output in outputs),
        "walking_mode_fact_count": sum(edge.walking.status == "AVAILABLE" for edge in edges),
        "transit_mode_fact_count": sum(edge.transit.status == "AVAILABLE" for edge in edges),
        "selected_mode_counts": dict(selected_modes),
        "worker_failure_count": len(repository.failures),
        "provider_request_count": len(provider.requests),
        "logical_duplicate_provider_requests": duplicate_requests,
        "external_calls": sum(
            edge.walking.external_call_count + edge.transit.external_call_count
            for edge in edges
        ),
        "worker_to_snapshot_p95_ms": _nearest_rank(durations, 0.95),
        "fixture_subgate": (
            "PASS"
            if len(outputs) == 30
            and len(edges) == 120
            and len(usable_edges) == 120
            and len(repository.failures) == 0
            and duplicate_requests == 0
            else "FAIL"
        ),
        "live_provider_claim": "NOT_RUN",
        "postgres_persistence_matrix_claim": "NOT_RUN",
        "full_text_card_gate_claim": "NOT_RUN",
    }


def run(data_root: Path) -> dict[str, Any]:
    return asyncio.run(run_positive_matrix(data_root.resolve(strict=True)))
