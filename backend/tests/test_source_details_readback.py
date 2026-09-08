"""Source-bound internal visits remain under their parent through saved edits.

External responses are fixed; PostgreSQL cases use disposable databases from
repository_for and never reset the running experience database.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest
from pydantic import model_serializer

from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.errors import ResourceAccessDeniedError, SourceUnavailableError
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.models import (
    ActivityDeleteCommand, ActivityMoveCommand, ActivityRole, ActivityTextEditCommand,
    CreateFullRequest, PlaceConfirmCommand, PlaceReplaceCommand, SourceDetailView, UndoCommand, UserFacingTripResult,
)
from app.trip_understanding.pipeline import PublicResultProjector, TripUnderstandingPipeline
from tests.test_experience_source_anchors import CapturedClient, provider
from tests.test_experience_v3_journey import repository_for


SOURCE = "北京一日游。\nDay1：故宫博物院，园内路线：太和殿、乾清宫。之后去景山公园。"
OPTIONAL_SOURCE = SOURCE.replace("之后去景山公园", "若有时间，可看园内珍宝馆。之后去景山公园")
DETAILS = [{"name": "太和殿", "optional": False}, {"name": "乾清宫", "optional": False}]


def raw_response(*, optional=False):
    result = {"destination": "北京", "day_labels": ["Day1"], "activities": [
        {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1,
         "category": "景点", "city": "北京", "city_evidence": "北京"}
        for name in ("故宫博物院", "太和殿", "乾清宫", "景山公园")]}
    if optional:
        result["activities"].insert(3, {"source_quote": "珍宝馆", "place_name": "珍宝馆", "role": "OPTIONAL",
            "day_index": 1, "category": "景点", "city": "北京", "city_evidence": "北京",
            "parent_source_quote": "故宫博物院", "role_evidence": "若有时间，可看园内珍宝馆"})
    return result


class RecordingPlaces(ControlledSnapshotPlaceResolver):
    def __init__(self):
        self.calls = []

    async def resolve(self, **query):
        self.calls.append((query["city"], query["atomic_place_name"]))
        return await super().resolve(**query)


async def build_source_details_result():
    client = CapturedClient(raw_response())
    places = RecordingPlaces()
    result = await TripUnderstandingPipeline(provider(client), places).run(SOURCE)
    assert len(client.calls) == 1
    assert places.calls == [("北京", "故宫博物院"), ("北京", "景山公园")]
    return result


def details(card):
    return [item.model_dump(mode="json") for item in card.source_details]


def cards(result):
    return [card for day in result.days for card in day.activities]


@pytest.mark.asyncio
async def test_fixed_provider_internal_names_are_visible_without_extra_cards_or_lookup():
    from evals.g07_text_convergence_v1.runner import _public_payload_is_redacted

    output = await build_source_details_result()
    assert [card.name for card in cards(output.public_result)] == ["故宫博物院", "景山公园"]
    assert details(cards(output.public_result)[0]) == DETAILS
    assert cards(output.public_result)[1].source_details == []
    assert output.public_result.coverage.complete
    assert output.public_result.coverage.confirmed_place_count == 2
    assert output.resolution_receipt["attempted_count"] == 2
    payload = output.public_result.model_dump(mode="json")
    assert _public_payload_is_redacted(payload)
    for field in ("span_start", "source_quote", "parent_mention_id", "mention_id", "activity_token"):
        poisoned = {"source_details": [{**DETAILS[0], field: "private-parent"}]}
        with pytest.raises(ValueError):
            SourceDetailView.model_validate(poisoned["source_details"][0])
        if field != "activity_token":
            assert not _public_payload_is_redacted(poisoned)
        assert field not in payload["days"][0]["activities"][0]["source_details"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [ActivityRole.PLANNED, ActivityRole.OPTIONAL, ActivityRole.REFERENCE])
async def test_explicit_internal_relations_use_parent_exit_for_each_supported_role(role):
    plan = (await build_source_details_result()).proposal.model_copy(deep=True)
    for mention in plan.mentions:
        if mention.parent_mention_id:
            mention.role = role
    places = RecordingPlaces()
    result = await TripUnderstandingPipeline(None, places).run(SOURCE, prepared_plan=plan)
    assert len(cards(result.public_result)) == 2
    assert len(places.calls) == 2
    assert result.public_result.days[0].alternatives == []
    assert details(cards(result.public_result)[0]) == [
        {**detail, "optional": role == ActivityRole.OPTIONAL} for detail in DETAILS]
    assert result.public_result.coverage.complete
    assert result.public_result.coverage.recognized_place_count == 2


@pytest.mark.asyncio
async def test_only_explicit_parent_relations_are_shown_and_parent_day_controls_location():
    output = await build_source_details_result()
    activities = [item.model_copy(deep=True) for item in output.activities]
    by_name = {item.compiled.mention.atomic_place_name: item.compiled.mention for item in activities}
    by_name["故宫博物院"].day_index = 2
    # Child order is explicit, not the order of the compiled list or its old day.
    by_name["太和殿"].sequence_index = 2
    by_name["乾清宫"].sequence_index = 1
    public = PublicResultProjector().project("北京", output.proposal.destination_basis,
        list(reversed(activities)), day_count=2, include_alternatives=True)
    assert public.days[0].activities[0].name == "景山公园"
    assert public.days[0].activities[0].source_details == []
    assert [item.name for item in public.days[1].activities[0].source_details] == ["乾清宫", "太和殿"]
    by_name["太和殿"].parent_mention_id = None
    by_name["乾清宫"].role = ActivityRole.EXCLUDED
    public = PublicResultProjector().project("北京", output.proposal.destination_basis, activities, day_count=2)
    assert all(card.source_details == [] for card in cards(public))


@pytest.mark.asyncio
async def test_dropped_internal_names_cannot_claim_complete_projection():
    class DroppingProjector(PublicResultProjector):
        def project(self, *args, **kwargs):
            result = super().project(*args, **kwargs)
            for card in cards(result):
                card.source_details = []
            return result

    output = await TripUnderstandingPipeline(provider(CapturedClient(raw_response())), RecordingPlaces(),
        projector=DroppingProjector()).run(SOURCE)
    assert not output.public_result.coverage.complete
    assert sum(issue.category == "PUBLIC_PROJECTION_OMISSION" for issue in output.proposal.diagnostics) == 2


@pytest.mark.asyncio
async def test_time_edit_keeps_details_but_parent_name_edit_clears_them():
    original = (await build_source_details_result()).public_result
    parent = cards(original)[0]
    timed = apply_public_command(original, ActivityTextEditCommand(command_type="ACTIVITY_TEXT_EDIT",
        activity_token=parent.activity_token, time_hint="上午" )).result
    assert details(cards(timed)[0]) == DETAILS
    renamed = apply_public_command(timed, ActivityTextEditCommand(command_type="ACTIVITY_TEXT_EDIT",
        activity_token=cards(timed)[0].activity_token, name="北海公园")).result
    assert cards(renamed)[0].source_details == []
    assert details(cards(original)[0]) == DETAILS


def test_missing_or_ambiguous_historical_relationships_do_not_hide_supplementary_items():
    from app.trip_understanding.readback import _with_source_relationships

    row = {"day_index": 1, "sequence_index": 2, "role": "OPTIONAL"}
    relation = {**row, "parent_mention_id": "parent", "relation_type": "INTERNAL_DETAIL"}
    for structure in ([], [relation, relation], [{**relation, "day_index": 2}]):
        assert _with_source_relationships([row], structure) == [row]
    assert _with_source_relationships([row], [relation])[0]["parent_mention_id"] == "parent"


@pytest.mark.asyncio
async def test_confirmation_without_old_identity_never_keeps_details_on_a_different_name():
    from app.trip_understanding.candidates import CandidatePlace, GCJ02Position

    original = (await build_source_details_result()).public_result
    parent = cards(original)[0]
    for name, expected in ((parent.name, DETAILS), ("故宫", []), ("北海公园", [])):
        selected = CandidatePlace(canonical_place_id="fixed-candidate", name=name, city="北京", category="景点",
            area_or_address="固定测试地址", position=GCJ02Position(longitude=116.4, latitude=39.9))
        updated = apply_public_command(original, PlaceConfirmCommand(command_type="PLACE_CONFIRM",
            activity_token=parent.activity_token, candidate_token="fixed-validation-happens-at-repository-boundary"),
            confirmed_place=selected).result
        assert details(cards(updated)[0]) == expected


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_parent_details_survive_save_move_replace_delete_undo_and_source_erasure(kind):
    from app.trip_understanding.candidates import CandidatePlace, GCJ02Position, issue_candidate
    from app.trip_understanding.repository import PostgresTripUnderstandingRepository
    from app.trip_understanding.route_geometry import InMemoryRouteGeometryCache
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from app.trip_understanding.source_crypto import SourceCipher
    from app.trip_understanding.worker import TripUnderstandingWorker

    async with repository_for(kind) as repository:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repository)
        expected_details = [*DETAILS, {"name": "珍宝馆", "optional": True}]
        client = CapturedClient(raw_response(optional=True))
        places = RecordingPlaces()
        created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL",
            "source": {"type": "TEXT", "text": OPTIONAL_SOURCE}}), owner_user_id="experience-owner",
            idempotency_key="source-details-create", now=now)
        captured = []
        load_source = repository.load_source

        async def capture(job, *, now):
            captured.append(job)
            return await load_source(job, now=now)

        repository.load_source = capture
        worker = TripUnderstandingWorker(repository, full_pipeline=TripUnderstandingPipeline(provider(client), places))
        assert await worker.run_once("details-worker", now=now)
        assert len(client.calls) == 1 and len(places.calls) == 2
        reader = (PostgresTripUnderstandingRepository(repository._pool, SourceCipher("experience-controlled-test-secret"),
            InMemoryRouteGeometryCache()) if kind == "postgres" else repository)

        async def read():
            resource = await reader.authorize(created.accepted.public_resource_id,
                capability_hash=None, user_id="experience-owner", now=now)
            supplementary = await reader.get_supplementary_view(resource, now=now)
            assert all(item.name != "珍宝馆" for day in supplementary.days for item in day.items)
            return resource, await reader.get_result(resource)

        resource, stored = await read()
        assert stored.result.ownership == "ACCOUNT"
        assert details(cards(stored.result)[0]) == expected_details
        with pytest.raises(ResourceAccessDeniedError):
            await reader.authorize(created.accepted.public_resource_id, capability_hash=None,
                user_id="not-the-owner", now=now)
        original_result_id = resource.current_result_id
        await service.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=cards(stored.result)[0].activity_token, target_day_index=2, target_position=0),
            expected_etag=stored.opaque_etag, idempotency_key="details-move", now=now)
        resource, stored = await read()
        assert stored.result.days[0].activities[0].name == "景山公园"
        assert details(stored.result.days[1].activities[0]) == expected_details
        assert resource.current_result_id != original_result_id

        original_place = await ControlledSnapshotPlaceResolver().resolve(city="北京", atomic_place_name="故宫博物院", category_hint="景点")
        for index, (identity, name, same_parent) in enumerate([
            (original_place.canonical_place_id, "故宫", True),
            ("fixed-other-parent", "故宫博物院", False),
            ("fixed-beihai-parent", "北海公园", False),
        ]):
            parent = stored.result.days[1].activities[0]
            selected = CandidatePlace(canonical_place_id=identity, name=name, city="北京", category="景点",
                area_or_address="固定测试地址", position=GCJ02Position(longitude=116.4, latitude=39.9))
            candidate = issue_candidate(selected, public_resource_id=resource.public_resource_id,
                activity_token=parent.activity_token, expected_etag=stored.opaque_etag, now=now)
            await service.apply_command(resource, PlaceConfirmCommand(command_type="PLACE_CONFIRM",
                activity_token=parent.activity_token, candidate_token=candidate.candidate_token),
                expected_etag=stored.opaque_etag, idempotency_key=f"details-confirm-{index}", now=now)
            resource, changed = await read()
            assert changed.result.days[1].activities[0].name == name
            assert details(changed.result.days[1].activities[0]) == (expected_details if same_parent else [])
            await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=changed.opaque_etag,
                idempotency_key=f"details-undo-confirm-{index}", now=now)
            resource, stored = await read()
            assert details(stored.result.days[1].activities[0]) == expected_details

        for action in ("replace", "rename", "delete"):
            parent = stored.result.days[1].activities[0]
            if action == "replace":
                command = PlaceReplaceCommand(command_type="PLACE_REPLACE", activity_token=parent.activity_token,
                    replacement={"name": "北海公园", "category": "景点", "area_or_address": "北京"})
            elif action == "rename":
                command = ActivityTextEditCommand(command_type="ACTIVITY_TEXT_EDIT", activity_token=parent.activity_token, name="北海公园")
            else:
                command = ActivityDeleteCommand(command_type="ACTIVITY_DELETE", activity_token=parent.activity_token)
            await service.apply_command(resource, command, expected_etag=stored.opaque_etag,
                idempotency_key="details-" + action, now=now)
            resource, changed = await read()
            assert all(card.source_details == [] for card in cards(changed.result))
            await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=changed.opaque_etag,
                idempotency_key="details-undo-" + action, now=now)
            resource, stored = await read()
            assert stored.result.days[1].activities[0].name == "故宫博物院"
            assert details(stored.result.days[1].activities[0]) == expected_details

        await service.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=stored.result.days[1].activities[0].activity_token, target_day_index=1, target_position=0),
            expected_etag=stored.opaque_etag, idempotency_key="details-move-before-erase", now=now)
        resource, stored = await read()
        await service.delete_source(resource, user_id="experience-owner", idempotency_key="details-erase", now=now)
        with pytest.raises(SourceUnavailableError):
            await load_source(captured[0], now=now)
        resource, stored = await read()
        assert details(stored.result.days[0].activities[0]) == expected_details
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=stored.opaque_etag,
            idempotency_key="details-undo-after-erase", now=now)
        resource, stored = await read()
        assert details(stored.result.days[1].activities[0]) == expected_details
        with pytest.raises(SourceUnavailableError):
            await load_source(captured[0], now=now)

        if kind == "postgres":
            # Write an old-shape result at its initial insertion, preserving
            # PostgreSQL's immutable-result trigger rather than bypassing it.
            class LegacyResult(UserFacingTripResult):
                @model_serializer(mode="wrap")
                def old_shape(self, handler):
                    payload = handler(self)
                    for day in payload["days"]:
                        for card in day["activities"]:
                            card.pop("source_details", None)
                    return payload

            old_created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL",
                "source": {"type": "TEXT", "text": OPTIONAL_SOURCE}}), owner_user_id="experience-owner",
                idempotency_key="legacy-details-create", now=now)
            old_job = await repository.claim_next(worker_id="legacy-fixture-writer", now=now, lease_seconds=60)
            old_output = await TripUnderstandingPipeline(provider(CapturedClient(raw_response(optional=True))), RecordingPlaces()).run(OPTIONAL_SOURCE)
            old_output.public_result = LegacyResult.model_validate(old_output.public_result.model_dump())
            await repository.complete_job(old_job, old_output, now=now)
            old_resource = await reader.authorize(old_created.accepted.public_resource_id,
                capability_hash=None, user_id="experience-owner", now=now)
            old_json = json.loads(await repository._pool.fetchval(
                "SELECT public_json FROM trip_understanding_results WHERE result_id=$1", old_resource.current_result_id))
            assert all("source_details" not in card for day in old_json["days"] for card in day["activities"])
            legacy = await reader.get_result(old_resource)
            assert all(card.source_details == [] for card in cards(legacy.result))
            old_supplementary = await reader.get_supplementary_view(old_resource, now=now)
            assert any(item.name == "珍宝馆" for day in old_supplementary.days for item in day.items)
            await service.apply_command(old_resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
                activity_token=legacy.result.days[0].activities[0].activity_token, target_day_index=2, target_position=0),
                expected_etag=legacy.opaque_etag, idempotency_key="legacy-details-move", now=now)
            old_resource = await reader.authorize(old_created.accepted.public_resource_id,
                capability_hash=None, user_id="experience-owner", now=now)
            old_supplementary = await reader.get_supplementary_view(old_resource, now=now)
            assert any(item.name == "珍宝馆" for day in old_supplementary.days for item in day.items)
