"""Schema-valid bounded replies must not hide the 161st source visit."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.trip_understanding.experience_inference import ExperienceQwenProvider, SemanticDraft, proposal_from_draft
from app.trip_understanding.failures import INPUT_CAPACITY_EXCEEDED
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.models import ActivityRole
from app.trip_understanding.source_capacity import saturated_source_capacity
from app.trip_understanding.worker import TripUnderstandingWorker
from tests.test_experience_inference import Client
from tests.test_trip_capacity_readback import CapacityModelClient, CapacityPlaces, capacity_source
from tests.test_trip_input_capacity import capacity_pipeline
from tests.test_trip_understanding_v3_api import _client


def bounded_payload(count=160, *, unprocessed=False):
    return dict(destination="北京", day_labels=[None],
        unprocessed_quotes=["容量测试161公园"] if unprocessed else [],
        activities=[dict(source_quote=f"容量测试{i:03d}公园", place_name=f"容量测试{i:03d}公园",
            role="PLANNED", day_index=1, category="景点") for i in range(1, count + 1)])


def test_pure_list_helper_proves_exact_and_overflow_without_creating_mentions():
    source, *_ = capacity_pipeline(161, 1)
    draft = SemanticDraft.model_validate(bounded_payload())
    # This historical/direct adapter does not perform the new live boundary.
    proposal = proposal_from_draft(source, draft)
    assert len(proposal.mentions) == 160
    assert saturated_source_capacity(source, proposal.mentions) == "OVERFLOW"
    exact, _ = capacity_source(160, 1)
    exact_proposal = proposal_from_draft(exact, draft)
    assert saturated_source_capacity(exact, exact_proposal.mentions) == "EXACT"


@pytest.mark.parametrize("suffix", ["取消最后一项。", "参考上面的列表。", "更正：只去第一项。", "任选一项。", "馆内还可看一项。"])
def test_pure_list_helper_does_not_count_ambiguous_source_as_overflow(suffix):
    source, *_ = capacity_pipeline(161, 1)
    proposal = proposal_from_draft(source, SemanticDraft.model_validate(bounded_payload()))
    assert saturated_source_capacity(source + "\n" + suffix, proposal.mentions) == "UNVERIFIED"


def test_pure_list_helper_rejects_references_clones_wrong_occurrence_and_wrong_order():
    source, *_ = capacity_pipeline(161, 1)
    mentions = proposal_from_draft(source, SemanticDraft.model_validate(bounded_payload())).mentions
    for changed in (
        [mentions[0]] * 160,
        [mentions[0].model_copy(update={"role": ActivityRole.REFERENCE}), *mentions[1:]],
        [mentions[0].model_copy(update={"span_start": 0}), *mentions[1:]],
        [mentions[1], mentions[0], *mentions[2:]],
    ):
        assert saturated_source_capacity(source, changed) == "UNVERIFIED"


class CountPlaces(CapacityPlaces):
    calls = 0
    async def resolve(self, **kwargs):
        self.calls += 1
        return await super().resolve(**kwargs)


@pytest.mark.parametrize("unprocessed", [False, True])
def test_schema_valid_160_reply_to_161_source_returns_split_before_any_place(unprocessed):
    source, *_ = capacity_pipeline(161, 1)
    content = json.dumps(bounded_payload(unprocessed=unprocessed), ensure_ascii=False)
    model, places = Client(content, content), CountPlaces()
    provider = ExperienceQwenProvider(api_key="fixed", base_url="https://fixed.invalid/v1", model="fixed", client=model)
    client, repository, _ = _client()
    accepted = client.post("/api/v3/trip-understandings", headers={"Idempotency-Key": "bounded-161"},
        json={"mode": "FULL", "source": {"type": "TEXT", "text": source}})
    asyncio.run(TripUnderstandingWorker(repository, full_pipeline=TripUnderstandingPipeline(provider, places)).run_once("fixed"))
    response = client.get(accepted.json()["result_url"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == INPUT_CAPACITY_EXCEEDED
    assert "160" in response.json()["detail"]["message"]
    assert len(model.calls) == 1 and places.calls == 0
    assert not repository.results


@pytest.mark.asyncio
async def test_full_unverifiable_reply_spends_only_existing_second_answer_and_stays_partial():
    source, _ = capacity_source(160, 1)
    source += "\n时间合适时慢慢游览。"
    content = json.dumps(bounded_payload(), ensure_ascii=False)
    model, places = Client(content, content), CountPlaces()
    provider = ExperienceQwenProvider(api_key="fixed", base_url="https://fixed.invalid/v1", model="fixed", client=model)
    result = await TripUnderstandingPipeline(provider, places).run(source)
    assert len(model.calls) == 2
    assert len(result.public_result.days[0].activities) == 160
    assert result.public_result.coverage.complete is False
    assert any(d.category == "SATURATED_SOURCE_COVERAGE_UNVERIFIED" for d in result.proposal.diagnostics)
    assert places.calls == 160


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", [False, True])
async def test_fourteen_scopes_share_boundary_without_more_calls_or_merging_revisits(overflow):
    from app.trip_understanding.errors import InferenceProviderUnavailableError
    _source, groups = capacity_source(160, 14)
    # Same name on different days is a distinct source occurrence; no model
    # or source visit is collapsed to obtain a 160-item total.
    groups = [[f"容量测试{i:03d}公园" for i in range(1, len(group) + 1)] for group in groups]
    source_groups = [list(group) for group in groups]
    if overflow:
        source_groups[-1].append("额外独立公园")
    source = "北京容量测试，所有地点是模拟测试资料。\n" + "\n".join(
        f"Day{day}：" + "、".join(names) + "。" for day, names in enumerate(source_groups, 1))
    class Model(CapacityModelClient):
        calls = 0
        async def create(self, **kwargs):
            self.calls += 1
            response = await super().create(**kwargs)
            payload = json.loads(response.choices[0].message.content)
            if "activities" in payload:
                payload.update(day_labels=[None] * max(row["day_index"] for row in payload["activities"]), unprocessed_quotes=[])
                response.choices[0].message.content = json.dumps(payload, ensure_ascii=False)
            return response
    model, places = Model(groups), CountPlaces()
    provider = ExperienceQwenProvider(api_key="fixed", base_url="https://fixed.invalid/v1", model="fixed", client=model)
    if overflow:
        with pytest.raises(InferenceProviderUnavailableError) as failure:
            await TripUnderstandingPipeline(provider, places).run(source)
        assert failure.value.category == INPUT_CAPACITY_EXCEEDED
        assert failure.value.external_call_count == 15
        assert places.calls == 0
    else:
        result = await TripUnderstandingPipeline(provider, places).run(source)
        assert result.public_result.coverage.complete
        assert len(result.public_result.days) == 14
        assert [[c.name for c in d.activities] for d in result.public_result.days] == groups
        assert places.calls == 12  # Shared identity checks, all 160 visits stay visible.
    assert model.calls == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["malformed", "empty", "timeout"])
async def test_unverifiable_full_first_answer_survives_bad_or_empty_second(second):
    source, _ = capacity_source(160, 1)
    source += "\n时间合适时慢慢游览。"
    content = json.dumps(bounded_payload(), ensure_ascii=False)
    class Model(Client):
        async def create(self, **kwargs):
            if self.calls and second == "timeout":
                self.calls.append(kwargs)
                raise TimeoutError
            return await super().create(**kwargs)
    model = Model(content, "{invalid" if second == "malformed" else json.dumps(
        dict(destination="北京", day_labels=[None], activities=[], unprocessed_quotes=[])))
    provider = ExperienceQwenProvider(api_key="fixed", base_url="https://fixed.invalid/v1", model="fixed", client=model)
    result = await TripUnderstandingPipeline(provider, CountPlaces()).run(source)
    assert len(model.calls) == 2
    assert len(result.public_result.days[0].activities) == 160
    assert not result.public_result.coverage.complete
    assert sum(d.category == "SATURATED_SOURCE_COVERAGE_UNVERIFIED" for d in result.proposal.diagnostics) == 1


def test_helper_cannot_borrow_a_header_or_count_a_narrative_tail_as_a_place():
    source, _ = capacity_source(160, 1)
    mentions = proposal_from_draft(source, SemanticDraft.model_validate(bounded_payload())).mentions
    for extra in ("、晚上休息。", "、然后慢慢回家。"):
        assert saturated_source_capacity(source.rstrip("。") + extra, mentions) == "UNVERIFIED"
    assert saturated_source_capacity("先去另一个公园。\n" + source, mentions) == "UNVERIFIED"
    assert saturated_source_capacity(source.replace("Day1：", "Day1.5："), mentions) == "UNVERIFIED"


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ["\n取消容量测试161公园。", "\n说明：容量测试161公园仅供参考。", "\n时间合适时慢慢游览。"])
async def test_reference_cancelled_and_unclear_remainders_never_count_as_161_visits(tail):
    source, *_ = capacity_pipeline(161, 1)
    source += tail
    content = json.dumps(bounded_payload(unprocessed=True), ensure_ascii=False)
    model = Client(content, content)
    provider = ExperienceQwenProvider(api_key="fixed", base_url="https://fixed.invalid/v1", model="fixed", client=model)
    result = await TripUnderstandingPipeline(provider, CountPlaces()).run(source)
    assert len(result.public_result.days[0].activities) == 160
    assert not result.public_result.coverage.complete
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_repeated_model_rows_do_not_create_a_saturated_source_warning_or_overflow():
    source = "北京一日游。\nDay1：容量测试001公园。"
    payload = bounded_payload(1)
    payload["activities"] *= 160
    model = Client(json.dumps(payload, ensure_ascii=False))
    provider = ExperienceQwenProvider(api_key="fixed", base_url="https://fixed.invalid/v1", model="fixed", client=model)
    result = await TripUnderstandingPipeline(provider, CountPlaces()).run(source)
    assert len(result.public_result.days[0].activities) == 1
    assert result.public_result.coverage.complete
    assert len(model.calls) == 1
