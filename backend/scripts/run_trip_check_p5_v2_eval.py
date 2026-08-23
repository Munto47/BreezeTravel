"""Run the P5 v2 A/B/C evaluation without reading an oracle or blind label bundle."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p5.active_contract import ACTIVE_CONTRACT_PATH, require_v2_formal_ready
from evals.trip_check_v1.p5.adapters_v2 import (
    ADAPTERS_V2,
    ADAPTER_VERSIONS_V2,
    EvaluationCachingPaddleOcrEngine,
)
from evals.trip_check_v1.p5.contracts_v2 import P5CaseV2, P5TerminalOutputV2, P5VariantRunSpecV2
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.runner_v2 import (
    FORMAL_COMMITMENT_FIELDS_V2,
    build_formal_commitments_v2,
    execute_terminal_v2,
    not_applicable_commitments_v2,
    validate_exact_terminal_set_v2,
    validate_run_spec_whitelist_v2,
    write_jsonl_atomic_v2,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent.resolve()
P5_ROOT = BACKEND_ROOT / "evals" / "trip_check_v1" / "p5"
DEFAULTS = {
    "nonblind": (
        P5_ROOT / "cases_nonblind_v2.jsonl",
        P5_ROOT / "materializations_nonblind_v2.jsonl",
        270,
    ),
    "frozen_blind": (
        P5_ROOT / "frozen_blind.v2.inputs.jsonl",
        P5_ROOT / "frozen_blind.v2.materializations.jsonl",
        90,
    ),
}
VARIANT_ALIASES = {
    "legacy": "legacy_a",
    "legacy_a": "legacy_a",
    "core": "core_b",
    "core_b": "core_b",
    "solver": "solver_c",
    "solver_c": "solver_c",
}
RUN_GROUP_FIELDS = {
    "schema_version",
    "run_id",
    "status",
    "formal_evidence",
    "lane",
    "subject_commit",
    "dirty_tree",
    "dataset_manifest_hash",
    "cases_file_sha256",
    "materializations_file_sha256",
    "case_count",
    "case_set_hash",
    "materialization_set_hash",
    "variant_ids",
    "variant_count",
    "terminal_count",
    "expected_terminal_count",
    "run_specs",
    "terminal_outputs_path",
    "terminal_outputs_file_sha256",
    "terminal_outputs_content_sha256",
    "variant_output_sha256",
    "replay_executed",
    "replay_match_count",
    "replay_mismatches",
    "blind_labels_read",
    "external_api_calls",
    "human_evidence",
    *FORMAL_COMMITMENT_FIELDS_V2,
    "manifest_hash",
}


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return completed.stdout.strip()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _git_is_ancestor(candidate_commit: str, subject_commit: str) -> bool:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", candidate_commit, subject_commit],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode not in (0, 1):
        raise RuntimeError("formal candidate ancestry could not be verified")
    return completed.returncode == 0


def _validate_dataset_bytes(
    *,
    lane: str,
    dataset: dict[str, Any],
    cases_path: Path,
    materializations_path: Path,
    template_path: Path,
    rubric_path: Path,
) -> None:
    expected_manifest_hash = digest({key: value for key, value in dataset.items() if key != "manifest_hash"})
    if dataset.get("manifest_hash") != expected_manifest_hash:
        raise ValueError("dataset manifest canonical hash mismatch")
    contracts = dataset.get("contract_hashes", {})
    if contracts.get("run_spec_template_sha256") != _sha256_file(template_path):
        raise ValueError("dataset manifest does not bind the selected RunSpec template bytes")
    if contracts.get("judge_rubric_sha256") != _sha256_file(rubric_path):
        raise ValueError("dataset manifest does not bind the selected rubric bytes")
    file_keys = (
        ("blind_cases", "blind_materializations")
        if lane == "frozen_blind"
        else ("nonblind_cases", "nonblind_materializations")
    )
    files = dataset.get("files", {})
    for key, path in zip(file_keys, (cases_path, materializations_path), strict=True):
        if files.get(key, {}).get("file_sha256") != _sha256_file(path):
            raise ValueError(f"dataset manifest does not bind selected {key} bytes")


def _require_formal_bindings(
    *,
    active_contract_path: Path,
    blind_seal_path: Path,
    lane: str,
    subject_commit: str,
    dataset_manifest_hash: str,
    cases_path: Path,
    materializations_path: Path,
    template_path: Path,
    rubric_path: Path,
    case_count: int,
) -> dict[str, str]:
    active = require_v2_formal_ready(active_contract_path)
    seal = _load_json(blind_seal_path)
    seal_sha256 = _sha256_file(blind_seal_path)
    try:
        commitments = build_formal_commitments_v2(
            active=active,
            active_contract_file_sha256=_sha256_file(active_contract_path),
            seal=seal,
            blind_seal_sha256=seal_sha256,
            dataset_manifest_hash=dataset_manifest_hash,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if not _git_is_ancestor(str(active["candidate_freeze_commit"]), subject_commit):
        raise RuntimeError("formal candidate freeze commit is not an ancestor of run subject HEAD")
    if lane != "frozen_blind":
        return commitments
    expected = {
        "schema_version": "trip-check-p5-blind-seal-v2",
        "split": "frozen_blind",
        "case_count": case_count,
        "inputs_file_sha256": _sha256_file(cases_path),
        "materializations_file_sha256": _sha256_file(materializations_path),
        "rubric_sha256": _sha256_file(rubric_path),
        "run_spec_template_sha256": _sha256_file(template_path),
    }
    for key, value in expected.items():
        if seal.get(key) != value:
            raise RuntimeError(f"formal frozen-blind seal binding mismatch: {key}")
    return commitments


def _variant_ids(raw: str) -> list[str]:
    values: list[str] = []
    for item in raw.split(","):
        key = item.strip()
        if key not in VARIANT_ALIASES:
            raise ValueError(f"unknown variant: {key}")
        value = VARIANT_ALIASES[key]
        if value not in values:
            values.append(value)
    return values


def _case_set_hash(cases: list[P5CaseV2]) -> str:
    return digest(
        [{"case_id": item.case_id, "case_hash": item.case_hash} for item in sorted(cases, key=lambda x: x.case_id)]
    )


def _materialization_set_hash(materializations: list[dict[str, Any]]) -> str:
    return digest(
        [
            {
                "case_id": item["case_id"],
                "materialization_id": item["materialization_id"],
                "materialization_hash": item["materialization_hash"],
            }
            for item in sorted(materializations, key=lambda x: x["case_id"])
        ]
    )


def _run_spec(
    *,
    lane: str,
    subject_commit: str,
    dirty_tree: bool,
    dataset_manifest_hash: str,
    case_set_hash: str,
    materialization_set_hash: str,
    rubric_hash: str,
    template: dict[str, Any],
    template_hash: str,
    variant_id: str,
) -> P5VariantRunSpecV2:
    adapter_version, repair_strategy = ADAPTER_VERSIONS_V2[variant_id]
    configured = template.get("variant_specs", {}).get(variant_id, {})
    if configured and configured != {
        "adapter_version": adapter_version,
        "repair_strategy": repair_strategy,
    }:
        raise ValueError("run template variant differs from frozen adapter contract")
    return P5VariantRunSpecV2(
        subject_commit=subject_commit,
        dirty_tree=dirty_tree,
        lane=lane,
        dataset_manifest_hash=dataset_manifest_hash,
        case_set_hash=case_set_hash,
        materialization_set_hash=materialization_set_hash,
        run_spec_template_hash=template_hash,
        rubric_hash=rubric_hash,
        renderer_version=str(template["renderer"]["version"]),
        ocr_engine_version=str(template["ocr_engine"]["version"]),
        evidence_policy_version=str(template["evidence_policy_version"]),
        fault_registry_version=str(template["fault_registry_version"]),
        random_seed=int(template["random_seed"]),
        budget=dict(template["budget"]),
        replay_hash_policy=str(template["replay_hash_policy"]),
        variant_id=variant_id,
        adapter_version=adapter_version,
        repair_strategy=repair_strategy,
    )


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    if set(value) != RUN_GROUP_FIELDS:
        raise ValueError("run group manifest fields differ from v2 contract")
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


async def execute_run(args: argparse.Namespace) -> dict[str, Any]:
    lane = args.lane.replace("-", "_")
    default_cases, default_materializations, formal_count = DEFAULTS[lane]
    cases_path = Path(args.cases_file).resolve() if args.cases_file else default_cases
    materializations_path = (
        Path(args.materializations_file).resolve() if args.materializations_file else default_materializations
    )
    dataset_path = (
        Path(args.dataset_manifest).resolve() if args.dataset_manifest else P5_ROOT / "dataset_v2.manifest.json"
    )
    template_path = (
        Path(args.run_spec_template).resolve() if args.run_spec_template else P5_ROOT / "run_spec_template_v2.json"
    )
    rubric_path = Path(args.rubric).resolve() if getattr(args, "rubric", None) else P5_ROOT / "judge_rubric_v2.json"
    active_contract_path = (
        Path(args.active_contract).resolve() if getattr(args, "active_contract", None) else ACTIVE_CONTRACT_PATH
    )
    blind_seal_path = (
        Path(args.blind_seal).resolve()
        if getattr(args, "blind_seal", None)
        else P5_ROOT / "sealed" / "frozen_blind.v2.seal.json"
    )
    if args.require_formal:
        require_v2_formal_ready(active_contract_path)
    cases = [P5CaseV2.model_validate(item) for item in _load_jsonl(cases_path)]
    materializations = _load_jsonl(materializations_path)
    if len({item.case_id for item in cases}) != len(cases):
        raise ValueError("case IDs must be unique")
    expected_split = "frozen_blind" if lane == "frozen_blind" else None
    if expected_split is not None and any(item.split != expected_split for item in cases):
        raise ValueError("frozen blind lane contains a non-blind case")
    if lane == "nonblind" and any(item.split == "frozen_blind" for item in cases):
        raise ValueError("nonblind lane contains a frozen blind case")
    if len({item["case_id"] for item in materializations}) != len(materializations):
        raise ValueError("materialization case IDs must be unique")
    materialization_by_case = {item["case_id"]: item for item in materializations}
    if set(materialization_by_case) != {item.case_id for item in cases}:
        raise ValueError("cases and materializations must have an exact one-to-one mapping")
    if args.case_id:
        requested = set(args.case_id)
        cases = [item for item in cases if item.case_id in requested]
        missing = requested - {item.case_id for item in cases}
        if missing:
            raise ValueError(f"unknown case IDs: {sorted(missing)}")
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        raise ValueError("selected case set is empty")
    selected_materializations = [materialization_by_case[item.case_id] for item in cases]
    variants = _variant_ids(args.variants)
    subject_commit = _git("rev-parse", "HEAD")
    dirty_tree = bool(_git("status", "--short"))
    if dirty_tree and not args.allow_dirty:
        raise RuntimeError("working tree is dirty; use --allow-dirty only for development runs")
    complete_lane = args.limit is None and not args.case_id
    formal_shape = len(cases) == formal_count and variants == ["legacy_a", "core_b", "solver_c"]
    if args.require_formal and (dirty_tree or not complete_lane or not formal_shape or not args.replay):
        raise RuntimeError("formal P5 v2 runs require clean exact lane x three variants with replay")

    output_root = (
        Path(args.output_dir).resolve() if args.output_dir else BACKEND_ROOT / "evidence" / "trip_check_v1" / "p5"
    )
    if lane == "frozen_blind":
        if not args.output_dir:
            raise RuntimeError("frozen blind outputs require an explicit external --output-dir")
        try:
            output_root.relative_to(REPO_ROOT)
        except ValueError:
            pass
        else:
            raise RuntimeError("frozen blind outputs must be outside the repository")
    dataset = _load_json(dataset_path)
    template = _load_json(template_path)
    _validate_dataset_bytes(
        lane=lane,
        dataset=dataset,
        cases_path=cases_path,
        materializations_path=materializations_path,
        template_path=template_path,
        rubric_path=rubric_path,
    )
    dataset_manifest_hash = str(dataset["manifest_hash"])
    rubric_hash = str(dataset["contract_hashes"]["judge_rubric_sha256"])
    if template.get("allowed_variant_differences") != [
        "variant_id",
        "adapter_version",
        "repair_strategy",
    ]:
        raise ValueError("RunSpec template variant whitelist differs from the v2 contract")
    if template.get("replay_hash_policy") != "p5-semantic-projection-v2":
        raise ValueError("RunSpec template replay hash policy differs from the v2 contract")
    cases_hash = _case_set_hash(cases)
    materializations_hash = _materialization_set_hash(selected_materializations)
    template_hash = _sha256_file(template_path)
    dataset_ready = bool(
        dataset.get("frozen") is True
        and dataset.get("generation", {}).get("formal_validation_eligible") is True
        and dataset.get("evidence_boundary", {}).get("actual_ocr") == "PASS"
    )
    if complete_lane:
        lane_contract = dataset.get("lanes", {}).get(lane, {})
        if lane_contract.get("case_count") != len(cases):
            raise ValueError("dataset manifest lane case count mismatch")
        if lane_contract.get("materialization_count") != len(selected_materializations):
            raise ValueError("dataset manifest lane materialization count mismatch")
        if lane_contract.get("case_set_hash") != cases_hash:
            raise ValueError("dataset manifest lane case set hash mismatch")
        if lane_contract.get("materialization_set_hash") != materializations_hash:
            raise ValueError("dataset manifest lane materialization set hash mismatch")
    if args.require_formal and not dataset_ready:
        raise RuntimeError("formal P5 v2 runs require a frozen, formal-eligible actual-OCR dataset")
    formal_commitments = not_applicable_commitments_v2()
    if args.require_formal:
        formal_commitments = _require_formal_bindings(
            active_contract_path=active_contract_path,
            blind_seal_path=blind_seal_path,
            lane=lane,
            subject_commit=subject_commit,
            dataset_manifest_hash=dataset_manifest_hash,
            cases_path=cases_path,
            materializations_path=materializations_path,
            template_path=template_path,
            rubric_path=rubric_path,
            case_count=len(cases),
        )
    run_id = args.run_id or datetime.now(timezone.utc).strftime("p5-v2-%Y%m%dT%H%M%SZ")
    run_dir = output_root / run_id
    if run_dir.exists():
        raise FileExistsError("run directory already exists; P5 v2 outputs are immutable")
    run_dir.mkdir(parents=True)
    outputs: list[P5TerminalOutputV2] = []
    replay_mismatches: list[dict[str, str]] = []
    specs: dict[str, Any] = {}
    spec_models: list[P5VariantRunSpecV2] = []
    shared_ocr_engine = EvaluationCachingPaddleOcrEngine()
    for variant_id in variants:
        adapter = (
            ADAPTERS_V2[variant_id](ocr_engine=shared_ocr_engine)
            if variant_id in {"core_b", "solver_c"}
            else ADAPTERS_V2[variant_id]()
        )
        spec = _run_spec(
            lane=lane,
            subject_commit=subject_commit,
            dirty_tree=dirty_tree,
            dataset_manifest_hash=dataset_manifest_hash,
            case_set_hash=cases_hash,
            materialization_set_hash=materializations_hash,
            rubric_hash=rubric_hash,
            template=template,
            template_hash=template_hash,
            variant_id=variant_id,
        )
        specs[variant_id] = spec.model_dump(mode="json")
        spec_models.append(spec)
        for case in cases:
            first = await execute_terminal_v2(
                case=case,
                materialization=materialization_by_case[case.case_id],
                run_spec=spec,
                adapter=adapter,
            )
            outputs.append(first)
            if args.replay:
                replay = await execute_terminal_v2(
                    case=case,
                    materialization=materialization_by_case[case.case_id],
                    run_spec=spec,
                    adapter=adapter,
                )
                if replay.replay_hash != first.replay_hash:
                    replay_mismatches.append(
                        {
                            "case_id": case.case_id,
                            "variant_id": variant_id,
                            "first": first.replay_hash,
                            "second": replay.replay_hash,
                        }
                    )
    validate_run_spec_whitelist_v2(spec_models)
    validate_exact_terminal_set_v2(outputs, case_ids={item.case_id for item in cases}, variant_ids=set(variants))
    terminal_path = run_dir / "terminal_outputs.jsonl"
    content_hash = write_jsonl_atomic_v2(terminal_path, outputs)
    variant_hashes = {
        variant_id: digest(
            [
                item.model_dump(mode="json")
                for item in sorted(
                    (candidate for candidate in outputs if candidate.variant_id == variant_id),
                    key=lambda candidate: candidate.case_id,
                )
            ]
        )
        for variant_id in sorted(variants)
    }
    replay_complete = bool(args.replay and not replay_mismatches)
    formal_evidence = bool(
        args.require_formal and dataset_ready and not dirty_tree and complete_lane and formal_shape and replay_complete
    )
    manifest: dict[str, Any] = {
        "schema_version": "trip-check-p5-run-group-v2",
        "run_id": run_id,
        "status": "PASS" if replay_complete else "REJECT",
        "formal_evidence": formal_evidence,
        "lane": lane,
        "subject_commit": subject_commit,
        "dirty_tree": dirty_tree,
        "dataset_manifest_hash": dataset_manifest_hash,
        "cases_file_sha256": _sha256_file(cases_path),
        "materializations_file_sha256": _sha256_file(materializations_path),
        "case_count": len(cases),
        "case_set_hash": cases_hash,
        "materialization_set_hash": materializations_hash,
        "variant_ids": variants,
        "variant_count": len(variants),
        "terminal_count": len(outputs),
        "expected_terminal_count": len(cases) * len(variants),
        "run_specs": specs,
        "terminal_outputs_path": terminal_path.name,
        "terminal_outputs_file_sha256": _sha256_file(terminal_path),
        "terminal_outputs_content_sha256": content_hash,
        "variant_output_sha256": variant_hashes,
        "replay_executed": bool(args.replay),
        "replay_match_count": len(outputs) - len(replay_mismatches) if args.replay else 0,
        "replay_mismatches": replay_mismatches,
        "blind_labels_read": False,
        "external_api_calls": 0,
        "human_evidence": False,
        **formal_commitments,
    }
    manifest["manifest_hash"] = digest(manifest)
    _write_json_atomic(run_dir / "run_group_manifest.json", manifest)
    return {**manifest, "run_dir": str(run_dir)}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--lane", choices=("nonblind", "frozen-blind", "frozen_blind"), required=True)
    value.add_argument("--variants", default="legacy,core,solver")
    value.add_argument("--cases-file")
    value.add_argument("--materializations-file")
    value.add_argument("--dataset-manifest")
    value.add_argument("--run-spec-template")
    value.add_argument("--rubric")
    value.add_argument("--active-contract")
    value.add_argument("--blind-seal")
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
