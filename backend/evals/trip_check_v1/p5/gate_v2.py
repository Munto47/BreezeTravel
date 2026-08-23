"""P5 v2 Gate manifest builder with full artifact readback."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.final_blind_scorer_v2 import (
    schema_contract_sha256_v2,
)
from evals.trip_check_v1.p5.scorer_v2 import validate_run_group_v2


class P5GateErrorV2(RuntimeError):
    pass


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|secret)\b['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_/-]{20,}",
        re.I,
    ),
)
_P4_SUBJECT_COMMIT = "85368777ca8d2d4e77cf053fc9a74018f9f9fc9a"
_COMMITMENT_CHAIN_FIELDS = frozenset(
    {
        "active_contract_file_sha256",
        "blind_seal_sha256",
        "external_bundle_sha256",
        "labels_canonical_sha256",
        "review_receipt_sha256",
    }
)
_FORMAL_DATASET_KEYS = (
    "nonblind_cases",
    "nonblind_materializations",
    "blind_cases",
    "blind_materializations",
)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5GateErrorV2(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise P5GateErrorV2(f"{label} must be an object")
    return value


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise P5GateErrorV2(f"artifact unreadable: {path.name}") from exc


def _validate_hash(value: dict[str, Any], field: str, label: str) -> None:
    if value.get(field) != digest({key: item for key, item in value.items() if key != field}):
        raise P5GateErrorV2(f"{label} hash mismatch")


def _repo_state(root: Path) -> tuple[str, bool]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--short"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise P5GateErrorV2("repository state unavailable") from exc
    return head, dirty


def _artifact(name: str, path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
        storage = "repository"
    except ValueError:
        relative = resolved.name
        storage = "external"
    return {
        "logical_name": name,
        "storage": storage,
        "path": relative,
        "sha256": _sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _artifact_from_bytes(
    name: str,
    path: Path,
    root: Path,
    payload: bytes,
) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
        storage = "repository"
    except ValueError:
        relative = resolved.name
        storage = "external"
    return {
        "logical_name": name,
        "storage": storage,
        "path": relative,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_link_or_junction(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True


def _contains_link_or_junction(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists() and _is_link_or_junction(current):
            return True
    return False


def _read_external_json(
    repo_root: Path,
    path: Path,
    label: str,
) -> tuple[Path, dict[str, Any], bytes]:
    if not path.is_absolute() or ".." in path.parts:
        raise P5GateErrorV2(f"{label} path must be absolute without traversal")
    absolute = path.absolute()
    if _contains_link_or_junction(absolute):
        raise P5GateErrorV2(f"{label} path cannot contain a symlink or junction")
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise P5GateErrorV2(f"{label} is unreadable") from exc
    if not resolved.is_file() or _inside(resolved, repo_root.resolve(strict=True)):
        raise P5GateErrorV2(f"{label} must be a file outside the repository")
    try:
        payload = resolved.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5GateErrorV2(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise P5GateErrorV2(f"{label} must be an object")
    return resolved, value, payload


def _validate_formal_validation_receipt(
    *,
    repo_root: Path,
    receipt_path: Path,
    schema_path: Path,
    dataset_path: Path,
    dataset: dict[str, Any],
    subject_commit: str,
) -> tuple[Path, dict[str, Any], bytes]:
    resolved, receipt, payload = _read_external_json(
        repo_root,
        receipt_path,
        "P5 v2 formal dataset validation receipt",
    )
    schema = _load_json(schema_path, "P5 v2 formal dataset validation receipt schema")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(receipt),
        key=lambda item: list(item.path),
    )
    if errors:
        raise P5GateErrorV2(
            f"P5 v2 formal dataset validation receipt schema rejected: {errors[0].message}"
        )
    _validate_hash(receipt, "receipt_hash", "P5 v2 formal dataset validation receipt")

    if (
        receipt.get("subject_commit") != subject_commit
        or receipt.get("schema_version") != "trip-check-p5-dataset-validation-v2"
        or receipt.get("status") != "PASS"
        or receipt.get("formal") is not True
        or receipt.get("manifest_hash") != dataset.get("manifest_hash")
        or receipt.get("errors") != []
    ):
        raise P5GateErrorV2("P5 v2 formal dataset validation receipt binding rejected")

    expected_manifest = {
        "path": "backend/evals/trip_check_v1/p5/dataset_v2.manifest.json",
        "file_sha256": _sha256(dataset_path),
        "manifest_hash": dataset["manifest_hash"],
    }
    if receipt.get("dataset_manifest") != expected_manifest:
        raise P5GateErrorV2("P5 v2 formal dataset manifest binding rejected")

    validator_path = repo_root / "backend" / "scripts" / "validate_trip_check_p5_dataset_v2.py"
    expected_validator = {
        "path": "backend/scripts/validate_trip_check_p5_dataset_v2.py",
        "code_sha256": _sha256(validator_path),
    }
    if receipt.get("validator") != expected_validator:
        raise P5GateErrorV2("P5 v2 formal dataset validator binding rejected")

    manifest_files = dataset.get("files")
    if not isinstance(manifest_files, dict) or set(manifest_files) != set(_FORMAL_DATASET_KEYS):
        raise P5GateErrorV2("P5 v2 formal dataset manifest files rejected")
    expected_files: dict[str, dict[str, Any]] = {}
    backend_root = repo_root / "backend"
    for key in _FORMAL_DATASET_KEYS:
        entry = manifest_files[key]
        if not isinstance(entry, dict):
            raise P5GateErrorV2("P5 v2 formal dataset manifest files rejected")
        relative_value = entry.get("path")
        if not isinstance(relative_value, str):
            raise P5GateErrorV2("P5 v2 formal dataset file path rejected")
        relative = Path(relative_value)
        if relative.is_absolute() or ".." in relative.parts:
            raise P5GateErrorV2("P5 v2 formal dataset file path rejected")
        try:
            source_path = (backend_root / relative).resolve(strict=True)
        except OSError as exc:
            raise P5GateErrorV2("P5 v2 formal dataset file unreadable") from exc
        if not source_path.is_file() or not _inside(source_path, backend_root.resolve(strict=True)):
            raise P5GateErrorV2("P5 v2 formal dataset file path rejected")
        if _sha256(source_path) != entry.get("file_sha256"):
            raise P5GateErrorV2("P5 v2 formal dataset file hash rejected")
        expected_files[key] = {
            "path": f"backend/{relative.as_posix()}",
            "file_sha256": entry.get("file_sha256"),
            "content_sha256": entry.get("content_sha256"),
            "row_count": entry.get("row_count"),
        }
    if receipt.get("dataset_files") != expected_files:
        raise P5GateErrorV2("P5 v2 formal dataset file binding rejected")
    return resolved, receipt, payload


def _commitment_artifact(name: str, sha256: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise P5GateErrorV2(f"invalid irreversible commitment: {name}")
    return {
        "logical_name": name,
        "storage": "external_irreversible_commitment",
        "path": None,
        "sha256": sha256,
        "size_bytes": None,
    }


def _commitment_chain(value: dict[str, Any], label: str) -> dict[str, str]:
    chain = {field: value.get(field) for field in _COMMITMENT_CHAIN_FIELDS}
    if any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in chain.values()):
        raise P5GateErrorV2(f"{label} commitment chain rejected")
    return chain  # type: ignore[return-value]


def _acceptance_c_checks(
    score: dict[str, Any],
    *,
    require_nonblind_splits: bool,
) -> dict[str, bool]:
    variants = score.get("variant_metrics")
    core_metrics = variants.get("core_b") if isinstance(variants, dict) else None
    overall = core_metrics.get("overall") if isinstance(core_metrics, dict) else None
    if not isinstance(overall, dict):
        raise P5GateErrorV2("Core B Acceptance C evidence missing")
    dimensions = overall.get("quality_dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != {
        "location_city_facts",
        "time_route_hotel_continuity",
        "other_advice",
    }:
        raise P5GateErrorV2("Core B quality dimension evidence missing")
    location = dimensions["location_city_facts"]
    continuity = dimensions["time_route_hotel_continuity"]
    other = dimensions["other_advice"]
    if not all(isinstance(item, dict) for item in (location, continuity, other)):
        raise P5GateErrorV2("Core B quality dimension evidence invalid")
    other_buckets = other.get("buckets")
    if (
        location.get("case_count", 0) <= 0
        or not isinstance(location.get("mean_score"), (int, float))
        or continuity.get("case_count", 0) <= 0
        or not isinstance(continuity.get("mean_score"), (int, float))
        or other.get("case_count", 0) <= 0
        or not isinstance(other_buckets, dict)
        or not other_buckets
        or any(
            not isinstance(bucket, dict)
            or bucket.get("case_count", 0) <= 0
            or not isinstance(bucket.get("mean_score"), (int, float))
            for bucket in other_buckets.values()
        )
    ):
        raise P5GateErrorV2("Core B quality dimension coverage invalid")
    checks = {
        "core_overall_score_gte_88": isinstance(overall.get("mean_score"), (int, float))
        and overall["mean_score"] >= 88,
        "core_location_city_score_gte_90": location["mean_score"] >= 90,
        "core_time_route_hotel_score_gte_90": continuity["mean_score"] >= 90,
        "core_other_advice_buckets_gte_80": all(
            bucket["mean_score"] >= 80 for bucket in other_buckets.values()
        ),
        "core_nonpass_finding_advice_coverage_100": overall.get(
            "nonpass_finding_count"
        )
        == overall.get("covered_nonpass_finding_count")
        and overall.get("nonpass_finding_advice_coverage_rate") == 1.0,
        "core_unsupported_claim_rate_zero": overall.get("unsupported_claim_count")
        == 0
        and overall.get("unsupported_claim_rate") == 0.0,
        "core_token_cost_zero_or_not_measured": overall.get(
            "usage_measurement_failure_count"
        )
        == 0
        and overall.get("token_count_total") == 0
        and overall.get("cost_usd_total") == 0,
    }
    if require_nonblind_splits:
        by_split = core_metrics.get("by_split")
        if not isinstance(by_split, dict):
            raise P5GateErrorV2("Core B split evidence missing")
        pilot = by_split.get("pilot")
        regression = by_split.get("regression")
        if not isinstance(pilot, dict) or not isinstance(regression, dict):
            raise P5GateErrorV2("Core B pilot/regression evidence missing")
        checks.update(
            {
                "core_pilot_18_of_18_pass": pilot.get("case_count") == 18
                and pilot.get("task_success_count") == 18,
                "core_regression_72_of_72_pass": regression.get("case_count") == 72
                and regression.get("task_success_count") == 72,
            }
        )
    return checks


def _secret_scan(paths: list[Path]) -> dict[str, Any]:
    matches = 0
    scanned = 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise P5GateErrorV2(f"secret scan unreadable: {path.name}") from exc
        scanned += 1
        matches += sum(bool(pattern.search(text)) for pattern in _SECRET_PATTERNS)
    return {
        "status": "PASS" if matches == 0 else "REJECT",
        "artifact_count": scanned,
        "match_count": matches,
    }


def _validate_blind_aggregate(value: dict[str, Any]) -> None:
    allowed = {
        "schema_version",
        "status",
        "decision",
        "evidence_class",
        "truth_provenance",
        "human_evidence",
        "minimum_bucket_size",
        "bindings",
        "case_count",
        "terminal_count",
        "variant_metrics",
        "core_gate_checks",
        "automated_proxy_judge",
        "live_provider_evidence",
        "public_e2e_evidence",
        *_COMMITMENT_CHAIN_FIELDS,
    }
    variants = value.get("variant_metrics")
    if set(value) != allowed or not isinstance(variants, dict) or set(variants) != {
        "legacy_a",
        "core_b",
        "solver_c",
    }:
        raise P5GateErrorV2("blind score aggregate allowlist rejected")
    sections = {
        "overall",
        "by_city",
        "by_input_kind",
        "by_difficulty",
        "by_fault_profile",
        "by_finding",
        "by_repair_outcome",
    }
    for metrics in variants.values():
        if not isinstance(metrics, dict) or set(metrics) != sections:
            raise P5GateErrorV2("blind score variant aggregate rejected")
        overall = metrics.get("overall")
        if not isinstance(overall, dict) or overall.get("case_count") != 90:
            raise P5GateErrorV2("blind score overall aggregate rejected")
        for section in sections - {"overall"}:
            buckets = metrics[section]
            if not isinstance(buckets, dict) or any(
                not isinstance(bucket, dict) or bucket.get("case_count", 0) < 5
                for bucket in buckets.values()
            ):
                raise P5GateErrorV2("blind score reversible bucket rejected")
    core = variants["core_b"]["overall"]
    expected_checks = {
        "mean_score_gte_88": core.get("mean_score", 0) >= 88,
        "deterministic_failure_zero": core.get("deterministic_failure_count") == 0,
        "wrong_city_or_poi_zero": core.get("wrong_city_or_poi_count") == 0,
        "hard_finding_miss_zero": core.get("hard_finding_miss_count") == 0,
        "unknown_failure_zero": core.get("unknown_failure_count") == 0,
        "candidate_receipt_failure_zero": core.get("candidate_receipt_failure_count") == 0,
        "concurrency_failure_zero": core.get("concurrency_failure_count") == 0,
        "postcheck_failure_zero": core.get("postcheck_failure_count") == 0,
        "replay_failure_zero": core.get("replay_failure_count") == 0,
    }
    passed = all(expected_checks.values())
    if (
        value.get("core_gate_checks") != expected_checks
        or value.get("status") != ("PASS" if passed else "REJECT")
        or value.get("decision")
        != ("ACCEPT_BLIND_SCORE" if passed else "REJECT")
    ):
        raise P5GateErrorV2("blind score deterministic aggregate mismatch")
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if "p5.blind." in serialized or '"case_id"' in serialized:
        raise P5GateErrorV2("blind score case detail leak rejected")


def _validate_judge_panel_allowlist(value: dict[str, Any]) -> None:
    if set(value) != {
        "schema_version",
        "status",
        "evidence_class",
        "human_calibration_performed",
        "round_count",
        "candidate_count",
        "agreement_threshold",
        "verdict_agreement_rate",
        "per_dimension_agreement_rate",
        "variant_metrics",
        "provenance",
        "mapping_sha256",
        "run_group_manifest_hash",
        "terminal_outputs_content_sha256",
        "deterministic_scorer_priority",
        "judge_may_override_deterministic_failure",
        "unsupported_claim_candidate_count",
        "report_hash",
    }:
        raise P5GateErrorV2("Judge panel aggregate allowlist rejected")
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if "p5.blind." in serialized or '"case_id"' in serialized:
        raise P5GateErrorV2("Judge panel case detail leak rejected")
    provenance = value.get("provenance")
    dimensions = value.get("per_dimension_agreement_rate")
    variants = value.get("variant_metrics")
    if (
        not isinstance(provenance, list)
        or len(provenance) != 3
        or not all(isinstance(item, dict) for item in provenance)
        or [item.get("round_index") for item in provenance] != [1, 2, 3]
        or any(
            item.get("api_usage_count") != 0
            or item.get("tool_usage_count") != 0
            or item.get("terminal_outputs_content_sha256")
            != value.get("terminal_outputs_content_sha256")
            for item in provenance
        )
        or any(
            len({item.get(key) for item in provenance}) != 3
            for key in ("evaluator_id", "agent_task_id", "agent_id")
        )
        or not isinstance(dimensions, dict)
        or set(dimensions)
        != {"clarity", "actionability", "evidence_boundary_expression"}
        or any(not isinstance(score, (int, float)) for score in dimensions.values())
        or not isinstance(variants, dict)
        or set(variants) != {"legacy_a", "core_b", "solver_c"}
        or value.get("unsupported_claim_candidate_count") != 0
    ):
        raise P5GateErrorV2("Judge panel provenance or coverage rejected")
    passed = value.get("verdict_agreement_rate", 0) >= value.get(
        "agreement_threshold", 1
    ) and all(
        score >= value.get("agreement_threshold", 1) for score in dimensions.values()
    )
    if value.get("status") != ("PASS" if passed else "BLOCKED"):
        raise P5GateErrorV2("Judge panel agreement status mismatch")


def build_p5_gate_manifest_v2(
    *,
    repo_root: Path,
    nonblind_run_dir: Path,
    nonblind_score_path: Path,
    blind_run_dir: Path,
    blind_score_path: Path,
    judge_panel_path: Path,
    formal_validation_receipt_path: Path,
    output_path: Path,
    require_current_subject: bool = True,
) -> dict[str, Any]:
    root = repo_root.resolve()
    p5_root = root / "backend" / "evals" / "trip_check_v1" / "p5"
    dataset_path = p5_root / "dataset_v2.manifest.json"
    nonblind_cases = p5_root / "cases_nonblind_v2.jsonl"
    nonblind_materializations = p5_root / "materializations_nonblind_v2.jsonl"
    blind_cases = p5_root / "frozen_blind.v2.inputs.jsonl"
    blind_materializations = p5_root / "frozen_blind.v2.materializations.jsonl"
    active_contract_path = p5_root / "active_contract.json"
    blind_seal_path = p5_root / "sealed" / "frozen_blind.v2.seal.json"
    run_spec_path = p5_root / "run_spec_template_v2.json"
    rubric_path = p5_root / "judge_rubric_v2.json"
    seal_schema_path = p5_root / "blind_seal_v2.schema.json"
    formal_validation_schema_path = (
        p5_root / "dataset_formal_validation_receipt_v2.schema.json"
    )
    p4_path = (
        root
        / "backend"
        / "evidence"
        / "trip_check_v1"
        / "p4"
        / "p4_gate_manifest.json"
    )

    dataset = _load_json(dataset_path, "P5 v2 dataset manifest")
    _validate_hash(dataset, "manifest_hash", "P5 v2 dataset manifest")
    counts = dataset.get("counts", {})
    if (
        dataset.get("schema_version") != "trip-check-p5-dataset-manifest-v2"
        or counts.get("total") != 360
        or counts.get("by_split")
        != {"pilot": 18, "dev": 180, "regression": 72, "frozen_blind": 90}
        or counts.get("by_city") != {"北京": 120, "上海": 120, "杭州": 120}
        or dataset.get("frozen") is not True
        or dataset.get("generation", {}).get("formal_validation_eligible") is not True
        or dataset.get("generation", {}).get("ocr_mode") != "actual"
    ):
        raise P5GateErrorV2("P5 v2 frozen dataset contract rejected")
    active_contract = _load_json(active_contract_path, "P5 v2 active contract")
    blind_seal = _load_json(blind_seal_path, "P5 v2 blind seal")
    sealing_commitment = dataset.get("sealing_commitment")
    if (
        active_contract.get("active_contract") != "trip-check-p5-v2"
        or active_contract.get("formal_evidence_status") != "READY"
        or active_contract.get("dataset_manifest_hash") != dataset["manifest_hash"]
        or not isinstance(sealing_commitment, dict)
        or sealing_commitment.get("status") != "SEALED"
        or sealing_commitment.get("blind_seal_v2_sha256")
        != _sha256(blind_seal_path)
        or active_contract.get("blind_seal_v2_sha256") != _sha256(blind_seal_path)
    ):
        raise P5GateErrorV2("P5 v2 active seal binding rejected")
    expected_schema_contract = schema_contract_sha256_v2(root)
    if (
        blind_seal.get("schema_version") != "trip-check-p5-blind-seal-v2"
        or blind_seal.get("schema_contract_sha256") != expected_schema_contract
        or blind_seal.get("run_spec_template_sha256") != _sha256(run_spec_path)
        or blind_seal.get("rubric_sha256") != _sha256(rubric_path)
        or dataset.get("contract_hashes", {}).get("run_spec_template_sha256")
        != _sha256(run_spec_path)
        or dataset.get("contract_hashes", {}).get("judge_rubric_sha256")
        != _sha256(rubric_path)
        or any(
            sealing_commitment.get(field) != blind_seal.get(field)
            for field in (
                "external_bundle_sha256",
                "labels_canonical_sha256",
                "review_receipt_sha256",
            )
        )
    ):
        raise P5GateErrorV2("P5 v2 contract or external commitment binding rejected")
    nonblind_run, _, nonblind_outputs = validate_run_group_v2(
        run_dir=nonblind_run_dir.resolve(),
        cases_path=nonblind_cases,
        materializations_path=nonblind_materializations,
        dataset_manifest_path=dataset_path,
        expected_lane="nonblind",
        require_formal=True,
    )
    blind_run, _, blind_outputs = validate_run_group_v2(
        run_dir=blind_run_dir.resolve(),
        cases_path=blind_cases,
        materializations_path=blind_materializations,
        dataset_manifest_path=dataset_path,
        expected_lane="frozen_blind",
        require_formal=True,
    )
    if len(nonblind_outputs) != 810 or len(blind_outputs) != 270:
        raise P5GateErrorV2("P5 v2 exact terminal count rejected")
    if nonblind_run["subject_commit"] != blind_run["subject_commit"]:
        raise P5GateErrorV2("run-group subject commits differ")
    subject_commit = nonblind_run["subject_commit"]
    (
        formal_validation_receipt_path,
        _,
        formal_validation_receipt_bytes,
    ) = _validate_formal_validation_receipt(
        repo_root=root,
        receipt_path=formal_validation_receipt_path,
        schema_path=formal_validation_schema_path,
        dataset_path=dataset_path,
        dataset=dataset,
        subject_commit=subject_commit,
    )
    expected_commitment_chain = {
        "active_contract_file_sha256": _sha256(active_contract_path),
        "blind_seal_sha256": _sha256(blind_seal_path),
        "external_bundle_sha256": blind_seal["external_bundle_sha256"],
        "labels_canonical_sha256": blind_seal["labels_canonical_sha256"],
        "review_receipt_sha256": blind_seal["review_receipt_sha256"],
    }
    if _commitment_chain(blind_run, "blind run") != expected_commitment_chain:
        raise P5GateErrorV2("blind run commitment chain mismatch")
    if require_current_subject:
        head, dirty = _repo_state(root)
        if dirty or head != subject_commit:
            raise P5GateErrorV2("current repository does not match clean P5 v2 subject")

    nonblind_score = _load_json(nonblind_score_path.resolve(), "non-blind score")
    _validate_hash(nonblind_score, "report_hash", "non-blind score")
    if (
        nonblind_score.get("schema_version")
        != "trip-check-p5-nonblind-score-report-v2"
        or nonblind_score.get("subject_commit") != subject_commit
        or nonblind_score.get("dataset_manifest_hash") != dataset["manifest_hash"]
        or nonblind_score.get("run_group_manifest_hash") != nonblind_run["manifest_hash"]
        or nonblind_score.get("case_count") != 270
        or nonblind_score.get("terminal_count") != 810
        or nonblind_score.get("solver_admission_inherited") != "REJECT"
        or nonblind_score.get("solver_may_promote_from_p5_score") is not False
        or nonblind_score.get("live_provider_evidence") is not False
        or nonblind_score.get("public_e2e_evidence") is not False
        or nonblind_score.get("human_evidence") is not False
    ):
        raise P5GateErrorV2("non-blind score binding rejected")

    blind_score = _load_json(blind_score_path.resolve(), "blind score")
    _validate_blind_aggregate(blind_score)
    if _commitment_chain(blind_score, "blind aggregate") != expected_commitment_chain:
        raise P5GateErrorV2("blind aggregate commitment chain mismatch")
    blind_bindings = blind_score.get("bindings", {})
    if (
        blind_score.get("schema_version") != "trip-check-p5-isolated-blind-score-v2"
        or blind_bindings.get("subject_commit") != subject_commit
        or blind_bindings.get("dataset_manifest_hash") != dataset["manifest_hash"]
        or blind_bindings.get("run_group_manifest_hash") != blind_run["manifest_hash"]
        or blind_bindings.get("terminal_outputs_file_sha256")
        != blind_run["terminal_outputs_file_sha256"]
        or blind_score.get("case_count") != 90
        or blind_score.get("terminal_count") != 270
        or blind_score.get("minimum_bucket_size", 0) < 5
        or blind_score.get("human_evidence") is not False
        or blind_score.get("live_provider_evidence") is not False
        or blind_score.get("public_e2e_evidence") is not False
    ):
        raise P5GateErrorV2("blind score binding rejected")

    judge = _load_json(judge_panel_path.resolve(), "Judge panel")
    _validate_judge_panel_allowlist(judge)
    _validate_hash(judge, "report_hash", "Judge panel")
    if (
        judge.get("schema_version") != "trip-check-p5-judge-panel-v2"
        or judge.get("run_group_manifest_hash") != blind_run["manifest_hash"]
        or judge.get("terminal_outputs_content_sha256")
        != blind_run["terminal_outputs_content_sha256"]
        or judge.get("round_count") != 3
        or judge.get("candidate_count") != 270
        or judge.get("agreement_threshold") != 0.85
        or judge.get("human_calibration_performed") is not False
        or judge.get("deterministic_scorer_priority") is not True
        or judge.get("judge_may_override_deterministic_failure") is not False
    ):
        raise P5GateErrorV2("Judge panel binding rejected")

    p4 = _load_json(p4_path, "P4 gate")
    _validate_hash(p4, "manifest_hash", "P4 gate")
    p4_solver = p4.get("solver_admission", {})
    if (
        p4.get("subject_commit") != _P4_SUBJECT_COMMIT
        or p4.get("status") != "PASS"
        or p4.get("p4_phase_status") != "PASS"
        or p4_solver.get("status") != "REJECT"
        or p4_solver.get("default_strategy") != "bounded_repair_v1"
    ):
        raise P5GateErrorV2("P4 solver admission inheritance rejected")

    scanned_paths = [
        dataset_path,
        active_contract_path,
        blind_seal_path,
        seal_schema_path,
        run_spec_path,
        rubric_path,
        formal_validation_schema_path,
        formal_validation_receipt_path,
        nonblind_run_dir / "run_group_manifest.json",
        nonblind_run_dir / nonblind_run["terminal_outputs_path"],
        nonblind_score_path.resolve(),
        blind_run_dir / "run_group_manifest.json",
        blind_run_dir / blind_run["terminal_outputs_path"],
        blind_score_path.resolve(),
        judge_panel_path.resolve(),
        p4_path,
    ]
    secret_scan = _secret_scan(scanned_paths)
    nonblind_acceptance = _acceptance_c_checks(
        nonblind_score,
        require_nonblind_splits=True,
    )
    blind_acceptance = _acceptance_c_checks(
        blind_score,
        require_nonblind_splits=False,
    )
    deterministic_pass = (
        nonblind_score.get("status") == "PASS"
        and blind_score.get("status") == "PASS"
    )
    semantic_pass = judge.get("status") == "PASS"
    checks = {
        "dataset_contract": True,
        "exact_1080_terminal_outputs": len(nonblind_outputs) + len(blind_outputs)
        == 1080,
        "nonblind_deterministic_gate": nonblind_score.get("status") == "PASS",
        "blind_deterministic_gate": blind_score.get("status") == "PASS",
        "judge_semantic_gate": semantic_pass,
        "judge_agreement_gte_85": judge.get("verdict_agreement_rate", 0) >= 0.85
        and all(
            value >= 0.85
            for value in judge.get("per_dimension_agreement_rate", {}).values()
        )
        and len(judge.get("per_dimension_agreement_rate", {})) == 3,
        "p4_phase_pass": True,
        "cp_sat_admission_remains_reject": True,
        "secret_scan": secret_scan["status"] == "PASS",
        "same_subject_commit": True,
        **{f"nonblind_{key}": value for key, value in nonblind_acceptance.items()},
        **{f"blind_{key}": value for key, value in blind_acceptance.items()},
        "judge_unsupported_claim_candidate_count_zero": judge.get(
            "unsupported_claim_candidate_count"
        )
        == 0,
    }
    gate_passed = deterministic_pass and all(checks.values())
    artifacts = [
        _artifact("dataset_manifest", dataset_path, root),
        _artifact(
            "formal_dataset_validation_receipt_schema",
            formal_validation_schema_path,
            root,
        ),
        _artifact_from_bytes(
            "formal_dataset_validation_receipt",
            formal_validation_receipt_path,
            root,
            formal_validation_receipt_bytes,
        ),
        _artifact("active_contract", active_contract_path, root),
        _artifact("blind_seal_v2", blind_seal_path, root),
        _artifact("blind_seal_schema_contract", seal_schema_path, root),
        _commitment_artifact("schema_contract_commitment", expected_schema_contract),
        _artifact("run_spec_template_v2", run_spec_path, root),
        _artifact("judge_rubric_v2", rubric_path, root),
        _commitment_artifact(
            "external_blind_bundle_commitment",
            expected_commitment_chain["external_bundle_sha256"],
        ),
        _commitment_artifact(
            "external_blind_review_receipt_commitment",
            expected_commitment_chain["review_receipt_sha256"],
        ),
        _commitment_artifact(
            "external_labels_canonical_commitment",
            expected_commitment_chain["labels_canonical_sha256"],
        ),
        _artifact("p4_gate_manifest", p4_path, root),
        _artifact(
            "nonblind_run_manifest", nonblind_run_dir / "run_group_manifest.json", root
        ),
        _artifact(
            "nonblind_terminal_outputs",
            nonblind_run_dir / nonblind_run["terminal_outputs_path"],
            root,
        ),
        _artifact("nonblind_score", nonblind_score_path, root),
        _artifact("blind_run_manifest", blind_run_dir / "run_group_manifest.json", root),
        _artifact(
            "blind_terminal_outputs",
            blind_run_dir / blind_run["terminal_outputs_path"],
            root,
        ),
        _artifact("blind_aggregate_score", blind_score_path, root),
        _artifact("judge_panel", judge_panel_path, root),
    ]
    manifest = {
        "schema_version": "trip-check-p5-gate-manifest-v2",
        "goal_id": "TC-P5-G01-evaluation-ablation",
        "status": "PASS" if gate_passed else "REJECT",
        "subject_commit": subject_commit,
        "dirty_tree": False,
        "dataset_manifest_hash": dataset["manifest_hash"],
        "counts": {
            "cases": 360,
            "nonblind_cases": 270,
            "blind_cases": 90,
            "variants": 3,
            "nonblind_terminal_outputs": 810,
            "blind_terminal_outputs": 270,
            "total_terminal_outputs": 1080,
            "judge_rounds": 3,
            "judge_items_per_round": 270,
        },
        "checks": checks,
        "failure_priority": [
            "NONBLIND_DETERMINISTIC",
            "BLIND_DETERMINISTIC",
            "JUDGE_SEMANTIC",
        ],
        "promotion_decision": "KEEP_CORE_B" if gate_passed else "REJECT_ALL_CANDIDATES",
        "default_runtime_strategy": "bounded_repair_v1",
        "solver_admission": {
            "inherited_from_p4_subject_commit": p4["subject_commit"],
            "status": "REJECT",
            "may_be_overridden_by_p5_score": False,
        },
        "evidence_boundaries": {
            "controlled_fixture": "EVALUATED" if gate_passed else "REJECT",
            "automated_proxy_judge": judge.get("status", "BLOCKED"),
            "live_provider": "NOT_RUN",
            "public_e2e": "NOT_RUN",
            "human_evidence": "NOT_RUN",
            "release": "NOT_RUN",
            "p6_candidate_gate": "REJECT",
        },
        "secret_scan": secret_scan,
        "artifact_index": artifacts,
    }
    manifest["manifest_hash"] = digest(manifest)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output_path.write_text(payload, encoding="utf-8", newline="\n")
    readback = _load_json(output_path, "written P5 v2 gate manifest")
    if readback != manifest or output_path.read_text(encoding="utf-8") != payload:
        raise P5GateErrorV2("P5 v2 gate manifest readback failed")
    return manifest


__all__ = ["P5GateErrorV2", "build_p5_gate_manifest_v2"]
