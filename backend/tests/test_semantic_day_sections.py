"""Whole-document context, literal day scopes, partial results and call accounting."""
import json
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.semantic_sections import DayStructure, anchored_sections
from app.trip_understanding.models import ActivityMoveCommand, ResolvedPlace, UndoCommand, UserFacingTripResult
from app.trip_understanding.pipeline import PublicResultProjector, TripUnderstandingPipeline


def source():
    return "北京旅行背景：" + "准备充分。" * 190 + "\nDay1：星河公园。\nDay2：月光桥。"


class ScopedClient:
    def __init__(self, *, second_fails=False, cross_day=False):
        self.calls = []
        self.second_fails = second_fails
        self.cross_day = cross_day
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        prompt = kwargs["messages"][0]["content"]
        if "只分析旅行原文的全局结构" in prompt:
            payload = {"cross_day_dependencies": self.cross_day, "sections": [
                {"day_index": 1, "start_quote": "Day1"}, {"day_index": 2, "start_quote": "Day2"}]}
        elif "本次只整理原文第2天" in prompt and self.second_fails:
            payload = None
        elif "本次只整理原文第1天" in prompt:
            payload = {"activities": [{"source_quote": "星河公园", "place_name": "星河公园", "day_index": 1, "role": "PLANNED", "role_evidence": "Day1：星河公园"}]}
        elif "本次只整理原文第2天" in prompt:
            payload = {"activities": [{"source_quote": "月光桥", "place_name": "月光桥", "day_index": 2, "role": "PLANNED", "role_evidence": "Day2：月光桥"}]}
        else:
            payload = {"activities": [
                {"source_quote": "星河公园", "place_name": "星河公园", "day_index": 1, "role": "PLANNED"},
                {"source_quote": "月光桥", "place_name": "月光桥", "day_index": 2, "role": "PLANNED"}]}
        return SimpleNamespace(model="test-model", usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(payload) if payload else "{bad"))])


def provider(client):
    return ExperienceQwenProvider(api_key="test", base_url="https://test.invalid", model="test-model", client=client)


@pytest.mark.asyncio
async def test_model_day_sections_rebind_original_spans_and_count_every_call():
    text = source()
    client = ScopedClient()
    result = await provider(client).propose(text)
    assert [(item.atomic_place_name, item.day_index) for item in result.mentions] == [("星河公园", 1), ("月光桥", 2)]
    assert all(text[item.span_start:item.span_end] == item.raw_text for item in result.mentions)
    assert all(text[item.role_evidence_start:item.role_evidence_end] == item.role_evidence for item in result.mentions)
    assert result.binding["external_calls"] == len(client.calls) == 3
    assert result.binding["input_tokens"] == 30 and result.binding["output_tokens"] == 60
    assert result.binding["day_scopes_completed"] == 2


@pytest.mark.asyncio
async def test_failed_day_does_not_destroy_usable_day_or_claim_full_coverage():
    client = ScopedClient(second_fails=True)
    result = await provider(client).propose(source())
    assert [item.day_index for item in result.mentions] == [1]
    assert result.day_count == 2 and result.unprocessed_count > 0
    assert result.binding["outcome"] == "PARTIAL_RESULT"
    assert result.binding["external_calls"] == len(client.calls) == 4
    assert any(issue.category == "DAY_SECTION_UNPROCESSED" for issue in result.diagnostics)


@pytest.mark.asyncio
async def test_cross_day_dependencies_remain_whole_document_with_all_calls_counted():
    client = ScopedClient(cross_day=True)
    result = await provider(client).propose(source())
    assert len(result.mentions) == 2
    assert result.binding["day_scope_fallback"] is True
    assert result.binding["external_calls"] == len(client.calls) == 2


def test_model_cannot_invent_or_omit_day_boundaries():
    plan = DayStructure.model_validate({"cross_day_dependencies": False, "sections": [
        {"day_index": 1, "start_quote": "Day1"}, {"day_index": 3, "start_quote": "Day2"}]})
    assert anchored_sections(source(), plan) == []


def chinese_day_plan(*, cross_day=False):
    return DayStructure.model_validate({"cross_day_dependencies": cross_day, "sections": [
        {"day_index": 1, "start_quote": "第一天"},
        {"day_index": 2, "start_quote": "第二天", "occurrence": 1}]})


@pytest.mark.parametrize("previous_tail", [
    "返回酒店，为第二天的高强度步行养精蓄锐。",
    "住在附近，以便第二天清晨能轻松开启行程。",
])
def test_day_reference_in_previous_paragraph_reanchors_to_unique_heading(previous_tail):
    text = "第一天，游览星河公园。" + previous_tail + "\n\n第二天，前往月光桥。"
    sections = anchored_sections(text, chinese_day_plan())
    assert sections == [(1, 0, text.rindex("第二天")), (2, text.rindex("第二天"), len(text))]
    assert previous_tail in text[sections[0][1]:sections[0][2]]
    assert text[sections[1][1]:sections[1][2]] == "第二天，前往月光桥。"


@pytest.mark.parametrize("prefix,suffix", [("**", "**"), ("## ", ""), ("> **", "**"), ("2. ", "")])
def test_heading_boundary_accepts_markdown_without_losing_source_offsets(prefix, suffix):
    text = f"第一天：星河公园。\n\n{prefix}第二天：月光桥。{suffix}"
    sections = anchored_sections(text, chinese_day_plan())
    assert sections[1] == (2, text.index("第二天"), len(text))


@pytest.mark.parametrize("text", [
    "第一天，游览星河公园，准备第二天去月光桥。",
    "第一天，游览星河公园。第二天，游览月光桥。",
    "第一天：星河公园。\n第二天：月光桥。\n第二天：重复摘要。",
    "第二天：月光桥。\n第一天：星河公园。",
])
def test_unsupported_or_ambiguous_headings_remain_whole_document(text):
    assert anchored_sections(text, chinese_day_plan()) == []


def test_unique_heading_does_not_override_cross_day_dependency():
    text = "第一天：星河公园。\n第二天：月光桥。\n后来将月光桥改到第一天。"
    assert anchored_sections(text, chinese_day_plan(cross_day=True)) == []


def observed_fourteen_day_structure():
    # The unchanged capacity input and actual global-structure response. Only
    # this response is real; generated day detail/identity replies below are
    # controlled integration fixtures, not a live quality claim.
    sample = json.loads((Path(__file__).parent / "fixtures/live_capacity_day_structure.json").read_text(encoding="utf-8"))
    return sample["source"], DayStructure.model_validate(sample["structure_response"])


def test_observed_day1_and_day10_are_distinct_complete_heading_tokens():
    text, plan = observed_fourteen_day_structure()
    assert plan.cross_day_dependencies is False
    sections = anchored_sections(text, plan)
    assert len(sections) == 14
    assert [day for day, _, _ in sections] == list(range(1, 15))
    assert all(text[left:right].startswith(f"Day{day}：") for day, left, right in sections)
    assert "".join(text[left:right] for _, left, right in sections) == text[text.index("Day1："):]


@pytest.mark.parametrize("suffix", ["\nDay1：重复摘要。", "\nDay10：重复摘要。", "\n## **Day1**：更正安排。"])
def test_real_repeated_heading_still_prevents_slicing(suffix):
    text, plan = observed_fourteen_day_structure()
    assert anchored_sections(text + suffix, plan) == []


def test_observed_fourteen_day_structure_still_respects_cross_day_guard():
    text, plan = observed_fourteen_day_structure()
    assert anchored_sections(text, plan.model_copy(update={"cross_day_dependencies": True})) == []


@pytest.mark.asyncio
async def test_observed_structure_dispatches_all_fourteen_scopes_without_dropping_revisits():
    from tests.test_trip_capacity_readback import CapacityModelClient, CapacityPlaces

    text, plan = observed_fourteen_day_structure()
    names_by_day = [line.partition("：")[2].rstrip("。").split("、")
                    for line in text.splitlines() if line.startswith("Day")]

    class ObservedStructureClient(CapacityModelClient):
        def __init__(self):
            super().__init__(names_by_day)
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            response = await super().create(**kwargs)
            if "只分析旅行原文的全局结构" in kwargs["messages"][0]["content"]:
                response.choices[0].message.content = plan.model_dump_json()
            return response

    client = ObservedStructureClient()
    output = await TripUnderstandingPipeline(provider(client), CapacityPlaces()).run(text)
    assert len(client.calls) == 15
    assert output.inference_binding["day_scopes_completed"] == 14
    assert not output.inference_binding.get("day_scope_fallback")
    assert [[card.name for card in day.activities] for day in output.public_result.days] == names_by_day
    assert sum(len(day.activities) for day in output.public_result.days) == 160
    assert output.public_result.coverage.complete is True


@pytest.mark.asyncio
async def test_reanchored_day_inputs_keep_previous_tail_and_original_place_spans():
    previous_tail = "返回酒店，为第二天的高强度步行养精蓄锐。"
    text = "北京旅行背景：" + "准备充分。" * 190 + "\n第一天：星河公园。" + previous_tail + "\n\n第二天：月光桥。"

    class ProseReferenceClient(ScopedClient):
        async def create(self, **kwargs):
            if "只分析旅行原文的全局结构" in kwargs["messages"][0]["content"]:
                self.calls.append(kwargs)
                return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
                    choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                        content=chinese_day_plan().model_dump_json()))])
            response = await super().create(**kwargs)
            payload = json.loads(response.choices[0].message.content)
            for activity in payload["activities"]:
                activity.pop("role_evidence", None)
            response.choices[0].message.content = json.dumps(payload)
            return response

    client = ProseReferenceClient()
    result = await provider(client).propose(text)
    first = next(call for call in client.calls if "本次只整理原文第1天" in call["messages"][0]["content"])
    second = next(call for call in client.calls if "本次只整理原文第2天" in call["messages"][0]["content"])
    assert previous_tail in first["messages"][-1]["content"]
    assert previous_tail not in second["messages"][-1]["content"]
    assert "第二天：月光桥。" in second["messages"][-1]["content"]
    assert [(item.atomic_place_name, item.day_index) for item in result.mentions] == [("星河公园", 1), ("月光桥", 2)]
    assert all(text[item.span_start:item.span_end] == item.raw_text for item in result.mentions)
    assert result.binding["external_calls"] == len(client.calls) == 3


class RecordingPlaces:
    """Synthetic place identities: exercises wiring without network or a database."""

    def __init__(self, *, districts=None):
        self.calls = []
        self.districts = districts or {}

    async def resolve(self, *, city, atomic_place_name, category_hint=None):
        self.calls.append((city, atomic_place_name))
        return ResolvedPlace(canonical_place_id=f"synthetic:{city}:{atomic_place_name}",
            name=atomic_place_name, category="景点", area_or_address="模拟地址",
            provider_binding={"city": city, "adcode": self.districts.get(city)})


@pytest.mark.asyncio
async def test_failed_day_remains_visible_after_provider_and_pipeline():
    result = await TripUnderstandingPipeline(provider(ScopedClient(second_fails=True)), RecordingPlaces()).run(source())
    assert len(result.public_result.days) == 2
    assert not result.public_result.days[1].activities
    assert [day.unprocessed_count for day in result.public_result.days] == [0, 1]
    assert result.public_result.coverage.unprocessed_count > 0
    assert result.public_result.status == "PARTIAL_RESULT"
    assert result.public_result.coverage.complete is False


class OptionalDayClient(ScopedClient):
    async def create(self, **kwargs):
        response = await super().create(**kwargs)
        payload = json.loads(response.choices[0].message.content)
        for activity in payload.get("activities", []):
            if activity.get("day_index") == 2:
                activity.update(role="OPTIONAL", role_evidence="月光桥作为备选")
        response.choices[0].message.content = json.dumps(payload)
        return response


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, {}, {"semantic_policy": "another-experiment"}])
async def test_optional_only_day_survives_pipeline_independently_of_observation_metadata(metadata):
    text = source().replace("Day2：月光桥。", "Day2：月光桥作为备选。")
    live_adapter = provider(OptionalDayClient())

    class ObservedProvider:
        async def propose(self, source_text):
            proposal = await live_adapter.propose(source_text)
            return proposal if metadata is None else proposal.model_copy(update={"binding": metadata})

    result = await TripUnderstandingPipeline(ObservedProvider(), RecordingPlaces()).run(text)
    assert len(result.public_result.days) == 2
    assert not result.public_result.days[1].activities
    assert [item.name for item in result.public_result.days[1].alternatives] == ["月光桥"]
    assert [day.unprocessed_count for day in result.public_result.days] == [0, 0]
    assert result.public_result.coverage.complete is True


@pytest.mark.asyncio
async def test_day_sections_keep_each_city_and_guard_the_source_district():
    text = "北京旅行背景：" + "准备充分。" * 190 + "\nDay1：上海浦东新区：星河公园。\nDay2：北京东城区：月光桥。"

    class CityClient(ScopedClient):
        async def create(self, **kwargs):
            response = await super().create(**kwargs)
            payload = json.loads(response.choices[0].message.content)
            for activity in payload.get("activities", []):
                day = activity["day_index"]
                city, district = ("上海", "浦东新区") if day == 1 else ("北京", "东城区")
                payload["destination"] = city
                activity.update(city=city, city_evidence=f"Day{day}：{city}{district}",
                    role_evidence=f"Day{day}：{city}{district}：{activity['place_name']}")
            response.choices[0].message.content = json.dumps(payload)
            return response

    places = RecordingPlaces(districts={"上海": "310101", "北京": "110101"})
    result = await TripUnderstandingPipeline(provider(CityClient()), places).run(text)
    assert sorted(places.calls) == [("上海", "星河公园"), ("北京", "月光桥")]
    assert result.activities[0].resolver_receipt["failure_category"] == "SOURCE_DISTRICT_MISMATCH"
    assert result.public_result.days[0].activities[0].status == "NEEDS_CONFIRMATION"
    assert result.public_result.days[1].activities[0].status == "READY"
    assert result.public_result.coverage.complete is False


@pytest.mark.asyncio
async def test_projection_cannot_claim_complete_after_losing_an_understood_alternative():
    text = source().replace("Day2：月光桥。", "Day2：月光桥作为备选。")

    class LossyProjector(PublicResultProjector):
        def project(self, *args, **kwargs):
            result = super().project(*args, **kwargs)
            return result.model_copy(update={"days": result.days[:1]})

    result = await TripUnderstandingPipeline(provider(OptionalDayClient()), RecordingPlaces(),
        projector=LossyProjector()).run(text)
    assert result.public_result.status == "PARTIAL_RESULT"
    assert result.public_result.coverage.complete is False
    assert result.public_result.coverage.unprocessed_count == 1
    issue = next(item for item in result.proposal.diagnostics if item.category == "PUBLIC_PROJECTION_OMISSION")
    assert text[issue.span_start:issue.span_end] == "月光桥"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_day_unprocessed_count_survives_save_edit_readback_and_undo(kind):
    from tests.test_experience_v3_journey import repository_for
    from app.trip_understanding.service import TripUnderstandingApplicationService

    text = source()
    now = datetime.now(timezone.utc)
    output = await TripUnderstandingPipeline(provider(ScopedClient(second_fails=True)), RecordingPlaces()).run(text)
    async with repository_for(kind) as repository:
        created = await repository.create_demo(capability_hash="a" * 64, source_text=text,
            idempotency_key="unfinished-day", request_hash="b" * 64, now=now, ttl_hours=24)
        job = await repository.claim_next(worker_id="day-retention", now=now, lease_seconds=60)
        await repository.complete_job(job, output, now=now)
        resource = await repository.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        stored = await repository.get_result(resource)
        assert [day.unprocessed_count for day in stored.result.days] == [0, 1]
        service = TripUnderstandingApplicationService(repository)
        await service.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=stored.result.days[0].activities[0].activity_token, target_day_index=2, target_position=0),
            expected_etag=stored.opaque_etag, idempotency_key="move-to-unfinished-day", now=now)
        resource = await repository.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        edited = await repository.get_result(resource)
        assert [day.unprocessed_count for day in edited.result.days] == [0, 1]
        assert not edited.result.days[0].activities and edited.result.days[1].activities
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=edited.opaque_etag,
            idempotency_key="undo-unfinished-day-move", now=now)
        resource = await repository.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        restored = await repository.get_result(resource)
        assert [day.unprocessed_count for day in restored.result.days] == [0, 1]
        assert restored.result.days[0].activities and not restored.result.days[1].activities
        assert restored.result.coverage.complete is False
        old_payload = restored.result.model_dump()
        for day in old_payload["days"]:
            day.pop("unprocessed_count")
        assert [day.unprocessed_count for day in UserFacingTripResult.model_validate(old_payload).days] == [0, 0]
