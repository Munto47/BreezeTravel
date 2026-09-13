"""Collect actual Qwen-generated travel inputs, never gold labels or fixtures.

Opt-in only. Credentials stay in the existing private runtime configuration.
Prompts and literal model responses are saved under ignored local artifacts;
stdout contains only bounded progress, usage and error categories.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values
from openai import AsyncOpenAI


ROOT = Path(__file__).resolve().parents[2]
CITIES = (("beijing", "北京"), ("shanghai", "上海"), ("guangzhou", "广州"), ("shenzhen", "深圳"), ("hangzhou", "杭州"))
LONG_TAIL_CITIES = (("chengdu", "成都"), ("xian", "西安"), ("chongqing", "重庆"), ("suzhou", "苏州"),
    ("nanjing", "南京"), ("wuhan", "武汉"), ("changsha", "长沙"), ("xiamen", "厦门"), ("qingdao", "青岛"), ("xinyu", "新余"))
BRIEFS = (
    "第一次来，3天经典路线，每天三到五站；普通文字叙述。",
    "4天慢节奏城市漫步，包含街道、特色建筑、美术馆；用自然段写。",
    "带父母玩3天，少爬山，避免频繁换酒店；用逐日清单写。",
    "情侣周末2天，白天游玩晚上看夜景；用Markdown标题和加粗写。",
    "亲子4天，公园、动物或科普内容穿插；注明可替换的雨天方案。",
    "5天第一次旅行，每天上午下午晚上分段，有一天主要休息。",
    "3天人文路线，尽量区分同名博物馆的不同馆区和入口。",
    "2天美食与景点混合，餐饮街区和具体餐馆建议分清，不必把推荐全都排进去。",
    "3天行程，第二天给互斥方案A/B，两个方案都保留各自顺序。",
    "4天跨城旅行，前两天在本城，后两天选邻近一座城市；逐日写清城市，最后附可选地点及不想去的地点。",
    "5天摄影旅行，明确区分实际走到的地点和远处眺望的建筑。",
    "3天首次出行，从高铁站抵达，最后一天从另一个交通点离开。",
    "4天行程，用表格呈现日期、上午、下午、晚上与用餐。",
    "周末2天，第一天和第二天都要真正回访同一处夜景区域，说明区别。",
    "3天传统建筑路线，写出游览景区内的说明但不要全拆成主站。",
    "4天避暑旅行，景点混合室内和室外，写自然的条件性安排。",
    "3天旅行，先给简短初稿，末尾明确调整第二天的一站并说明最终安排。",
    "5天自由行，含空白休息日、返程半天及尚未选定的住宿。",
    "3天散步路线，名字保持完整，遇到商场内部楼层或门店请明确限定。",
    "4天学生旅行，紧凑但合理，每日各给两个午餐候选，由旅行者选一个。",
    "3天不去最拥挤景点的替代路线，明确写出哪些地点排除。",
    "4天慢旅行，第二天和第三天调换时仍保持最终各日顺序。",
    "2天演出前后游览，不编造具体演出票或场次。",
    "3天，混合中文Day1与第二天标题，最后一天只有交通返程。",
    "5天历史街区旅行，每天路线摘要之后再给详细介绍，避免误认成重复到访。",
    "4天骑行与步行结合，但地点之间的交通只给方式，不编造精确分钟。",
    "3天城中与郊外组合，郊外景点作为可选整日分支。",
    "2天夜游为主，白天休息；用真实自然的聊天口吻回答。",
    "6天旅行，安排文化、商业街、公园和休息日，后半段也写完整。",
    "3天，多个独立地点用顿号和箭头表达，但菜名列表只作为餐饮介绍。",
    "4天旅行，保留一处地标简称，同时在说明中给全称。",
    "3天，主线之外列附近可选地点，不要把每个推荐混成必去。",
    "5天旅行，其中一天给两条路线，各路线尾站可能回到同一商圈。",
    "4天市内公共交通旅行，偏好全季、汉庭或其他连锁住宿，酒店仅作建议。",
    "3天轻松旅行，某一天上午一站下午一站，中间明确安排午餐区域。",
    "2天文化旅行，在说明里提及不去的周边地点并明确不是主站。",
    "7天旅行，逐日结构完整，可有空日；包含真实名称而不要追求地点过多。",
    "4天路线，第二天原定计划取消一站，末尾给出最终保留安排。",
    "3天家庭旅行，一处公园含内部游览路线，公园外再安排独立下一站。",
    "5天旅行，使用自然口吻和适量括号、列表、不同标点，不写机器JSON。",
)


def planned_cases(count: int) -> list[dict]:
    if not 1 <= count <= 230:
        raise ValueError("count must be between 1 and 230")
    cases = []
    for family, brief in enumerate(BRIEFS):
        split = "development" if family % 4 < 2 else "validation" if family % 4 == 2 else "holdout"
        for code, city in CITIES:
            length = "1800到2400" if family in {5, 24, 28, 36} else "600到1100"
            cases.append({"id": f"qwen-{code}-{family:02d}", "family_id": f"qwen-brief-{family:02d}",
                "city": city, "split": split,
                "long_text": family in {5, 24, 28, 36},
                "prompt": f"请为我写一份{city}旅游攻略。{brief}"
                    f"请用中文给普通旅行者看，约{length}字。地点尽量具体且完整，按天表达先后。"
                    "用餐和住宿可给建议，不冒充已预订。不确定营业、房态、门票或交通时长时不要编造。"
                    "直接提供自然的旅行计划正文，不用JSON，也不要解释你如何回答。"})
    for family, brief in enumerate(("3天初次旅游，含主线和可选地点。", "4天旅行，含内部景点说明、具体菜名和取消的备选。",
                                   "5天长攻略，每天先摘要再详述，约1800到2400字，含邻城一日游并写清城市。")):
        for code, city in LONG_TAIL_CITIES:
            cases.append({"id": f"qwen-tail-{code}-{family:02d}", "family_id": f"qwen-tail-brief-{family:02d}",
                "city": city, "split": "long_tail", "long_text": family == 2,
                "prompt": f"请为我写一份{city}旅游攻略。{brief}请使用自然中文、具体地点名称和逐日顺序。"
                    "未知价格、房态、开放时间不编造；推荐与已选定行程分清，直接给正文，不要JSON。"})
    return cases[:count]


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def summary(rows: list[dict], attempts: list[dict] | None = None) -> dict:
    attempts = attempts if attempts is not None else rows
    return {"cases": len(rows), "completed": sum(row["status"] == "COMPLETED" for row in rows),
        "failed": sum(row["status"] != "COMPLETED" for row in rows),
        "external_calls": len(attempts),
        "input_tokens": sum(row.get("input_tokens") or 0 for row in attempts),
        "output_tokens": sum(row.get("output_tokens") or 0 for row in attempts),
        "usage_complete": all(row.get("input_tokens") is not None and row.get("output_tokens") is not None for row in attempts),
        "estimated_cost_cny": (round(sum(row["estimated_cost_cny"] for row in attempts), 6)
            if attempts and all(row.get("estimated_cost_cny") is not None for row in attempts) else None),
        "gold_labels": 0, "provenance": "platform_generated"}


async def collect(args) -> int:
    values = dotenv_values(args.config_env, interpolate=False)
    required = ("QWEN_API_KEY", "QWEN_API_URL", "TRIP_UNDERSTANDING_QWEN_MODEL")
    if any(not values.get(key) for key in required):
        raise ValueError("Missing private model configuration")
    cases = planned_cases(args.count)
    folder = args.output_root.resolve() / args.batch_id
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {
        "schema_version": "platform-travel-corpus-v1", "batch_id": args.batch_id,
        "provenance": "platform_generated", "platform": "Qwen API", "gold_status": "NOT_ANNOTATED",
        "created_at": datetime.now(timezone.utc).isoformat(), "cases": [],
    }
    rows = {row["id"]: row for row in manifest["cases"]}
    attempts = list(manifest.get("attempts", manifest["cases"]))
    # Recover a response written just before a process interruption. Literal
    # model output is immutable even when the manifest write did not complete.
    known_artifacts = {row["artifact"] for row in attempts}
    for artifact in sorted((folder / "responses").glob("*.json")):
        relative = artifact.relative_to(folder).as_posix()
        if relative in known_artifacts:
            continue
        record = json.loads(artifact.read_text(encoding="utf-8"))
        if record.get("provenance") != "platform_generated" or not isinstance(record.get("attempt"), int):
            raise ValueError("Unexpected existing corpus artifact; refusing to overwrite")
        restored = {key: value for key, value in record.items() if key not in {"prompt", "output", "gold_status"}}
        restored["artifact"] = relative
        attempts.append(restored)
        current = rows.get(restored["id"])
        if current is None or restored["attempt"] > current["attempt"]:
            rows[restored["id"]] = restored
    model = values["TRIP_UNDERSTANDING_QWEN_MODEL"]
    client = AsyncOpenAI(api_key=values["QWEN_API_KEY"], base_url=values["QWEN_API_URL"], timeout=args.timeout, max_retries=0)
    rates = [float(values[key]) if values.get(key) else None for key in
        ("TRIP_UNDERSTANDING_QWEN_INPUT_CNY_PER_MILLION", "TRIP_UNDERSTANDING_QWEN_OUTPUT_CNY_PER_MILLION")]
    slots = asyncio.Semaphore(args.concurrency)

    async def one(case):
        existing = rows.get(case["id"])
        if existing and (existing["status"] == "COMPLETED" or not args.retry_failed):
            retained = json.loads((folder / existing["artifact"]).read_text(encoding="utf-8"))
            if retained.get("prompt") != case["prompt"]:
                raise ValueError("Prompt family changed; use a new batch instead of rewriting corpus provenance")
            return
        async with slots:
            started = asyncio.get_running_loop().time()
            attempt = int(existing.get("attempt", 0)) + 1 if existing else 1
            row = {**{key: case[key] for key in ("id", "family_id", "city", "split")},
                "attempt": attempt, "status": "FAILED", "provenance": "platform_generated", "model": model,
                "long_text": case["long_text"], "prompt_sha256": hashlib.sha256(case["prompt"].encode()).hexdigest(),
                "created_at": datetime.now(timezone.utc).isoformat()}
            record = {**row, "prompt": case["prompt"], "output": None, "gold_status": "NOT_ANNOTATED"}
            try:
                response = await client.chat.completions.create(model=model,
                    messages=[{"role": "user", "content": case["prompt"]}], temperature=0.8,
                    max_tokens=max(args.max_tokens, 4600) if case["long_text"] else args.max_tokens,
                    extra_body={"enable_thinking": False})
                output = response.choices[0].message.content or ""
                usage = response.usage
                row.update(input_tokens=getattr(usage, "prompt_tokens", None), output_tokens=getattr(usage, "completion_tokens", None),
                    reported_model=response.model, finish_reason=response.choices[0].finish_reason,
                    output_sha256=hashlib.sha256(output.encode()).hexdigest(), output_characters=len(output))
                if row["input_tokens"] is not None and row["output_tokens"] is not None and all(rate is not None for rate in rates):
                    row["estimated_cost_cny"] = (row["input_tokens"] * rates[0] + row["output_tokens"] * rates[1]) / 1_000_000
                row["status"] = "COMPLETED" if output.strip() and row["finish_reason"] == "stop" else "INCOMPLETE_RESPONSE"
                record["output"] = output
            except Exception as error:
                row["error_category"] = type(error).__name__
            row["elapsed_ms"] = round((asyncio.get_running_loop().time() - started) * 1000, 2)
            row["artifact"] = f"responses/{case['id']}-attempt-{attempt}.json"
            record.update(row)
            if (folder / row["artifact"]).exists():
                raise ValueError("An immutable platform response already exists for this attempt")
            save_json(folder / row["artifact"], record)
            rows[case["id"]] = row
            attempts.append(row)
            manifest["cases"] = list(rows.values())
            manifest["attempts"] = attempts
            manifest["summary"] = summary(manifest["cases"], attempts)
            save_json(manifest_path, manifest)
            print(json.dumps({"case_id": row["id"], "status": row["status"], "input_tokens": row.get("input_tokens"),
                "output_tokens": row.get("output_tokens"), "elapsed_ms": row["elapsed_ms"]}), flush=True)

    try:
        await asyncio.gather(*(one(case) for case in cases))
    finally:
        await client.close()
    manifest["summary"] = summary(list(rows.values()), attempts)
    manifest["attempts"] = attempts
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(manifest_path, manifest)
    print(json.dumps({"summary": manifest["summary"]}), flush=True)
    return int(any(rows[case["id"]]["status"] != "COMPLETED" for case in cases))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-env", type=Path, default=ROOT / ".local-artifacts/experience/experience.env")
    parser.add_argument("--output-root", type=Path, default=ROOT / ".local-artifacts/corpus")
    parser.add_argument("--batch-id", default="five-city-20260907")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=2)
    parser.add_argument("--max-tokens", type=int, default=2200)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.batch_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in args.batch_id):
        parser.error("batch-id must contain only letters, numbers, dash and underscore")
    if args.dry_run:
        print(json.dumps({"planned_cases": len(planned_cases(args.count)), "external_calls": 0,
                          "families": len({row['family_id'] for row in planned_cases(args.count)})}))
        return 0
    logging.disable(logging.CRITICAL)
    return asyncio.run(collect(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_category": type(error).__name__, "message": "Collection stopped; private error details omitted"}))
        raise SystemExit(2) from None
