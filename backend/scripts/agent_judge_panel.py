"""Export and aggregate a zero-API, independent Codex subagent Judge panel."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.run_daily_query_eval import (
    JUDGE_PROMPT,
    _expected_for,
    _judge_payload_places,
    _summarize,
    validate_dataset,
)


SCORE_NAMES = (
    "intent_relevance",
    "geographic_fit",
    "entity_coverage",
    "constraint_adherence",
    "persona_fit",
    "practical_usefulness",
    "groundedness",
)
SEMANTIC_THRESHOLDS = {
    "constraint_adherence": 4.2,
    "geographic_fit": 4.2,
    "persona_fit": 4.2,
    "practical_usefulness": 4.2,
    "groundedness": 4.7,
}
EXPECTED_MODEL = "gpt-5.6-sol"


def _sha256(path: Path) -> str:
    # Judge artifacts may be produced on Windows and verified on Linux.
    # Normalize text line endings so the binding survives a clean checkout.
    raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(raw).hexdigest()


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def export_blind_bundle(report_path: Path, dataset_path: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = validate_dataset(dataset)
    rows = list(report.get("cases") or [])
    if len(rows) != 150 or {str(row.get("id")) for row in rows} != {case["id"] for case in cases}:
        raise ValueError("盲评源报告必须完整覆盖三城 150 条且 case ID 与数据集一致")
    usage = report.get("api_usage") or {}
    if any(int(usage.get(key) or 0) for key in ("provider_calls", "generation_llm_calls", "judge_api_calls")):
        raise ValueError("盲评包只能从零外部调用的冻结重放报告导出")
    rows_by_id = {str(row["id"]): row for row in rows}
    rubric = {
        "version": "daily-query-agent-panel-v1",
        "prompt": JUDGE_PROMPT,
        "score_names": list(SCORE_NAMES),
        "case_pass_rule": "all seven integer scores >= 4 and critical_violations is empty",
        "evidence_boundary": "judge only the request, supplied cards and response; never add outside facts",
    }
    blind_cases = []
    for case in cases:
        row = rows_by_id[case["id"]]
        output = row.get("output") or {}
        blind_cases.append({
            "id": case["id"],
            "city": case["city"],
            "intent": case["intent"],
            "persona": case["persona"],
            "dimensions": case.get("dimensions") or [],
            "query": case["query"],
            "semantic_requirement": _expected_for(case)["semantic_requirement"],
            "places": _judge_payload_places(output),
            "response_text": output.get("text") or "",
        })
    return {
        "schema_version": "1.0",
        "kind": "codex_subagent_blind_judge_bundle",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {"cities": ["北京", "上海", "杭州"], "cases_per_city": 50, "total": 150},
        "bindings": {
            "source_report_sha256": _sha256(report_path),
            "dataset_sha256": _sha256(dataset_path),
            "rubric_sha256": _canonical_sha256(rubric),
            "execution_tree_sha256": (report.get("reproducibility") or {}).get("backend_execution_tree", {}).get("sha256"),
        },
        "rubric": rubric,
        "output_contract": {
            "schema_version": "1.0",
            "kind": "codex_subagent_judge_round",
            "evaluator": {
                "kind": "codex_subagent",
                "model": EXPECTED_MODEL,
                "evaluator_id": "unique non-empty id",
                "round": "1, 2 or 3",
                "blind": True,
            },
            "bindings": "copy exactly from this bundle",
            "cases": [{
                "id": "case id",
                "scores": {name: "integer 0..5" for name in SCORE_NAMES},
                "critical_violations": ["strings only"],
                "passed": "boolean derived from the case_pass_rule",
                "root_cause_hint": "one concise subsystem hint or none",
                "summary": "one concise Chinese conclusion",
            }],
        },
        "provenance_policy": {
            "judge_api_calls": 0,
            "human_labels": False,
            "claim": "GPT-5.6-sol 三模型独立盲评；真人校准未执行",
        },
        "cases": blind_cases,
    }


def validate_round(round_payload: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    if round_payload.get("schema_version") != "1.0" or round_payload.get("kind") != "codex_subagent_judge_round":
        raise ValueError("Judge round schema/kind 不受支持")
    evaluator = round_payload.get("evaluator") or {}
    if evaluator.get("kind") != "codex_subagent" or evaluator.get("model") != EXPECTED_MODEL:
        raise ValueError(f"Judge 必须来自 codex_subagent/{EXPECTED_MODEL}")
    if not evaluator.get("evaluator_id") or evaluator.get("blind") is not True:
        raise ValueError("Judge round 必须有独立 evaluator_id 且 blind=true")
    if round_payload.get("bindings") != bundle.get("bindings"):
        raise ValueError("Judge round bindings 与盲评包不一致")
    if "human_label" in json.dumps(round_payload, ensure_ascii=False):
        raise ValueError("子 Agent Judge 不得写入 human_label")

    expected_ids = [str(case["id"]) for case in bundle.get("cases") or []]
    rows = list(round_payload.get("cases") or [])
    row_ids = [str(row.get("id") or "") for row in rows]
    if len(rows) != 150 or len(row_ids) != len(set(row_ids)) or set(row_ids) != set(expected_ids):
        raise ValueError("Judge round 必须恰好覆盖 150 个唯一 case")
    for row in rows:
        scores = row.get("scores") or {}
        if set(scores) != set(SCORE_NAMES):
            raise ValueError(f"{row.get('id')} scores 字段不完整")
        if any(type(scores[name]) is not int or not 0 <= scores[name] <= 5 for name in SCORE_NAMES):
            raise ValueError(f"{row.get('id')} scores 必须是 0..5 整数")
        violations = row.get("critical_violations")
        if not isinstance(violations, list) or any(not isinstance(item, str) for item in violations):
            raise ValueError(f"{row.get('id')} critical_violations 非法")
        derived_pass = all(scores[name] >= 4 for name in SCORE_NAMES) and not violations
        if type(row.get("passed")) is not bool or row["passed"] != derived_pass:
            raise ValueError(f"{row.get('id')} passed 与固定规则不一致")
        if not isinstance(row.get("root_cause_hint"), str) or not isinstance(row.get("summary"), str):
            raise ValueError(f"{row.get('id')} 缺少文本结论")
    return round_payload


def _bucket_summary(votes: dict[str, bool], cases: list[dict[str, Any]], field: str) -> dict[str, Any]:
    result: dict[str, dict[str, int | float]] = {}
    for case in cases:
        name = str(case[field])
        row = result.setdefault(name, {"total": 0, "passed": 0, "pass_rate": 0.0})
        row["total"] += 1
        row["passed"] += int(votes[case["id"]])
    for row in result.values():
        row["pass_rate"] = row["passed"] / row["total"] if row["total"] else 0.0
    return dict(sorted(result.items()))


def _round_report(source: dict[str, Any], dataset: dict[str, Any], judgment: dict[str, Any]) -> dict[str, Any]:
    cases = validate_dataset(dataset)
    cases_by_id = {case["id"]: case for case in cases}
    judged_by_id = {str(row["id"]): row for row in judgment["cases"]}
    report = copy.deepcopy(source)
    rows = []
    for source_row in source.get("cases") or []:
        row = copy.deepcopy(source_row)
        raw = judged_by_id[str(row["id"])]
        row["judge"] = {
            **copy.deepcopy(raw),
            "judge_provider": "codex_subagent",
            "judge_model": EXPECTED_MODEL,
            "evaluator_id": judgment["evaluator"]["evaluator_id"],
        }
        row["passed"] = bool((row.get("deterministic") or {}).get("passed") and raw["passed"])
        row["evaluation_status"] = "completed"
        rows.append(row)
    report["schema_version"] = "5.0"
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["cases"] = rows
    report["mode"]["llm_judge"] = False
    report["judge_chain"] = {
        "kind": "codex_subagent",
        "model": EXPECTED_MODEL,
        "evaluator_id": judgment["evaluator"]["evaluator_id"],
        "network_calls": 0,
        "human_labels": False,
    }
    report["summary"] = _summarize(rows, cases_by_id)
    return report


def aggregate_panel(
    bundle_path: Path,
    source_report_path: Path,
    dataset_path: Path,
    judgment_paths: list[Path],
    round_report_dir: Path,
) -> dict[str, Any]:
    if len(judgment_paths) != 3:
        raise ValueError("必须提供三个独立 Judge round")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    source = json.loads(source_report_path.read_text(encoding="utf-8"))
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    if bundle.get("bindings", {}).get("source_report_sha256") != _sha256(source_report_path):
        raise ValueError("source report 已变化，拒绝聚合旧评审")
    judgments = [validate_round(json.loads(path.read_text(encoding="utf-8")), bundle) for path in judgment_paths]
    evaluator_ids = [str(item["evaluator"]["evaluator_id"]) for item in judgments]
    rounds = [item["evaluator"].get("round") for item in judgments]
    if len(set(evaluator_ids)) != 3 or len(set(rounds)) != 3:
        raise ValueError("三个 Judge round 必须来自不同 evaluator_id 和 round")

    round_reports = []
    for index, judgment in enumerate(judgments, start=1):
        report = _round_report(source, dataset, judgment)
        target = round_report_dir / f"agent_judge_round{index}.json"
        _write_json(target, report)
        round_reports.append({"path": target.as_posix(), "sha256": _sha256(target), "summary": report["summary"]})

    cases = validate_dataset(dataset)
    judgments_by_round = [{str(row["id"]): row for row in item["cases"]} for item in judgments]
    majority: dict[str, bool] = {}
    disagreements = []
    unanimous = 0
    for case in cases:
        case_id = case["id"]
        votes = [bool(rows[case_id]["passed"]) for rows in judgments_by_round]
        majority[case_id] = sum(votes) >= 2
        if len(set(votes)) == 1:
            unanimous += 1
        else:
            disagreements.append({
                "id": case_id,
                "city": case["city"],
                "intent": case["intent"],
                "votes": dict(zip(evaluator_ids, votes, strict=True)),
                "root_causes": {
                    evaluator_ids[index]: judgments_by_round[index][case_id]["root_cause_hint"]
                    for index in range(3)
                },
            })
    majority_total = sum(majority.values())
    round_summaries = [item["summary"] for item in round_reports]
    quality_passed = all(
        summary["pass_rate"] >= 0.85
        and all(row["pass_rate"] >= 0.80 for row in summary["by_city"].values())
        and all(row["pass_rate"] >= (0.75 if name == "all" else 0.80) for name, row in summary["by_intent"].items())
        and summary["by_intent_group"]["compound"]["pass_rate"] >= 0.80
        and all(summary["average_judge_scores"].get(name, 0.0) >= threshold for name, threshold in SEMANTIC_THRESHOLDS.items())
        and summary["judge_errors"] == 0
        and summary["judged"] == 150
        for summary in round_summaries
    )
    agreement_rate = unanimous / len(cases)
    return {
        "schema_version": "1.0",
        "kind": "codex_subagent_judge_panel",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bindings": bundle["bindings"],
        "evaluators": [item["evaluator"] for item in judgments],
        "round_reports": round_reports,
        "panel_summary": {
            "total": len(cases),
            "majority_passed": majority_total,
            "majority_pass_rate": majority_total / len(cases),
            "unanimous_cases": unanimous,
            "unanimous_agreement_rate": agreement_rate,
            "by_city": _bucket_summary(majority, cases, "city"),
            "by_intent": _bucket_summary(majority, cases, "intent"),
            "disagreement_count": len(disagreements),
            "quality_thresholds_passed": quality_passed,
            "agreement_threshold_passed": agreement_rate >= 0.85,
            "passed": quality_passed and agreement_rate >= 0.85,
        },
        "disagreements": disagreements,
        "provenance": {
            "judge_api_calls": 0,
            "human_calibration_performed": False,
            "claim": "GPT-5.6-sol 三模型独立盲评达到既定校准阈值；真人校准未执行",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--report", type=Path, required=True)
    export.add_argument("--dataset", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate-round")
    validate.add_argument("--bundle", type=Path, required=True)
    validate.add_argument("--judgment", type=Path, required=True)
    aggregate = commands.add_parser("aggregate")
    aggregate.add_argument("--bundle", type=Path, required=True)
    aggregate.add_argument("--source-report", type=Path, required=True)
    aggregate.add_argument("--dataset", type=Path, required=True)
    aggregate.add_argument("--judgment", type=Path, action="append", required=True)
    aggregate.add_argument("--round-report-dir", type=Path, required=True)
    aggregate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "export":
        payload = export_blind_bundle(args.report, args.dataset)
        _write_json(args.output, payload)
    elif args.command == "validate-round":
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        payload = validate_round(json.loads(args.judgment.read_text(encoding="utf-8")), bundle)
        print(json.dumps({"valid": True, "evaluator": payload["evaluator"]}, ensure_ascii=False))
        return
    else:
        payload = aggregate_panel(
            args.bundle,
            args.source_report,
            args.dataset,
            args.judgment,
            args.round_report_dir,
        )
        _write_json(args.output, payload)
    print(args.output)


if __name__ == "__main__":
    main()
