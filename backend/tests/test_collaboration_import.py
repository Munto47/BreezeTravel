from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.trip_understanding.collaboration_import import (
    CollaborationRouteUnavailableError,
    prepare_collaboration_import,
)
from app.trip_understanding.full_text import build_full_text_pipeline
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.worker import TripUnderstandingWorker


def _saved_route() -> dict:
    return {
        "city": "北京",
        "days": [
            {
                "dayIndex": 0,
                "date": "2026-09-06",
                "slots": [
                    {
                        "startTime": "09:00",
                        "endTime": "11:00",
                        "place": {
                            "placeId": "provider-secret-id",
                            "name": "故宫博物院",
                            "category": "attraction",
                            "address": "不应导入的地址",
                            "coords": {"lng": 116.39, "lat": 39.92},
                            "amapPhotos": ["https://private.invalid/photo.jpg"],
                            "ragMeta": {"sourceNoteIds": ["reference-id"]},
                            "retrieval_provider": "provider-name",
                        },
                        "transport": {"mode": "driving", "durationMins": 28},
                    },
                    {
                        "start_time": "13:30",
                        "end_time": "15:00",
                        "place": {"name": "景山公园", "category": "ATTRACTION"},
                    },
                ],
            }
        ],
    }


def _guard_args(prepared) -> dict:
    tokens = prepared.internal_binding["collaboration_place_guard_tokens"]
    city_token = prepared.internal_binding["collaboration_city_guard_token"]
    assert isinstance(tokens, list)
    assert all(isinstance(token, str) for token in tokens)
    return {
        "collaboration_guard_tokens": tuple(tokens),
        "collaboration_city_guard_token": (
            city_token if isinstance(city_token, str) else None
        ),
    }


def test_saved_collaboration_route_becomes_text_only_source() -> None:
    prepared = prepare_collaboration_import(
        user_id="account-owner",
        room_id="low-entropy-room-code",
        saved_itinerary_id="saved-route-uuid",
        city="北京",
        itinerary_data=_saved_route(),
        idempotency_key="owner-click-1",
    )
    assert prepared.source_text == (
        "北京1日行程。\n"
        "Day 1｜2026-09-06\n"
        "09:00 去故宫博物院（景点）。\n"
        "13:30 去景山公园（景点）。"
    )
    serialized = json.dumps(
        {
            "source": prepared.source_text,
            "binding": prepared.internal_binding,
            "request_hash": prepared.request_hash,
            "key": prepared.internal_idempotency_key,
        },
        ensure_ascii=False,
    )
    assert all(value not in serialized for value in (
        "low-entropy-room-code",
        "saved-route-uuid",
        "provider-secret-id",
        "private.invalid",
        "reference-id",
        "provider-name",
        "不应导入的地址",
    ))
    assert "116.39" not in prepared.source_text
    assert "28" not in prepared.source_text
    assert set(prepared.internal_binding) == {
        "status",
        "source_origin",
        "room_ref_hash",
        "saved_itinerary_ref_hash",
        "saved_content_hash",
        "normalized_text_hash",
        "collaboration_guard_version",
        "collaboration_place_guard_tokens",
        "collaboration_city_guard_token",
    }


@pytest.mark.asyncio
async def test_normalized_start_times_remain_visit_times_through_full_pipeline() -> None:
    prepared = prepare_collaboration_import(
        user_id="account-owner",
        room_id="room",
        saved_itinerary_id="saved",
        city="北京",
        itinerary_data=_saved_route(),
        idempotency_key="click",
    )

    output = await build_full_text_pipeline().run(
        prepared.source_text,
        **_guard_args(prepared),
    )
    cards = [card for day in output.public_result.days for card in day.activities]

    assert [(card.name, card.time_hint) for card in cards] == [
        ("故宫博物院", "09:00"),
        ("景山公园", "13:30"),
    ]


@pytest.mark.asyncio
async def test_guard_sequence_restarts_for_each_saved_day() -> None:
    route = _saved_route()
    route["days"].append(
        {
            "dayIndex": 1,
            "date": "2026-09-07",
            "slots": [
                {
                    "startTime": "09:30",
                    "place": {"name": "天坛公园", "category": "attraction"},
                }
            ],
        }
    )
    prepared = prepare_collaboration_import(
        user_id="account-owner",
        room_id="room",
        saved_itinerary_id="saved",
        city="北京",
        itinerary_data=route,
        idempotency_key="click",
    )

    output = await build_full_text_pipeline().run(
        prepared.source_text,
        **_guard_args(prepared),
    )
    cards = [card for day in output.public_result.days for card in day.activities]

    assert [(card.name, card.status) for card in cards] == [
        ("故宫博物院", "READY"),
        ("景山公园", "READY"),
        ("天坛公园", "READY"),
    ]
    assert output.resolution_receipt["attempted_count"] == 3


@pytest.mark.asyncio
async def test_one_saved_slot_cannot_expand_into_extra_confirmed_places() -> None:
    route = _saved_route()
    route["days"][0]["slots"] = [
        {
            "startTime": "09:00",
            "place": {
                "name": "故宫博物院 颐和园",
                "category": "attraction",
            },
        }
    ]
    prepared = prepare_collaboration_import(
        user_id="account-owner",
        room_id="room",
        saved_itinerary_id="saved",
        city="北京",
        itinerary_data=route,
        idempotency_key="click",
    )

    output = await build_full_text_pipeline().run(
        prepared.source_text,
        **_guard_args(prepared),
    )
    cards = [card for day in output.public_result.days for card in day.activities]

    assert len(cards) >= 2  # The controlled parser demonstrates the split attempt.
    assert all(card.status == "NEEDS_CONFIRMATION" for card in cards)
    assert all(card.name == "地点待确认" for card in cards)
    assert output.resolution_receipt["attempted_count"] == 0
    assert output.resolution_receipt["place_external_call_count"] == 0


@pytest.mark.asyncio
async def test_worker_carries_collaboration_guard_from_private_binding() -> None:
    route = _saved_route()
    route["days"][0]["slots"] = [
        {
            "place": {
                "name": "故宫博物院 颐和园",
                "category": "attraction",
            }
        }
    ]
    prepared = prepare_collaboration_import(
        user_id="account-owner",
        room_id="room",
        saved_itinerary_id="saved",
        city="北京",
        itinerary_data=route,
        idempotency_key="click",
    )
    repository = InMemoryTripUnderstandingRepository()
    service = TripUnderstandingApplicationService(repository)
    created = await service.create_from_collaboration(
        prepared,
        owner_user_id="account-owner",
    )

    assert await TripUnderstandingWorker(
        repository,
        full_pipeline=build_full_text_pipeline(),
    ).run_once("collaboration-guard-worker")
    resource = await service.authorize(
        created.accepted.public_resource_id,
        capability_hash=None,
        user_id="account-owner",
        now=datetime.now(timezone.utc),
    )
    stored = await repository.get_result(resource)

    assert stored is not None
    cards = [
        card
        for day in stored.result.days
        for card in day.activities
    ]
    assert len(cards) == 2
    assert all(card.status == "NEEDS_CONFIRMATION" for card in cards)
    assert all(card.name == "地点待确认" for card in cards)


@pytest.mark.parametrize(
    ("city", "name"),
    [
        ("北京", "故宫博物院。去颐和园"),
        ("北京", "https://example.invalid/place"),
        ("北京 Day 2", "故宫博物院"),
    ],
)
def test_collaboration_fields_cannot_inject_text_structure(
    city: str,
    name: str,
) -> None:
    route = _saved_route()
    route["days"][0]["slots"] = [
        {"place": {"name": name, "category": "attraction"}}
    ]
    if name != "故宫博物院":
        with pytest.raises(CollaborationRouteUnavailableError):
            prepare_collaboration_import(
                user_id="owner",
                room_id="room",
                saved_itinerary_id="saved",
                city=city,
                itinerary_data=route,
                idempotency_key="key",
            )
        return

    prepared = prepare_collaboration_import(
        user_id="owner",
        room_id="room",
        saved_itinerary_id="saved",
        city=city,
        itinerary_data=route,
        idempotency_key="key",
    )
    assert "Day 2" not in prepared.source_text
    assert prepared.internal_binding["collaboration_city_guard_token"] is None


def test_same_key_is_bound_to_room_and_full_content_not_row_identity() -> None:
    base = dict(
        user_id="account-owner",
        room_id="room-a",
        saved_itinerary_id="saved-a",
        city="北京",
        itinerary_data=_saved_route(),
        idempotency_key="same-click",
    )
    first = prepare_collaboration_import(**base)
    assert prepare_collaboration_import(**base) == first

    changed = _saved_route()
    changed["days"][0]["slots"][1]["place"]["name"] = "北海公园"
    content_changed = prepare_collaboration_import(**{**base, "itinerary_data": changed})
    row_changed = prepare_collaboration_import(**{**base, "saved_itinerary_id": "saved-b"})
    room_changed = prepare_collaboration_import(**{**base, "room_id": "room-b"})
    assert first.request_hash == row_changed.request_hash
    assert len({first.request_hash, content_changed.request_hash, room_changed.request_hash}) == 3
    assert first.internal_binding["saved_itinerary_ref_hash"] != row_changed.internal_binding["saved_itinerary_ref_hash"]
    assert first.internal_idempotency_key == content_changed.internal_idempotency_key


@pytest.mark.parametrize("payload", [{}, {"days": []}, {"days": [{"slots": []}]}, "not-json"])
def test_empty_or_damaged_saved_route_is_rejected(payload) -> None:
    with pytest.raises(CollaborationRouteUnavailableError):
        prepare_collaboration_import(
            user_id="owner",
            room_id="room",
            saved_itinerary_id="saved",
            city=None,
            itinerary_data=payload,
            idempotency_key="key",
        )
