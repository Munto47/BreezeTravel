from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import suggestions as suggestions_api
from app.api import trip_workspaces as trip_workspaces_api
from app.audit.repositories import InMemoryAuditRepository
from app.config import Settings
from app.constraints.amap_types import typecodes_for_category
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.members.repositories import InMemoryMemberConstraintRepository
from app.schemas.place import Coordinates, PlaceCategory
from app.suggestions.frozen_snapshot import (
    FrozenSnapshotCandidateSource,
    FrozenSnapshotError,
    FrozenSnapshotRouteSource,
    FrozenSnapshotSpec,
    FrozenSuggestionSnapshot,
    validate_suggestion_provider_configuration,
)
from app.suggestions.models import SuggestionClassification, SuggestionIntent
from app.suggestions.providers import ProviderCandidateQuery
from app.suggestions.ranking import AnchorCandidateRanker, RankingContext
from app.suggestions.repositories import InMemorySuggestionRepository
from app.utils.auth import get_current_user
from evals.continuous import HttpResponse, run_builder_http


REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = (
    REPO_ROOT
    / "backend"
    / "evidence"
    / "real_provider_local_authorized"
    / "suggestion_snapshot_2026-08-21.json"
)
SNAPSHOT_RELATIVE = SNAPSHOT_PATH.relative_to(REPO_ROOT).as_posix()
SNAPSHOT_FILE_SHA256 = "9e93086e4c764ac7c5aa628d6e857a7b03f3a8939a9971a7631e27607a792c04"
SNAPSHOT_ID = "d64231204b8319cb488754afc331800dd0c51e41b2a2f3ba15194c3c2f2bc5bd"
BUILDER_SPEC = REPO_ROOT / "backend" / "evals" / "run_specs" / "dual-entry-builder-http-slice.json"
REPLAY_AT = datetime.fromisoformat("2026-08-21T04:11:29.399183+00:00")
ANCHORS = {
    "北京": ("B000A7BD6T", "故宫博物院", Coordinates(lng=116.3913, lat=39.9163)),
    "上海": ("B00155H52F", "外滩", Coordinates(lng=121.4896, lat=31.2393)),
    "杭州": ("B0FFHZ0001", "西湖风景名胜区", Coordinates(lng=120.1551, lat=30.2523)),
}
ALL_INTENTS = (
    SuggestionIntent.NEARBY,
    SuggestionIntent.POPULAR,
    SuggestionIntent.FUN,
    SuggestionIntent.FOOD,
)


def _spec(
    path: Path = SNAPSHOT_PATH,
    *,
    file_hash: str = SNAPSHOT_FILE_SHA256,
    snapshot_id: str = SNAPSHOT_ID,
) -> FrozenSnapshotSpec:
    return FrozenSnapshotSpec(
        path=path.relative_to(REPO_ROOT).as_posix(),
        file_sha256=file_hash,
        snapshot_id=snapshot_id,
    )


def _query(
    city: str,
    *,
    intents: tuple[SuggestionIntent, ...] = ALL_INTENTS,
    name: str | None = None,
) -> ProviderCandidateQuery:
    place_id, anchor_name, coords = ANCHORS[city]
    categories = (PlaceCategory.ATTRACTION, PlaceCategory.FOOD)
    keywords = ("附近", "热门", "口碑", "景点", "好玩", "美食", "餐厅")
    radius = 15_000
    if len(intents) == 1:
        intent = intents[0]
        categories = {
            SuggestionIntent.NEARBY: (PlaceCategory.ATTRACTION, PlaceCategory.FOOD),
            SuggestionIntent.POPULAR: (PlaceCategory.ATTRACTION, PlaceCategory.FOOD),
            SuggestionIntent.FUN: (PlaceCategory.ATTRACTION,),
            SuggestionIntent.FOOD: (PlaceCategory.FOOD,),
        }[intent]
        keywords = {
            SuggestionIntent.NEARBY: ("附近",),
            SuggestionIntent.POPULAR: ("热门", "口碑"),
            SuggestionIntent.FUN: ("景点", "好玩"),
            SuggestionIntent.FOOD: ("美食", "餐厅"),
        }[intent]
        radius = {
            SuggestionIntent.NEARBY: 5_000,
            SuggestionIntent.POPULAR: 15_000,
            SuggestionIntent.FUN: 12_000,
            SuggestionIntent.FOOD: 5_000,
        }[intent]
    return ProviderCandidateQuery(
        city=city,
        intents=intents,
        typecodes=tuple(
            dict.fromkeys(
                code for category in categories for code in typecodes_for_category(category)
            )
        ),
        radius_m=radius,
        anchor_name=name or anchor_name,
        anchor_place_id=place_id,
        anchor_coords=coords,
        keywords=keywords,
        transport_mode="walking",
    )


def _rewrite_snapshot(tmp_path: Path, mutate) -> FrozenSnapshotSpec:
    payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    mutate(payload)
    without_integrity = {key: value for key, value in payload.items() if key != "integrity"}
    snapshot_id = hashlib.sha256(
        json.dumps(
            without_integrity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload["integrity"] = {
        "artifact_payload_sha256": snapshot_id,
        "hash_algorithm": "SHA-256 over canonical UTF-8 JSON excluding integrity",
        "passed": True,
        "validation_errors": [],
    }
    path = tmp_path / "snapshot.json"
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    path.write_bytes(raw)
    return FrozenSnapshotSpec(
        path=path.name,
        file_sha256=hashlib.sha256(raw).hexdigest(),
        snapshot_id=snapshot_id,
    )


@pytest.mark.parametrize("city", ["北京", "上海", "杭州"])
@pytest.mark.asyncio
async def test_three_city_snapshot_returns_only_exact_original_receipts(city):
    source = FrozenSnapshotCandidateSource(_spec())
    routes = FrozenSnapshotRouteSource(_spec())
    batch = await source.search(_query(city))

    assert len(batch.candidates) == 6
    assert all(item.canonical_place.city == city for item in batch.candidates)
    assert all(item.provider_receipt.provider == "amap" for item in batch.candidates)
    assert all(item.provider_receipt.execution_mode.value == "live" for item in batch.candidates)
    assert all(item.current_facts for item in batch.candidates)
    first = batch.candidates[0]
    route = await routes.route_times(_query(city), first)
    assert route.status == "AVAILABLE"
    assert route.route_receipts[0].observed_at <= REPLAY_AT
    assert route.route_receipts[0].origin_place_id == ANCHORS[city][0]
    assert route.route_receipts[0].destination_place_id == first.canonical_place.place_id


@pytest.mark.parametrize("city", ["北京", "上海", "杭州"])
@pytest.mark.asyncio
async def test_far_popular_candidates_rank_after_four_suitable_options(city):
    result = await AnchorCandidateRanker(
        FrozenSnapshotCandidateSource(_spec()),
        FrozenSnapshotRouteSource(_spec()),
        route_prior_loader=None,
    ).rank(
        RankingContext(
            query=_query(city),
            allowed_categories=frozenset({PlaceCategory.ATTRACTION, PlaceCategory.FOOD}),
            selected_place_ids=frozenset({ANCHORS[city][0]}),
            selected_place_names=frozenset({ANCHORS[city][1]}),
            as_of=REPLAY_AT,
        )
    )

    suitable = [
        item
        for item in result.candidates
        if item.classification
        in {SuggestionClassification.ON_ROUTE, SuggestionClassification.ACCEPTABLE_DETOUR}
    ]
    deferred = [
        item
        for item in result.candidates
        if item.classification is SuggestionClassification.DEFER_TO_OTHER_DAY
    ]
    assert result.provider_status == "OK"
    assert len(suitable) == 4
    assert deferred
    assert max(item.rank_position for item in suitable) < min(item.rank_position for item in deferred)
    assert all(item.route_delta.delta_route_minutes <= 30 for item in result.acceptable_top3)


@pytest.mark.asyncio
async def test_single_captured_intent_supported_but_uncaptured_combination_rejected():
    source = FrozenSnapshotCandidateSource(_spec())
    food = await source.search(_query("北京", intents=(SuggestionIntent.FOOD,)))
    assert food.candidates
    assert all(item.canonical_place.category == PlaceCategory.FOOD.value for item in food.candidates)
    with pytest.raises(FrozenSnapshotError, match="INTENT_COMBINATION_NOT_CAPTURED"):
        await source.search(
            _query("北京", intents=(SuggestionIntent.NEARBY, SuggestionIntent.FOOD))
        )


@pytest.mark.asyncio
async def test_query_anchor_and_city_must_match_exactly():
    source = FrozenSnapshotCandidateSource(_spec())
    with pytest.raises(FrozenSnapshotError, match="QUERY_NOT_EXACT"):
        await source.search(_query("北京", name="故宫"))
    wrong_id = _query("北京").model_copy(update={"anchor_place_id": "CLIENT-SPOOFED-ID"})
    with pytest.raises(FrozenSnapshotError, match="QUERY_NOT_EXACT"):
        await source.search(wrong_id)
    wrong_coords = _query("北京").model_copy(
        update={"anchor_coords": Coordinates(lng=116.3914, lat=39.9163)}
    )
    with pytest.raises(FrozenSnapshotError, match="QUERY_NOT_EXACT"):
        await source.search(wrong_coords)
    wrong_city = _query("北京").model_copy(update={"city": "北京市"})
    with pytest.raises(FrozenSnapshotError, match="CITY_NOT_CAPTURED"):
        await source.search(wrong_city)


def test_byte_hash_and_payload_hash_tampering_both_fail_closed(tmp_path):
    path = tmp_path / "tampered.json"
    raw = SNAPSHOT_PATH.read_bytes().replace(
        "中山公园".encode(), "中山公园X".encode(), 1
    )
    path.write_bytes(raw)
    with pytest.raises(FrozenSnapshotError, match="FILE_HASH_MISMATCH"):
        FrozenSuggestionSnapshot.load(
            FrozenSnapshotSpec(
                path=path.name,
                file_sha256=SNAPSHOT_FILE_SHA256,
                snapshot_id=SNAPSHOT_ID,
            ),
            repo_root=tmp_path,
        )
    with pytest.raises(FrozenSnapshotError, match="PAYLOAD_HASH_MISMATCH"):
        FrozenSuggestionSnapshot.load(
            FrozenSnapshotSpec(
                path=path.name,
                file_sha256=hashlib.sha256(raw).hexdigest(),
                snapshot_id=SNAPSHOT_ID,
            ),
            repo_root=tmp_path,
        )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("evidence_class", "fixture", "EVIDENCE_CLASS_MISMATCH"),
        ("evidence_subtype", "other", "EVIDENCE_SUBTYPE_MISMATCH"),
        ("overall_status", "failed", "STATUS_NOT_PASSED"),
        ("claim_boundary", {}, "CLAIM_BOUNDARY_MISMATCH"),
    ],
)
def test_rehashed_but_wrong_authority_envelope_is_rejected(tmp_path, field, value, reason):
    spec = _rewrite_snapshot(tmp_path, lambda payload: payload.__setitem__(field, value))
    with pytest.raises(FrozenSnapshotError, match=reason):
        FrozenSuggestionSnapshot.load(spec, repo_root=tmp_path)


@pytest.mark.asyncio
async def test_expired_receipts_are_stale_without_timestamp_refresh():
    result = await AnchorCandidateRanker(
        FrozenSnapshotCandidateSource(_spec()),
        FrozenSnapshotRouteSource(_spec()),
        route_prior_loader=None,
    ).rank(
        RankingContext(
            query=_query("北京"),
            allowed_categories=frozenset({PlaceCategory.ATTRACTION, PlaceCategory.FOOD}),
            as_of=REPLAY_AT + timedelta(days=2),
        )
    )
    assert result.acceptable_top3 == ()
    assert all(item.evidence_freshness.status.value == "STALE" for item in result.candidates)
    assert min(item.provider_receipt.observed_at for item in result.candidates) < REPLAY_AT


@pytest.mark.asyncio
async def test_missing_route_remains_unknown_and_is_not_synthesized(tmp_path):
    def remove_route(payload):
        payload["cities"][0]["candidates"][0]["route_times"] = {
            "status": "UNKNOWN",
            "previous_to_candidate_minutes": None,
            "candidate_to_next_minutes": None,
            "previous_to_next_minutes": None,
            "route_receipts": [],
            "reason_code": "SNAPSHOT_ROUTE_NOT_CAPTURED",
        }

    spec = _rewrite_snapshot(tmp_path, remove_route)
    source = FrozenSnapshotCandidateSource(spec, repo_root=tmp_path)
    routes = FrozenSnapshotRouteSource(spec, repo_root=tmp_path)
    batch = await source.search(_query("北京"))
    route = await routes.route_times(_query("北京"), batch.candidates[0])
    assert route.status == "UNKNOWN"
    assert route.reason_code == "SNAPSHOT_ROUTE_NOT_CAPTURED"
    assert route.route_receipts == ()


def test_client_anchor_coordinates_without_any_live_origin_receipt_are_rejected(tmp_path):
    def remove_all_routes(payload):
        for city in payload["cities"]:
            for candidate in city["candidates"]:
                candidate["route_times"] = {
                    "status": "UNKNOWN",
                    "previous_to_candidate_minutes": None,
                    "candidate_to_next_minutes": None,
                    "previous_to_next_minutes": None,
                    "route_receipts": [],
                    "reason_code": "SNAPSHOT_ROUTE_NOT_CAPTURED",
                }

    spec = _rewrite_snapshot(tmp_path, remove_all_routes)
    with pytest.raises(FrozenSnapshotError, match="ANCHOR_PROVIDER_RECEIPT_MISSING"):
        FrozenSuggestionSnapshot.load(spec, repo_root=tmp_path)


def test_provider_mode_forbids_snapshot_live_fixture_mixing():
    valid = Settings(
        _env_file=None,
        runtime_profile="local_real",
        suggestion_provider_mode="frozen_snapshot",
        suggestion_snapshot_path=SNAPSHOT_RELATIVE,
        suggestion_snapshot_sha256=SNAPSHOT_FILE_SHA256,
        suggestion_snapshot_id=SNAPSHOT_ID,
        suggestion_snapshot_replay_at=REPLAY_AT,
    )
    validate_suggestion_provider_configuration(valid)
    with pytest.raises(FrozenSnapshotError, match="FIELDS_FORBIDDEN"):
        validate_suggestion_provider_configuration(
            valid.model_copy(update={"suggestion_provider_mode": "live"})
        )


def _repositories(city: str):
    place_id, name, coords = ANCHORS[city]
    dates = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
    revision = with_content_hash(
        ItineraryRevisionContent(
            itinerary_id=f"itinerary-{city}",
            workspace_id=f"workspace-{city}",
            revision=1,
            source_type=RevisionSource.IMPORT,
            city=city,
            date_range=dates,
            days=[
                ItineraryDay(
                    day_index=0,
                    date=dates.start,
                    stops=[
                        ItineraryStop(
                            stop_id="anchor-a",
                            place_id=place_id,
                            raw_name=name,
                            day_index=0,
                            order_index=0,
                        )
                    ],
                ),
                ItineraryDay(day_index=1, date=dates.end, stops=[]),
            ],
            change_summary={
                "map_stop_projections": {
                    "anchor-a": {
                        "place_id": place_id,
                        "canonical_name": name,
                        "coords": coords.model_dump(mode="json"),
                        "coordinate_role": "CANONICAL_PROVIDER_POI",
                        "provenance": "IMMUTABLE_PROVIDER_RECEIPT",
                        "receipt_hash": "a" * 64,
                    }
                }
            },
            created_by="snapshot-user",
        )
    )
    workspace = TripWorkspace(
        workspace_id=f"workspace-{city}",
        room_id=f"room-{city}",
        city=city,
        trip_date_range=dates,
        current_itinerary_revision=1,
        created_by="snapshot-user",
    )
    itineraries = InMemoryItineraryRepository()
    asyncio.run(itineraries.create_workspace(workspace, revision))
    return itineraries, InMemorySuggestionRepository(itineraries)


@pytest.mark.parametrize("city", ["北京", "上海", "杭州"])
def test_public_api_uses_snapshot_and_rejects_uncaptured_next_anchor(monkeypatch, city):
    itineraries, suggestions = _repositories(city)
    provider = suggestions_api.DefaultRankedSuggestionProvider(
        itineraries,
        candidate_source_factory=lambda _city, _now: FrozenSnapshotCandidateSource(_spec()),
        route_source_factory=lambda: FrozenSnapshotRouteSource(_spec()),
        route_prior_loader=None,
        clock=lambda: REPLAY_AT,
    )
    app = FastAPI()
    app.include_router(suggestions_api.router, prefix="/api")
    app.dependency_overrides[suggestions_api.get_itinerary_repository] = lambda: itineraries
    app.dependency_overrides[suggestions_api.get_suggestion_repository] = lambda: suggestions
    app.dependency_overrides[suggestions_api.get_ranked_suggestion_provider] = lambda: provider
    app.dependency_overrides[get_current_user] = lambda: "snapshot-user"
    monkeypatch.setattr(suggestions_api, "require_room_member", AsyncMock(return_value=None))
    monkeypatch.setattr(suggestions_api.settings, "suggestion_provider_mode", "frozen_snapshot")
    monkeypatch.setattr(suggestions_api.settings, "suggestion_snapshot_path", SNAPSHOT_RELATIVE)
    monkeypatch.setattr(suggestions_api.settings, "suggestion_snapshot_sha256", SNAPSHOT_FILE_SHA256)
    monkeypatch.setattr(suggestions_api.settings, "suggestion_snapshot_id", SNAPSHOT_ID)
    monkeypatch.setattr(suggestions_api.settings, "suggestion_snapshot_replay_at", REPLAY_AT)
    client = TestClient(app)

    created = client.post(
        f"/api/trip-workspaces/workspace-{city}/suggestion-sets",
        json={
            "base_revision": 1,
            "day_index": 0,
            "insert_after_stop_id": "anchor-a",
            "intents": [intent.value for intent in ALL_INTENTS],
            "session_id": f"snapshot-session-{city}",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert len(body["candidates"]) == 4
    assert all(item["route_delta"]["delta_route_minutes"] <= 30 for item in body["candidates"])
    assert all(item["provider_receipt"]["execution_mode"] == "live" for item in body["candidates"])

    candidate = body["candidates"][0]
    city_key = hashlib.sha256(city.encode("utf-8")).hexdigest()[:12]
    accepted = client.post(
        f"/api/trip-workspaces/workspace-{city}/suggestion-sets/{body['suggestion_set_id']}"
        f"/candidates/{candidate['candidate_id']}:accept",
        headers={"If-Match": '"1"', "Idempotency-Key": f"accept-{city_key}"},
    )
    assert accepted.status_code == 200, accepted.text
    second = client.post(
        f"/api/trip-workspaces/workspace-{city}/suggestion-sets",
        json={
            "base_revision": 2,
            "day_index": 0,
            "insert_after_stop_id": accepted.json()["stop_id"],
            "intents": [intent.value for intent in ALL_INTENTS],
            "session_id": f"snapshot-session-{city}",
        },
    )
    assert second.status_code == 503
    assert second.json()["detail"]["reason_code"] == "SUGGESTION_PROVIDER_FAILED"


class _AsgiTransport:
    def __init__(self, client: TestClient):
        self.client = client

    def request(self, method, url, *, headers, json_body, timeout_seconds):
        del timeout_seconds
        response = self.client.request(method, url, headers=dict(headers), json=json_body)
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = response.text
        return HttpResponse(response.status_code, dict(response.headers), body)


def test_checked_in_builder_runner_reaches_real_asgi_and_fails_on_uncaptured_second_hop(
    tmp_path,
    monkeypatch,
):
    itineraries = InMemoryItineraryRepository()
    suggestions = InMemorySuggestionRepository(itineraries)
    audits = InMemoryAuditRepository()
    members = InMemoryMemberConstraintRepository(itineraries)
    provider = suggestions_api.DefaultRankedSuggestionProvider(
        itineraries,
        candidate_source_factory=lambda _city, _now: FrozenSnapshotCandidateSource(_spec()),
        route_source_factory=lambda: FrozenSnapshotRouteSource(_spec()),
        clock=lambda: REPLAY_AT,
    )
    app = FastAPI()

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "suggestion_provider": {
                "mode": "frozen_snapshot",
                "snapshot_id": SNAPSHOT_ID,
                "snapshot_sha256": SNAPSHOT_FILE_SHA256,
                "replay_at": REPLAY_AT.isoformat(),
            },
        }

    @app.post("/api/auth/test-login")
    def login():
        return {"token": "snapshot-test-token", "user_id": "snapshot-user"}

    @app.post("/api/room")
    def create_room():
        return {"status": "ok", "room_id": "snapshot-room"}

    @app.get("/api/trip-workspaces/{workspace_id}/resume")
    def resume(workspace_id: str):
        return {"workspace_id": workspace_id, "authority": "test-asgi-readback"}

    app.include_router(trip_workspaces_api.router, prefix="/api")
    app.include_router(suggestions_api.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: "snapshot-user"
    app.dependency_overrides[trip_workspaces_api.get_itinerary_repository] = lambda: itineraries
    app.dependency_overrides[trip_workspaces_api.get_audit_repository] = lambda: audits
    app.dependency_overrides[trip_workspaces_api.get_member_constraint_repository] = lambda: members
    app.dependency_overrides[trip_workspaces_api.get_suggestion_repository] = lambda: suggestions
    app.dependency_overrides[suggestions_api.get_itinerary_repository] = lambda: itineraries
    app.dependency_overrides[suggestions_api.get_suggestion_repository] = lambda: suggestions
    app.dependency_overrides[suggestions_api.get_ranked_suggestion_provider] = lambda: provider
    monkeypatch.setattr(trip_workspaces_api, "require_room_member", AsyncMock(return_value=None))
    monkeypatch.setattr(suggestions_api, "require_room_member", AsyncMock(return_value=None))
    monkeypatch.setattr(suggestions_api.settings, "suggestion_provider_mode", "frozen_snapshot")
    monkeypatch.setattr(suggestions_api.settings, "suggestion_snapshot_path", SNAPSHOT_RELATIVE)
    monkeypatch.setattr(suggestions_api.settings, "suggestion_snapshot_sha256", SNAPSHOT_FILE_SHA256)
    monkeypatch.setattr(suggestions_api.settings, "suggestion_snapshot_id", SNAPSHOT_ID)
    monkeypatch.setattr(suggestions_api.settings, "suggestion_snapshot_replay_at", REPLAY_AT)

    result = run_builder_http(
        BUILDER_SPEC,
        runs_root=tmp_path / "runs",
        transport=_AsgiTransport(TestClient(app)),
        environ={},
    )

    assert result.gate["decision"] == "REJECT"
    assert result.gate["execution"]["attempted"] is True
    assert result.gate["execution"]["provider_handshake"]["snapshot_id"] == SNAPSHOT_ID
    transactions = [
        json.loads(line)
        for line in (result.run_dir / "http_transactions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(item["step"] == "create_suggestion_set_1" and item["status_code"] == 201 for item in transactions)
    assert any(item["step"] == "accept_candidate_1" and item["status_code"] == 200 for item in transactions)
    assert any(item["step"] == "create_suggestion_set_2" and item["status_code"] == 503 for item in transactions)
    outputs = [
        json.loads(line)
        for line in (result.run_dir / "product_outputs.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(item["failure_code"] == "SUGGESTION_PROVIDER_UNAVAILABLE" for item in outputs)
