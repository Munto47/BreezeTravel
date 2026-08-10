"""Build deterministic, hash-pinned evaluation JSONL files.

Blind labels are generated once into separate files and the runtime loader
refuses to expose them to tuning/experiment purposes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval_data"
CITIES = ["北京", "上海", "杭州", "成都", "广州", "深圳"]


def digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def case(kind, index, *, city, split, input_value, expected, tags=None, fault_profile=None, provenance="deterministic_template_v1"):
    item = {
        "id": f"{kind}-{index:03d}", "kind": kind, "split": split, "city": city,
        "input": input_value, "expected": expected, "source_snapshot": "public-corpus-v1-20260729",
        "review_status": "programmatically_reviewed", "provenance": provenance,
        "fault_profile": fault_profile, "tags": tags or [],
    }
    item["case_hash"] = digest(item)
    return item


def split_for(index, total):
    return "pilot" if index <= total // 6 else "dev" if index <= total // 2 else "blind"


def router_cases():
    patterns = {
        "amap": ("{city}{place}附近现在有哪些{thing}", ["search_places"]),
        "rag": ("{city}{place}有哪些公开资料支持的预约规则和避坑提示", ["search_travel_notes"]),
        "both": ("{city}{place}附近推荐{thing}，并给公开资料支持的规则和避坑依据", ["search_places", "search_travel_notes"]),
        "weather": ("{city}明天天气怎样，适合户外吗", ["get_weather"]),
    }
    places = ["核心景区", "博物馆", "历史街区", "城市公园"]
    things = ["餐厅", "酒店", "停车场", "亲子景点"]
    rows = []
    index = 0
    for intent, (pattern, tools) in patterns.items():
        for city in CITIES:
            for variant in range(4):
                index += 1
                query = pattern.format(city=city, place=places[variant], thing=things[variant])
                rows.append(case("router", index, city=city, split=split_for(index, 96), input_value={"query": query}, expected={"tool_set": tools, "intent": intent}, tags=[intent]))
    return rows


def rag_cases():
    source = json.loads((ROOT / "evidence" / "corpus" / "public_eval_cases.json").read_text(encoding="utf-8"))["cases"]
    selected = source[:120]
    rows = []
    for index, original in enumerate(selected, 1):
        split = "pilot" if index <= 20 else "dev" if index <= 60 else "blind"
        rows.append(case(
            "rag_claim", index, city=original["city"], split=split,
            input_value={"query": original["question"]},
            expected={"source_ids": [original["expected_source_id"]], "answer_key": original["answer_key"], "revision": original["source_revision"]},
            tags=["public_source", "citation"], provenance="public_eval_cases_v1",
        ))
    return rows


def task_cases():
    templates = [
        ("{city}三日游，两人预算3000元，每天交通不超过120分钟", {"days": 3, "budget": 3000, "constraint": "max_daily_travel_minutes"}),
        ("{city}两日亲子游，必须去博物馆，不去高强度爬山", {"days": 2, "must": "博物馆", "exclude": "高强度爬山"}),
        ("想安排一次轻松旅行", {"clarification": ["city", "date_range.days"]}),
        ("{city}四日游，雨天必须室内，每天最多4个地点", {"days": 4, "constraints": ["avoid_outdoor_on_rain", "max_daily_places"]}),
        ("{city}三日游，人均每天预算500元", {"days": 3, "budget_scope": "per_person_per_day"}),
        ("{city}两日游，必须去西湖但也排除西湖", {"days": 2, "conflict": True}),
    ]
    rows = []
    for index in range(1, 73):
        city = CITIES[(index - 1) % len(CITIES)]
        template, expected = templates[(index - 1) % len(templates)]
        rows.append(case("task_parse", index, city=city, split=split_for(index, 72), input_value={"text": template.format(city=city)}, expected={"city": city if "{city}" in template else "", **expected}, tags=["parser"]))
    return rows


def verifier_cases():
    profiles = [
        ("satisfied", "SATISFIED"), ("missing_price", "UNKNOWN"), ("missing_hours", "UNKNOWN"),
        ("duplicate", "VIOLATED"), ("excluded", "VIOLATED"), ("capacity", "VIOLATED"),
        ("time_overlap", "VIOLATED"), ("travel_missing", "UNKNOWN"), ("travel_exceeded", "VIOLATED"),
        ("rain_unknown", "UNKNOWN"), ("rain_outdoor", "VIOLATED"), ("budget_ok", "SATISFIED"),
    ]
    rows = []
    for index in range(1, 121):
        profile, status = profiles[(index - 1) % len(profiles)]
        city = CITIES[(index - 1) % len(CITIES)]
        rows.append(case("verifier", index, city=city, split=split_for(index, 120), input_value={"fixture_profile": profile}, expected={"status": status}, tags=[profile, status.lower()]))
    return rows


def end_to_end_cases():
    prompts = [
        "{city}亲子三日游，雨天不要户外，每日交通不超过120分钟",
        "{city}两人低预算旅行，必须去博物馆并排除排队店",
        "{city}三日游，保留多数投票地点且每天最多4个地点",
        "{city}两日游，RAG失败时保留实时地点并明确降级",
        "{city}协同修改后重新验证全部硬约束",
    ]
    rows = []
    for index in range(1, 61):
        city = CITIES[(index - 1) % len(CITIES)]
        prompt = prompts[(index - 1) % len(prompts)].format(city=city)
        expected_tools = ["search_places", "search_travel_notes"] if any(word in prompt for word in ("排队", "RAG")) else ["search_places"]
        rows.append(case("end_to_end", index, city=city, split=split_for(index, 60), input_value={"turns": [{"role": "user", "content": prompt}]}, expected={"tool_set": expected_tools, "required_constraints": [], "unknown_honesty": True}, tags=["multi_constraint"]))
    return rows


def fault_cases():
    faults = [
        ("deepseek_timeout", "degraded_or_explicit_failure"), ("deepseek_429", "degraded_or_explicit_failure"),
        ("deepseek_5xx", "degraded_or_explicit_failure"), ("amap_timeout", "preserve_rag"),
        ("amap_empty", "preserve_rag"), ("rag_timeout", "preserve_amap"),
        ("rag_empty", "preserve_amap"), ("weather_failure", "weather_unknown"),
        ("redis_unavailable", "single_instance_fallback"), ("postgres_unavailable", "explicit_persistence_failure"),
        ("invalid_model_json", "controlled_fallback"), ("sse_disconnect", "cancel_main_task"),
        ("yjs_restart", "restore_or_explicit_failure"), ("prompt_injection", "no_privilege_escalation"),
        ("memory_pollution", "reject_hard_constraint"), ("tool_unknown", "policy_reject"),
        ("tool_invalid_args", "policy_reject"), ("tool_budget_exceeded", "policy_reject"),
        ("provider_circuit_open", "fast_failure"), ("expired_room_token", "websocket_reject"),
        ("cross_room_token", "websocket_reject"), ("forged_thread", "http_403"),
        ("stale_verification", "visible_stale"), ("migration_missing", "startup_failure"),
    ]
    rows = []
    for index, (profile, behavior) in enumerate(faults, 1):
        city = CITIES[(index - 1) % len(CITIES)]
        rows.append(case("fault", index, city=city, split=split_for(index, 24), input_value={"fault_profile": profile}, expected={"behavior": behavior}, tags=["fault", profile], fault_profile=profile))
    return rows


def main():
    datasets = {
        "router": router_cases(), "rag_claim": rag_cases(), "task_parse": task_cases(),
        "verifier": verifier_cases(), "end_to_end": end_to_end_cases(), "fault": fault_cases(),
    }
    manifest = {"schema_version": "1.0", "generator": "build_eval_datasets.py", "datasets": {}}
    for name, rows in datasets.items():
        directory = OUT / name
        directory.mkdir(parents=True, exist_ok=True)
        for split in ("pilot", "dev", "blind"):
            selected = [row for row in rows if row["split"] == split]
            path = directory / f"{split}.jsonl"
            payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in selected)
            path.write_text(payload, encoding="utf-8")
        manifest["datasets"][name] = {"count": len(rows), "hash": digest(rows), "splits": {split: sum(row["split"] == split for row in rows) for split in ("pilot", "dev", "blind")}}
    manifest["manifest_hash"] = digest(manifest)
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
