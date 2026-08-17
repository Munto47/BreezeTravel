"""Fail-closed audit for the four acceptance groups in goal-objective.md."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SEMANTIC_SCORE_THRESHOLDS = {
    "constraint_adherence": 4.2,
    "geographic_fit": 4.2,
    "persona_fit": 4.2,
    "practical_usefulness": 4.2,
    "groundedness": 4.7,
}


def _gate(passed: bool | None, evidence: Any, requirement: str) -> dict[str, Any]:
    return {
        "status": "blocked" if passed is None else ("passed" if passed else "failed"),
        "requirement": requirement,
        "evidence": evidence,
    }


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    return report.get("summary") or {}


def _normalized_replay_sha256(report: dict[str, Any]) -> str:
    payload = [
        {
            "id": row.get("id"),
            "deterministic": row.get("deterministic"),
            "output": row.get("output"),
        }
        for row in report.get("cases") or []
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _amap_calls(report: dict[str, Any]) -> tuple[int, int, float, str]:
    summary = _summary(report)
    integrity = summary.get("retrieval_integrity") or {}
    calls = int(integrity.get("amap_tool_call_count") or 0)
    failures = int(integrity.get("amap_failure_count") or 0)
    if not calls:
        receipts = [
            receipt
            for row in report.get("cases") or []
            for receipt in (((row.get("output") or {}).get("done") or {}).get("tool_receipts") or [])
            if receipt and receipt.get("tool") == "search_places"
        ]
        calls = len(receipts)
        receipt_failures = sum(receipt.get("status") == "error" for receipt in receipts)
        if failures and receipt_failures != failures:
            raise ValueError("summary and raw Amap failure counts disagree")
        failures = receipt_failures
        source = "raw_search_places_receipts"
    else:
        source = "summary.retrieval_integrity"
    if not calls:
        raise ValueError("report contains no Amap tool calls")
    return calls, failures, failures / calls, source


def _hard_failure_counts(report: dict[str, Any]) -> dict[str, int]:
    counts = {"entity_or_order_or_district": 0, "missing_category": 0, "wrong_category": 0}
    for row in report.get("cases") or []:
        for reason in (row.get("deterministic") or {}).get("failures") or []:
            if reason.startswith(("缺少指定地点", "指定地点顺序错误", "不在")):
                counts["entity_or_order_or_district"] += 1
            if reason.startswith("缺少必需品类"):
                counts["missing_category"] += 1
            if reason.startswith("存在意图外品类"):
                counts["wrong_category"] += 1
    return counts


def audit_goal(
    live_reports: list[dict[str, Any]],
    snapshot_reports: list[dict[str, Any]],
    judge_reports: list[dict[str, Any]],
    model_panel: dict[str, Any] | None = None,
    human_labels: dict[str, Any] | None = None,
    agreement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if len(live_reports) != 2:
        raise ValueError("exactly two live reports are required")
    if len(snapshot_reports) != 3:
        raise ValueError("exactly three snapshot reports are required")

    live_summaries = [_summary(report) for report in live_reports]
    snapshot_summaries = [_summary(report) for report in snapshot_reports]
    live_calls = [_amap_calls(report) for report in live_reports]
    live_rates = [float(summary.get("pass_rate") or 0.0) for summary in live_summaries]
    purity_rows = [
        {
            key: (summary.get("retrieval_integrity") or {}).get(key)
            for key in ("fixture_places", "fallback_places", "canonical_duplicate_count")
        }
        for summary in live_summaries
    ]
    audits_complete = all(
        all((row.get("deterministic") or {}).get("retrieval_integrity", {}).get("retrieval_audit_count", 0) > 0
            for row in report.get("cases") or [])
        for report in live_reports
    )
    circuit_open = sum(
        int((row.get("deterministic") or {}).get("retrieval_integrity", {}).get("failure_categories", {}).get("circuit_open", 0))
        for report in live_reports for row in report.get("cases") or []
    )
    data_reliability = {
        "purity": _gate(
            all(all(value == 0 for value in row.values()) for row in purity_rows),
            purity_rows,
            "fixture, fallback and canonical duplicate are zero in both live runs",
        ),
        "audit_coverage": _gate(audits_complete, {"case_count": 300}, "every live case has retrieval audit evidence"),
        "cascade_breaker": _gate(circuit_open == 0, {"circuit_open": circuit_open}, "no cross-request circuit_open cascade"),
        "tool_failure_rate": _gate(
            all(rate < 0.01 for _, _, rate, _ in live_calls),
            [
                {"calls": calls, "failures": failures, "rate": rate, "source": source}
                for calls, failures, rate, source in live_calls
            ],
            "Amap tool failure rate is below 1 percent in both live runs",
        ),
        "live_stability": _gate(
            abs(live_rates[0] - live_rates[1]) <= 0.05,
            {"pass_rates": live_rates, "absolute_difference": abs(live_rates[0] - live_rates[1])},
            "two live pass rates differ by no more than 5 percentage points",
        ),
    }

    hard_counts = [_hard_failure_counts(report) for report in snapshot_reports]
    totals = [int(summary.get("total") or 0) for summary in snapshot_summaries]
    replay_hashes = [_normalized_replay_sha256(report) for report in snapshot_reports]
    zero_external_calls = all(
        all(int((report.get("api_usage") or {}).get(key) or 0) == 0 for key in (
            "provider_calls", "generation_llm_calls", "judge_api_calls",
        ))
        for report in snapshot_reports
    )
    deterministic = {
        "three_replays_identical": _gate(
            len(set(replay_hashes)) == 1 and all(total == 150 for total in totals),
            {"normalized_sha256": replay_hashes, "totals": totals},
            "three complete 150-case frozen replays have an identical normalized hash",
        ),
        "replay_external_calls": _gate(
            zero_external_calls,
            [report.get("api_usage") or {} for report in snapshot_reports],
            "frozen replay provider, generation LLM and Judge API calls are all zero",
        ),
        "entity_order_district": _gate(
            all(item["entity_or_order_or_district"] == 0 for item in hard_counts),
            hard_counts,
            "supported entity, order and administrative-district hard errors are zero",
        ),
        "missing_category": _gate(
            all(item["missing_category"] / total < 0.02 for item, total in zip(hard_counts, totals, strict=True)),
            [{"count": item["missing_category"], "total": total} for item, total in zip(hard_counts, totals, strict=True)],
            "missing required category is below 2 percent in every snapshot round",
        ),
        "wrong_category": _gate(
            all(item["wrong_category"] / total < 0.01 for item, total in zip(hard_counts, totals, strict=True)),
            [{"count": item["wrong_category"], "total": total} for item, total in zip(hard_counts, totals, strict=True)],
            "out-of-intent category is below 1 percent in every snapshot round",
        ),
    }

    valid_judges = len(judge_reports) == 3 and all(
        int(_summary(report).get("judged") or 0) == 150
        and int(_summary(report).get("judge_errors") or 0) == 0
        and (report.get("judge_chain") or {}).get("kind") == "codex_subagent"
        and int((report.get("judge_chain") or {}).get("network_calls") or 0) == 0
        for report in judge_reports
    )
    recommendation_quality: dict[str, Any] = {}
    if valid_judges:
        judge_summaries = [_summary(report) for report in judge_reports]
        judged_rates = [float(summary.get("pass_rate") or 0.0) for summary in judge_summaries]
        judged_groups = {
            "cities": [summary.get("by_city") or {} for summary in judge_summaries],
            "intents": [summary.get("by_intent") or {} for summary in judge_summaries],
            "compound": [((summary.get("by_intent_group") or {}).get("compound") or {}) for summary in judge_summaries],
        }
        recommendation_quality.update({
            "snapshot_overall": _gate(
                all(rate >= 0.85 for rate in judged_rates), judged_rates,
                "three fully judged snapshot rounds are at least 85 percent",
            ),
            "snapshot_cities": _gate(
                all(float(stats.get("pass_rate") or 0.0) >= 0.80
                    for groups in judged_groups["cities"] for stats in groups.values()),
                judged_groups["cities"], "every judged city is at least 80 percent in every round",
            ),
            "snapshot_intents": _gate(
                all(float(stats.get("pass_rate") or 0.0) >= (0.75 if name == "all" else 0.80)
                    for groups in judged_groups["intents"] for name, stats in groups.items()),
                judged_groups["intents"], "every judged intent is at least 80 percent and all is at least 75 percent",
            ),
            "snapshot_compound": _gate(
                all(float(stats.get("pass_rate") or 0.0) >= 0.80 for stats in judged_groups["compound"]),
                judged_groups["compound"], "judged compound is at least 80 percent in every round",
            ),
        })
        score_evidence = [summary.get("average_judge_scores") or {} for summary in judge_summaries]
        semantic_passed = all(
            float(scores.get(name) or 0.0) >= threshold
            for scores in score_evidence for name, threshold in SEMANTIC_SCORE_THRESHOLDS.items()
        )
        recommendation_quality["semantic_scores"] = _gate(
            semantic_passed, score_evidence, "all three Judge rounds meet semantic score thresholds",
        )
    else:
        missing_judge_evidence = {
            "judge_report_count": len(judge_reports),
            "reason": "three complete 150-case Judge reports are required",
        }
        recommendation_quality.update({
            "snapshot_overall": _gate(None, missing_judge_evidence, "three fully judged snapshot rounds are at least 85 percent"),
            "snapshot_cities": _gate(None, missing_judge_evidence, "every judged city is at least 80 percent in every round"),
            "snapshot_intents": _gate(None, missing_judge_evidence, "every judged intent is at least 80 percent and all is at least 75 percent"),
            "snapshot_compound": _gate(None, missing_judge_evidence, "judged compound is at least 80 percent in every round"),
        })
        recommendation_quality["semantic_scores"] = _gate(
            None,
            missing_judge_evidence,
            "all three Judge rounds meet semantic score thresholds",
        )

    honesty_rows = [summary.get("high_risk_honesty") or {} for summary in snapshot_summaries]
    high_risk = {
        "unsupported_claims": _gate(
            all(int(row.get("unsupported_affirmative_claim_count") or 0) == 0 for row in honesty_rows),
            honesty_rows, "unsupported high-risk affirmative claims are zero",
        ),
        "confirmation_actions": _gate(
            all(float(row.get("confirmation_action_coverage") or 0.0) == 1.0 for row in honesty_rows),
            honesty_rows, "UNKNOWN and REQUIRES_CONFIRMATION cards have 100 percent action coverage",
        ),
    }
    panel_summary = (model_panel or {}).get("panel_summary") or {}
    panel_provenance = (model_panel or {}).get("provenance") or {}
    panel_valid = (
        (model_panel or {}).get("kind") == "codex_subagent_judge_panel"
        and len((model_panel or {}).get("evaluators") or []) == 3
        and int(panel_provenance.get("judge_api_calls") or 0) == 0
        and panel_provenance.get("human_calibration_performed") is False
        and int(panel_summary.get("total") or 0) == 150
    )
    panel_gate_passed = None if model_panel is None else (
        panel_valid
        and float(panel_summary.get("unanimous_agreement_rate") or 0.0) >= 0.85
        and bool(panel_summary.get("quality_thresholds_passed"))
    )
    high_risk["model_panel_agreement"] = _gate(
        panel_gate_passed,
        model_panel or {"reason": "three-model GPT-5.6-sol panel report is required"},
        "three independent GPT-5.6-sol Judges meet quality gates and at least 85 percent unanimous agreement",
    )

    label_rows = list((human_labels or {}).get("cases") or [])
    complete_labels = len(label_rows) >= 30 and all(
        row.get("human_label") in {"pass", "fail", True, False} for row in label_rows
    )
    human_disclosure = {
        "performed": bool(complete_labels and agreement),
        "sample_size": len(label_rows),
        "agreement": agreement if complete_labels else None,
        "claim_allowed": bool(complete_labels and agreement),
        "status": "completed" if complete_labels and agreement else "not_run",
    }

    groups = {
        "data_and_reliability": data_reliability,
        "deterministic_contract": deterministic,
        "recommendation_quality": recommendation_quality,
        "high_risk_honesty": high_risk,
    }
    statuses = [gate["status"] for group in groups.values() for gate in group.values()]
    return {
        "schema_version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_status": "blocked" if "blocked" in statuses else ("failed" if "failed" in statuses else "passed"),
        "calibration_disclosure": {
            "model_panel": "gpt-5.6-sol independent blind panel",
            "human_calibration": human_disclosure,
            "allowed_claim": "模型评审组达到既定校准阈值；真人校准未执行",
        },
        "groups": groups,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", type=Path, action="append", required=True)
    parser.add_argument("--snapshot", type=Path, action="append", required=True)
    parser.add_argument("--judge", type=Path, action="append", default=[])
    parser.add_argument("--model-panel", type=Path)
    parser.add_argument("--human-labels", type=Path)
    parser.add_argument("--agreement", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    def load(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))
    payload = audit_goal(
        [load(path) for path in args.live],
        [load(path) for path in args.snapshot],
        [load(path) for path in args.judge],
        load(args.model_panel) if args.model_panel else None,
        load(args.human_labels) if args.human_labels else None,
        load(args.agreement) if args.agreement else None,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if payload["overall_status"] == "passed" else 1)


if __name__ == "__main__":
    main()
