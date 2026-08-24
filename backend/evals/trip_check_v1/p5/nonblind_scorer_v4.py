"""Formal deterministic scorer for a validated P5 v4 non-blind run group."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.trip_check_v1.p5 import scorer_v3 as _scorer_v3
from evals.trip_check_v1.p5.contracts_v3 import (
    P5CaseV3,
    P5TerminalOutputV3,
)
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.nonblind_runner_v4 import (
    NONBLIND_CASE_COUNT_V4,
    NONBLIND_TERMINAL_COUNT_V4,
    P5NonblindRunnerErrorV4,
    validate_nonblind_run_group_v4,
)
from evals.trip_check_v1.p5.runner_v4 import (
    DATASET_ID_V4,
    P5TerminalOutputV4,
    VARIANT_IDS_V4,
)


SCORE_REPORT_SCHEMA_V4 = "trip-check-p5-nonblind-score-report-v4"
CASE_SCORE_SCHEMA_V4 = "trip-check-p5-case-score-v4"


class P5NonblindScoringErrorV4(RuntimeError):
    """Stable fail-closed scorer error for invalid v4 evidence."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class P5CaseScoreV4(_scorer_v3.P5CaseScoreV3):
    """Versioned case score; semantics are deliberately inherited from v3."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["trip-check-p5-case-score-v4"] = CASE_SCORE_SCHEMA_V4


class P5NonblindScoreReportV4(BaseModel):
    """Independent v4 report envelope over deterministic v3 score semantics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-nonblind-score-report-v4"] = (
        SCORE_REPORT_SCHEMA_V4
    )
    status: Literal["PASS", "REJECT"]
    formal_validation_performed: bool
    development_only: bool
    scoring_semantics: dict[str, Any]
    subject_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    upstream_ref: str = Field(min_length=1)
    upstream_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    dirty_tree: Literal[False] = False
    dataset_id: Literal["trip-check-p5-360-v4"] = DATASET_ID_V4
    dataset_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_group_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_index_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_spec_template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminal_outputs_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminal_outputs_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_count: Literal[270] = NONBLIND_CASE_COUNT_V4
    terminal_count: Literal[810] = NONBLIND_TERMINAL_COUNT_V4
    replay_readback_count: Literal[810] = NONBLIND_TERMINAL_COUNT_V4
    variant_metrics: dict[str, Any]
    paired_comparisons: dict[str, Any]
    zero_tolerance_checks: dict[str, bool]
    stage_gate_checks: dict[str, bool]
    promotion_decision: Literal["KEEP_CORE_B", "REJECT_ALL"]
    solver_admission_inherited: Literal["REJECT"] = "REJECT"
    solver_may_promote_from_p5_score: Literal[False] = False
    evidence_boundary: dict[str, Any]
    case_scores: list[P5CaseScoreV4]
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def hash_and_decision_are_bound(self) -> "P5NonblindScoreReportV4":
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if self.report_hash != digest(payload):
            raise ValueError("report_hash does not bind the complete v4 report")
        if self.status == "PASS" and (
            not self.formal_validation_performed
            or self.development_only
            or self.promotion_decision != "KEEP_CORE_B"
            or not all(self.zero_tolerance_checks.values())
            or not all(self.stage_gate_checks.values())
        ):
            raise ValueError("PASS is not supported by the bound evidence")
        if self.status == "REJECT" and self.promotion_decision != "REJECT_ALL":
            raise ValueError("REJECT must reject every candidate")
        if set(self.variant_metrics) != set(VARIANT_IDS_V4):
            raise ValueError("variant metrics do not bind the exact v4 variant set")
        if len(self.case_scores) != NONBLIND_TERMINAL_COUNT_V4:
            raise ValueError("case scores do not bind all 810 terminals")
        return self


def _project_terminal_to_v3(output: P5TerminalOutputV4) -> P5TerminalOutputV3:
    """Rebind a validated v4 terminal into scorer_v3's semantic hash domain."""

    payload = output.model_dump(mode="json")
    payload["schema_version"] = "trip-check-p5-terminal-output-v3"
    provisional = P5TerminalOutputV3.model_validate(payload)
    semantic_hash = _scorer_v3.semantic_output_hash_v3(provisional)
    payload["semantic_output_hash"] = semantic_hash
    payload["replay_hash"] = semantic_hash
    return P5TerminalOutputV3.model_validate(payload)


def _require_validated_shape(
    *,
    manifest: Mapping[str, Any],
    cases: Sequence[P5CaseV3],
    outputs: Sequence[P5TerminalOutputV4],
    materializations: Mapping[str, Mapping[str, Any]],
    formal_validation_performed: bool,
) -> None:
    case_ids = {case.case_id for case in cases}
    expected_keys = {
        (case_id, variant_id)
        for case_id in case_ids
        for variant_id in VARIANT_IDS_V4
    }
    actual_keys = [(output.case_id, output.variant_id) for output in outputs]
    if (
        manifest.get("schema_version") != "trip-check-p5-run-group-v4"
        or manifest.get("lane") != "nonblind"
        or manifest.get("status") != "PASS"
        or manifest.get("dataset_id") != DATASET_ID_V4
        or manifest.get("case_count") != NONBLIND_CASE_COUNT_V4
        or manifest.get("terminal_count") != NONBLIND_TERMINAL_COUNT_V4
        or manifest.get("replay_readback_count") != NONBLIND_TERMINAL_COUNT_V4
        or manifest.get("variant_ids") != list(VARIANT_IDS_V4)
        or manifest.get("dirty_tree") is not False
        or manifest.get("upstream_commit") != manifest.get("subject_commit")
        or not manifest.get("upstream_ref")
        or len(cases) != NONBLIND_CASE_COUNT_V4
        or len(case_ids) != NONBLIND_CASE_COUNT_V4
        or len(outputs) != NONBLIND_TERMINAL_COUNT_V4
        or len(actual_keys) != len(set(actual_keys))
        or set(actual_keys) != expected_keys
        or set(materializations) != case_ids
    ):
        raise P5NonblindScoringErrorV4("V4_NONBLIND_VALIDATED_SHAPE_INVALID")
    if formal_validation_performed and manifest.get("formal_evidence") is not True:
        raise P5NonblindScoringErrorV4("V4_NONBLIND_FORMAL_RUN_REQUIRED")


def build_nonblind_score_report_v4(
    *,
    manifest: Mapping[str, Any],
    cases: Sequence[P5CaseV3],
    outputs: Sequence[P5TerminalOutputV4],
    materializations: Mapping[str, Mapping[str, Any]],
    formal_validation_performed: bool,
) -> dict[str, Any]:
    """Reuse v3 case/aggregate semantics and emit a hash-bound v4 envelope."""

    _require_validated_shape(
        manifest=manifest,
        cases=cases,
        outputs=outputs,
        materializations=materializations,
        formal_validation_performed=formal_validation_performed,
    )
    try:
        projected_outputs = [_project_terminal_to_v3(output) for output in outputs]
        base = _scorer_v3.build_score_report_v3(
            manifest=manifest,
            cases=cases,
            outputs=projected_outputs,
            materializations=materializations,
            formal_validation_performed=formal_validation_performed,
            include_case_scores=True,
        )
        raw_case_scores = base.get("case_scores")
        if not isinstance(raw_case_scores, list):
            raise ValueError("scorer_v3 omitted case scores")
        case_scores = [
            P5CaseScoreV4.model_validate(
                {**item, "schema_version": CASE_SCORE_SCHEMA_V4}
            ).model_dump(mode="json")
            for item in raw_case_scores
        ]
    except P5NonblindScoringErrorV4:
        raise
    except Exception as exc:
        reason = getattr(exc, "reason_code", "V4_NONBLIND_SCORING_FAILED")
        raise P5NonblindScoringErrorV4(str(reason)) from exc

    zero_checks = dict(base["zero_tolerance_checks"])
    stage_checks = dict(base["stage_gate_checks"])
    passed = bool(
        formal_validation_performed
        and base.get("status") == "PASS"
        and zero_checks
        and stage_checks
        and all(zero_checks.values())
        and all(stage_checks.values())
    )
    payload: dict[str, Any] = {
        "schema_version": SCORE_REPORT_SCHEMA_V4,
        "status": "PASS" if passed else "REJECT",
        "formal_validation_performed": formal_validation_performed,
        "development_only": not formal_validation_performed,
        "scoring_semantics": {
            "case_semantics": "trip-check-p5-deterministic-scorer-v3",
            "aggregate_semantics": "trip-check-p5-aggregate-v3",
            "terminal_projection": "v4-to-v3-semantic-hash-rebinding",
            "source_terminal_schema": "trip-check-p5-terminal-output-v4",
            "report_envelope": SCORE_REPORT_SCHEMA_V4,
        },
        "subject_commit": manifest["subject_commit"],
        "upstream_ref": manifest["upstream_ref"],
        "upstream_commit": manifest["upstream_commit"],
        "dirty_tree": False,
        "dataset_id": DATASET_ID_V4,
        "dataset_manifest_hash": manifest["dataset_manifest_hash"],
        "run_group_manifest_hash": manifest["manifest_hash"],
        "artifact_index_hash": manifest["artifact_index_hash"],
        "run_spec_template_sha256": manifest["run_spec_template_sha256"],
        "rubric_sha256": manifest["rubric_sha256"],
        "terminal_outputs_file_sha256": manifest[
            "terminal_outputs_file_sha256"
        ],
        "terminal_outputs_content_sha256": manifest[
            "terminal_outputs_content_sha256"
        ],
        "case_count": NONBLIND_CASE_COUNT_V4,
        "terminal_count": NONBLIND_TERMINAL_COUNT_V4,
        "replay_readback_count": NONBLIND_TERMINAL_COUNT_V4,
        "variant_metrics": base["variant_metrics"],
        "paired_comparisons": base["paired_comparisons"],
        "zero_tolerance_checks": zero_checks,
        "stage_gate_checks": stage_checks,
        "promotion_decision": "KEEP_CORE_B" if passed else "REJECT_ALL",
        "solver_admission_inherited": "REJECT",
        "solver_may_promote_from_p5_score": False,
        "evidence_boundary": {
            "formal_v4_active_seal_git_artifact_validation": (
                "PASS" if formal_validation_performed else "DIAGNOSTIC_ONLY"
            ),
            "controlled_snapshot": (
                "PASS" if formal_validation_performed else "DIAGNOSTIC_ONLY"
            ),
            "nonblind_oracle_access": "SCORER_ONLY_AFTER_TERMINAL_SEAL",
            "product_adapter_oracle_access": "DENIED_ATTESTED_BY_RUNNER",
            "blind_labels_read": False,
            "replay_readback": (
                "PASS_810_HASH_BOUND"
                if formal_validation_performed
                else "DIAGNOSTIC_810_HASH_BOUND"
            ),
            "solver_p4_admission": "REJECT_INHERITED_NO_SCORE_OVERRIDE",
            "historical_ocr_receipt_replay": "PASS_HISTORICAL_V2_RECEIPT",
            "fresh_actual_ocr_execution": "NOT_RUN",
            "automated_proxy_judge": "NOT_RUN",
            "human_calibration_performed": False,
            "human_evidence": "NOT_RUN",
            "live_provider": "NOT_RUN",
            "public_e2e": "NOT_RUN",
            "production_release": "NOT_RUN",
            "main_merge": "NOT_RUN",
        },
        "case_scores": case_scores,
    }
    payload["report_hash"] = digest(payload)
    try:
        report = P5NonblindScoreReportV4.model_validate(payload)
    except Exception as exc:
        raise P5NonblindScoringErrorV4("V4_NONBLIND_REPORT_INVALID") from exc
    return report.model_dump(mode="json")


def score_nonblind_run_group_v4(
    *,
    run_dir: Path,
    repo_root: Path,
    require_formal: bool = True,
    run_validator: Callable[..., tuple[dict[str, Any], list[Any], list[Any], dict[str, dict[str, Any]]]]
    | None = None,
) -> dict[str, Any]:
    """Validate the v4 run first; diagnostic scoring can never return PASS."""

    validator = run_validator or validate_nonblind_run_group_v4
    try:
        manifest, cases, outputs, materializations = validator(
            run_dir=run_dir,
            repo_root=repo_root,
            require_formal=require_formal,
        )
        return build_nonblind_score_report_v4(
            manifest=manifest,
            cases=cases,
            outputs=outputs,
            materializations=materializations,
            formal_validation_performed=require_formal,
        )
    except P5NonblindScoringErrorV4:
        raise
    except (P5NonblindRunnerErrorV4, _scorer_v3.P5V3ScoringError) as exc:
        raise P5NonblindScoringErrorV4(exc.reason_code) from exc
    except Exception as exc:
        raise P5NonblindScoringErrorV4("V4_NONBLIND_SCORING_FAILED") from exc


__all__ = [
    "CASE_SCORE_SCHEMA_V4",
    "P5CaseScoreV4",
    "P5NonblindScoreReportV4",
    "P5NonblindScoringErrorV4",
    "SCORE_REPORT_SCHEMA_V4",
    "build_nonblind_score_report_v4",
    "score_nonblind_run_group_v4",
]
