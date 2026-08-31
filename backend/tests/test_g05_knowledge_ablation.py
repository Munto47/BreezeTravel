from datetime import datetime
from pathlib import Path

from evals.g05_knowledge import load_admission_manifest
from evals.g05_knowledge.ablation import evaluate_knowledge_ablation, load_ablation_oracle


ROOT = Path(__file__).parents[1]


def test_g05_ablation_meets_quality_actionability_and_isolation_thresholds() -> None:
    report = evaluate_knowledge_ablation(
        load_admission_manifest(ROOT / "eval_data/g05_knowledge/admission_v1.json"),
        load_ablation_oracle(ROOT / "eval_data/g05_knowledge/ablation_oracle_v1.json"),
        as_of=datetime.fromisoformat("2026-08-31T09:00:00+08:00"),
        samples=80,
        rounds=3,
    )

    assert report.shown_count == 4
    assert report.precision == 1.0
    assert report.unsupported_count == 0
    assert report.actionability_lift_percentage_points >= 5.0
    assert report.authoritative_field_changes == 0
    assert report.p95_regression <= 0.20
    assert report.passed
