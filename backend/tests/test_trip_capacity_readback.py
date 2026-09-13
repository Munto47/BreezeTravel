"""Capacity through the actual provider/pipeline and isolated PostgreSQL.

Model responses and place identities are controlled synthetic data. These
checks measure supported capacity and persisted editing, not live accuracy.
The pure build_capacity_result helper is also used by the browser/PNG checks.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from types import SimpleNamespace

import pytest

from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.models import (
    ActivityMoveCommand, ActivityTextEditCommand, CreateFullRequest,
    PipelineOutput, ResolvedPlace, UndoCommand,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline


def capacity_source(count: int = 160, day_count: int = 14) -> tuple[str, list[list[str]]]:
    if not 1 <= day_count <= 14 or not day_count <= count <= 160:
        raise ValueError("Capacity fixture requires 1–14 populated days and at most 160 places")
    groups = []
    offset = 0
    for day in range(day_count):
        size = count // day_count + (day < count % day_count)
        groups.append([f"容量测试{index:03d}公园" for index in range(offset + 1, offset + size + 1)])
        offset += size
    source = "北京容量测试，所有地点是模拟测试资料。\n" + "\n".join(
        f"Day{day}：" + "、".join(names) + "。" for day, names in enumerate(groups, 1))
    return source, groups


class CapacityModelClient:
    """Return fixed semantic data for the real whole-document/day-scope adapter."""
    def __init__(self, groups: list[list[str]]):
        self.groups = groups
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        if "只分析旅行原文的全局结构" in prompt:
            payload = {"cross_day_dependencies": False, "sections": [
                {"day_index": day, "start_quote": f"Day{day}："}
                for day in range(1, len(self.groups) + 1)]}
        else:
            scope = re.search(r"本次只整理原文第(\d+)天", prompt)
            selected_day = int(scope[1]) if scope else None
            payload = {"destination": "北京",
                       "activities": [{"source_quote": name, "place_name": name,
                                       "day_index": day, "role": "PLANNED", "category": "景点"}
                                      for day, names in enumerate(self.groups, 1)
                                      for name in names if selected_day in {None, day}]}
        return SimpleNamespace(model="controlled-capacity-model",
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=100),
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))])


class CapacityPlaces:
    async def resolve(self, *, city, atomic_place_name, category_hint=None):
        return ResolvedPlace(canonical_place_id="synthetic-capacity:" + atomic_place_name,
            name=atomic_place_name, category="景点", area_or_address="容量测试模拟地址",
            provider_binding={"city": city, "synthetic": True})


async def build_capacity_result(count: int = 160, day_count: int = 14, *, relative_only: bool = False) -> PipelineOutput:
    source, groups = capacity_source(count, day_count)
    provider = ExperienceQwenProvider(api_key="controlled-test-only", base_url="https://capacity.invalid/v1",
        model="controlled-capacity-model", deadline_seconds=60, max_output_tokens=4096,
        client=CapacityModelClient(groups), relative_only=relative_only, enable_source_visits=relative_only)
    return await TripUnderstandingPipeline(provider, CapacityPlaces(), relative_only=relative_only).run(source)


def names_of(result) -> list[list[str]]:
    return [[card.name for card in day.activities] for day in result.days]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("count,day_count", [(80, 1), (81, 1), (160, 1), (160, 14)])
async def test_relative_capacity_last_place_move_replace_undo_redo_survives_fresh_pg_reader(count, day_count):
    from app.trip_understanding.candidates import CandidatePlace, GCJ02Position, issue_candidate
    from app.trip_understanding.models import PlaceConfirmCommand, RedoCommand
    from app.trip_understanding.repository import PostgresTripUnderstandingRepository
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from app.trip_understanding.source_crypto import SourceCipher
    from tests.test_experience_v3_journey import repository_for

    source, expected = capacity_source(count, day_count)
    output = await build_capacity_result(count, day_count, relative_only=True)
    assert names_of(output.public_result) == expected
    assert output.resolution_receipt["attempted_count"] == count
    assert output.resolution_receipt["budget_limited_count"] == 0
    async with repository_for("postgres") as repository:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repository)
        created = await service.create_full(CreateFullRequest.model_validate({
            "mode": "FULL", "source": {"type": "TEXT", "text": source}}),
            owner_user_id="experience-owner", idempotency_key="relative-capacity", now=now)
        job = await repository.claim_next(worker_id="relative-capacity", now=now, lease_seconds=60)
        await repository.complete_job(job, output, now=now)
        versions = set()

        async def reread(wanted):
            reader = PostgresTripUnderstandingRepository(repository._pool, SourceCipher("experience-controlled-test-secret"))
            resource = await reader.authorize(created.accepted.public_resource_id,
                capability_hash=None, user_id="experience-owner", now=now)
            stored = await reader.get_result(resource)
            assert names_of(stored.result) == wanted
            assert stored.result.coverage.complete
            assert stored.result.coverage.confirmed_place_count == count
            assert len(stored.result.days) == day_count
            for day in stored.result.days:
                for card in day.activities:
                    assert card.start_time is None and card.end_time is None
                    assert card.visit_duration_minutes is None and card.time_hint is None
            assert stored.opaque_etag not in versions
            versions.add(stored.opaque_etag)
            return resource, stored

        resource, stored = await reread(expected)
        map_jobs = await repository._pool.fetchval("SELECT count(*) FROM trip_map_render_jobs")
        moved = [list(names) for names in expected]
        moved[-1].append(moved[0].pop(0))

        async def apply(command, wanted):
            nonlocal resource, stored
            await service.apply_command(resource, command, expected_etag=stored.opaque_etag,
                idempotency_key=f"relative-capacity-{len(versions)}", now=now)
            resource, stored = await reread(wanted)
            assert await repository._pool.fetchval("SELECT count(*) FROM trip_map_render_jobs") == map_jobs

        await apply(ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=stored.result.days[0].activities[0].activity_token,
            target_day_index=day_count, target_position=len(expected[-1]) - (day_count == 1)), moved)
        await apply(UndoCommand(command_type="UNDO"), expected)
        assert stored.result.can_redo
        await apply(RedoCommand(command_type="REDO"), moved)

        last = stored.result.days[-1].activities[-1]
        candidate = issue_candidate(CandidatePlace(canonical_place_id="synthetic-capacity:replacement",
            city="北京", name="容量替换末位公园", category="景点", area_or_address="固定测试地址",
            position=GCJ02Position(longitude=116.4, latitude=39.9)),
            public_resource_id=resource.public_resource_id, activity_token=last.activity_token,
            expected_etag=stored.opaque_etag, now=now)
        replaced = [list(names) for names in moved]
        replaced[-1][-1] = "容量替换末位公园"
        await apply(PlaceConfirmCommand(command_type="PLACE_CONFIRM", activity_token=last.activity_token,
            candidate_token=candidate.candidate_token), replaced)
        await apply(UndoCommand(command_type="UNDO"), moved)
        await apply(RedoCommand(command_type="REDO"), replaced)
        assert len(versions) == 7


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("count,day_count", [(80, 1), (81, 1), (160, 1), (160, 14)])
async def test_capacity_survives_postgres_last_position_edit_undo_and_new_reader(count, day_count):
    from app.trip_understanding.repository import PostgresTripUnderstandingRepository
    from app.trip_understanding.route_geometry import InMemoryRouteGeometryCache
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from app.trip_understanding.source_crypto import SourceCipher
    from tests.test_experience_v3_journey import repository_for

    source, expected = capacity_source(count, day_count)
    output = await build_capacity_result(count, day_count)
    assert names_of(output.public_result) == expected
    assert output.public_result.status == "READY"
    assert output.public_result.coverage.complete
    assert output.public_result.coverage.confirmed_place_count == count
    assert output.resolution_receipt["attempted_count"] == count
    assert output.resolution_receipt["budget_limited_count"] == 0
    if day_count == 14:
        assert output.inference_binding["day_scopes_completed"] == 14

    async with repository_for("postgres") as repository:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repository)
        created = await service.create_full(CreateFullRequest.model_validate({
            "mode": "FULL", "source": {"type": "TEXT", "text": source}}),
            owner_user_id="experience-owner", idempotency_key="capacity-create", now=now)
        job = await repository.claim_next(worker_id="capacity-worker", now=now, lease_seconds=60)
        assert job is not None
        await repository.complete_job(job, output, now=now)

        # A fresh repository instance must decode each committed result from PostgreSQL.
        reader = PostgresTripUnderstandingRepository(repository._pool,
            SourceCipher("experience-controlled-test-secret"), InMemoryRouteGeometryCache())

        async def reread():
            resource = await reader.authorize(created.accepted.public_resource_id,
                capability_hash=None, user_id="experience-owner", now=now)
            stored = await reader.get_result(resource)
            assert stored is not None
            assert len(stored.result.days) == day_count
            assert sum(len(day.activities) for day in stored.result.days) == count
            assert stored.result.coverage.confirmed_place_count == count
            assert stored.result.coverage.complete
            return resource, stored

        resource, original = await reread()
        assert names_of(original.result) == expected
        move = ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=original.result.days[0].activities[0].activity_token,
            target_day_index=day_count,
            target_position=len(expected[-1]) - (day_count == 1))
        await service.apply_command(resource, move, expected_etag=original.opaque_etag,
            idempotency_key="capacity-move-last", now=now)
        resource, moved = await reread()
        moved_names = [list(names) for names in expected]
        moved_names[-1].append(moved_names[0].pop(0))
        assert names_of(moved.result) == moved_names
        assert moved.opaque_etag != original.opaque_etag

        await service.apply_command(resource, UndoCommand(command_type="UNDO"),
            expected_etag=moved.opaque_etag, idempotency_key="capacity-undo-move", now=now)
        resource, restored = await reread()
        assert names_of(restored.result) == expected
        assert restored.opaque_etag not in {original.opaque_etag, moved.opaque_etag}

        last = restored.result.days[-1].activities[-1]
        await service.apply_command(resource, ActivityTextEditCommand(command_type="ACTIVITY_TEXT_EDIT",
            activity_token=last.activity_token, time_hint="16:30 到达"),
            expected_etag=restored.opaque_etag, idempotency_key="capacity-edit-last", now=now)
        resource, edited = await reread()
        assert names_of(edited.result) == expected
        assert edited.result.days[-1].activities[-1].time_hint == "16:30 到达"
        await service.apply_command(resource, UndoCommand(command_type="UNDO"),
            expected_etag=edited.opaque_etag, idempotency_key="capacity-undo-edit", now=now)
        _, final = await reread()
        assert names_of(final.result) == expected
        assert final.result.days[-1].activities[-1].time_hint == last.time_hint
        rows = await repository._pool.fetchval(
            "SELECT count(*) FROM trip_understanding_results WHERE understanding_id=$1", resource.understanding_id)
        assert rows == 5
