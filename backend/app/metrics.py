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
}


def inc(key: str, amount: int = 1) -> None:
    """线程安全无锁自增（asyncio 单线程模型下足够）"""
    _store[key] = _store.get(key, 0) + amount


def set_val(key: str, value) -> None:
    _store[key] = value


def snapshot() -> dict:
    """返回当前指标快照（浅拷贝）"""
    return dict(_store)
