"""
全局运行时指标存储

供 main.py（/metrics 端点读取）和 chat.py（写入 Agent 级指标）共同使用，
避免在 main.py 与 api 路由之间形成循环导入。
"""

_store: dict = {
    # 请求级
    "total_chat_requests": 0,
    "total_optimize_requests": 0,
    "startup_time": None,
    # Agent 级
    "agent_success_count": 0,
    "agent_failure_count": 0,
    "critic_trigger_count": 0,
    "tool_calls_total": 0,
    "tool_calls_amap": 0,
    "tool_calls_rag": 0,
    "tool_calls_weather": 0,
    "total_react_iterations": 0,
    # Reliability / degradation signals.  These deliberately count events, not
    # requests, so a dashboard can distinguish a successful degraded answer.
    "agent_degraded_count": 0,
    "rag_empty_count": 0,
    "tool_error_count": 0,
    "sse_disconnect_count": 0,
    "estimated_llm_cost_usd": 0.0,
}
_labelled: dict[str, dict[str, int | float]] = {
    "tool_outcomes": {}, "model_usage": {}, "error_categories": {},
}


def inc(key: str, amount: int = 1) -> None:
    """线程安全无锁自增（asyncio 单线程模型下足够）"""
    _store[key] = _store.get(key, 0) + amount


def set_val(key: str, value) -> None:
    _store[key] = value


def snapshot() -> dict:
    """返回当前指标快照（浅拷贝）"""
    return {**_store, "labelled": {name: dict(values) for name, values in _labelled.items()}}


def observe(label_set: str, label: str, amount: int | float = 1) -> None:
    """Bounded internal labels; callers use fixed names, never user text."""
    bucket = _labelled.setdefault(label_set, {})
    bucket[label] = bucket.get(label, 0) + amount
