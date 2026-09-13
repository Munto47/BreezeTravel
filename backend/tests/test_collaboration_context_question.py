"""A room follow-up answers about its candidates without changing the shared list."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agents.graph import build_graph
from app.agents.nodes import router, synthesizer, tool_executor
from app.agents import context_answer
from app.api import chat
from app.schemas.api import ChatRequest
from app.schemas.place import Coordinates, Place, PlaceCategory


def candidates():
    return [Place(place_id=f"internal-{index}", name=name, category=PlaceCategory.ATTRACTION,
                  address="北京测试地址", city="北京", coords=Coordinates(lng=116.4, lat=39.9),
                  description=description)
            for index, (name, description) in enumerate([
                ("颐和园", "皇家园林，可漫步赏景。"), ("天坛公园", "传统祭坛建筑。"),
                ("故宫博物院", "明清皇宫。")])]


@pytest.mark.asyncio
async def test_context_answer_uses_selected_names_without_new_places_or_false_partial_failure(monkeypatch):
    places = candidates()
    monkeypatch.setattr(router.settings, "demo_mode", False)
    monkeypatch.setattr(router.settings, "ft_router_enabled", False)
    calls = []

    class Model:
        async def ainvoke(self, messages):
            calls.append(messages)
            assert len(messages) == 2  # Historical requests must not become the current request.
            prompt = messages[0].content
            assert '"name": "颐和园"' in prompt and '"selected": true' in prompt
            assert "故宫博物院" not in prompt and "天坛公园" not in prompt
            assert "internal-" not in prompt
            return AIMessage(content="建议优先选颐和园，皇家园林和湖景适合第一次来北京慢慢游览。")

    monkeypatch.setattr(context_answer, "get_context_model", lambda: Model())
    forbidden_tools = AsyncMock(side_effect=AssertionError("A comparison must not fetch new places or routes"))
    monkeypatch.setattr(tool_executor, "run", forbidden_tools)
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": "context-test"}}
    await graph.aupdate_state(config, {"synthesized_places": places, "messages": [AIMessage(content="旧推荐清单")],
                                      "trip_city": "北京"}, as_node="critic")
    monkeypatch.setattr(chat, "get_graph_with_persistence", AsyncMock(return_value=graph))
    request = ChatRequest(thread_id="context-test", room_id="room-context", user_id="member",
        message="这些地点中，第一次来北京可以优先选哪一处？请简短回答。", trip_city="北京",
        selected_place_ids=[chat._public_place_id("room-context", places[0].place_id), "unknown-public-id"])
    for question in (request.message, "刚才选的里面，哪一个适合慢慢游览？"):
        request.message = question
        stream = [json.loads(line.removeprefix("data: ").strip()) async for line in chat._event_stream(
            request, "safe-test-trace", SimpleNamespace(is_disconnected=AsyncMock(return_value=False)))]
        assert not [item for item in stream if item["event"] in {"place", "place_update", "place_remove", "error"}]
        assert stream[-1] == {"event": "done", "data": {"status": "READY", "total_places": 0}}
        answer = "".join(item["data"]["delta"] for item in stream if item["event"] == "text")
        assert answer == "建议优先选颐和园，皇家园林和湖景适合第一次来北京慢慢游览。"
        assert all(term not in json.dumps(stream, ensure_ascii=False) for term in (
            "internal-", "规则", "answer_only", "conversation_places", "router", "critic", "trace_id"))
    assert len(calls) == 2
    forbidden_tools.assert_not_awaited()
    state = (await graph.aget_state(config)).values
    assert [p.name for p in state["conversation_places"]] == [p.name for p in places]
    assert state["selected_place_ids"] == [places[0].place_id]
    newer = places[0].model_copy(update={"place_id": "new-batch-place", "name": "新一批候选"})
    await graph.aupdate_state(config, {"synthesized_places": [newer]}, as_node="critic")
    assert await chat._previous_collaboration_places(graph, config) == [*places, newer]


@pytest.mark.parametrize("query,expected", [
    ("这些地点中第一次来优先哪一个？", True),
    ("颐和园和天坛公园有什么区别？", True),
    ("这些太远了，再推荐三处景点", False),
    ("这些地点附近有哪家饭店？", False),
    ("颐和园今天的开放时间？", False),
    ("帮我推荐杭州景点", False),
])
def test_context_comparisons_do_not_take_over_fresh_or_dynamic_queries(query, expected):
    assert router._is_context_question(query, candidates()) is expected


def test_explicit_named_comparison_takes_precedence_over_selection_and_unselected_room_keeps_all_candidates():
    places = candidates()
    assert router._question_places("天坛公园和故宫博物院有什么区别？", places, [places[0].place_id]) == places[1:]
    assert router._question_places("这些地点优先选哪里？", places, []) == places


@pytest.mark.asyncio
async def test_old_failed_followup_can_read_previous_room_candidates_without_replaying_tools():
    places = candidates()
    config = {"configurable": {"thread_id": "only-this-room"}}
    class OldGraph:
        checkpointer = object()
        async def aget_state(self, requested):
            assert requested == config
            return SimpleNamespace(values={"synthesized_places": []})
        async def aget_state_history(self, requested, *, limit):
            assert requested == config and limit == 24
            yield SimpleNamespace(values={"synthesized_places": []})
            yield SimpleNamespace(values={"synthesized_places": places})
    assert await chat._previous_collaboration_places(OldGraph(), config) == places


@pytest.mark.asyncio
async def test_context_load_failure_is_an_error_without_calling_the_model(monkeypatch):
    monkeypatch.setattr(chat, "get_graph_with_persistence", AsyncMock(return_value=object()))
    monkeypatch.setattr(chat, "_previous_collaboration_places", AsyncMock(side_effect=TimeoutError()))
    request = ChatRequest(thread_id="unavailable", user_id="member", message="这些优先选哪一处？")
    events = [json.loads(line.removeprefix("data: ").strip()) async for line in chat._event_stream(
        request, "safe-test", SimpleNamespace(is_disconnected=AsyncMock(return_value=False)))]
    assert events == [{"event": "error", "data": {"message": "暂时无法读取房间地点，请稍后重试。"}}]


@pytest.mark.asyncio
async def test_fresh_place_search_after_an_answer_still_delivers_new_cards(monkeypatch):
    places = candidates()
    monkeypatch.setattr(router.settings, "demo_mode", False)
    monkeypatch.setattr(router.settings, "deterministic_routing_enabled", True)
    dispatches = []

    async def search(state):
        calls = state["messages"][-1].tool_calls
        dispatches.extend(calls)
        return {"amap_places": places, "eligible_amap_places": places, "eligible_candidates_computed": True,
            "messages": [ToolMessage(content="已取得三处景点", tool_call_id=call["id"]) for call in calls]}

    async def synthesize(state):
        assert not state["answer_only"] and state["final_response"] is None
        return {"synthesized_places": state["amap_places"], "final_response": "新找到三处北京景点。"}

    monkeypatch.setattr(tool_executor, "run", search)
    monkeypatch.setattr(synthesizer, "run", synthesize)
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": "search-after-answer"}}
    await graph.aupdate_state(config, {"conversation_places": places[:1], "answer_only": True,
        "final_response": "之前的问答", "messages": [AIMessage(content="之前的问答")]}, as_node="critic")
    monkeypatch.setattr(chat, "get_graph_with_persistence", AsyncMock(return_value=graph))
    request = ChatRequest(thread_id="search-after-answer", room_id="search-room", user_id="member",
        message="这些暂不考虑，再推荐三处北京景点", trip_city="北京")
    stream = [json.loads(line.removeprefix("data: ").strip()) async for line in chat._event_stream(
        request, "safe-search-trace", SimpleNamespace(is_disconnected=AsyncMock(return_value=False)))]
    assert dispatches and all(call["name"] == "search_places" for call in dispatches)
    assert {item["data"]["place"]["name"] for item in stream if item["event"] == "place"} == {p.name for p in places}
    assert stream[-1] == {"event": "done", "data": {"status": "READY", "total_places": 3}}
