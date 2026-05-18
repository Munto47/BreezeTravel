"""
Sprint 3 — F1：Router 意图分类训练数据生成

使用 DeepSeek API 批量生成旅游查询 → 意图标签 的训练样本（数据蒸馏）。

意图类别：
  - amap    : 找地点/景点/餐厅/住宿（需要 POI 搜索）
  - rag     : 求攻略/游记/避坑经验（需要 RAG 语义检索）
  - both    : 既要口碑好又要具体地点（同时调两路）
  - weather : 询问天气/季节/穿衣建议

输出格式（Alpaca ChatML，适配 Qwen2.5）：
  {"messages": [...], "label": "amap", "query_rewrite": "..."}

用法：
  export DEEPSEEK_API_KEY=sk-xxx
  python -m scripts.generate_training_data --samples 1500 --out data/router_train.jsonl
"""

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

# 自动加载 .env（脚本从 backend/ 运行时读取 backend/../.env 或 backend/.env）
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env", override=False)
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)

from openai import OpenAI

CITIES = ["成都", "北京", "上海", "西安", "杭州", "重庆", "广州", "厦门", "大理", "丽江", "三亚", "青岛"]

INTENT_TARGETS = {
    "amap":    400,
    "rag":     400,
    "both":    400,
    "weather": 300,
}

SEED_QUERIES = {
    "amap": [
        "成都有什么好吃的火锅？",
        "帮我找北京故宫附近的住宿",
        "杭州西湖旁边有什么景点？",
        "西安回民街附近的餐厅推荐",
        "重庆洪崖洞在哪里？",
        "找三亚的海鲜餐厅",
        "大理古城附近的民宿",
        "厦门鼓浪屿有什么值得去的地方？",
    ],
    "rag": [
        "成都旅游有什么避坑指南？",
        "西安旅游攻略，第一次去怎么安排？",
        "杭州西湖游玩建议，有哪些注意事项？",
        "去北京旅游需要提前准备什么？",
        "三亚自由行有什么经验分享？",
        "大理旅游体验怎么样？适合带小孩吗？",
        "厦门旅游游记，几天比较合适？",
        "重庆旅游被坑过的地方有哪些？",
    ],
    "both": [
        "成都评价最好的火锅店有哪些？",
        "西安口碑最好的景点推荐",
        "杭州大家最推荐去哪里玩？",
        "北京值得打卡的地方，本地人推荐那些？",
        "三亚哪个沙滩最值得去？有游记吗？",
        "大理哪里风景最美、游客评价也好？",
        "厦门当地人最爱去的餐厅是哪些？",
        "重庆网红打卡地有哪些真的值得去的？",
    ],
    "weather": [
        "成都几月份去最合适？",
        "西安旅游天气怎么样？需要带厚衣服吗？",
        "杭州春天去会下雨吗？",
        "北京冬天冷不冷？适合旅游吗？",
        "三亚夏天去热不热？",
        "大理的天气如何？全年都适合旅游吗？",
        "去厦门玩天气怎么样？",
        "重庆夏天热到什么程度？",
    ],
}

SYSTEM_PROMPT = """你是一个旅游意图分类数据生成专家。

任务：根据给定的城市和意图类型，生成多样化的旅游查询问题，并为每个问题标注：
1. intent：意图类别（amap/rag/both/weather）
2. query_rewrite：清晰改写后的查询（去除口语化、补充城市信息）

意图定义：
- amap   : 用户要找具体地点/POI（景点、餐厅、住宿、娱乐），需要地图搜索
- rag    : 用户要求攻略/游记/经验分享/避坑建议，需要检索游记库
- both   : 用户要找口碑好的地点（兼有具体地点+评价需求），两路都要
- weather: 用户问天气/季节/最佳出行时间/穿衣建议

输出格式（JSON Lines，每行一个）：
{"query": "...", "city": "...", "intent": "amap", "query_rewrite": "..."}

要求：
- 语气多样：口语/书面/方言味/简短/详细都要有
- 不要重复，每条都要自然且真实
- query_rewrite 必须明确包含城市名，语义清晰
"""


def build_generation_prompt(intent: str, city: str, n: int, seeds: list[str]) -> str:
    seed_str = "\n".join(f"- {s}" for s in random.sample(seeds, min(3, len(seeds))))
    return f"""请生成 {n} 条属于 intent="{intent}" 类别的旅游查询，目的地城市为"{city}"。

参考示例风格（不要直接抄写）：
{seed_str}

直接输出 {n} 行 JSON，不要解释，不要 markdown 代码块。"""


def call_deepseek(client: OpenAI, prompt: str, model: str = "deepseek-chat") -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.9,
        max_tokens=2000,
    )
    return resp.choices[0].message.content or ""


def parse_jsonl_response(raw: str) -> list[dict]:
    results = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 去掉可能的 markdown 代码块标记
        if line.startswith("```"):
            continue
        try:
            obj = json.loads(line)
            if "query" in obj and "intent" in obj and "query_rewrite" in obj:
                results.append(obj)
        except json.JSONDecodeError:
            # 尝试从行中提取 JSON 对象
            match = re.search(r'\{.*\}', line)
            if match:
                try:
                    obj = json.loads(match.group())
                    if "query" in obj and "intent" in obj and "query_rewrite" in obj:
                        results.append(obj)
                except json.JSONDecodeError:
                    pass
    return results


def to_chat_messages(query: str, city: str, intent: str, query_rewrite: str) -> dict:
    """转换为 Qwen2.5 ChatML 格式（SFTTrainer 直接使用）"""
    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一个旅行意图分类器。根据用户查询，输出 JSON：\n"
                    '{"intent": "amap"|"rag"|"both"|"weather", "query_rewrite": "改写后的查询"}\n'
                    "意图定义：\n"
                    "- amap   : 找具体地点/POI（景点/餐厅/住宿）\n"
                    "- rag    : 求攻略/游记/避坑经验\n"
                    "- both   : 找口碑好的地点（同时需要地点和评价）\n"
                    "- weather: 询问天气/季节/穿衣建议\n"
                    "只输出 JSON，不要解释。"
                ),
            },
            {
                "role": "user",
                "content": f'用户查询: "{query}"\n目的地城市: {city}',
            },
            {
                "role": "assistant",
                "content": json.dumps(
                    {"intent": intent, "query_rewrite": query_rewrite},
                    ensure_ascii=False,
                ),
            },
        ],
        "label": intent,
        "city": city,
        "original_query": query,
    }


def generate_for_intent(
    client: OpenAI,
    intent: str,
    target_count: int,
    model: str,
    verbose: bool,
) -> list[dict]:
    samples = []
    seeds = SEED_QUERIES[intent]
    batch_size = 10
    retries = 0
    max_retries = 3

    while len(samples) < target_count and retries < max_retries * 10:
        need = target_count - len(samples)
        city = random.choice(CITIES)
        n = min(batch_size, need + 2)

        prompt = build_generation_prompt(intent, city, n, seeds)

        try:
            raw = call_deepseek(client, prompt, model)
            parsed = parse_jsonl_response(raw)
            for item in parsed:
                item.setdefault("city", city)
                item["intent"] = intent  # 强制保持正确标签
                samples.append(to_chat_messages(
                    item["query"],
                    item.get("city", city),
                    intent,
                    item["query_rewrite"],
                ))
                if len(samples) >= target_count:
                    break

            if verbose:
                print(f"  [{intent}] {len(samples)}/{target_count} 条，本批解析 {len(parsed)} 条")

            # 避免限流
            time.sleep(0.5)
            retries = 0

        except Exception as exc:
            retries += 1
            print(f"  [{intent}] 调用失败（重试 {retries}）: {exc}", file=sys.stderr)
            time.sleep(2 ** retries)

    return samples[:target_count]


def main():
    parser = argparse.ArgumentParser(description="生成 Router LoRA 微调训练数据")
    parser.add_argument("--samples", type=int, default=1500, help="总样本数（默认 1500）")
    parser.add_argument("--out", default="data/router_train.jsonl", help="训练集输出路径")
    parser.add_argument("--test-out", default="data/router_test.jsonl", help="测试集输出路径（20%%）")
    parser.add_argument("--model", default="deepseek-chat", help="DeepSeek 模型")
    parser.add_argument("--api-key", default=os.getenv("DEEPSEEK_API_KEY"), help="DeepSeek API Key")
    parser.add_argument("--api-url", default="https://api.deepseek.com/v1", help="API Base URL")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")
    args = parser.parse_args()

    if not args.api_key:
        print("错误：请设置 DEEPSEEK_API_KEY 环境变量或传入 --api-key", file=sys.stderr)
        sys.exit(1)

    # 按比例分配样本数
    total = args.samples
    scale = total / sum(INTENT_TARGETS.values())
    targets = {k: max(50, int(v * scale)) for k, v in INTENT_TARGETS.items()}

    client = OpenAI(api_key=args.api_key, base_url=args.api_url)

    all_samples: list[dict] = []
    for intent, count in targets.items():
        print(f"\n生成 {intent} 类别，目标 {count} 条...")
        samples = generate_for_intent(client, intent, count, args.model, args.verbose)
        all_samples.extend(samples)
        print(f"  完成：{len(samples)} 条")

    # 打乱顺序
    random.shuffle(all_samples)

    # 8:2 切分训练/测试集
    split = int(len(all_samples) * 0.8)
    train_samples = all_samples[:split]
    test_samples = all_samples[split:]

    # 写出
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for s in train_samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    test_path = Path(args.test_out)
    test_path.parent.mkdir(parents=True, exist_ok=True)
    with open(test_path, "w", encoding="utf-8") as f:
        for s in test_samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    # 统计
    from collections import Counter
    label_counts = Counter(s["label"] for s in all_samples)
    print(f"\n数据生成完成:")
    print(f"  总计: {len(all_samples)} 条")
    print(f"  训练: {len(train_samples)} 条 → {args.out}")
    print(f"  测试: {len(test_samples)} 条 → {args.test_out}")
    print(f"  类别分布: {dict(label_counts)}")


if __name__ == "__main__":
    main()
