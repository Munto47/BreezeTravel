"""Custodian-scoped, aggregate-only scorer for the sealed P5 v4 blind run."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Protocol

from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest
from evals.trip_check_v1.p5.runner_v4 import (
    BLIND_CASE_COUNT_V4,
    BLIND_TERMINAL_COUNT_V4,
    BlindDatasetPathsV4,
    VARIANT_IDS_V4,
    validate_blind_run_group_v4,
)
from evals.trip_check_v1.p5.semantic_contract_v3 import (
    validate_oracle_payload_compatibility_v3,
)


SCORE_SCHEMA_V4 = "trip-check-p5-isolated-blind-score-v4"
AUTHORIZATION_SCHEMA_V4 = "trip-check-p5-custodian-score-authorization-v4"


class P5BlindScoringErrorV4(RuntimeError):
    """A stable scorer error that never carries label or case detail."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _fail(reason_code: str, exc: Exception | None = None) -> None:
    if exc is None:
        raise P5BlindScoringErrorV4(reason_code)
    raise P5BlindScoringErrorV4(reason_code) from exc


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _contains_link(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.is_symlink() or (
                hasattr(current, "is_junction") and current.is_junction()
            ):
                return True
        except OSError:
            return True
    return False


class CustodianBundleReaderV4(Protocol):
    role: str
    run_group_manifest_hash: str
    bundle_sha256: str

    def read_committed_bundle(self, *, expected_sha256: str, repo_root: Path) -> bytes: ...


@dataclass(frozen=True)
class ExternalCustodianBundleReaderV4:
    """Concrete process-boundary adapter used only by the blind custodian CLI."""

    bundle_path: Path
    authorization_path: Path
    role: str
    run_group_manifest_hash: str
    bundle_sha256: str

    @classmethod
    def from_external_files(
        cls,
        *,
        bundle_path: Path,
        authorization_path: Path,
        repo_root: Path,
    ) -> "ExternalCustodianBundleReaderV4":
        root = repo_root.resolve()
        resolved: dict[str, Path] = {}
        for name, path in {
            "bundle": bundle_path,
            "authorization": authorization_path,
        }.items():
            if not path.is_absolute() or ".." in path.parts or _contains_link(path.absolute()):
                _fail("CUSTODIAN_EXTERNAL_PATH_INVALID")
            try:
                target = path.resolve(strict=True)
            except OSError as exc:
                _fail("CUSTODIAN_EXTERNAL_ARTIFACT_UNREADABLE", exc)
            if _inside(target, root):
                _fail("CUSTODIAN_ARTIFACT_INSIDE_REPOSITORY")
            resolved[name] = target
        try:
            authorization = json.loads(
                resolved["authorization"].read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _fail("CUSTODIAN_AUTHORIZATION_INVALID", exc)
        if not isinstance(authorization, dict) or set(authorization) != {
            "schema_version",
            "role",
            "purpose",
            "single_use",
            "run_group_manifest_hash",
            "bundle_sha256",
        } or (
            authorization.get("schema_version") != AUTHORIZATION_SCHEMA_V4
            or authorization.get("role") != "blind_custodian"
            or authorization.get("purpose") != "score_external_blind_bundle_v4"
            or authorization.get("single_use") is not True
            or not isinstance(authorization.get("run_group_manifest_hash"), str)
            or not isinstance(authorization.get("bundle_sha256"), str)
        ):
            _fail("CUSTODIAN_AUTHORIZATION_INVALID")
        return cls(
            bundle_path=resolved["bundle"],
            authorization_path=resolved["authorization"],
            role="blind_custodian",
            run_group_manifest_hash=authorization["run_group_manifest_hash"],
            bundle_sha256=authorization["bundle_sha256"],
        )

    def read_committed_bundle(self, *, expected_sha256: str, repo_root: Path) -> bytes:
        if self.role != "blind_custodian" or self.bundle_sha256 != expected_sha256:
            _fail("CUSTODIAN_BUNDLE_COMMITMENT_MISMATCH")
        try:
            payload = self.bundle_path.read_bytes()
        except OSError as exc:
            _fail("CUSTODIAN_BUNDLE_UNREADABLE", exc)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            _fail("CUSTODIAN_BUNDLE_SHA256_MISMATCH")
        if _inside(self.bundle_path.resolve(), repo_root.resolve()):
            _fail("CUSTODIAN_BUNDLE_INSIDE_REPOSITORY")
        return payload


def canonical_labels_hash_v4(labels: Sequence[Mapping[str, Any]]) -> str:
    ordered = sorted(labels, key=lambda item: str(item.get("case_id", "")))
    payload = b"".join(canonical_bytes(item) + b"\n" for item in ordered)
    return hashlib.sha256(payload).hexdigest()


def _default_dataset_paths() -> BlindDatasetPathsV4:
    from evals.trip_check_v1.p5.data_contract_v2 import JUDGE_RUBRIC_PATH_V2
    from evals.trip_check_v1.p5.data_contract_v3 import (
        ACTIVE_CONTRACT_PATH,
    )
    from evals.trip_check_v1.p5.data_contract_v4 import (
        BLIND_INPUT_PATH_V4,
        BLIND_MATERIALIZATIONS_PATH_V4,
        BLIND_SEAL_PATH_V4,
        MANIFEST_PATH_V4,
        RUN_SPEC_TEMPLATE_PATH_V4,
    )

    return BlindDatasetPathsV4(
        inputs=BLIND_INPUT_PATH_V4,
        materializations=BLIND_MATERIALIZATIONS_PATH_V4,
        manifest=MANIFEST_PATH_V4,
        seal=BLIND_SEAL_PATH_V4,
        run_spec_template=RUN_SPEC_TEMPLATE_PATH_V4,
        rubric=JUDGE_RUBRIC_PATH_V2,
        active_contract=ACTIVE_CONTRACT_PATH,
    )


def _validate_bundle(
    *,
    payload: bytes,
    expected_case_ids: set[str],
    labels_canonical_sha256: str,
    oracle_validator: Callable[[Mapping[str, Any]], Any],
) -> dict[str, Any]:
    try:
        bundle = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("BLIND_BUNDLE_INVALID_JSON", exc)
    if not isinstance(bundle, dict) or set(bundle) != {
        "schema_version",
        "evidence_class",
        "human_evidence",
        "dataset_binding",
        "labels",
    } or (
        bundle.get("schema_version") != "trip-check-p5-blind-label-bundle-v2"
        or bundle.get("evidence_class") != "controlled_blind_oracle"
        or bundle.get("human_evidence") is not False
    ):
        _fail("BLIND_BUNDLE_CONTRACT_INVALID")
    labels = bundle.get("labels")
    if not isinstance(labels, list) or len(labels) != BLIND_CASE_COUNT_V4:
        _fail("BLIND_BUNDLE_LABEL_SET_INVALID")
    labels_by_id: dict[str, Any] = {}
    for label in labels:
        if not isinstance(label, dict) or set(label) != {
            "schema_version",
            "case_id",
            "oracle",
        } or (
            label.get("schema_version") != "trip-check-p5-blind-label-v2"
            or not isinstance(label.get("case_id"), str)
            or not isinstance(label.get("oracle"), dict)
        ):
            _fail("BLIND_BUNDLE_LABEL_SCHEMA_INVALID")
        case_id = label["case_id"]
        if case_id in labels_by_id:
            _fail("BLIND_BUNDLE_LABEL_DUPLICATE")
        try:
            labels_by_id[case_id] = oracle_validator(label["oracle"])
        except Exception as exc:
            _fail("BLIND_BUNDLE_ORACLE_SCHEMA_INVALID", exc)
    if set(labels_by_id) != expected_case_ids:
        _fail("BLIND_BUNDLE_LABEL_CASE_SET_MISMATCH")
    if canonical_labels_hash_v4(labels) != labels_canonical_sha256:
        _fail("BLIND_LABEL_COMMITMENT_MISMATCH")
    return labels_by_id


class _BlindOracleCaseView:
    def __init__(self, source: Any, oracle: Any) -> None:
        self._source = source
        self.oracle = oracle

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)


def _value(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item[name]
    return getattr(item, name)


def _safe_variant_aggregate(
    scores: Sequence[Any], outputs: Sequence[Any]
) -> dict[str, Any]:
    count = len(scores)
    nonpass = sum(int(_value(item, "nonpass_finding_count")) for item in scores)
    covered = sum(
        int(_value(item, "covered_nonpass_finding_count")) for item in scores
    )
    measured_tokens = [
        int(_value(item, "token_count"))
        for item in scores
        if isinstance(_value(item, "token_count"), int)
    ]
    measured_costs = [
        float(_value(item, "cost_usd"))
        for item in scores
        if isinstance(_value(item, "cost_usd"), (int, float))
    ]
    latencies = sorted(float(_value(item, "latency_ms")) for item in outputs)
    p95_index = max(0, min(len(latencies) - 1, (95 * len(latencies) + 99) // 100 - 1))
    return {
        "case_count": count,
        "task_success_count": sum(bool(_value(item, "task_success")) for item in scores),
        "task_success_rate": (
            sum(bool(_value(item, "task_success")) for item in scores) / count
            if count
            else 0.0
        ),
        "mean_score": (
            sum(float(_value(item, "score")) for item in scores) / count
            if count
            else 0.0
        ),
        "deterministic_failure_count": sum(
            not bool(_value(item, "deterministic_pass")) for item in scores
        ),
        "wrong_city_or_poi_count": sum(
            int(_value(item, "wrong_city_or_poi_count") or 0) for item in scores
        ),
        "hard_finding_miss_count": sum(
            len(_value(item, "missing_reason_codes")) for item in scores
        ),
        "unknown_failure_count": sum(
            _value(item, "unknown_preservation") == "FAIL" for item in scores
        ),
        "candidate_receipt_failure_count": sum(
            _value(item, "candidate_receipt_coverage") == "FAIL" for item in scores
        ),
        "concurrency_failure_count": sum(
            _value(item, "concurrency_result") == "FAIL" for item in scores
        ),
        "postcheck_failure_count": sum(
            _value(item, "repair_postcheck") == "FAIL" for item in scores
        ),
        "replay_failure_count": sum(
            not bool(_value(item, "replay_hash_match")) for item in scores
        ),
        "nonpass_finding_count": nonpass,
        "covered_nonpass_finding_count": covered,
        "nonpass_finding_advice_coverage_rate": covered / nonpass if nonpass else 1.0,
        "unsupported_claim_count": sum(
            int(_value(item, "unsupported_claim_count")) for item in scores
        ),
        "usage_measurement_failure_count": sum(
            _value(item, "usage_measurement") == "FAIL" for item in scores
        ),
        "token_count_total": sum(measured_tokens),
        "token_count_not_measured_count": sum(
            _value(item, "token_count") == "NOT_MEASURED" for item in scores
        ),
        "cost_usd_total": sum(measured_costs),
        "cost_not_measured_count": sum(
            _value(item, "cost_usd") == "NOT_MEASURED" for item in scores
        ),
        "latency_p50_ms": median(latencies) if latencies else None,
        "latency_p95_ms": latencies[p95_index] if latencies else None,
        "terminal_status_counts": dict(
            sorted(Counter(str(_value(item, "terminal_status")) for item in outputs).items())
        ),
    }


def _default_oracle_validator(value: Mapping[str, Any]) -> Any:
    from evals.trip_check_v1.p5.contracts_v2 import P5OracleV2

    return P5OracleV2.model_validate(value)


def _default_case_scorer(
    case: Any, output: Any, oracle: Any, materialization: Mapping[str, Any]
) -> Any:
    from evals.trip_check_v1.p5.nonblind_scorer_v4 import _project_terminal_to_v3
    from evals.trip_check_v1.p5.scorer_v3 import score_case_v3

    return score_case_v3(
        _BlindOracleCaseView(case, oracle),
        _project_terminal_to_v3(output),
        materialization=materialization,
    )


def score_isolated_blind_v4(
    *,
    repo_root: Path,
    run_dir: Path,
    expected_bundle_sha256: str,
    custodian_reader: CustodianBundleReaderV4,
    dataset_paths: BlindDatasetPathsV4 | None = None,
    run_validator: Callable[..., tuple[dict[str, Any], list[Any], list[Any], dict[str, dict[str, Any]]]] = validate_blind_run_group_v4,
    oracle_validator: Callable[[Mapping[str, Any]], Any] = _default_oracle_validator,
    case_scorer: Callable[[Any, Any, Any, Mapping[str, Any]], Any] = _default_case_scorer,
) -> dict[str, Any]:
    """Return only variant-level aggregates; label material never crosses this API."""

    paths = dataset_paths or _default_dataset_paths()
    try:
        manifest, cases, outputs, materializations = run_validator(
            run_dir=run_dir,
            repo_root=repo_root,
            require_formal=True,
            dataset_paths=paths,
        )
    except Exception as exc:
        reason = getattr(exc, "reason_code", "BLIND_RUN_GROUP_INVALID")
        _fail(str(reason), exc)
    if (
        custodian_reader.role != "blind_custodian"
        or custodian_reader.run_group_manifest_hash != manifest.get("manifest_hash")
        or custodian_reader.bundle_sha256 != expected_bundle_sha256
    ):
        _fail("CUSTODIAN_AUTHORIZATION_BINDING_MISMATCH")
    try:
        seal = json.loads(paths.seal.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("BLIND_SEAL_INVALID", exc)
    if not isinstance(seal, dict) or (
        seal.get("external_bundle_sha256") != expected_bundle_sha256
        or not isinstance(seal.get("labels_canonical_sha256"), str)
    ):
        _fail("BLIND_BUNDLE_SEAL_HASH_MISMATCH")
    payload = custodian_reader.read_committed_bundle(
        expected_sha256=expected_bundle_sha256,
        repo_root=repo_root,
    )
    case_ids = {str(_value(case, "case_id")) for case in cases}
    labels_by_id = _validate_bundle(
        payload=payload,
        expected_case_ids=case_ids,
        labels_canonical_sha256=str(seal["labels_canonical_sha256"]),
        oracle_validator=oracle_validator,
    )
    case_by_id = {str(_value(case, "case_id")): case for case in cases}
    try:
        semantic_mismatch = False
        for case_id, case in case_by_id.items():
            semantic_mismatch = bool(
                validate_oracle_payload_compatibility_v3(
                    case,
                    materializations[case_id],
                    labels_by_id[case_id],
                )
            ) or semantic_mismatch
    except Exception:
        _fail("BLIND_ORACLE_PAYLOAD_SEMANTIC_MISMATCH")
    if semantic_mismatch:
        _fail("BLIND_ORACLE_PAYLOAD_SEMANTIC_MISMATCH")
    scores = [
        case_scorer(
            case_by_id[str(_value(output, "case_id"))],
            output,
            labels_by_id[str(_value(output, "case_id"))],
            materializations[str(_value(output, "case_id"))],
        )
        for output in outputs
    ]
    variant_metrics: dict[str, Any] = {}
    for variant_id in VARIANT_IDS_V4:
        variant_scores = [
            item for item in scores if str(_value(item, "variant_id")) == variant_id
        ]
        variant_outputs = [
            item for item in outputs if str(_value(item, "variant_id")) == variant_id
        ]
        if len(variant_scores) != BLIND_CASE_COUNT_V4:
            _fail("BLIND_SCORE_VARIANT_COUNT_INVALID")
        variant_metrics[variant_id] = _safe_variant_aggregate(
            variant_scores, variant_outputs
        )
    core = variant_metrics["core_b"]
    zero_tolerance_checks = {
        "mean_score_gte_88": core["mean_score"] >= 88,
        "deterministic_failure_zero": core["deterministic_failure_count"] == 0,
        "wrong_city_or_poi_zero": core["wrong_city_or_poi_count"] == 0,
        "hard_finding_miss_zero": core["hard_finding_miss_count"] == 0,
        "unknown_failure_zero": core["unknown_failure_count"] == 0,
        "candidate_receipt_failure_zero": core[
            "candidate_receipt_failure_count"
        ]
        == 0,
        "concurrency_failure_zero": core["concurrency_failure_count"] == 0,
        "postcheck_failure_zero": core["postcheck_failure_count"] == 0,
        "replay_failure_zero": core["replay_failure_count"] == 0,
        "unsupported_claim_zero": core["unsupported_claim_count"] == 0,
        "usage_failure_zero": core["usage_measurement_failure_count"] == 0,
    }
    passed = all(zero_tolerance_checks.values())
    receipt: dict[str, Any] = {
        "schema_version": SCORE_SCHEMA_V4,
        "status": "PASS" if passed else "REJECT",
        "evidence_boundary": {
            "truth_provenance": "external_controlled_blind_oracle",
            "custodian_scoped_bundle_read": True,
            "case_level_output": False,
            "dimension_bucket_output": False,
            "label_output": False,
            "automated_proxy_judge": "NOT_RUN",
        },
        "bindings": {
            "subject_commit": manifest["subject_commit"],
            "dataset_manifest_hash": manifest["dataset_manifest_hash"],
            "run_group_manifest_hash": manifest["manifest_hash"],
            "terminal_outputs_file_sha256": manifest[
                "terminal_outputs_file_sha256"
            ],
            "terminal_outputs_content_sha256": manifest[
                "terminal_outputs_content_sha256"
            ],
            "artifact_index_hash": manifest["artifact_index_hash"],
            "blind_seal_sha256": manifest["blind_seal_sha256"],
            "run_spec_template_sha256": manifest["run_spec_template_sha256"],
        },
        "case_count": BLIND_CASE_COUNT_V4,
        "terminal_count": BLIND_TERMINAL_COUNT_V4,
        "replay_readback_count": BLIND_TERMINAL_COUNT_V4,
        "variant_metrics": variant_metrics,
        "zero_tolerance_checks": zero_tolerance_checks,
        "human_calibration_performed": False,
        "human_evidence": False,
        "live_provider_evidence": False,
        "public_e2e_evidence": False,
    }
    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    if any(case_id in serialized for case_id in case_ids) or any(
        token in serialized
        for token in ('"labels"', '"oracle"', '"case_id"', '"buckets"')
    ):
        _fail("BLIND_AGGREGATE_DISCLOSURE_DETECTED")
    receipt["report_hash"] = digest(receipt)
    return receipt


__all__ = [
    "AUTHORIZATION_SCHEMA_V4",
    "CustodianBundleReaderV4",
    "ExternalCustodianBundleReaderV4",
    "P5BlindScoringErrorV4",
    "SCORE_SCHEMA_V4",
    "canonical_labels_hash_v4",
    "score_isolated_blind_v4",
]
