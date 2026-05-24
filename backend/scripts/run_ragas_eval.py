"""
自定义 RAG 评估脚本（LLM-Judge 方式，兼容 DeepSeek n=1 限制）

不依赖 RAGAS 库的内部批处理（避免 n>1 BadRequestError），
直接用 DeepSeek 作为评估 LLM，逐条打分三项核心指标：

  Faithfulness      : 回答中每个声明是否能在检索上下文中找到支撑
  Answer Relevancy  : 回答与用户问题的相关度
  Context Recall    : 参考答案中的关键信息在检索结果中的覆盖率

用法（从项目根目录执行）：
  $env:PYTHONPATH="backend"
  python backend/scripts/run_ragas_eval.py

输出：
  backend/results/ragas_eval.json   ── JSON 格式分数
  backend/results/ragas_eval.txt    ── 可读报告
"""

import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── 使用扩展后的 21 条评估集（来自 test_rag.py）──────────────────────────────
from tests.test_rag import _EVAL_DATASET


# ═══════════════════════════════════════════════════════════════════
# Step 1: RAG 检索 + Synthesizer 生成答案
# ═══════════════════════════════════════════════════════════════════

_RAG_ANSWER_PROMPT = """你是一位专业的旅行顾问。根据以下游记摘录，回答用户的旅行问题。

【用户问题】
{question}

【参考游记摘录】
{contexts}

请根据游记内容简洁回答，100-200字，不要超出游记提供的信息范围。"""


async def run_rag_queries(dataset: list[dict]) -> list[dict]:
    """RAG 检索 + 直接 LLM 生成答案（不依赖 Synthesizer，避免 amap_places=[] 导致报错）"""
    from app.agents.nodes import rag_retrieval
    from app.agents.state import default_working_context
    from app.config import settings
    from langchain_core.messages import HumanMessage
    from openai import AsyncOpenAI

    llm = AsyncOpenAI(
        api_key=settings.effective_llm_api_key,
        base_url=settings.effective_llm_api_url,
    )

    results = []
    for item in dataset:
        question = item["question"]
        city = item.get("city", _infer_city(question))
        print(f"  [{len(results)+1:02d}/{len(dataset)}] 检索：{question[:35]}...")

        state = {
            "messages": [HumanMessage(content=question)],
            "thread_id": "ragas-eval", "user_id": "eval",
            "trip_city": city, "intent": item.get("intent", "rag"), "query_rewrite": question,
            "amap_places": [], "rag_chunks": [], "synthesized_places": [],
            "final_response": None, "itinerary": None,
            "selected_place_ids": [], "working_context": default_working_context(),
            "user_long_term_prefs": None, "react_iterations": 0,
            "critic_retry": False, "critic_reason": None, "critic_iterations": 0,
        }
        rag_result = await rag_retrieval.run(state)
        state.update(rag_result)

        contexts = [c["content"] for c in state.get("rag_chunks", [])]
        ctx_text = "\n\n---\n\n".join(contexts) if contexts else "无相关游记"

        # 直接用 LLM 基于检索上下文生成答案（绕过 Synthesizer 对 amap_places 的依赖）
        try:
            resp = await llm.chat.completions.create(
                model=settings.llm_model_synthesizer,
                messages=[{"role": "user", "content": _RAG_ANSWER_PROMPT.format(
                    question=question, contexts=ctx_text
                )}],
                max_tokens=300,
                temperature=0.3,
            )
            answer = resp.choices[0].message.content.strip()
        except Exception as e:
            answer = f"（生成失败：{e}）"

        results.append({
            "question": question,
            "answer": answer,
            "contexts": contexts if contexts else ["无相关上下文"],
            "ground_truth": item["ground_truth"],
            "city": city,
            "intent": item.get("intent", ""),
        })
        print(f"       ✓ 答案 {len(answer)} 字，上下文 {len(contexts)} 段")
    return results


def _infer_city(question: str) -> str:
    for city in ["成都", "北京", "上海", "厦门", "广州", "深圳", "杭州"]:
        if city in question:
            return city
    return "成都"


# ═══════════════════════════════════════════════════════════════════
# Step 2: LLM-Judge 评估（不依赖 RAGAS 内部 n>1 机制）
# ═══════════════════════════════════════════════════════════════════

_FAITHFULNESS_PROMPT = """你是一个严格的 RAG 系统评估专家。

【用户问题】
{question}

【检索到的上下文】
{contexts}

【系统回答】
{answer}

任务：评估系统回答的"忠实度"（Faithfulness）。
忠实度 = 回答中所有声明都能在上下文中找到支撑，没有凭空捏造的信息。

请给出 0.0~1.0 的分数：
- 1.0：回答完全基于上下文，无任何幻觉
- 0.7~0.9：大部分基于上下文，有少量推断
- 0.4~0.6：约一半内容有上下文支撑
- 0~0.3：大量内容无上下文支撑或有明显幻觉

只输出一个数字（如 0.85），不要输出其他内容。"""

_RELEVANCY_PROMPT = """你是一个严格的 RAG 系统评估专家。

【用户问题】
{question}

【系统回答】
{answer}

任务：评估回答与问题的"相关度"（Answer Relevancy）。
相关度 = 回答是否直接回应了用户的问题，没有跑题或答非所问。

请给出 0.0~1.0 的分数：
- 1.0：完全切题，直接回答了问题
- 0.7~0.9：基本切题，有少量跑题
- 0.4~0.6：部分回答了问题
- 0~0.3：基本没有回答问题或完全跑题

只输出一个数字（如 0.85），不要输出其他内容。"""

_RECALL_PROMPT = """你是一个严格的 RAG 系统评估专家。

【用户问题】
{question}

【检索到的上下文】
{contexts}

【参考答案（人工标注的正确答案）】
{ground_truth}

任务：评估"上下文召回率"（Context Recall）。
召回率 = 参考答案中的关键信息有多少能在检索到的上下文中找到。

请给出 0.0~1.0 的分数：
- 1.0：参考答案的所有关键信息都在上下文中
- 0.7~0.9：大部分关键信息在上下文中
- 0.4~0.6：约一半关键信息在上下文中
- 0~0.3：上下文几乎不包含参考答案的关键信息

只输出一个数字（如 0.65），不要输出其他内容。"""


async def llm_judge_one(client, prompt: str) -> float:
    """调用 DeepSeek 获取单条评分（n=1，无批处理）"""
    try:
        resp = await client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=0,
        )
        text = resp.choices[0].message.content.strip()
        # 提取第一个浮点数
        import re
        m = re.search(r"([\d.]+)", text)
        val = float(m.group(1)) if m else 0.0
        return max(0.0, min(1.0, val))
    except Exception as e:
        print(f"       [LLM-Judge] 评分失败：{e}")
        return float("nan")


async def evaluate_with_llm_judge(results: list[dict]) -> dict:
    """对所有样本逐条调用 LLM-Judge，计算三项指标均值"""
    from openai import AsyncOpenAI
    from app.config import settings

    client = AsyncOpenAI(
        api_key=settings.effective_llm_api_key,
        base_url=settings.effective_llm_api_url,
    )

    faithfulness_scores = []
    relevancy_scores = []
    recall_scores = []
    per_sample = []

    total = len(results)
    for i, item in enumerate(results, 1):
        q = item["question"]
        a = item["answer"]
        ctx_text = "\n\n---\n\n".join(item["contexts"])
        gt = item["ground_truth"]

        print(f"  [{i:02d}/{total}] 评估：{q[:30]}...")

        # 三项指标并发评估（各自 n=1）
        f_score, r_score, c_score = await asyncio.gather(
            llm_judge_one(client, _FAITHFULNESS_PROMPT.format(
                question=q, contexts=ctx_text, answer=a)),
            llm_judge_one(client, _RELEVANCY_PROMPT.format(
                question=q, answer=a)),
            llm_judge_one(client, _RECALL_PROMPT.format(
                question=q, contexts=ctx_text, ground_truth=gt)),
        )

        import math
        f_ok = not math.isnan(f_score)
        r_ok = not math.isnan(r_score)
        c_ok = not math.isnan(c_score)

        if f_ok: faithfulness_scores.append(f_score)
        if r_ok: relevancy_scores.append(r_score)
        if c_ok: recall_scores.append(c_score)

        print(f"       F={f_score:.2f}  R={r_score:.2f}  C={c_score:.2f}")
        per_sample.append({
            "question": q[:50], "city": item.get("city"), "intent": item.get("intent"),
            "faithfulness": round(f_score, 4) if f_ok else None,
            "answer_relevancy": round(r_score, 4) if r_ok else None,
            "context_recall": round(c_score, 4) if c_ok else None,
        })

    def _mean(lst):
        valid = [x for x in lst if not (x != x)]  # filter NaN
        return round(sum(valid) / len(valid), 4) if valid else 0.0

    return {
        "faithfulness":     _mean(faithfulness_scores),
        "answer_relevancy": _mean(relevancy_scores),
        "context_recall":   _mean(recall_scores),
        "n_samples": total,
        "n_valid": {
            "faithfulness": len(faithfulness_scores),
            "answer_relevancy": len(relevancy_scores),
            "context_recall": len(recall_scores),
        },
        "per_sample": per_sample,
    }


# ═══════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════

async def main():
    results_dir = Path(__file__).parent.parent / "results"
    results_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("=" * 65)
    print("BreezeTravel Advanced RAG 评估（LLM-Judge，DeepSeek n=1）")
    print(f"时间：{ts}")
    print(f"评估集：{len(_EVAL_DATASET)} 条（7 城市 × 3 意图类型）")
    print("=" * 65)

    print(f"\n[Step 1] RAG 检索 + Synthesizer 生成答案...")
    results = await run_rag_queries(_EVAL_DATASET)
    print(f"  完成：{len(results)} 条问答对")

    print(f"\n[Step 2] LLM-Judge 逐条评估三项指标...")
    scores = await evaluate_with_llm_judge(results)

    avg = (scores["faithfulness"] + scores["answer_relevancy"] + scores["context_recall"]) / 3

    # 按意图类型分组分析
    intent_groups: dict[str, list] = {}
    for s in scores["per_sample"]:
        intent = s.get("intent", "unknown")
        intent_groups.setdefault(intent, []).append(s)

    report_lines = [
        "",
        "=" * 65,
        "RAGAS 评估结果（LLM-Judge 方式）— BreezeTravel Advanced RAG",
        "=" * 65,
        f"时间：{ts}",
        f"评估集：{scores['n_samples']} 条（7 城市 × 3 意图类型）",
        f"评估 LLM：deepseek-chat（n=1，自定义 Judge Prompt）",
        f"检索配置：HyDE + BM25+pgvector RRF + bge-reranker 精排",
        f"游记语料：347 篇 / 2075 chunk / 7 城市",
        "",
        f"  Faithfulness      : {scores['faithfulness']:.4f}  "
        f"{'✅' if scores['faithfulness'] >= 0.75 else '❌'}  （目标 ≥ 0.75，n={scores['n_valid']['faithfulness']}）",
        f"  Answer Relevancy  : {scores['answer_relevancy']:.4f}  "
        f"{'✅' if scores['answer_relevancy'] >= 0.75 else '❌'}  （目标 ≥ 0.75，n={scores['n_valid']['answer_relevancy']}）",
        f"  Context Recall    : {scores['context_recall']:.4f}  "
        f"{'✅' if scores['context_recall'] >= 0.65 else '❌'}  （目标 ≥ 0.65，n={scores['n_valid']['context_recall']}）",
        f"  综合平均          : {avg:.4f}",
        "",
    ]

    # 按意图类型分布
    report_lines.append("  按意图类型 Context Recall：")
    for intent, samples in intent_groups.items():
        recalls = [s["context_recall"] for s in samples if s["context_recall"] is not None]
        avg_recall = sum(recalls) / len(recalls) if recalls else 0.0
        flag = "✅" if avg_recall >= 0.65 else "⚠️"
        report_lines.append(f"    {flag} {intent:<10}: {avg_recall:.4f}  ({len(samples)} 条)")

    report = "\n".join(report_lines)
    print(report)

    # 保存结果
    txt_path = results_dir / "ragas_eval.txt"
    json_path = results_dir / "ragas_eval.json"

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(report)

    json_result = {
        "timestamp": ts,
        "eval_method": "llm_judge_deepseek_n1",
        "scores": scores,
        "avg": round(avg, 4),
        "config": {
            "retrieval": "HyDE + BM25+pgvector RRF",
            "embedding_model": "BAAI/bge-m3",
            "eval_llm": "deepseek-chat (n=1)",
            "corpus": "347篇合成游记，2075 chunk，7城市",
            "dataset_size": len(_EVAL_DATASET),
        },
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_result, f, ensure_ascii=False, indent=2)

    print(f"\n结果已保存：\n  {txt_path}\n  {json_path}")
    return scores


if __name__ == "__main__":
    asyncio.run(main())
