from __future__ import annotations

from datetime import date

import pytest

from app.importing.entity_resolver import EntityResolver
from app.importing.models import RawStop, SourceSpan
from app.importing.parser import ItineraryTextParser
from app.importing.repositories import InMemoryImportRepository
from app.importing.service import ImportApplicationService
from app.itineraries.hash_service import compute_content_hash, with_content_hash
from app.itineraries.models import (
    CommitmentKind,
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository


SHANGHAI_COMPACT_TEXT = """第1天：14:00到上海虹桥站，14:30-17:30 上海迪士尼（已预约）
第2天：09:00-12:00 上海博物馆，12:30去浦东机场，14:00航班不可改，不要把航班当普通 POI；合理映射返程站点"""


class ExactProvider:
    async def search(self, *, query: str, city: str) -> list[dict]:
        place_ids = {
            "上海虹桥站": "hongqiao-railway",
            "上海迪士尼": "shanghai-disney",
            "上海博物馆": "shanghai-museum",
            "浦东机场": "pudong-airport",
        }
        place_id = place_ids.get(query)
        if place_id is None:
            return []
        return [{
            "place_id": place_id,
            "name": query,
            "city": city,
            "district": "浦东新区" if "浦东" in query or "迪士尼" in query else "长宁区",
            "address": "受控测试地址",
            "category": "transport" if "站" in query or "机场" in query else "attraction",
            "coords": {"lng": 121.47, "lat": 31.23},
            "retrieval_provider": "controlled_test",
            "execution_mode": "fixture",
            "retrieval_request_hash": "a" * 64,
            "retrieval_response_hash": "b" * 64,
            "retrieval_observed_at": "2026-08-21T00:00:00+00:00",
        }]


def test_compact_chinese_segments_preserve_commitment_roles_without_fake_flight_poi() -> None:
    draft = ItineraryTextParser().parse(SHANGHAI_COMPACT_TEXT, import_id="compact-commitments")

    assert draft.errors == []
    assert [(stop.day_index, stop.raw_name) for stop in draft.raw_stops] == [
        (0, "上海虹桥站"),
        (0, "上海迪士尼"),
        (1, "上海博物馆"),
        (1, "浦东机场"),
    ]
    assert [stop.commitment_kind for stop in draft.raw_stops] == [
        CommitmentKind.ARRIVAL,
        CommitmentKind.FIXED_VISIT,
        None,
        CommitmentKind.RETURN_DEPARTURE,
    ]
    assert draft.raw_stops[1].fixed_commitment is True
    assert draft.raw_stops[3].fixed_commitment is True
    assert all("航班不可改" != stop.raw_name for stop in draft.raw_stops)
    assert all("合理映射" not in stop.raw_name for stop in draft.raw_stops)
    for stop in draft.raw_stops:
        assert SHANGHAI_COMPACT_TEXT[stop.source_span.start:stop.source_span.end] == stop.source_sentence


def test_explicit_departure_transport_maps_to_terminal_place() -> None:
    draft = ItineraryTextParser().parse(
        "第1天：09:00 高铁离开上海虹桥站\n第2天：10:00 抵达浦东机场",
        import_id="terminal-commitments",
    )

    assert [(stop.raw_name, stop.commitment_kind) for stop in draft.raw_stops] == [
        ("上海虹桥站", CommitmentKind.RETURN_DEPARTURE),
        ("浦东机场", CommitmentKind.ARRIVAL),
    ]


@pytest.mark.asyncio
async def test_commitment_kind_flows_from_raw_import_into_revision_and_hash() -> None:
    itinerary_repository = InMemoryItineraryRepository()
    workspace = TripWorkspace(
        workspace_id="workspace-commitment-flow",
        room_id="room-commitment-flow",
        city="上海",
        trip_date_range=TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2)),
        created_by="commitment-user",
    )
    await itinerary_repository.create_workspace(workspace)
    service = ImportApplicationService(
        import_repository=InMemoryImportRepository(itinerary_repository),
        itinerary_repository=itinerary_repository,
        entity_resolver=EntityResolver(ExactProvider()),
    )

    itinerary_import = await service.create_import(
        workspace_id=workspace.workspace_id,
        source_type="AI_TEXT",
        raw_text=SHANGHAI_COMPACT_TEXT,
        actor_user_id="commitment-user",
    )
    applied = await service.apply_import(
        itinerary_import.import_id,
        actor_user_id="commitment-user",
    )
    stops = [stop for day in applied.revision.days for stop in day.stops]

    assert [stop.commitment_kind for stop in stops] == [
        CommitmentKind.ARRIVAL,
        CommitmentKind.FIXED_VISIT,
        None,
        CommitmentKind.RETURN_DEPARTURE,
    ]
    changed_stop = stops[0].model_copy(update={"commitment_kind": CommitmentKind.RETURN_DEPARTURE})
    changed_day = applied.revision.days[0].model_copy(
        update={"stops": [changed_stop, applied.revision.days[0].stops[1]]},
    )
    changed_revision = applied.revision.model_copy(
        update={"days": [changed_day, applied.revision.days[1]]},
    )
    assert compute_content_hash(changed_revision) != applied.revision.content_hash


def test_legacy_stop_json_without_commitment_kind_remains_valid() -> None:
    raw_stop = RawStop.model_validate({
        "raw_stop_id": "raw-legacy",
        "import_id": "import-legacy",
        "day_index": 0,
        "raw_name": "上海博物馆",
        "source_span": SourceSpan(start=0, end=5).model_dump(),
        "source_sentence": "上海博物馆",
        "fixed_commitment": False,
    })
    stop = ItineraryStop(
        stop_id="stop-legacy",
        place_id="place-legacy",
        day_index=0,
        order_index=0,
    )
    assert raw_stop.commitment_kind is None
    assert stop.commitment_kind is None

    content = ItineraryRevisionContent(
        itinerary_id="itinerary-legacy",
        workspace_id="workspace-legacy",
        revision=1,
        source_type=RevisionSource.IMPORT,
        city="上海",
        date_range=TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2)),
        days=[
            ItineraryDay(day_index=0, date=date(2026, 9, 1), stops=[stop]),
            ItineraryDay(day_index=1, date=date(2026, 9, 2), stops=[]),
        ],
        created_by="legacy-user",
    )
    assert len(with_content_hash(content).content_hash) == 64
