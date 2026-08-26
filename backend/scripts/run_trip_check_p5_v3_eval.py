"""Run the receipt-bound P5 v3 non-blind evaluation and deterministic replay."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p5.adapters_v3 import (  # noqa: E402
    ADAPTERS_V3,
    ADAPTER_VERSIONS_V3,
    EvaluationCachingPaddleOcrEngineV3,
    validate_materialization_v3,
)
from evals.trip_check_v1.p5.contracts_v3 import (  # noqa: E402
    P5ArtifactIndexEntryV3,
    P5ArtifactIndexV3,
    P5CaseResultV3,
    P5CaseV3,
    P5FailureRecordV3,
    P5VariantRunSpecV3,
)
from evals.trip_check_v1.p5.data_contract import (  # noqa: E402
    digest,
    file_sha256,
    load_jsonl,
)
from evals.trip_check_v1.p5.data_contract_v2 import JUDGE_RUBRIC_PATH_V2  # noqa: E402
from evals.trip_check_v1.p5.data_contract_v3 import (  # noqa: E402
    ACTIVE_CONTRACT_PATH,
    BLIND_INPUT_PATH_V3,
    BLIND_MATERIALIZATIONS_PATH_V3,
    BLIND_SEAL_PATH_V3,
    MANIFEST_PATH_V3,
    NONBLIND_MATERIALIZATIONS_PATH_V3,
    NONBLIND_PATH_V3,
    RUN_SPEC_TEMPLATE_PATH_V3,
    case_set_hash_v3,
    materialization_set_hash_v3,
)
from evals.trip_check_v1.p5.runner_v3 import (  # noqa: E402
    build_case_result_v3,
    build_failure_record_v3,
    execute_terminal_v3,
    validate_exact_terminal_set_v3,
    validate_run_spec_whitelist_v3,
    write_models_jsonl_atomic_v3,
)


VARIANT_ALIASES = {
    "legacy": "legacy_a",
    "legacy_a": "legacy_a",
    "core": "core_b",
    "core_b": "core_b",
    "solver": "solver_c",
    "solver_c": "solver_c",
}
DEFAULTS = {
    "nonblind": (NONBLIND_PATH_V3, NONBLIND_MATERIALIZATIONS_PATH_V3, 270),
    "frozen_blind": (BLIND_INPUT_PATH_V3, BLIND_MATERIALIZATIONS_PATH_V3, 90),
}


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=REPO_ROOT, text=True, encoding="utf-8"
    ).strip()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path.name}")
    return value


def _variant_ids(raw: str) -> list[str]:
    values: list[str] = []
    for item in raw.split(","):
        key = item.strip()
        if key not in VARIANT_ALIASES:
            raise ValueError(f"unknown P5 v3 variant: {key}")
        value = VARIANT_ALIASES[key]
        if value not in values:
            values.append(value)
    return values


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _git_is_ancestor(candidate: str, subject: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", candidate, subject],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _validate_manifest_and_lane(
    *,
    lane: str,
    manifest: Mapping[str, Any],
    cases_path: Path,
    materializations_path: Path,
    cases_raw: list[dict[str, Any]],
    materializations: list[dict[str, Any]],
    template_path: Path,
    rubric_path: Path,
    complete_lane: bool,
) -> tuple[str, str]:
    if (
        manifest.get("schema_version") != "trip-check-p5-dataset-manifest-v3"
        or manifest.get("dataset_id") != "trip-check-p5-360-v3"
        or manifest.get("manifest_hash")
        != digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
    ):
        raise ValueError("P5 v3 dataset manifest is invalid")
    contract_hashes = manifest.get("contract_hashes")
    if not isinstance(contract_hashes, Mapping):
        raise ValueError("P5 v3 manifest contract hashes are missing")
    if contract_hashes.get("run_spec_template_sha256") != file_sha256(template_path):
        raise ValueError("P5 v3 RunSpec template differs from dataset manifest")
    if contract_hashes.get("judge_rubric_sha256") != file_sha256(rubric_path):
        raise ValueError("P5 v3 rubric differs from dataset manifest")
    if not complete_lane:
        return case_set_hash_v3(cases_raw), materialization_set_hash_v3(materializations)

    file_keys = (
        ("nonblind_cases", "nonblind_materializations")
        if lane == "nonblind"
        else ("blind_cases", "blind_materializations")
    )
    files = manifest.get("files")
    lane_manifest = manifest.get("lanes", {}).get(lane)
    if not isinstance(files, Mapping) or not isinstance(lane_manifest, Mapping):
        raise ValueError("P5 v3 lane manifest is missing")
    cases_entry = files.get(file_keys[0])
    materializations_entry = files.get(file_keys[1])
    if not isinstance(cases_entry, Mapping) or not isinstance(
        materializations_entry, Mapping
    ):
        raise ValueError("P5 v3 lane file bindings are missing")
    for path, rows, entry in (
        (cases_path, cases_raw, cases_entry),
        (materializations_path, materializations, materializations_entry),
    ):
        if (
            entry.get("file_sha256") != file_sha256(path)
            or entry.get("content_sha256") != digest(rows)
            or entry.get("row_count") != len(rows)
        ):
            raise ValueError(f"P5 v3 lane file readback mismatch: {path.name}")
    cases_hash = case_set_hash_v3(cases_raw)
    materializations_hash = materialization_set_hash_v3(materializations)
    if (
        lane_manifest.get("case_count") != len(cases_raw)
        or lane_manifest.get("materialization_count") != len(materializations)
        or lane_manifest.get("case_set_hash") != cases_hash
        or lane_manifest.get("materialization_set_hash") != materializations_hash
    ):
        raise ValueError("P5 v3 lane set commitment mismatch")
    return cases_hash, materializations_hash


def _require_v3_active_ready(
    *, active_path: Path, manifest: Mapping[str, Any], subject_commit: str
) -> dict[str, str]:
    if not BLIND_SEAL_PATH_V3.is_file():
        raise RuntimeError("P5_V3_FORMAL_CONTRACT_NOT_READY: blind seal missing")
    active = _load_json(active_path)
    commitment = manifest.get("sealing_commitment")
    if not isinstance(commitment, Mapping):
        raise RuntimeError("P5_V3_FORMAL_CONTRACT_NOT_READY: commitment missing")
    if (
        active.get("schema_version") != "trip-check-p5-active-contract-v1"
        or active.get("active_contract") != "trip-check-p5-v3"
        or active.get("formal_evidence_status") != "READY"
        or active.get("dataset_manifest_hash") != manifest.get("manifest_hash")
        or active.get("blind_seal_v3_sha256") != file_sha256(BLIND_SEAL_PATH_V3)
        or manifest.get("frozen") is not True
        or manifest.get("formal_validation_eligible") is not True
        or manifest.get("seal_status") != "SEALED"
    ):
        raise RuntimeError("P5_V3_FORMAL_CONTRACT_NOT_READY")
    candidate_commit = str(commitment.get("candidate_freeze_commit", ""))
    if (
        active.get("candidate_freeze_commit") != candidate_commit
        or not _git_is_ancestor(candidate_commit, subject_commit)
    ):
        raise RuntimeError("P5_V3_FORMAL_CONTRACT_CANDIDATE_MISMATCH")
    return {
        "active_contract_file_sha256": file_sha256(active_path),
        "blind_seal_file_sha256": file_sha256(BLIND_SEAL_PATH_V3),
        "candidate_freeze_commit": candidate_commit,
    }


def _run_spec(
    *,
    lane: str,
    subject_commit: str,
    dirty_tree: bool,
    manifest_hash: str,
    cases_hash: str,
    materializations_hash: str,
    template: Mapping[str, Any],
    template_hash: str,
    rubric_hash: str,
    variant_id: str,
) -> P5VariantRunSpecV3:
    adapter_version, repair_strategy = ADAPTER_VERSIONS_V3[variant_id]
    configured = template.get("variant_specs", {}).get(variant_id)
    if configured != {
        "adapter_version": adapter_version,
        "repair_strategy": repair_strategy,
    }:
        raise ValueError("P5 v3 RunSpec template differs from adapter contract")
    historical_ocr = template.get("historical_ocr_evidence")
    if not isinstance(historical_ocr, Mapping):
        raise ValueError("P5 v3 historical OCR policy is missing")
    return P5VariantRunSpecV3(
        subject_commit=subject_commit,
        dirty_tree=dirty_tree,
        lane=lane,
        dataset_manifest_hash=manifest_hash,
        case_set_hash=cases_hash,
        materialization_set_hash=materializations_hash,
        run_spec_template_hash=template_hash,
        rubric_hash=rubric_hash,
        renderer_version=str(template["renderer"]["version"]),
        ocr_engine_version=str(historical_ocr["engine_version"]),
        evidence_policy_version=str(template["evidence_policy_version"]),
        fault_registry_version=str(template["fault_registry_version"]),
        random_seed=int(template["random_seed"]),
        budget=dict(template["budget"]),
        replay_hash_policy=str(template["replay_hash_policy"]),
        variant_id=variant_id,
        adapter_version=adapter_version,
        repair_strategy=repair_strategy,
    )


def _validate_terminal_ocr_receipts(
    outputs: list[Any],
    *,
    expected_product_replays: int,
    materialization_by_case: Mapping[str, Mapping[str, Any]],
) -> dict[str, int]:
    replay_receipts = []
    legacy_cache_access = 0
    confirmation_lines = 0
    unique_confirmation_lines: set[tuple[str, int]] = set()
    for terminal in outputs:
        matching = [
            receipt
            for receipt in terminal.receipts
            if isinstance(receipt, Mapping)
            and receipt.get("type") == "ocr_replay_provenance"
        ]
        if terminal.input_kind == "SYNTHETIC_SCREENSHOT" and terminal.variant_id in {
            "core_b",
            "solver_c",
        }:
            if len(matching) != 1:
                raise ValueError("P5 v3 screenshot terminal lacks adjacent OCR provenance")
            ocr_receipts = [
                receipt
                for receipt in terminal.receipts
                if isinstance(receipt, Mapping) and receipt.get("type") == "ocr"
            ]
            expected_ocr = materialization_by_case[terminal.case_id].get(
                "ocr_baseline_receipt"
            )
            if (
                len(ocr_receipts) != 1
                or not isinstance(expected_ocr, Mapping)
                or ocr_receipts[0].get("lines") != expected_ocr.get("lines")
            ):
                raise ValueError(
                    "P5 v3 terminal does not preserve frozen OCR lines and confirmation flags"
                )
            for index, line in enumerate(ocr_receipts[0]["lines"]):
                if line.get("requires_confirmation") is True:
                    confirmation_lines += 1
                    unique_confirmation_lines.add((terminal.case_id, index))
            receipt = matching[0]
            if (
                receipt.get("mode") != "FROZEN_ACTUAL_OCR_RECEIPT_REPLAY"
                or receipt.get("fresh_model_inference") is not False
                or receipt.get("receipt_match") is not True
                or receipt.get("cleanup_status") != "DELETED"
                or receipt.get("cleanup_error_category") is not None
                or receipt.get("temporary_original_absent") is not True
                or receipt.get("materialization_hash") != terminal.materialization_hash
            ):
                raise ValueError("P5 v3 screenshot terminal OCR provenance is invalid")
            replay_receipts.append(receipt)
        elif terminal.variant_id == "legacy_a" and matching:
            legacy_cache_access += len(matching)
    if len(replay_receipts) != expected_product_replays:
        raise ValueError("P5 v3 OCR terminal provenance count mismatch")
    if legacy_cache_access:
        raise ValueError("P5 v3 Legacy adapter accessed the OCR cache")
    return {
        "terminal_provenance_count": len(replay_receipts),
        "legacy_cache_access_count": legacy_cache_access,
        "low_confidence_confirmation_line_count": confirmation_lines,
        "unique_source_low_confidence_line_count": len(unique_confirmation_lines),
    }


def _artifact_entry(path: Path, *, generated_at: datetime) -> P5ArtifactIndexEntryV3:
    return P5ArtifactIndexEntryV3(
        path=path.name,
        byte_size=path.stat().st_size,
        sha256=file_sha256(path),
        generated_by="scripts.run_trip_check_p5_v3_eval",
        generated_at=generated_at,
    )


async def execute_run(args: argparse.Namespace) -> dict[str, Any]:
    lane = args.lane.replace("-", "_")
    default_cases, default_materializations, formal_count = DEFAULTS[lane]
    cases_path = Path(args.cases_file).resolve() if args.cases_file else default_cases
    materializations_path = (
        Path(args.materializations_file).resolve()
        if args.materializations_file
        else default_materializations
    )
    manifest_path = (
        Path(args.dataset_manifest).resolve()
        if args.dataset_manifest
        else MANIFEST_PATH_V3
    )
    template_path = (
        Path(args.run_spec_template).resolve()
        if args.run_spec_template
        else RUN_SPEC_TEMPLATE_PATH_V3
    )
    rubric_path = Path(args.rubric).resolve() if args.rubric else JUDGE_RUBRIC_PATH_V2
    active_path = (
        Path(args.active_contract).resolve()
        if args.active_contract
        else ACTIVE_CONTRACT_PATH
    )
    manifest = _load_json(manifest_path)
    template = _load_json(template_path)
    if lane == "frozen_blind":
        # Before a v3 READY contract this fails here.  After READY, a separate
        # isolated blind-run slice must explicitly remove the second boundary.
        _require_v3_active_ready(
            active_path=active_path,
            manifest=manifest,
            subject_commit=_git("rev-parse", "HEAD"),
        )
        raise RuntimeError("P5_V3_BLIND_RUNNER_NOT_ENABLED_IN_NONBLIND_SLICE")

    cases_raw = load_jsonl(cases_path)
    materializations = load_jsonl(materializations_path)
    cases = [P5CaseV3.model_validate(row) for row in cases_raw]
    if any(case.split == "frozen_blind" for case in cases):
        raise ValueError("P5 v3 nonblind lane contains a frozen-blind case")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("P5 v3 case IDs must be unique")
    if len({row.get("case_id") for row in materializations}) != len(materializations):
        raise ValueError("P5 v3 materialization case IDs must be unique")
    materialization_by_case = {str(row["case_id"]): row for row in materializations}
    if set(materialization_by_case) != {case.case_id for case in cases}:
        raise ValueError("P5 v3 cases/materializations are not one-to-one")
    for case in cases:
        validate_materialization_v3(case, materialization_by_case[case.case_id])

    complete_lane = args.limit is None and not args.case_id
    cases_hash, materializations_hash = _validate_manifest_and_lane(
        lane=lane,
        manifest=manifest,
        cases_path=cases_path,
        materializations_path=materializations_path,
        cases_raw=cases_raw,
        materializations=materializations,
        template_path=template_path,
        rubric_path=rubric_path,
        complete_lane=complete_lane,
    )
    if args.case_id:
        requested = set(args.case_id)
        cases = [case for case in cases if case.case_id in requested]
        missing = requested - {case.case_id for case in cases}
        if missing:
            raise ValueError(f"unknown P5 v3 case IDs: {sorted(missing)}")
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        raise ValueError("selected P5 v3 case set is empty")
    selected_materializations = [materialization_by_case[case.case_id] for case in cases]
    if not complete_lane:
        selected_raw = [case.model_dump(mode="json", exclude_none=True) for case in cases]
        cases_hash = case_set_hash_v3(selected_raw)
        materializations_hash = materialization_set_hash_v3(selected_materializations)

    variants = _variant_ids(args.variants)
    subject_commit = _git("rev-parse", "HEAD")
    dirty_tree = bool(_git("status", "--short"))
    if dirty_tree and not args.allow_dirty:
        raise RuntimeError("working tree is dirty; --allow-dirty is development-only")
    formal_shape = (
        len(cases) == formal_count
        and variants == ["legacy_a", "core_b", "solver_c"]
    )
    if args.require_formal and (
        dirty_tree or not complete_lane or not formal_shape or not args.replay
    ):
        raise RuntimeError(
            "formal P5 v3 runs require clean exact nonblind x three variants with replay"
        )
    commitments = {
        "active_contract_file_sha256": "NOT_APPLICABLE",
        "blind_seal_file_sha256": "NOT_APPLICABLE",
        "candidate_freeze_commit": "NOT_APPLICABLE",
    }
    if args.require_formal:
        commitments = _require_v3_active_ready(
            active_path=active_path,
            manifest=manifest,
            subject_commit=subject_commit,
        )

    if template.get("allowed_variant_differences") != [
        "variant_id",
        "adapter_version",
        "repair_strategy",
    ]:
        raise ValueError("P5 v3 RunSpec variant whitelist mismatch")
    if template.get("replay_hash_policy") != "p5-semantic-projection-v3":
        raise ValueError("P5 v3 replay hash policy mismatch")
    if template.get("execution_mode") != "controlled_snapshot":
        raise ValueError("P5 v3 runner forbids non-snapshot execution")

    shared_ocr = EvaluationCachingPaddleOcrEngineV3()
    screenshot_image_hashes: set[str] = set()
    if set(variants) & {"core_b", "solver_c"}:
        for case in cases:
            materialization = validate_materialization_v3(
                case, materialization_by_case[case.case_id]
            )
            receipt = materialization.get("ocr_baseline_receipt")
            render = materialization.get("render_receipt")
            if case.input_kind == "SYNTHETIC_SCREENSHOT":
                if not isinstance(receipt, Mapping) or not isinstance(render, Mapping):
                    raise ValueError("P5 v3 screenshot lacks frozen OCR/render receipt")
                if receipt.get("asset_hash") != render.get("image_sha256"):
                    raise ValueError("P5 v3 OCR/render image hash conflict")
                shared_ocr.preload(receipt)
                screenshot_image_hashes.add(str(receipt["asset_hash"]))
            elif receipt is not None or render is not None:
                raise ValueError("P5 v3 text case unexpectedly binds screenshot receipts")

    output_root = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else BACKEND_ROOT / "evidence" / "trip_check_v1" / "p5_v3"
    )
    run_id = args.run_id or datetime.now(timezone.utc).strftime(
        "p5-v3-%Y%m%dT%H%M%SZ"
    )
    run_dir = output_root / run_id
    if run_dir.exists():
        raise FileExistsError("P5 v3 run directory already exists")
    run_dir.mkdir(parents=True)

    specs: list[P5VariantRunSpecV3] = []
    specs_by_variant: dict[str, Any] = {}
    terminals: list[Any] = []
    replay_terminals: list[Any] = []
    replay_mismatches: list[dict[str, str]] = []
    template_hash = file_sha256(template_path)
    rubric_hash = file_sha256(rubric_path)
    for variant_id in variants:
        spec = _run_spec(
            lane=lane,
            subject_commit=subject_commit,
            dirty_tree=dirty_tree,
            manifest_hash=str(manifest["manifest_hash"]),
            cases_hash=cases_hash,
            materializations_hash=materializations_hash,
            template=template,
            template_hash=template_hash,
            rubric_hash=rubric_hash,
            variant_id=variant_id,
        )
        specs.append(spec)
        specs_by_variant[variant_id] = spec.model_dump(mode="json")
        adapter = (
            ADAPTERS_V3[variant_id](ocr_engine=shared_ocr)
            if variant_id in {"core_b", "solver_c"}
            else ADAPTERS_V3[variant_id]()
        )
        for case in cases:
            first = await execute_terminal_v3(
                case=case,
                materialization=materialization_by_case[case.case_id],
                run_spec=spec,
                adapter=adapter,
            )
            terminals.append(first)
            if args.replay:
                replay = await execute_terminal_v3(
                    case=case,
                    materialization=materialization_by_case[case.case_id],
                    run_spec=spec,
                    adapter=adapter,
                )
                replay_terminals.append(replay)
                if replay.replay_hash != first.replay_hash:
                    replay_mismatches.append(
                        {
                            "case_id": case.case_id,
                            "variant_id": variant_id,
                            "first": first.replay_hash,
                            "second": replay.replay_hash,
                        }
                    )

    validate_run_spec_whitelist_v3(specs)
    validate_exact_terminal_set_v3(
        terminals,
        case_ids={case.case_id for case in cases},
        variant_ids=set(variants),
    )
    case_by_id = {case.case_id: case for case in cases}
    case_results: list[P5CaseResultV3] = [
        build_case_result_v3(
            run_id=run_id,
            case=case_by_id[terminal.case_id],
            terminal=terminal,
        )
        for terminal in terminals
    ]
    failure_records: list[P5FailureRecordV3] = []
    for terminal in terminals:
        record = build_failure_record_v3(run_id=run_id, lane=lane, terminal=terminal)
        if record is not None:
            failure_records.append(record)

    execution_multiplier = 2 if args.replay else 1
    expected_product_ocr_replays = (
        len(screenshot_image_hashes)
        * execution_multiplier
        * len(set(variants) & {"core_b", "solver_c"})
    )
    terminal_ocr = _validate_terminal_ocr_receipts(
        [*terminals, *replay_terminals],
        expected_product_replays=expected_product_ocr_replays,
        materialization_by_case=materialization_by_case,
    )
    ocr_provenance = shared_ocr.provenance()
    expected_ocr_counts = {
        "lookup_count": expected_product_ocr_replays,
        "hit_count": expected_product_ocr_replays,
        "receipt_match_count": expected_product_ocr_replays,
        "cleanup_deleted_count": expected_product_ocr_replays,
        "miss_count": 0,
        "fallback_count": 0,
        "fresh_prediction_count": 0,
        "unique_hash_count": len(screenshot_image_hashes),
    }
    if any(ocr_provenance.get(key) != value for key, value in expected_ocr_counts.items()):
        raise ValueError("P5 v3 frozen OCR replay provenance count mismatch")
    if formal_shape and args.replay and len(screenshot_image_hashes) != 126:
        raise ValueError("formal P5 v3 nonblind lane must bind 126 unique screenshot hashes")

    case_results_path = run_dir / "case_results.jsonl"
    failures_path = run_dir / "failure_records.jsonl"
    case_results_content_hash = write_models_jsonl_atomic_v3(
        case_results_path, case_results
    )
    failure_records_content_hash = write_models_jsonl_atomic_v3(
        failures_path, failure_records
    )
    generated_at = datetime.now(timezone.utc)
    entries = [
        _artifact_entry(case_results_path, generated_at=generated_at),
        _artifact_entry(failures_path, generated_at=generated_at),
    ]
    index_payload = {
        "schema_version": "trip-check-p5-artifact-index-v3",
        "subject_commit": subject_commit,
        "dirty_tree": dirty_tree,
        "entries": [entry.model_dump(mode="json") for entry in entries],
    }
    index_payload["artifact_index_hash"] = digest(index_payload)
    artifact_index = P5ArtifactIndexV3.model_validate(index_payload)
    artifact_index_path = run_dir / "artifact_index.json"
    _write_json_atomic(artifact_index_path, artifact_index.model_dump(mode="json"))

    replay_complete = bool(args.replay and not replay_mismatches)
    formal_evidence = bool(
        args.require_formal
        and not dirty_tree
        and complete_lane
        and formal_shape
        and replay_complete
    )
    manifest_status = "PASS" if (not args.replay or replay_complete) else "REJECT"
    run_manifest: dict[str, Any] = {
        "schema_version": "trip-check-p5-run-group-v3",
        "run_id": run_id,
        "status": manifest_status,
        "formal_evidence": formal_evidence,
        "lane": lane,
        "subject_commit": subject_commit,
        "dirty_tree": dirty_tree,
        "dataset_manifest_hash": manifest["manifest_hash"],
        "cases_file_sha256": file_sha256(cases_path),
        "materializations_file_sha256": file_sha256(materializations_path),
        "case_count": len(cases),
        "case_set_hash": cases_hash,
        "materialization_set_hash": materializations_hash,
        "variant_ids": variants,
        "variant_count": len(variants),
        "terminal_count": len(terminals),
        "expected_terminal_count": len(cases) * len(variants),
        "run_specs": specs_by_variant,
        "case_results_path": case_results_path.name,
        "case_results_file_sha256": file_sha256(case_results_path),
        "case_results_content_sha256": case_results_content_hash,
        "failure_records_path": failures_path.name,
        "failure_records_file_sha256": file_sha256(failures_path),
        "failure_records_content_sha256": failure_records_content_hash,
        "failure_record_count": len(failure_records),
        "artifact_index_path": artifact_index_path.name,
        "artifact_index_hash": artifact_index.artifact_index_hash,
        "replay_executed": bool(args.replay),
        "replay_match_count": len(terminals) - len(replay_mismatches)
        if args.replay
        else 0,
        "replay_mismatches": replay_mismatches,
        "replay_hash_policy": "p5-semantic-projection-v3",
        "ocr_replay_provenance": {
            **ocr_provenance,
            **terminal_ocr,
            "nonblind_unique_image_hashes": len(screenshot_image_hashes),
            "expected_formal_lookup_count": 504,
        },
        "hidden_retry_count": 0,
        "blind_labels_read": False,
        "external_api_calls": 0,
        "fresh_ocr_model_inferences": 0,
        "human_evidence": "NOT_RUN",
        **commitments,
    }
    run_manifest["manifest_hash"] = digest(run_manifest)
    _write_json_atomic(run_dir / "run_group_manifest.json", run_manifest)
    return {**run_manifest, "run_dir": str(run_dir)}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--lane",
        choices=("nonblind", "frozen-blind", "frozen_blind"),
        required=True,
    )
    value.add_argument("--variants", default="legacy,core,solver")
    value.add_argument("--cases-file")
    value.add_argument("--materializations-file")
    value.add_argument("--dataset-manifest")
    value.add_argument("--run-spec-template")
    value.add_argument("--rubric")
    value.add_argument("--active-contract")
    value.add_argument("--case-id", action="append")
    value.add_argument("--limit", type=int)
    value.add_argument("--replay", action="store_true")
    value.add_argument("--allow-dirty", action="store_true")
    value.add_argument("--require-formal", action="store_true")
    value.add_argument("--run-id")
    value.add_argument("--output-dir")
    return value


def main() -> None:
    result = asyncio.run(execute_run(parser().parse_args()))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
