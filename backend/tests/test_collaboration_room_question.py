"""Fixed-model room questions: no live model, map, weather or route requests."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.nodes import router
from app.agents import context_answer
from app.agents.graph import build_graph
from app.api import chat
from app.services import room_chat_context
from langgraph.checkpoint.memory import MemorySaver
from tests.test_collaboration_context_question import candidates
from tests import test_room_current_itinerary as room_fixture

api = room_fixture.api
places, publish, read = room_fixture.places, room_fixture.publish, room_fixture.read


QUESTION = "我们已选的共同路线 Day 2 依次是哪两个地点？想逛街吃小吃更适合其中哪一处？请说明理由，保留当前选择和路线。"


@pytest.fixture(autouse=True)
def actual_router(monkeypatch):
    monkeypatch.setattr(router.settings, "demo_mode", False)
    monkeypatch.setattr(router.settings, "ft_router_enabled", False)


def question_state():
    places = candidates()
    return {"messages": [HumanMessage(content="已选地点中，哪一个以皇家园林和湖景为主？")],
        "conversation_places": places, "selected_place_ids": [p.place_id for p in places],
        "trip_city": "北京", "react_iterations": 0}


@pytest.mark.asyncio
async def test_context_tool_request_never_dispatches_search(monkeypatch):
    model = AsyncMock()
    model.ainvoke.return_value = AIMessage(content="", tool_calls=[{
        "id": "forbidden", "name": "search_places", "args": {"query": "颐和园"}}])
    monkeypatch.setattr(context_answer, "get_context_model", lambda: model)
    # The existing answer branch must not turn an unsupported model response into new cards.
    with pytest.raises(ValueError, match="context answer unavailable"):
        await router.run(question_state())
    assert model.ainvoke.await_count == 1


@pytest.mark.asyncio
async def test_context_absolute_clock_is_not_published(monkeypatch):
    model = AsyncMock()
    model.ainvoke.return_value = AIMessage(content="建议09:00到颐和园，11:30去故宫。")
    monkeypatch.setattr(context_answer, "get_context_model", lambda: model)
    with pytest.raises(ValueError, match="context answer unavailable"):
        await router.run(question_state())


@pytest.mark.parametrize("answer", ["建议在颐和园游玩2小时。", "颐和园适合停留半天。",
    "建议预留两个小时参观颐和园。", "Day 2 先到故宫，参观大约九十分钟。"])
@pytest.mark.asyncio
async def test_explicit_visit_duration_is_rejected_without_rewriting_the_answer(monkeypatch, answer):
    model = AsyncMock()
    model.ainvoke.return_value = AIMessage(content=answer)
    monkeypatch.setattr(context_answer, "get_context_model", lambda: model)
    with pytest.raises(ValueError, match="context answer unavailable"):
        await router.run(question_state())
    assert model.ainvoke.await_count == 1


def test_duration_guard_does_not_conflate_relative_days_or_transport_with_stay_time():
    assert not context_answer._VISIT_DURATION.search("Day 2 的已保存路线显示驾车 12 分钟。")


@pytest.mark.parametrize("query", [QUESTION,
    "这些地点哪处适合看园林？不要新增地点，不要安排时刻。",
    "已选地点哪个好？不用搜索。", "已选点哪处适合散步？不添加其他地点。"])
def test_existing_place_questions_with_negative_search_instructions_keep_question_lane(query):
    assert router._is_context_question(query, candidates())


@pytest.mark.parametrize("query", ["这些地点附近再推荐三家店", "另外找一处景点，不要昂贵的", "帮我搜索北京的小吃店"])
def test_actual_new_place_requests_still_search(query):
    assert not router._is_context_question(query, candidates())


def test_actual_question_model_configuration_has_no_tools_or_sdk_retry(monkeypatch):
    import langchain_openai
    captured = {}
    monkeypatch.setattr(context_answer.settings, "deepseek_api_key", "fixed-not-real")
    def factory(**kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", factory)
    context_answer.get_context_model()
    assert captured["max_retries"] == 0 and captured["max_tokens"] == 500
    assert captured["timeout"] == context_answer.settings.chat_deadline_seconds
    assert captured["model"] == context_answer.settings.llm_model_router
    assert "tools" not in captured


@pytest.mark.asyncio
async def test_cancelled_question_does_not_fall_back_to_search(monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def wait(_):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    model = AsyncMock()
    model.ainvoke.side_effect = wait
    monkeypatch.setattr(context_answer, "get_context_model", lambda: model)
    task = asyncio.create_task(router.run(question_state()))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and model.ainvoke.await_count == 1


def events(response):
    assert response.status_code == 200, response.text
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


async def saved_room(api, monkeypatch):
    monkeypatch.setattr(room_chat_context, "get_pool", AsyncMock(return_value=api.pool))
    monkeypatch.setattr(chat, "get_graph_with_persistence", AsyncMock(return_value=build_graph(MemorySaver())))
    monkeypatch.setattr(chat, "check_public_chat_limit", AsyncMock())
    saved = await api.http.post("/api/room/shared-room/places/sync",
        json={"places": [{**p.model_dump(mode="json"), "room_selected": True} for p in places()]})
    assert saved.status_code == 200, saved.text
    response = await publish(api)
    assert response.status_code == 200, response.text
    return (await read(api)).json()


async def ask(api, *, user="member-a", **changes):
    return await api.http.post("/api/chat", headers={"x-test-user": user}, json={
        "thread_id": "shared-thread", "room_id": "shared-room", "user_id": user,
        "message": QUESTION, "selected_place_ids": [p.place_id for p in places()],
        "trip_city": "北京", **changes})


@pytest.mark.asyncio
async def test_saved_room_fresh_chat_reads_current_day_order_without_writes_or_search(api, monkeypatch):
    current = await saved_room(api, monkeypatch)
    prompts = []
    class Model:
        async def ainvoke(self, messages):
            prompts.append(messages)
            assert len(messages) == 2 and messages[1].content == QUESTION
            data = json.loads(messages[0].content.split("\n", 1)[1])
            assert len(data["places"]) == 4 and all(p["selected"] for p in data["places"])
            expected = [{"day_index": day["day_index"] + 1,
                "places": [slot["place"]["name"] for slot in day["slots"]]}
                for day in current["itinerary_data"]["days"]]
            assert data["current_relative_route"] == expected
            assert all(term not in str(data) for term in ("09:00", "9999", "place_", "opening_hours", "estimated_duration"))
            return AIMessage(content="已保存路线的 Day 2 按页面先后保留；餐饮地点更适合吃东西。")
    monkeypatch.setattr(context_answer, "get_context_model", lambda: Model())
    monkeypatch.setattr(chat, "_previous_collaboration_places", AsyncMock(side_effect=AssertionError("Old graph history is not required")))
    forbidden = AsyncMock(side_effect=AssertionError("Existing room questions cannot run tools"))
    from app.agents.nodes import tool_executor
    monkeypatch.setattr(tool_executor, "run", forbidden)
    # Rebuild after patching graph nodes; there is deliberately no conversation history.
    monkeypatch.setattr(chat, "get_graph_with_persistence", AsyncMock(return_value=build_graph(MemorySaver())))
    before_places = (await api.http.get("/api/room/shared-room/places")).json()
    stream = events(await ask(api))
    assert stream[-1] == {"event": "done", "data": {"status": "READY", "total_places": 0}}
    assert not [e for e in stream if e["event"] in {"place", "place_update", "place_remove", "error"}]
    assert len(prompts) == 1
    forbidden.assert_not_awaited()
    assert (await read(api)).json() == current
    assert (await api.http.get("/api/room/shared-room/places")).json() == before_places


@pytest.mark.asyncio
async def test_room_scope_and_stale_selected_ids_reject_before_model(api, monkeypatch):
    await saved_room(api, monkeypatch)
    factory = AsyncMock(side_effect=AssertionError("Rejected room requests cannot call a model"))
    monkeypatch.setattr(context_answer, "get_context_model", factory)
    assert (await ask(api, user="outsider")).status_code == 403
    assert (await ask(api, thread_id="another-room-thread")).status_code == 403
    assert (await ask(api, user_id="member-b")).status_code == 403
    assert (await ask(api, selected_place_ids=["not-in-room"])).status_code == 409
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_changed_selection_does_not_describe_old_route_as_current(api, monkeypatch):
    await saved_room(api, monkeypatch)
    changed = await api.http.post("/api/room/shared-room/places/sync", json={"places": [
        {**p.model_dump(mode="json"), "room_selected": i == 0} for i, p in enumerate(places())]})
    assert changed.status_code == 200
    model = AsyncMock()
    async def answer(messages):
        data = json.loads(messages[0].content.split("\n", 1)[1])
        assert data["current_relative_route"] == []
        assert [p["name"] for p in data["places"]] == [places()[0].name]
        return AIMessage(content="当前选择尚无对应的共同路线。")
    model.ainvoke.side_effect = answer
    monkeypatch.setattr(context_answer, "get_context_model", lambda: model)
    result = events(await ask(api, selected_place_ids=[places()[0].place_id]))
    assert result[-1]["data"]["status"] == "READY"
    assert model.ainvoke.await_count == 1


@pytest.mark.asyncio
async def test_old_client_cannot_use_old_selection_after_member_changed_room_selection(api, monkeypatch):
    current = await saved_room(api, monkeypatch)
    changed = await api.http.post("/api/room/shared-room/places/sync", headers={"x-test-user": "member-b"},
        json={"places": [{**p.model_dump(mode="json"), "room_selected": i != 0} for i, p in enumerate(places())]})
    assert changed.status_code == 200
    before = (await api.http.get("/api/room/shared-room/places")).json()
    factory = AsyncMock(side_effect=AssertionError("Stale selection cannot call a model"))
    monkeypatch.setattr(context_answer, "get_context_model", factory)
    assert (await ask(api)).status_code == 409
    factory.assert_not_called()
    assert (await api.http.get("/api/room/shared-room/places")).json() == before
    assert (await read(api)).json() == current


@pytest.mark.asyncio
async def test_model_failure_does_not_publish_new_places_or_false_completion(api, monkeypatch):
    current = await saved_room(api, monkeypatch)
    model = AsyncMock()
    model.ainvoke.side_effect = RuntimeError("private-provider-error-must-not-be-returned")
    monkeypatch.setattr(context_answer, "get_context_model", lambda: model)
    stream = events(await ask(api))
    assert stream[-1]["event"] == "error"
    assert not [e for e in stream if e["event"] in {"place", "place_update", "place_remove", "done"}]
    assert "private-provider" not in json.dumps(stream)
    assert model.ainvoke.await_count == 1
    assert (await read(api)).json() == current
