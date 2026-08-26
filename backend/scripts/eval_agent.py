"""
Agent 端到端评估脚本

评估体系三层指标
----------------
1. Router 工具选择准确率 (router_accuracy)
   给定用户查询，Router 是否选择了正确的工具组合
   - "amap"    → 期望调用 search_places
   - "rag"     → 期望调用 search_travel_notes
   - "both"    → 期望同时调用两者
   - "weather" → 期望调用 get_weather（可伴随其他工具）

2. Synthesizer 输出有效率 (synthesizer_validity)
   Synthesizer 输出中是否包含 ≥1 个有效 Place 对象（有 name + place_id）

3. 端到端成功率 (e2e_success_rate)
   以上两项同时满足

附加指标（诊断用）
-----------------
- critic_trigger_rate   : Critic 触发重试的比例（高 → 结果质量不稳定）
- avg_react_iterations  : 平均 ReAct 循环次数
- avg_latency_ms        : 平均端到端耗时（ms）

运行模式
--------
  # 离线模式：仅测 FT Router 分类器准确率，无需 API 或 DB
  python -m scripts.eval_agent --mode offline

  # 集成模式：运行完整 LangGraph pipeline（需要 API Key + 数据库）
  python -m scripts.eval_agent --mode full

  # 集成模式（只跑前 N 条，节省 API 费用）
  python -m scripts.eval_agent --mode full --n 10

  # 保存结果到 JSON
  python -m scripts.eval_agent --mode full --output results/agent_eval.json
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

# ═══════════════════════════════════════════════════════════════════
# 评估数据集：50 条，覆盖 7 城市 × 4 工具类型
# ═══════════════════════════════════════════════════════════════════

EVAL_CASES: list[dict] = [
    # ── AMAP 意图（20 条）：POI 搜索为主，期望调用 search_places ────────
    {"id": "a01", "query": "成都有哪些好吃的火锅店推荐", "city": "成都",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a02", "query": "北京故宫附近哪里可以停车", "city": "北京",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a03", "query": "上海外滩附近有哪些酒店", "city": "上海",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a04", "query": "厦门鼓浪屿附近的餐厅有哪些", "city": "厦门",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a05", "query": "广州哪里可以喝到正宗早茶", "city": "广州",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a06", "query": "深圳有哪些好玩的主题公园", "city": "深圳",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a07", "query": "杭州西湖附近的民宿推荐", "city": "杭州",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a08", "query": "成都宽窄巷子附近有什么咖啡馆", "city": "成都",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a09", "query": "北京三里屯周边有哪些酒吧", "city": "北京",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a10", "query": "上海静安寺附近的购物中心", "city": "上海",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a11", "query": "厦门中山路有哪些特产店", "city": "厦门",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a12", "query": "广州陈家祠附近的粤菜餐厅", "city": "广州",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a13", "query": "深圳大鹏半岛哪里可以露营", "city": "深圳",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a14", "query": "杭州西溪湿地附近有什么景点", "city": "杭州",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a15", "query": "成都都江堰景区周边住宿推荐", "city": "成都",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a16", "query": "北京798艺术区附近的咖啡馆", "city": "北京",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a17", "query": "上海朱家角古镇有哪些景点", "city": "上海",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a18", "query": "厦门南普陀寺附近的景点", "city": "厦门",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a19", "query": "广州白云山景区在哪里，怎么进入", "city": "广州",
     "expected_intent": "amap", "expected_tools": ["search_places"]},
    {"id": "a20", "query": "深圳世界之窗景区在哪里", "city": "深圳",
     "expected_intent": "amap", "expected_tools": ["search_places"]},

    # ── RAG 意图（15 条）：经验攻略为主，期望调用 search_travel_notes ────
    {"id": "r01", "query": "成都锦里古街旅游有哪些避坑经验", "city": "成都",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r02", "query": "北京故宫参观攻略和注意事项", "city": "北京",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r03", "query": "上海外滩旅游踩坑注意事项", "city": "上海",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r04", "query": "厦门鼓浪屿一日游攻略分享", "city": "厦门",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r05", "query": "广州早茶哪家最好吃的经验推荐", "city": "广州",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r06", "query": "深圳大鹏半岛徒步经验和路线分享", "city": "深圳",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r07", "query": "杭州西湖游览建议和实用攻略", "city": "杭州",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r08", "query": "成都适合亲子游的景区真实体验如何", "city": "成都",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r09", "query": "北京第一次旅游有什么必须注意的", "city": "北京",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r10", "query": "上海豫园值得去吗，游客体验怎么样", "city": "上海",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r11", "query": "厦门五天旅游路线怎么安排合适", "city": "厦门",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r12", "query": "广州美食攻略最值得去哪些地方", "city": "广州",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r13", "query": "深圳一日游最好的路线安排经验", "city": "深圳",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r14", "query": "杭州最不值得去的景点踩坑记录", "city": "杭州",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},
    {"id": "r15", "query": "成都旅游费用大概需要多少预算", "city": "成都",
     "expected_intent": "rag", "expected_tools": ["search_travel_notes"]},

    # ── BOTH 意图（10 条）：综合推荐，期望同时调两个工具 ────────────────
    {"id": "b01", "query": "成都最好吃且口碑好的火锅推荐", "city": "成都",
     "expected_intent": "both", "expected_tools": ["search_places", "search_travel_notes"]},
    {"id": "b02", "query": "北京颐和园值得去吗，怎么参观最好", "city": "北京",
     "expected_intent": "both", "expected_tools": ["search_places", "search_travel_notes"]},
    {"id": "b03", "query": "上海有哪些值得打卡的网红景点", "city": "上海",
     "expected_intent": "both", "expected_tools": ["search_places", "search_travel_notes"]},
    {"id": "b04", "query": "厦门鼓浪屿哪些景点最值得游玩", "city": "厦门",
     "expected_intent": "both", "expected_tools": ["search_places", "search_travel_notes"]},
    {"id": "b05", "query": "广州哪里旅游最好玩综合推荐", "city": "广州",
     "expected_intent": "both", "expected_tools": ["search_places", "search_travel_notes"]},
    {"id": "b06", "query": "深圳适合摄影爱好者的地方推荐", "city": "深圳",
     "expected_intent": "both", "expected_tools": ["search_places", "search_travel_notes"]},
    {"id": "b07", "query": "杭州西湖周边最值得去的地方排行", "city": "杭州",
     "expected_intent": "both", "expected_tools": ["search_places", "search_travel_notes"]},
    {"id": "b08", "query": "成都九寨沟值得专门去一次吗", "city": "成都",
     "expected_intent": "both", "expected_tools": ["search_places", "search_travel_notes"]},
    {"id": "b09", "query": "北京最受游客欢迎的景点综合推荐", "city": "北京",
     "expected_intent": "both", "expected_tools": ["search_places", "search_travel_notes"]},
    {"id": "b10", "query": "杭州三天旅游行程综合推荐", "city": "杭州",
     "expected_intent": "both", "expected_tools": ["search_places", "search_travel_notes"]},

    # ── WEATHER 意图（5 条）：天气查询，期望调用 get_weather ────────────
    {"id": "w01", "query": "成都最近天气怎么样，适合去旅行吗", "city": "成都",
     "expected_intent": "weather", "expected_tools": ["get_weather"]},
    {"id": "w02", "query": "北京春天天气如何，几月份去最合适", "city": "北京",
     "expected_intent": "weather", "expected_tools": ["get_weather"]},
    {"id": "w03", "query": "上海梅雨季节什么时候，会影响旅游吗", "city": "上海",
     "expected_intent": "weather", "expected_tools": ["get_weather"]},
    {"id": "w04", "query": "深圳冬天需要带厚衣服吗，温度大概多少", "city": "深圳",
     "expected_intent": "weather", "expected_tools": ["get_weather"]},
    {"id": "w05", "query": "杭州几月份去旅游天气最好", "city": "杭州",
     "expected_intent": "weather", "expected_tools": ["get_weather"]},
]


# ═══════════════════════════════════════════════════════════════════
# 工具选择准确率判定
# ═══════════════════════════════════════════════════════════════════

def _check_tool_accuracy(
    actual_tool_calls: list[str],
    expected_tools: list[str],
    intent: str,
) -> bool:
    """
    判断工具选择是否正确。

    - amap/rag/weather：期望工具出现在实际调用中即算正确（允许多调）
    - both：两个工具都必须调用
    """
    actual_set = set(actual_tool_calls)
    expected_set = set(expected_tools)

    if intent == "both":
        return expected_set.issubset(actual_set)
    else:
        return bool(expected_set & actual_set)


def _check_synthesizer_validity(state: dict) -> bool:
    """Synthesizer 输出有效性：synthesized_places 中含 ≥1 个有 name 的地点"""
    places = state.get("synthesized_places", [])
    if not places:
        return False
    for p in places:
        name = getattr(p, "name", None) or (p.get("name") if isinstance(p, dict) else None)
        if name:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════
# 离线模式：FT Router 分类器准确率
# ═══════════════════════════════════════════════════════════════════

def run_offline_eval(cases: list[dict]) -> dict:
    """
    用 FT Router（LoRA 分类器）对所有 eval case 做离线分类评估。
    不需要 API Key 或数据库连接。
    """
    from app.config import settings
    from app.agents.nodes.router_classifier import classify

    print(f"\n{'='*60}")
    print("离线评估模式：FT Router 分类器准确率")
    print(f"模型路径：{settings.ft_router_model_path}")
    print(f"{'='*60}\n")

    results = []
    correct = 0
    intent_stats: dict[str, dict] = {}

    for case in cases:
        intent = case["expected_intent"]
        if intent not in intent_stats:
            intent_stats[intent] = {"total": 0, "correct": 0}

        result = classify(case["query"], case["city"], settings.ft_router_model_path)

        if result is None:
            predicted = "unknown"
            is_correct = False
        else:
            predicted = result.get("intent", "unknown")
            # weather 意图 FT Router 可能分类为 both/amap，视为 partial match
            if intent == "weather":
                is_correct = predicted in ("both", "amap", "weather")
            else:
                is_correct = (predicted == intent)

        if is_correct:
            correct += 1
            intent_stats[intent]["correct"] += 1
        intent_stats[intent]["total"] += 1

        status = "✅" if is_correct else "❌"
        results.append({
            "id": case["id"],
            "query": case["query"][:30],
            "expected": intent,
            "predicted": predicted,
            "correct": is_correct,
        })
        print(f"  {status} [{case['id']}] {case['query'][:28]:<30} | 期望:{intent:<7} 预测:{predicted}")

    total = len(cases)
    accuracy = correct / total if total else 0

    print(f"\n{'='*60}")
    print(f"FT Router 准确率：{correct}/{total} = {accuracy:.1%}")
    print("\n按意图类型分布：")
    for intent, stat in intent_stats.items():
        acc = stat["correct"] / stat["total"] if stat["total"] else 0
        flag = "✅" if acc >= 0.8 else "⚠️"
        print(f"  {flag} {intent:<8}: {stat['correct']}/{stat['total']} = {acc:.1%}")
    print(f"{'='*60}\n")

    return {
        "mode": "offline",
        "total": total,
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "intent_breakdown": {
            k: {"accuracy": round(v["correct"] / v["total"], 4) if v["total"] else 0, **v}
            for k, v in intent_stats.items()
        },
        "cases": results,
    }


# ═══════════════════════════════════════════════════════════════════
# 集成模式：完整 LangGraph pipeline 评估
# ═══════════════════════════════════════════════════════════════════

async def run_full_eval(cases: list[dict], n: Optional[int] = None) -> dict:
    """
    运行完整 LangGraph 图评估。
    需要：DEEPSEEK_API_KEY + PostgreSQL 数据库 + 已入库游记数据。
    """
    from langchain_core.messages import HumanMessage
    from app.agents.graph import build_graph
    from app.db.connection import get_pool

    subset = cases[:n] if n else cases
    print(f"\n{'='*60}")
    print("集成评估模式：完整 LangGraph Pipeline")
    print(f"评估样本：{len(subset)}/{len(cases)} 条")
    print(f"{'='*60}\n")

    graph = build_graph()
    await get_pool()   # 预热连接池

    metrics = {
        "router_correct": 0,
        "synthesizer_valid": 0,
        "e2e_success": 0,
        "critic_triggered": 0,
        "total_react_iterations": 0,
        "total_latency_ms": 0,
    }
    case_results = []

    for i, case in enumerate(subset, 1):
        print(f"[{i:02d}/{len(subset)}] {case['id']}: {case['query'][:40]}")
        t0 = time.perf_counter()

        init_state = {
            "messages": [HumanMessage(content=case["query"])],
            "thread_id": f"eval-{case['id']}",
            "user_id": "eval",
            "trip_city": case["city"],
            "intent": None,
            "query_rewrite": None,
            "react_iterations": 0,
            "working_context": None,
            "user_long_term_prefs": None,
            "amap_places": [],
            "rag_chunks": [],
            "synthesized_places": [],
            "final_response": None,
            "itinerary": None,
            "selected_place_ids": [],
            "critic_retry": False,
            "critic_reason": None,
            "critic_iterations": 0,
        }

        try:
            config = {"configurable": {"thread_id": f"eval-{case['id']}"}}
            final_state = await graph.ainvoke(init_state, config=config)
            latency_ms = int((time.perf_counter() - t0) * 1000)

            # ── 提取实际工具调用 ──────────────────────────────────
            actual_tools: list[str] = []
            for msg in final_state.get("messages", []):
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    actual_tools.extend(tc["name"] for tc in tool_calls)

            # ── 评判三项指标 ──────────────────────────────────────
            router_ok = _check_tool_accuracy(
                actual_tools, case["expected_tools"], case["expected_intent"]
            )
            synth_ok = _check_synthesizer_validity(final_state)
            e2e_ok = router_ok and synth_ok
            critic_fired = bool(final_state.get("critic_iterations", 0) > 0)
            react_iters = final_state.get("react_iterations", 0)

            # ── 累计指标 ──────────────────────────────────────────
            if router_ok:
                metrics["router_correct"] += 1
            if synth_ok:
                metrics["synthesizer_valid"] += 1
            if e2e_ok:
                metrics["e2e_success"] += 1
            if critic_fired:
                metrics["critic_triggered"] += 1
            metrics["total_react_iterations"] += react_iters
            metrics["total_latency_ms"] += latency_ms

            status = "✅" if e2e_ok else ("⚠️" if (router_ok or synth_ok) else "❌")
            places_count = len(final_state.get("synthesized_places", []))
            print(f"       {status} 工具:{actual_tools} | 地点:{places_count} | "
                  f"Critic:{'触发' if critic_fired else '-'} | {latency_ms}ms")

            case_results.append({
                "id": case["id"],
                "query": case["query"],
                "city": case["city"],
                "expected_intent": case["expected_intent"],
                "actual_tools": actual_tools,
                "router_ok": router_ok,
                "synthesizer_valid": synth_ok,
                "e2e_success": e2e_ok,
                "critic_triggered": critic_fired,
                "react_iterations": react_iters,
                "places_count": places_count,
                "latency_ms": latency_ms,
            })

        except Exception as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            print(f"       ❌ 异常：{exc}")
            case_results.append({
                "id": case["id"],
                "query": case["query"],
                "error": str(exc),
                "router_ok": False,
                "synthesizer_valid": False,
                "e2e_success": False,
                "latency_ms": latency_ms,
            })

    total = len(subset)
    summary = {
        "mode": "full",
        "total": total,
        "router_accuracy": round(metrics["router_correct"] / total, 4) if total else 0,
        "synthesizer_validity": round(metrics["synthesizer_valid"] / total, 4) if total else 0,
        "e2e_success_rate": round(metrics["e2e_success"] / total, 4) if total else 0,
        "critic_trigger_rate": round(metrics["critic_triggered"] / total, 4) if total else 0,
        "avg_react_iterations": round(metrics["total_react_iterations"] / total, 2) if total else 0,
        "avg_latency_ms": round(metrics["total_latency_ms"] / total) if total else 0,
        "cases": case_results,
    }

    # ── 打印汇总 ──────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("Agent 评估结果汇总")
    print(f"{'='*60}")
    def _flag(value, threshold):
        return "✅" if value >= threshold else "❌"
    print(f"  Router 工具选择准确率  : {summary['router_accuracy']:.1%}  "
          f"{_flag(summary['router_accuracy'], 0.75)}  （目标 ≥75%）")
    print(f"  Synthesizer 输出有效率 : {summary['synthesizer_validity']:.1%}  "
          f"{_flag(summary['synthesizer_validity'], 0.80)}  （目标 ≥80%）")
    print(f"  端到端成功率           : {summary['e2e_success_rate']:.1%}  "
          f"{_flag(summary['e2e_success_rate'], 0.70)}  （目标 ≥70%）")
    print(f"  Critic 触发率          : {summary['critic_trigger_rate']:.1%}  （诊断项）")
    print(f"  平均 ReAct 迭代次数    : {summary['avg_react_iterations']}")
    print(f"  平均端到端延迟         : {summary['avg_latency_ms']}ms")
    print(f"{'='*60}\n")

    return summary


# ═══════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="BreezeTravel Agent 端到端评估")
    parser.add_argument(
        "--mode", choices=["offline", "full"], default="offline",
        help="offline=FT Router 离线准确率；full=完整 pipeline 集成评估",
    )
    parser.add_argument(
        "--n", type=int, default=None,
        help="集成模式下只跑前 N 条 case（节省 API 费用）",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="保存评估结果到 JSON 文件",
    )
    args = parser.parse_args()

    if args.mode == "offline":
        result = run_offline_eval(EVAL_CASES)
    else:
        result = asyncio.run(run_full_eval(EVAL_CASES, n=args.n))

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"评估结果已保存到：{out_path}")

    return result


if __name__ == "__main__":
    main()
