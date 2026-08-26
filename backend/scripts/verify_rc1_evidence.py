"""Build and verify the three-city RC1 evidence index without network calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.audit_daily_query_goal import _normalized_replay_sha256


BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
EXPECTED_CITIES = ["北京", "上海", "杭州"]
ZERO_CALL_KEYS = ("provider_calls", "generation_llm_calls", "judge_api_calls")


def _sha256(path: Path) -> str:
    # Evidence JSON is generated on both Windows and Linux.  Bind the content,
    # not the checkout's platform-specific text line endings.
    raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(raw).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 必须是 JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _repo_relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def _reference(path: Path) -> dict[str, str]:
    return {"path": _repo_relative(path), "sha256": _sha256(path)}


def _resolve_reference(reference: dict[str, Any]) -> Path:
    relative = str(reference.get("path") or "")
    path = (ROOT / relative).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"证据路径越界: {relative}") from exc
    if not path.is_file() or _sha256(path) != reference.get("sha256"):
        raise ValueError(f"证据缺失或哈希漂移: {relative}")
    return path


def _assert_scope(report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    if summary.get("total") != 150:
        raise ValueError("评测报告必须恰好包含 150 条")
    by_city = summary.get("by_city") or {}
    if list(by_city) != EXPECTED_CITIES or any(
        by_city[city].get("total") != 50 for city in EXPECTED_CITIES
    ):
        raise ValueError("评测范围必须固定为北京、上海、杭州各 50 条")


def _assert_offline_replay(report: dict[str, Any]) -> None:
    _assert_scope(report)
    summary = report["summary"]
    usage = report.get("api_usage") or {}
    if any(int(usage.get(key) or 0) for key in ZERO_CALL_KEYS):
        raise ValueError("冻结重放发生了外部调用")
    if summary.get("passed") != 150 or summary.get("system_errors") != 0:
        raise ValueError("冻结重放不是 150/150 或存在系统错误")
    category_coverage = summary.get("category_coverage") or {}
    if not category_coverage.get("passed"):
        raise ValueError("冻结重放的品类缺失/错误比例超过 RC1 上限")
    honesty = summary.get("high_risk_honesty") or {}
    if honesty.get("unsupported_affirmative_claim_count") != 0:
        raise ValueError("冻结重放存在无依据高风险肯定断言")
    if honesty.get("confirmation_action_coverage") != 1.0:
        raise ValueError("冻结重放确认动作覆盖率不是 100%")


def _assert_panel(
    panel: dict[str, Any],
    bindings: dict[str, Any],
    *,
    require_quality_pass: bool,
) -> None:
    if panel.get("kind") != "codex_subagent_judge_panel":
        raise ValueError("RC1 Judge 不是 Codex 子 Agent 评审组")
    if panel.get("bindings") != bindings:
        raise ValueError("Judge panel 与冻结重放绑定不一致")
    evaluators = panel.get("evaluators") or []
    if len(evaluators) != 3 or len({row.get("evaluator_id") for row in evaluators}) != 3:
        raise ValueError("Judge panel 不是三个独立 evaluator")
    if any(row.get("model") != "gpt-5.6-sol" for row in evaluators):
        raise ValueError("Judge panel 模型不是 gpt-5.6-sol")
    panel_summary = panel.get("panel_summary") or {}
    if panel_summary.get("agreement_threshold_passed") is not True:
        raise ValueError("Judge panel 未达到三方一致性阈值")
    if require_quality_pass and panel_summary.get("passed") is not True:
        raise ValueError("Judge panel 未通过 RC1 阈值")
    round_reports = panel.get("round_reports") or []
    if len(round_reports) != 3 or any(
        (row.get("summary") or {}).get("total") != 150
        or (row.get("summary") or {}).get("judged") != 150
        or (row.get("summary") or {}).get("judge_errors") != 0
        or (row.get("summary") or {}).get("system_errors") != 0
        for row in round_reports
    ):
        raise ValueError("Judge panel 三轮覆盖不完整或存在评审错误")
    provenance = panel.get("provenance") or {}
    if provenance.get("judge_api_calls") != 0 or provenance.get("human_calibration_performed") is not False:
        raise ValueError("Judge provenance 违反零 API 或真人校准边界")


def _assert_live_report(report: dict[str, Any]) -> float:
    _assert_scope(report)
    summary = report["summary"]
    usage = report.get("api_usage") or {}
    if usage.get("paid_generation_authorized") is not True:
        raise ValueError("真实链路未记录显式付费授权")
    if int(usage.get("judge_api_calls") or 0) != 0:
        raise ValueError("真实链路调用了 API Judge")
    if int(usage.get("generation_llm_calls") or 0) <= 0 or int(usage.get("provider_calls") or 0) <= 0:
        raise ValueError("真实链路未记录 DeepSeek 或高德调用")
    if summary.get("system_errors") != 0 or summary.get("infrastructure_errors") != 0:
        raise ValueError("真实链路存在系统或基础设施错误")
    integrity = summary.get("retrieval_integrity") or {}
    if any(int(integrity.get(key) or 0) for key in (
        "fixture_places", "fallback_places", "canonical_duplicate_count",
    )):
        raise ValueError("真实链路存在 fixture、fallback 或规范化重复实体")
    if float(integrity.get("amap_tool_failure_rate") or 0.0) >= 0.01:
        raise ValueError("真实链路高德失败率未低于 1%")
    quality = summary.get("recommendation_quality_under_valid_retrieval") or {}
    if quality.get("eligible") != 150 or quality.get("pass_rate") != 1.0:
        raise ValueError("真实链路有效检索样本质量不是 150/150")
    if not (summary.get("category_coverage") or {}).get("passed"):
        raise ValueError("真实链路的品类缺失/错误比例超过 RC1 上限")
    return float(summary.get("pass_rate") or 0.0)


def build_summary(
    replay_paths: list[Path],
    panel_path: Path,
    live_paths: list[Path],
    output_path: Path,
    *,
    candidate: bool = False,
) -> dict[str, Any]:
    if len(replay_paths) != 3 or len(live_paths) not in {0, 2}:
        raise ValueError("RC1 需要三次冻结重放；完整证据需要两次真实链路")
    if candidate and live_paths:
        raise ValueError("候选版证据不应混入未计划执行的付费真实链路")
    replays = [_load(path) for path in replay_paths]
    for replay in replays:
        _assert_offline_replay(replay)
    normalized_hashes = [_normalized_replay_sha256(replay) for replay in replays]
    if len(set(normalized_hashes)) != 1:
        raise ValueError("三次冻结重放规范化输出哈希不一致")
    tree_hashes = [
        ((replay.get("reproducibility") or {}).get("backend_execution_tree") or {}).get("sha256")
        for replay in replays
    ]
    if len(set(tree_hashes)) != 1 or not tree_hashes[0]:
        raise ValueError("三次冻结重放执行树哈希不一致")
    dataset_hashes = [
        (replay.get("reproducibility") or {}).get("dataset_sha256") for replay in replays
    ]
    if len(set(dataset_hashes)) != 1 or not dataset_hashes[0]:
        raise ValueError("三次冻结重放数据集哈希不一致")

    panel = _load(panel_path)
    _assert_panel(
        panel,
        {
            "source_report_sha256": _sha256(replay_paths[0]),
            "dataset_sha256": dataset_hashes[0],
            "rubric_sha256": panel.get("bindings", {}).get("rubric_sha256"),
            "execution_tree_sha256": tree_hashes[0],
        },
        require_quality_pass=not candidate,
    )
    live_reports = [_load(path) for path in live_paths]
    live_rates = [_assert_live_report(report) for report in live_reports]
    if len(live_rates) == 2 and abs(live_rates[0] - live_rates[1]) > 0.05:
        raise ValueError("两轮真实链路通过率差异超过 5 个百分点")

    panel_summary = panel.get("panel_summary") or {}
    panel_quality_passed = panel_summary.get("quality_thresholds_passed") is True
    panel_agreement_passed = panel_summary.get("agreement_threshold_passed") is True
    payload = {
        "schema_version": "1.0",
        "kind": (
            "three_city_local_rc1_candidate_evidence"
            if candidate else "three_city_local_rc1_evidence"
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {"cities": EXPECTED_CITIES, "cases_per_city": 50, "total": 150},
        "bindings": {
            "dataset_sha256": dataset_hashes[0],
            "execution_tree_sha256": tree_hashes[0],
            "normalized_replay_sha256": normalized_hashes[0],
        },
        "frozen_replays": [_reference(path) for path in replay_paths],
        "model_judge_panel": _reference(panel_path),
        "live_product_runs": [_reference(path) for path in live_paths],
        "acceptance": {
            "offline_replay_passed": True,
            "model_panel_agreement_passed": panel_agreement_passed,
            "model_panel_quality_passed": panel_quality_passed,
            "live_runs_complete": len(live_paths) == 2,
            "overall_rc1_passed": panel_quality_passed and len(live_paths) == 2,
            "judge_api_calls": 0,
            "human_calibration_performed": False,
            "post_v22_hardening_revalidated": not candidate,
        },
        "claim": (
            "三城本地可靠性强化候选版：冻结重放通过，模型评审一致性阈值通过，"
            "质量总门禁未通过；真人校准未执行"
            if candidate else
            "GPT-5.6-sol 三模型独立盲评及完整 RC1 门禁通过；真人校准未执行"
        ),
        "excluded_claims": [
            "full RC1 release", "public deployment", "human calibration",
            "Judge-human agreement", "real-user validation", "production SLO",
        ],
    }
    _write_json(output_path, payload)
    return payload


def verify_summary(summary_path: Path, *, require_complete: bool) -> dict[str, Any]:
    summary = _load(summary_path)
    supported_kinds = {
        "three_city_local_rc1_evidence",
        "three_city_local_rc1_candidate_evidence",
    }
    if summary.get("schema_version") != "1.0" or summary.get("kind") not in supported_kinds:
        raise ValueError("RC1 evidence schema/kind 不受支持")
    candidate = summary.get("kind") == "three_city_local_rc1_candidate_evidence"
    if require_complete and candidate:
        raise ValueError("候选版证据不能作为完整 RC1 evidence 验收")
    if summary.get("scope") != {"cities": EXPECTED_CITIES, "cases_per_city": 50, "total": 150}:
        raise ValueError("RC1 evidence scope 已漂移")
    replay_refs = summary.get("frozen_replays") or []
    if len(replay_refs) != 3:
        raise ValueError("RC1 evidence 必须引用三次冻结重放")
    replay_paths = [_resolve_reference(reference) for reference in replay_refs]
    panel_path = _resolve_reference(summary.get("model_judge_panel") or {})
    live_refs = summary.get("live_product_runs") or []
    if require_complete and len(live_refs) != 2:
        raise ValueError("完整 RC1 evidence 必须引用两次真实链路")
    live_paths = [_resolve_reference(reference) for reference in live_refs]

    rebuilt = build_summary(
        replay_paths,
        panel_path,
        live_paths,
        summary_path.with_suffix(".verified.tmp"),
        candidate=candidate,
    )
    summary_path.with_suffix(".verified.tmp").unlink()
    if rebuilt["bindings"] != summary.get("bindings") or rebuilt["acceptance"] != summary.get("acceptance"):
        raise ValueError("RC1 evidence 绑定或验收状态与原始证据不一致")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--replay", type=Path, action="append", required=True)
    build.add_argument("--panel", type=Path, required=True)
    build.add_argument("--live-run", type=Path, action="append", default=[])
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--candidate", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("--summary", type=Path, required=True)
    verify.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    if args.command == "build":
        build_summary(
            args.replay,
            args.panel,
            args.live_run,
            args.output,
            candidate=args.candidate,
        )
        print(args.output)
    else:
        payload = verify_summary(args.summary, require_complete=args.require_complete)
        print(json.dumps({"valid": True, "kind": payload["kind"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
