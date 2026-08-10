import asyncio

from langchain_core.messages import HumanMessage

from app.agents.routing_policy import plan_tools


def test_explicit_mixed_request_requires_both_tools():
    plan = plan_tools("推荐杭州西湖附近适合亲子的餐厅，并给避坑建议")
    assert plan is not None
    assert plan.intent == "both"
    assert set(plan.tools) == {"search_places", "search_travel_notes"}


def test_single_mode_requests_are_left_to_router():
    assert plan_tools("杭州西湖附近有什么餐厅") is None
    assert plan_tools("杭州西湖预约规则和注意事项") is None


def test_router_turns_mixed_request_into_real_tool_calls(monkeypatch):
    from app.agents.nodes import router
    from app.config import settings
    monkeypatch.setattr(settings, "demo_mode", False)
    state = {"messages": [HumanMessage(content="推荐杭州西湖附近餐厅，并给避坑建议")], "trip_city": "杭州", "react_iterations": 0}
    result = asyncio.run(router.run(state))
    assert {call["name"] for call in result["messages"][0].tool_calls} == {"search_places", "search_travel_notes"}
