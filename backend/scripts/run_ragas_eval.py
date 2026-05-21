"""
RAGAS 独立评估脚本（解耦 pytest skip，直接用 DeepSeek 作为评估 LLM）

用法：
  cd backend
  DEEPSEEK_API_KEY=sk-xxx OPENAI_API_KEY=sk-siliconflow... \\
    OPENAI_API_URL=https://api.siliconflow.cn/v1 EMBEDDING_MODEL=BAAI/bge-m3 \\
    DATABASE_URL=postgresql+asyncpg://... python -m scripts.run_ragas_eval

输出：
  results/ragas_eval.txt  —— 详细评估结果
  results/ragas_scores.json —— 机器可读的分数 JSON
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── 评估数据集（10 个代表性旅行问题 + 参考答案）──────────────────────────
EVAL_DATASET = [
    {
        "question": "成都锦里古街有什么好吃的小吃？",
        "ground_truth": "锦里古街有三大炮、冰粉、叶儿粑、糖油果子等传统成都小吃，建议晚上去人少一些。",
    },
    {
        "question": "北京故宫参观需要注意什么？",
        "ground_truth": "故宫需要提前网上预约门票，不能自带食物入内，旺季人多建议早到，游览时间至少3小时。",
    },
    {
        "question": "上海外滩附近住哪里比较方便？",
        "ground_truth": "外滩附近有和平饭店、浦东香格里拉等高端酒店，南京东路步行街周边也有较多中端选择。",
    },
    {
        "question": "厦门鼓浪屿一日游怎么安排？",
        "ground_truth": "建议早上坐轮渡上岛避开高峰，先游日光岩，再逛龙头路小吃街，下午去菽庄花园，傍晚返回。",
    },
    {
        "question": "成都都江堰景区怎么去最方便？",
        "ground_truth": "从成都市区坐地铁2号线到犀浦，换乘城际铁路约30分钟可达都江堰，票价约15元。",
    },
]


def _infer_city(question: str) -> str:
    for city in ["成都", "北京", "上海", "厦门"]:
        if city in question:
            return city
    return "成都"


async def run_rag_queries() -> list[dict]:
    """对评估数据集跑 RAG 检索 + Synthesizer，收集 (question, answer, contexts, ground_truth)"""
    from app.agents.nodes import rag_retrieval, synthesizer
    from app.agents.state import default_working_context
    from langchain_core.messages import HumanMessage

    results = []
    for item in EVAL_DATASET:
        question = item["question"]
        city = _infer_city(question)
        print(f"  检索中：{question[:30]}...")

        state = {
            "messages": [HumanMessage(content=question)],
            "thread_id": "ragas-eval",
            "user_id": "eval",
            "trip_city": city,
            "intent": "rag",
            "query_rewrite": question,
            "amap_places": [],
            "rag_chunks": [],
            "synthesized_places": [],
            "final_response": None,
            "itinerary": None,
            "selected_place_ids": [],
            "working_context": default_working_context(),
            "user_long_term_prefs": None,
            "react_iterations": 0,
            "critic_retry": False,
            "critic_reason": None,
            "critic_iterations": 0,
        }

        # RAG 检索
        rag_result = await rag_retrieval.run(state)
        state.update(rag_result)

        # Synthesizer 生成答案
        synth_result = await synthesizer.run(state)
        answer = synth_result.get("final_response", "") or ""
        contexts = [c["content"] for c in state.get("rag_chunks", [])]

        results.append({
            "question": question,
            "answer": answer,
            "contexts": contexts if contexts else ["无相关上下文"],
            "ground_truth": item["ground_truth"],
        })
        print(f"    ✓ 答案 {len(answer)} 字，上下文 {len(contexts)} 段")

    return results


def compute_ragas_scores(results: list[dict]) -> dict:
    """用 DeepSeek 作为评估 LLM，计算 RAGAS 三项指标（适配 RAGAS 0.4.3）"""
    from datasets import Dataset
    from ragas import evaluate
    # RAGAS 0.4.3 新导入路径
    try:
        from ragas.metrics.collections import faithfulness, answer_relevancy, context_recall
    except ImportError:
        from ragas.metrics import faithfulness, answer_relevancy, context_recall

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    sf_key = os.environ.get("OPENAI_API_KEY", "")
    sf_url = os.environ.get("OPENAI_API_URL", "https://api.siliconflow.cn/v1")

    if not deepseek_key:
        raise ValueError("DEEPSEEK_API_KEY 未设置")

    # ── RAGAS 0.4.3 API ──────────────────────────────────────────────────
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning)

    from openai import OpenAI
    from ragas.llms import llm_factory
    from langchain_openai import OpenAIEmbeddings
    from ragas.embeddings import LangchainEmbeddingsWrapper

    # DeepSeek 作为评估 LLM（llm_factory 是 0.4.3 推荐用法）
    ds_client = OpenAI(api_key=deepseek_key, base_url="https://api.deepseek.com/v1")
    eval_llm = llm_factory("deepseek-chat", client=ds_client)

    # SiliconFlow BAAI/bge-m3 作为评估 Embedding（LangchainEmbeddingsWrapper 仍支持）
    eval_emb = LangchainEmbeddingsWrapper(OpenAIEmbeddings(
        model="BAAI/bge-m3",
        api_key=sf_key,
        base_url=sf_url,
    ))

    print("  RAGAS 已配置：LLM=deepseek-chat，Embedding=BAAI/bge-m3")

    dataset = Dataset.from_list(results)

    print("  开始 RAGAS 评估（每项指标调用 LLM，约需 2-5 分钟）...")
    result = evaluate(
        dataset=dataset,
        metrics=[faithfulness, answer_relevancy, context_recall],
        llm=eval_llm,
        embeddings=eval_emb,
        raise_exceptions=False,   # 允许部分失败，不中断
        show_progress=True,
    )

    # EvaluationResult.to_pandas() → DataFrame，列名 = 指标名
    df = result.to_pandas()
    print(f"\n  原始评估 DataFrame 列: {list(df.columns)}")

    def _col_mean(col_name: str) -> float:
        if col_name not in df.columns:
            return 0.0
        valid = df[col_name].dropna()
        return round(float(valid.mean()), 4) if len(valid) > 0 else 0.0

    return {
        "faithfulness":     _col_mean("faithfulness"),
        "answer_relevancy": _col_mean("answer_relevancy"),
        "context_recall":   _col_mean("context_recall"),
        "n_samples": len(df),
        "per_sample": df[
            [c for c in ["question", "faithfulness", "answer_relevancy", "context_recall"]
             if c in df.columns]
        ].to_dict(orient="records"),
    }


async def main():
    Path("results").mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("=" * 60)
    print("BreezeTravel RAGAS 评估")
    print(f"时间：{ts}")
    print("配置：HyDE + 混合检索(BM25+pgvector RRF) + bge-reranker 精排")
    print("=" * 60)

    # Step 1: 跑 RAG 查询
    print("\n[Step 1] 对 5 个旅行问题执行 Advanced RAG 检索...")
    results = await run_rag_queries()
    print(f"  完成：{len(results)} 条问答对")

    # Step 2: 计算 RAGAS 指标
    print("\n[Step 2] 计算 RAGAS 三项指标（使用 DeepSeek 评估 LLM）...")
    scores = compute_ragas_scores(results)

    avg = round((scores["faithfulness"] + scores["answer_relevancy"] + scores["context_recall"]) / 3, 4)

    # Step 3: 打印结果
    report = f"""
{'=' * 60}
RAGAS 评估结果 — BreezeTravel Advanced RAG
{'=' * 60}
时间：{ts}
数据集：{len(EVAL_DATASET)} 个旅行问题（成都/北京/上海/厦门）
检索配置：HyDE查询扩展 + BM25+pgvector混合检索 + bge-reranker精排

┌──────────────────────┬────────┐
│ 指标                 │  分数  │
├──────────────────────┼────────┤
│ Faithfulness         │ {scores['faithfulness']:.4f} │  回答忠实于检索上下文（不幻觉）
│ Answer Relevancy     │ {scores['answer_relevancy']:.4f} │  回答与用户问题的相关度
│ Context Recall       │ {scores['context_recall']:.4f} │  检索结果覆盖参考答案关键信息
├──────────────────────┼────────┤
│ 综合平均             │ {avg:.4f} │
└──────────────────────┴────────┘

Embedding 模型：BAAI/bge-m3（SiliconFlow，1024维）
评估 LLM：deepseek-chat
游记语料：80篇合成游记，209个chunk，4城市
"""
    print(report)

    # Step 4: 保存结果
    with open("results/ragas_eval.txt", "w", encoding="utf-8") as f:
        f.write(report)

    json_result = {
        "timestamp": ts,
        "scores": scores,
        "avg": avg,
        "config": {
            "retrieval": "HyDE + BM25+pgvector RRF + bge-reranker",
            "embedding_model": "BAAI/bge-m3",
            "eval_llm": "deepseek-chat",
            "corpus": "80篇合成游记，209个chunk，4城市",
        }
    }
    with open("results/ragas_scores.json", "w", encoding="utf-8") as f:
        json.dump(json_result, f, ensure_ascii=False, indent=2)

    print(f"结果已保存：results/ragas_eval.txt  results/ragas_scores.json")
    return scores


if __name__ == "__main__":
    asyncio.run(main())
