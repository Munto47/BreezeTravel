from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JudgeCalibration:
    sample_size: int
    agreement: float
    false_positive_rate: float
    calibrated: bool


def calibrate_binary_judge(human_labels: list[bool], judge_labels: list[bool]) -> JudgeCalibration:
    if len(human_labels) != len(judge_labels) or not human_labels:
        raise ValueError("paired non-empty labels are required")
    agreement = sum(a == b for a, b in zip(human_labels, judge_labels)) / len(human_labels)
    negatives = sum(not value for value in human_labels)
    false_positives = sum(not human and judge for human, judge in zip(human_labels, judge_labels))
    false_positive_rate = false_positives / negatives if negatives else 0.0
    return JudgeCalibration(
        sample_size=len(human_labels),
        agreement=agreement,
        false_positive_rate=false_positive_rate,
        calibrated=len(human_labels) >= 10 and agreement >= 0.8 and false_positive_rate <= 0.1,
    )


def bad_case_registry(results: list[dict], success_key: str = "passed") -> list[dict]:
    return [
        {
            "case_id": row["case_id"],
            "expected": row.get("expected"),
            "actual": row.get("actual"),
            "error_category": row.get("error_category", "metric_failure"),
            "reproduce": row.get("reproduce"),
        }
        for row in results
        if not row.get(success_key, False)
    ]
