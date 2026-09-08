"""Public results for browser tests, generated from the current backend.

The hotel, missing-name and truncated fixtures contain saved real responses. The
model transport is replayed and every external place identity is fixed and
synthetic. These scenarios test downstream preservation, not live accuracy.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.trip_understanding.models import ResolvedPlace
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_experience_inference import Client
from tests.test_semantic_day_sections import OptionalDayClient, ScopedClient, provider, source
from tests.test_trip_input_capacity import capacity_pipeline
from tests.test_trip_understanding_v3_api import _client
from app.trip_understanding.worker import TripUnderstandingWorker


class FixedReplayPlaces:
    async def resolve(self, *, city, atomic_place_name, category_hint=None):
        return ResolvedPlace(
            canonical_place_id=f"synthetic-replay:{city}:{atomic_place_name}",
            name=atomic_place_name,
            category=category_hint or "景点",
            area_or_address="固定模拟地点身份，未调用真实地点服务",
            provider_binding={"city": city},
        )


async def truncated_whole_replay():
    fixtures = Path(__file__).parent / "fixtures"
    sample = json.loads((fixtures / "live_capacity_truncated_whole.json").read_text(encoding="utf-8"))
    text = json.loads((fixtures / sample["source_fixture"]).read_text(encoding="utf-8"))["source"]
    client = Client(*[sample["model_response_content"]] * sample["identical_attempts"])
    original_create = client.create

    async def create(**kwargs):
        response = await original_create(**kwargs)
        response.choices[0].finish_reason = sample["finish_reason"]
        return response

    client.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    live = provider(client)
    # Replay the saved whole-document failure, not a newly invented per-day
    # response. The actual provider adapter, pipeline and projector all run.
    live.enable_day_sections = False
    output = await TripUnderstandingPipeline(live, FixedReplayPlaces()).run(text)
    assert len(client.calls) == sample["identical_attempts"]
    return output.public_result.model_dump(mode="json")


async def public_replays():
    results = {}
    for kind in ("partial", "optional"):
        client = ScopedClient(second_fails=True) if kind == "partial" else OptionalDayClient()
        text = source()
        if kind == "optional":
            text = text.replace("Day2：月光桥。", "Day2：月光桥作为备选。")
        output = await TripUnderstandingPipeline(provider(client), FixedReplayPlaces()).run(text)
        results[kind] = output.public_result.model_dump(mode="json")

    for kind, fixture in (("lodging", "live_lodging_revisit.json"),
                          ("missing_name", "live_missing_place_name.json")):
        sample = json.loads((Path(__file__).parent / "fixtures" / fixture).read_text(encoding="utf-8"))
        content = json.dumps(sample["model_response"], ensure_ascii=False)
        # The second saved response exercises the existing bounded repair path
        # for missing fields. No client here can contact a model service.
        output = await TripUnderstandingPipeline(provider(Client(content, content)), FixedReplayPlaces()).run(sample["source"])
        results[kind] = output.public_result.model_dump(mode="json")
    results["truncated_whole"] = await truncated_whole_replay()
    text, pipeline, _, _ = capacity_pipeline(161, 1)
    day_text = "北京15日行程。\n" + "\n".join(f"Day{d}：测试{d:03d}公园。" for d in range(1, 16))
    day_reply = json.dumps(dict(destination="北京", activities=[
        dict(source_quote=f"测试{d:03d}公园", place_name=f"测试{d:03d}公园", day_index=d, role="PLANNED")
        for d in range(1, 16)]))
    day_pipeline = TripUnderstandingPipeline(provider(Client(day_reply)), FixedReplayPlaces())
    for kind, original, full_pipeline in (("item_limit", text, pipeline), ("day_limit", day_text, day_pipeline)):
        client, repository, _ = _client()
        created = client.post("/api/v3/trip-understandings", headers={"Idempotency-Key": kind},
                              json={"mode": "FULL", "source": {"type": "TEXT", "text": original}})
        assert created.status_code == 202
        await TripUnderstandingWorker(repository, full_pipeline=full_pipeline).run_once("page-capacity-replay")
        response = client.get(created.json()["result_url"])
        assert response.status_code == 409
        results[kind] = {"source": original, "failure": response.json()}
    return results


if __name__ == "__main__":
    print(json.dumps(asyncio.run(public_replays()), ensure_ascii=False))
