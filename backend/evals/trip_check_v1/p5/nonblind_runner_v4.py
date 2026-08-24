"""Formal repository-external runner and readback for P5 v4 non-blind data."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest, file_sha256
from evals.trip_check_v1.p5.runner_v4 import (
    ACTIVE_CONTRACT_V4,
    DATASET_ID_V4,
    P5TerminalOutputV4,
    P5VariantRunSpecV4,
    VARIANT_IDS_V4,
    build_run_spec_v4,
    execute_terminal_v4,
    semantic_output_hash_v4,
)


NONBLIND_CASE_COUNT_V4 = 270
NONBLIND_TERMINAL_COUNT_V4 = 810
NONBLIND_SCREENSHOT_HASH_COUNT_V4 = 126
NONBLIND_OCR_LOOKUP_COUNT_V4 = 504
RUN_GROUP_SCHEMA_V4 = "trip-check-p5-run-group-v4"
ARTIFACT_INDEX_SCHEMA_V4 = "trip-check-p5-nonblind-artifact-index-v4"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class P5NonblindRunnerErrorV4(RuntimeError):
    """Stable fail-closed validation error for the v4 non-blind lane."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _fail(reason_code: str, exc: Exception | None = None) -> None:
    if exc is None:
        raise P5NonblindRunnerErrorV4(reason_code)
    raise P5NonblindRunnerErrorV4(reason_code) from exc


def _load_json(path: Path, reason_code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(reason_code, exc)
    if not isinstance(value, dict):
        _fail(reason_code)
    return value


def _load_jsonl(path: Path, reason_code: str) -> list[dict[str, Any]]:
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(reason_code, exc)
    if any(not isinstance(row, dict) for row in rows):
        _fail(reason_code)
    return rows


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(canonical_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_jsonl_atomic(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_external_directory(path: Path, repo_root: Path) -> Path:
    absolute = path.absolute()
    if ".." in absolute.parts or _contains_link(absolute):
        _fail("NONBLIND_OUTPUT_PATH_UNSAFE")
    try:
        parent = absolute.parent.resolve(strict=True)
    except OSError as exc:
        _fail("NONBLIND_OUTPUT_PARENT_UNREADABLE", exc)
    resolved = parent / absolute.name
    if _is_inside(resolved, repo_root.resolve()):
        _fail("NONBLIND_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY")
    return resolved


def _git(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        _fail("NONBLIND_GIT_READBACK_FAILED", exc)
    return result.stdout.strip()


def _git_is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        _fail("NONBLIND_GIT_READBACK_FAILED", exc)
    if result.returncode not in {0, 1}:
        _fail("NONBLIND_GIT_READBACK_FAILED")
    return result.returncode == 0


@dataclass(frozen=True)
class NonblindDatasetPathsV4:
    cases: Path
    materializations: Path
    manifest: Path
    seal: Path
    run_spec_template: Path
    rubric: Path
    active_contract: Path


@dataclass(frozen=True)
class NonblindExecutionResultV4:
    terminals: tuple[Mapping[str, Any], ...]
    replay_terminals: tuple[Mapping[str, Any], ...]
    run_specs: Mapping[str, Mapping[str, Any]]
    screenshot_hashes: frozenset[str]
    ocr_provenance: Mapping[str, int]


class NonblindExecutionEngineV4(Protocol):
    async def execute(
        self,
        *,
        case_rows: Sequence[Mapping[str, Any]],
        materialization_rows: Sequence[Mapping[str, Any]],
        run_spec_template: Mapping[str, Any],
        run_spec_context: Mapping[str, Any],
    ) -> NonblindExecutionResultV4: ...


class V3PayloadNonblindExecutionEngineV4:
    """Run v3 payloads through v4 RunSpecs/adapters with no oracle access."""

    async def execute(
        self,
        *,
        case_rows: Sequence[Mapping[str, Any]],
        materialization_rows: Sequence[Mapping[str, Any]],
        run_spec_template: Mapping[str, Any],
        run_spec_context: Mapping[str, Any],
    ) -> NonblindExecutionResultV4:
        from evals.trip_check_v1.p5.adapters_v3 import (
            EvaluationCachingPaddleOcrEngineV3,
        )
        from evals.trip_check_v1.p5.adapters_v4 import ADAPTERS_V4, ADAPTER_VERSIONS_V4
        from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3
        from evals.trip_check_v1.p5.data_contract_v4 import validate_materialization_v4

        try:
            cases = [P5CaseV3.model_validate(row) for row in case_rows]
        except Exception as exc:
            _fail("NONBLIND_CASE_PAYLOAD_INVALID", exc)
        if any(case.split == "frozen_blind" or case.oracle is None for case in cases):
            _fail("NONBLIND_CASE_ORACLE_BOUNDARY_INVALID")
        materialization_by_case = {
            str(row.get("case_id")): row for row in materialization_rows
        }
        if len(materialization_by_case) != len(materialization_rows) or set(
            materialization_by_case
        ) != {case.case_id for case in cases}:
            _fail("NONBLIND_MATERIALIZATION_CASE_SET_INVALID")

        validated: dict[str, Mapping[str, Any]] = {}
        shared_ocr = EvaluationCachingPaddleOcrEngineV3()
        screenshot_hashes: set[str] = set()
        for case in cases:
            try:
                materialization = validate_materialization_v4(
                    case, materialization_by_case[case.case_id]
                )
            except Exception as exc:
                _fail("NONBLIND_MATERIALIZATION_PAYLOAD_INVALID", exc)
            validated[case.case_id] = materialization
            receipt = materialization.get("ocr_baseline_receipt")
            render = materialization.get("render_receipt")
            if case.input_kind == "SYNTHETIC_SCREENSHOT":
                if not isinstance(receipt, Mapping) or not isinstance(render, Mapping):
                    _fail("NONBLIND_SCREENSHOT_RECEIPT_MISSING")
                if receipt.get("asset_hash") != render.get("image_sha256"):
                    _fail("NONBLIND_SCREENSHOT_RECEIPT_HASH_MISMATCH")
                shared_ocr.preload(receipt)
                screenshot_hashes.add(str(receipt["asset_hash"]))
            elif receipt is not None or render is not None:
                _fail("NONBLIND_TEXT_CASE_HAS_SCREENSHOT_RECEIPT")

        if (
            run_spec_template.get("allowed_variant_differences")
            != ["variant_id", "adapter_version", "repair_strategy"]
            or run_spec_template.get("execution_mode") != "controlled_snapshot"
            or run_spec_template.get("replay_hash_policy")
            != "p5-semantic-projection-v4"
        ):
            _fail("NONBLIND_RUN_SPEC_POLICY_INVALID")
        specs = {
            variant_id: build_run_spec_v4(
                lane="nonblind",
                subject_commit=str(run_spec_context["subject_commit"]),
                dirty_tree=bool(run_spec_context["dirty_tree"]),
                dataset_manifest_hash=str(
                    run_spec_context["dataset_manifest_hash"]
                ),
                case_set_hash=str(run_spec_context["case_set_hash"]),
                materialization_set_hash=str(
                    run_spec_context["materialization_set_hash"]
                ),
                run_spec_template_hash=str(
                    run_spec_context["run_spec_template_sha256"]
                ),
                rubric_hash=str(run_spec_context["rubric_sha256"]),
                template=run_spec_template,
                variant_id=variant_id,
                adapter_versions=ADAPTER_VERSIONS_V4,
            )
            for variant_id in VARIANT_IDS_V4
        }
        terminals: list[Mapping[str, Any]] = []
        replays: list[Mapping[str, Any]] = []
        for variant_id in VARIANT_IDS_V4:
            adapter = (
                ADAPTERS_V4[variant_id](ocr_engine=shared_ocr)
                if variant_id in {"core_b", "solver_c"}
                else ADAPTERS_V4[variant_id]()
            )
            for case in cases:
                for target in (terminals, replays):
                    output = await execute_terminal_v4(
                        case=case,
                        materialization=validated[case.case_id],
                        run_spec=specs[variant_id],
                        adapter=adapter,
                    )
                    target.append(output.model_dump(mode="json"))
        return NonblindExecutionResultV4(
            terminals=tuple(terminals),
            replay_terminals=tuple(replays),
            run_specs={
                key: value.model_dump(mode="json") for key, value in specs.items()
            },
            screenshot_hashes=frozenset(screenshot_hashes),
            ocr_provenance=shared_ocr.provenance(),
        )


def _default_dataset_paths() -> NonblindDatasetPathsV4:
    from evals.trip_check_v1.p5.data_contract_v2 import JUDGE_RUBRIC_PATH_V2
    from evals.trip_check_v1.p5.data_contract_v3 import ACTIVE_CONTRACT_PATH
    from evals.trip_check_v1.p5.data_contract_v4 import (
        BLIND_SEAL_PATH_V4,
        MANIFEST_PATH_V4,
        NONBLIND_MATERIALIZATIONS_PATH_V4,
        NONBLIND_PATH_V4,
        RUN_SPEC_TEMPLATE_PATH_V4,
    )

    return NonblindDatasetPathsV4(
        cases=NONBLIND_PATH_V4,
        materializations=NONBLIND_MATERIALIZATIONS_PATH_V4,
        manifest=MANIFEST_PATH_V4,
        seal=BLIND_SEAL_PATH_V4,
        run_spec_template=RUN_SPEC_TEMPLATE_PATH_V4,
        rubric=JUDGE_RUBRIC_PATH_V2,
        active_contract=ACTIVE_CONTRACT_PATH,
    )


def _validate_dataset_envelope(
    *, paths: NonblindDatasetPathsV4, require_formal: bool
) -> tuple[
    dict[str, Any],
    dict[str, Any] | None,
    dict[str, Any] | None,
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    manifest = _load_json(paths.manifest, "NONBLIND_DATASET_MANIFEST_INVALID")
    template = _load_json(paths.run_spec_template, "NONBLIND_TEMPLATE_INVALID")
    if (
        manifest.get("schema_version") != "trip-check-p5-dataset-manifest-v4"
        or manifest.get("dataset_id") != DATASET_ID_V4
        or manifest.get("manifest_hash")
        != digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
    ):
        _fail("NONBLIND_DATASET_ENVELOPE_INVALID")
    files = manifest.get("files")
    lane = manifest.get("lanes", {}).get("nonblind")
    if not isinstance(files, Mapping) or not isinstance(lane, Mapping):
        _fail("NONBLIND_DATASET_FILE_BINDING_MISSING")
    cases = _load_jsonl(paths.cases, "NONBLIND_INPUTS_INVALID")
    materializations = _load_jsonl(
        paths.materializations, "NONBLIND_MATERIALIZATIONS_INVALID"
    )
    for name, path, rows in (
        ("nonblind_cases", paths.cases, cases),
        ("nonblind_materializations", paths.materializations, materializations),
    ):
        binding = files.get(name)
        if not isinstance(binding, Mapping) or (
            binding.get("row_count") != len(rows)
            or binding.get("file_sha256") != file_sha256(path)
            or binding.get("content_sha256") != digest(rows)
        ):
            _fail("NONBLIND_DATASET_FILE_BINDING_MISMATCH")
    case_ids = [row.get("case_id") for row in cases]
    materialization_ids = [row.get("case_id") for row in materializations]
    if (
        len(cases) != NONBLIND_CASE_COUNT_V4
        or len(materializations) != NONBLIND_CASE_COUNT_V4
        or len(set(case_ids)) != NONBLIND_CASE_COUNT_V4
        or len(set(materialization_ids)) != NONBLIND_CASE_COUNT_V4
        or set(case_ids) != set(materialization_ids)
        or any(row.get("split") == "frozen_blind" for row in cases)
        or any("oracle" not in row for row in cases)
        or Counter(row.get("split") for row in cases)
        != {"pilot": 18, "dev": 180, "regression": 72}
        or lane.get("case_count") != NONBLIND_CASE_COUNT_V4
        or lane.get("materialization_count") != NONBLIND_CASE_COUNT_V4
    ):
        _fail("NONBLIND_CASE_SET_INVALID")
    contract_hashes = manifest.get("contract_hashes")
    from evals.trip_check_v1.p5.data_contract_v3 import (
        case_set_hash_v3,
        materialization_set_hash_v3,
    )

    if not isinstance(contract_hashes, Mapping) or (
        contract_hashes.get("run_spec_template_sha256")
        != file_sha256(paths.run_spec_template)
        or contract_hashes.get("judge_rubric_sha256") != file_sha256(paths.rubric)
        or template.get("schema_version") != "trip-check-p5-run-spec-v4"
        or template.get("replay_hash_policy") != "p5-semantic-projection-v4"
        or lane.get("case_set_hash") != case_set_hash_v3(cases)
        or lane.get("materialization_set_hash")
        != materialization_set_hash_v3(materializations)
    ):
        _fail("NONBLIND_CONTRACT_ARTIFACT_BINDING_INVALID")

    seal: dict[str, Any] | None = None
    active: dict[str, Any] | None = None
    if require_formal:
        seal = _load_json(paths.seal, "NONBLIND_BLIND_SEAL_INVALID")
        active = _load_json(paths.active_contract, "NONBLIND_ACTIVE_CONTRACT_INVALID")
        commitment = manifest.get("sealing_commitment")
        if (
            manifest.get("frozen") is not True
            or manifest.get("formal_validation_eligible") is not True
            or manifest.get("seal_status") != "SEALED"
            or not isinstance(commitment, Mapping)
            or seal.get("schema_version") != "trip-check-p5-blind-seal-v4"
            or seal.get("split") != "frozen_blind"
            or seal.get("case_count") != 90
            or seal.get("scoring_payload_present") is not False
            or active.get("schema_version") != "trip-check-p5-active-contract-v1"
            or active.get("active_contract") != ACTIVE_CONTRACT_V4
            or active.get("formal_evidence_status") != "READY"
            or active.get("dataset_manifest_hash") != manifest["manifest_hash"]
            or active.get("blind_seal_v4_sha256") != file_sha256(paths.seal)
            or commitment.get("blind_seal_file_sha256") != file_sha256(paths.seal)
            or commitment.get("candidate_freeze_commit")
            != active.get("candidate_freeze_commit")
            or seal.get("candidate_freeze_commit")
            != active.get("candidate_freeze_commit")
            or seal.get("run_spec_template_sha256")
            != file_sha256(paths.run_spec_template)
            or seal.get("rubric_sha256") != file_sha256(paths.rubric)
        ):
            _fail("NONBLIND_FORMAL_DATASET_CONTRACT_INVALID")
    return manifest, seal, active, cases, materializations, template


def _validate_git_subject(
    *,
    repo_root: Path,
    subject_commit: str,
    upstream_ref: str,
    upstream_commit: str,
    candidate_freeze_commit: str,
) -> None:
    if (
        not upstream_ref
        or upstream_commit != subject_commit
        or _git(repo_root, "rev-parse", "HEAD") != subject_commit
        or _git(repo_root, "rev-parse", upstream_ref) != upstream_commit
        or _git(repo_root, "status", "--short")
        or not _git_is_ancestor(repo_root, candidate_freeze_commit, subject_commit)
    ):
        _fail("NONBLIND_FORMAL_SUBJECT_OR_UPSTREAM_INVALID")


def _terminal_key(row: Mapping[str, Any]) -> tuple[str, str]:
    case_id = row.get("case_id")
    variant_id = row.get("variant_id")
    if not isinstance(case_id, str) or variant_id not in VARIANT_IDS_V4:
        _fail("NONBLIND_TERMINAL_KEY_INVALID")
    return case_id, str(variant_id)


def _validate_execution_result(
    result: NonblindExecutionResultV4,
    *,
    context: Mapping[str, Any],
    case_input_kinds: Mapping[str, str],
    expected_screenshot_hashes: set[str],
) -> None:
    terminals = list(result.terminals)
    replays = list(result.replay_terminals)
    terminal_by_key = {_terminal_key(row): row for row in terminals}
    replay_by_key = {_terminal_key(row): row for row in replays}
    if (
        len(terminals) != NONBLIND_TERMINAL_COUNT_V4
        or len(replays) != NONBLIND_TERMINAL_COUNT_V4
        or len(terminal_by_key) != NONBLIND_TERMINAL_COUNT_V4
        or len(replay_by_key) != NONBLIND_TERMINAL_COUNT_V4
        or set(terminal_by_key) != set(replay_by_key)
        or set(result.run_specs) != set(VARIANT_IDS_V4)
    ):
        _fail("NONBLIND_TERMINAL_EXACT_SET_INVALID")
    try:
        specs = {
            variant_id: P5VariantRunSpecV4.model_validate(
                result.run_specs[variant_id]
            )
            for variant_id in VARIANT_IDS_V4
        }
    except Exception as exc:
        _fail("NONBLIND_RUN_SPEC_SCHEMA_INVALID", exc)
    common_specs = []
    for variant_id, spec in specs.items():
        if (
            spec.variant_id != variant_id
            or spec.lane != "nonblind"
            or spec.subject_commit != context["subject_commit"]
            or spec.dirty_tree is not False
            or spec.dataset_manifest_hash != context["dataset_manifest_hash"]
            or spec.case_set_hash != context["case_set_hash"]
            or spec.materialization_set_hash
            != context["materialization_set_hash"]
            or spec.run_spec_template_hash
            != context["run_spec_template_sha256"]
            or spec.rubric_hash != context["rubric_sha256"]
            or spec.replay_hash_policy != "p5-semantic-projection-v4"
            or not spec.adapter_version.endswith("-v4")
        ):
            _fail("NONBLIND_RUN_SPEC_BINDING_INVALID")
        common_specs.append(
            {
                key: value
                for key, value in spec.model_dump(mode="json").items()
                if key
                not in {"variant_id", "adapter_version", "repair_strategy"}
            }
        )
    if any(item != common_specs[0] for item in common_specs[1:]):
        _fail("NONBLIND_RUN_SPEC_VARIANT_WHITELIST_VIOLATION")
    expected_keys = {
        (case_id, variant_id)
        for case_id in case_input_kinds
        for variant_id in VARIANT_IDS_V4
    }
    if set(terminal_by_key) != expected_keys:
        _fail("NONBLIND_TERMINAL_CASE_SET_INVALID")
    terminal_provenance_count = 0
    for key in sorted(terminal_by_key):
        first = terminal_by_key[key]
        replay = replay_by_key[key]
        if (
            first.get("input_kind") != case_input_kinds[key[0]]
            or replay.get("input_kind") != case_input_kinds[key[0]]
            or first.get("run_spec_hash") != specs[key[1]].run_spec_hash
            or replay.get("run_spec_hash") != specs[key[1]].run_spec_hash
            or first.get("adapter_version") != specs[key[1]].adapter_version
            or replay.get("adapter_version") != specs[key[1]].adapter_version
            or first.get("repair_strategy") != specs[key[1]].repair_strategy
            or replay.get("repair_strategy") != specs[key[1]].repair_strategy
            or first.get("semantic_output_hash") != semantic_output_hash_v4(first)
            or replay.get("semantic_output_hash") != semantic_output_hash_v4(replay)
            or first.get("replay_hash") != first.get("semantic_output_hash")
            or replay.get("replay_hash") != replay.get("semantic_output_hash")
            or first.get("replay_hash") != replay.get("replay_hash")
        ):
            _fail("NONBLIND_REPLAY_HASH_MISMATCH")
        for row in (first, replay):
            try:
                P5TerminalOutputV4.model_validate(row)
            except Exception as exc:
                _fail("NONBLIND_TERMINAL_SCHEMA_INVALID", exc)
            capability = row.get("capability_outcomes")
            if not isinstance(capability, Mapping) or {
                "authoritative_oracle_access": capability.get(
                    "authoritative_oracle_access"
                ),
                "blind_label_access": capability.get("blind_label_access"),
                "external_api_calls": capability.get("external_api_calls"),
            } != {
                "authoritative_oracle_access": "DENIED",
                "blind_label_access": "DENIED",
                "external_api_calls": "0",
            }:
                _fail("NONBLIND_TERMINAL_CAPABILITY_BOUNDARY_INVALID")
            receipts = row.get("receipts", [])
            matching = [
                receipt
                for receipt in receipts
                if isinstance(receipt, Mapping)
                and receipt.get("type") == "ocr_replay_provenance"
            ]
            required = row.get("input_kind") == "SYNTHETIC_SCREENSHOT" and row.get(
                "variant_id"
            ) in {"core_b", "solver_c"}
            if required:
                if len(matching) != 1 or {
                    "mode": matching[0].get("mode"),
                    "fresh_model_inference": matching[0].get(
                        "fresh_model_inference"
                    ),
                    "receipt_match": matching[0].get("receipt_match"),
                    "cleanup_status": matching[0].get("cleanup_status"),
                    "cleanup_error_category": matching[0].get(
                        "cleanup_error_category"
                    ),
                    "temporary_original_absent": matching[0].get(
                        "temporary_original_absent"
                    ),
                } != {
                    "mode": "FROZEN_ACTUAL_OCR_RECEIPT_REPLAY",
                    "fresh_model_inference": False,
                    "receipt_match": True,
                    "cleanup_status": "DELETED",
                    "cleanup_error_category": None,
                    "temporary_original_absent": True,
                }:
                    _fail("NONBLIND_TERMINAL_OCR_PROVENANCE_INVALID")
                terminal_provenance_count += 1
            elif matching:
                _fail("NONBLIND_TERMINAL_OCR_PROVENANCE_UNEXPECTED")
    expected_ocr = {
        "lookup_count": NONBLIND_OCR_LOOKUP_COUNT_V4,
        "hit_count": NONBLIND_OCR_LOOKUP_COUNT_V4,
        "receipt_match_count": NONBLIND_OCR_LOOKUP_COUNT_V4,
        "cleanup_deleted_count": NONBLIND_OCR_LOOKUP_COUNT_V4,
        "miss_count": 0,
        "fallback_count": 0,
        "fresh_prediction_count": 0,
        "unique_hash_count": NONBLIND_SCREENSHOT_HASH_COUNT_V4,
    }
    if (
        set(result.screenshot_hashes) != expected_screenshot_hashes
        or len(result.screenshot_hashes) != NONBLIND_SCREENSHOT_HASH_COUNT_V4
        or terminal_provenance_count != NONBLIND_OCR_LOOKUP_COUNT_V4
        or any(result.ocr_provenance.get(key) != value for key, value in expected_ocr.items())
    ):
        _fail("NONBLIND_OCR_REPLAY_PROVENANCE_INVALID")


async def run_nonblind_v4(
    *,
    repo_root: Path,
    output_root: Path,
    run_id: str,
    subject_commit: str,
    upstream_ref: str,
    upstream_commit: str,
    dirty_tree: bool,
    dataset_paths: NonblindDatasetPathsV4 | None = None,
    engine: NonblindExecutionEngineV4 | None = None,
    require_formal: bool = True,
) -> dict[str, Any]:
    """Execute exactly 270 cases x three variants, then replay all 810."""

    if _RUN_ID.fullmatch(run_id) is None:
        _fail("NONBLIND_RUN_ID_INVALID")
    if dirty_tree or upstream_commit != subject_commit or not upstream_ref:
        _fail("NONBLIND_SUBJECT_NOT_CLEAN_UPSTREAM_COMMIT")
    root = repo_root.resolve()
    external_output = _require_external_directory(output_root, root)
    run_dir = external_output / run_id
    if run_dir.exists():
        _fail("NONBLIND_RUN_DIRECTORY_ALREADY_EXISTS")
    paths = dataset_paths or _default_dataset_paths()
    manifest, _seal, active, cases, materializations, template = (
        _validate_dataset_envelope(paths=paths, require_formal=require_formal)
    )
    candidate_commit = (
        str(active.get("candidate_freeze_commit"))
        if active is not None
        else "NOT_APPLICABLE"
    )
    if require_formal:
        _validate_git_subject(
            repo_root=root,
            subject_commit=subject_commit,
            upstream_ref=upstream_ref,
            upstream_commit=upstream_commit,
            candidate_freeze_commit=candidate_commit,
        )
    context = {
        "subject_commit": subject_commit,
        "dirty_tree": False,
        "dataset_manifest_hash": manifest["manifest_hash"],
        "case_set_hash": manifest["lanes"]["nonblind"]["case_set_hash"],
        "materialization_set_hash": manifest["lanes"]["nonblind"][
            "materialization_set_hash"
        ],
        "run_spec_template_sha256": file_sha256(paths.run_spec_template),
        "rubric_sha256": file_sha256(paths.rubric),
    }
    execution = await (engine or V3PayloadNonblindExecutionEngineV4()).execute(
        case_rows=cases,
        materialization_rows=materializations,
        run_spec_template=template,
        run_spec_context=context,
    )
    case_input_kinds = {
        str(case["case_id"]): str(case["input_kind"]) for case in cases
    }
    materialization_by_id = {
        str(row["case_id"]): row for row in materializations
    }
    expected_screenshot_hashes = {
        str(materialization_by_id[case_id]["ocr_baseline_receipt"]["asset_hash"])
        for case_id, input_kind in case_input_kinds.items()
        if input_kind == "SYNTHETIC_SCREENSHOT"
    }
    _validate_execution_result(
        execution,
        context=context,
        case_input_kinds=case_input_kinds,
        expected_screenshot_hashes=expected_screenshot_hashes,
    )
    terminals = list(execution.terminals)
    replays = list(execution.replay_terminals)
    run_dir.mkdir(parents=True)
    terminals_path = run_dir / "terminal_outputs.jsonl"
    replays_path = run_dir / "replay_readback.jsonl"
    _write_jsonl_atomic(terminals_path, terminals)
    _write_jsonl_atomic(replays_path, replays)
    entries = [
        {
            "path": terminals_path.name,
            "byte_size": terminals_path.stat().st_size,
            "sha256": file_sha256(terminals_path),
            "content_sha256": digest(terminals),
        },
        {
            "path": replays_path.name,
            "byte_size": replays_path.stat().st_size,
            "sha256": file_sha256(replays_path),
            "content_sha256": digest(replays),
        },
    ]
    artifact_index = {
        "schema_version": ARTIFACT_INDEX_SCHEMA_V4,
        "subject_commit": subject_commit,
        "dirty_tree": False,
        "entries": entries,
    }
    artifact_index["artifact_index_hash"] = digest(artifact_index)
    artifact_index_path = run_dir / "artifact_index.json"
    _atomic_write_json(artifact_index_path, artifact_index)
    run_manifest: dict[str, Any] = {
        "schema_version": RUN_GROUP_SCHEMA_V4,
        "run_id": run_id,
        "status": "PASS",
        "formal_evidence": require_formal,
        "lane": "nonblind",
        "subject_commit": subject_commit,
        "upstream_ref": upstream_ref,
        "upstream_commit": upstream_commit,
        "dirty_tree": False,
        "dataset_id": DATASET_ID_V4,
        "dataset_manifest_hash": manifest["manifest_hash"],
        "blind_seal_sha256": file_sha256(paths.seal)
        if require_formal
        else "NOT_APPLICABLE",
        "active_contract_file_sha256": file_sha256(paths.active_contract)
        if require_formal
        else "NOT_APPLICABLE",
        "candidate_freeze_commit": candidate_commit,
        "run_spec_template_sha256": file_sha256(paths.run_spec_template),
        "rubric_sha256": file_sha256(paths.rubric),
        "inputs_file_sha256": file_sha256(paths.cases),
        "materializations_file_sha256": file_sha256(paths.materializations),
        "case_count": NONBLIND_CASE_COUNT_V4,
        "case_set_hash": context["case_set_hash"],
        "materialization_set_hash": context["materialization_set_hash"],
        "variant_ids": list(VARIANT_IDS_V4),
        "variant_count": len(VARIANT_IDS_V4),
        "run_specs": dict(execution.run_specs),
        "terminal_count": NONBLIND_TERMINAL_COUNT_V4,
        "expected_terminal_count": NONBLIND_TERMINAL_COUNT_V4,
        "terminal_outputs_path": terminals_path.name,
        "terminal_outputs_file_sha256": file_sha256(terminals_path),
        "terminal_outputs_content_sha256": digest(terminals),
        "replay_outputs_path": replays_path.name,
        "replay_outputs_file_sha256": file_sha256(replays_path),
        "replay_outputs_content_sha256": digest(replays),
        "replay_executed": True,
        "replay_match_count": NONBLIND_TERMINAL_COUNT_V4,
        "replay_readback_count": NONBLIND_TERMINAL_COUNT_V4,
        "replay_mismatches": [],
        "replay_hash_policy": "p5-semantic-projection-v4",
        "artifact_index_path": artifact_index_path.name,
        "artifact_index_hash": artifact_index["artifact_index_hash"],
        "ocr_replay_provenance": {
            **dict(execution.ocr_provenance),
            "nonblind_unique_image_hashes": len(execution.screenshot_hashes),
            "terminal_provenance_count": NONBLIND_OCR_LOOKUP_COUNT_V4,
            "expected_formal_lookup_count": NONBLIND_OCR_LOOKUP_COUNT_V4,
        },
        "hidden_retry_count": 0,
        "blind_labels_read": False,
        "external_api_calls": 0,
        "fresh_ocr_model_inferences": 0,
        "human_calibration_performed": False,
        "human_evidence": False,
        "live_provider_evidence": False,
        "public_e2e_evidence": False,
    }
    run_manifest["manifest_hash"] = digest(run_manifest)
    _atomic_write_json(run_dir / "run_group_manifest.json", run_manifest)
    return {**run_manifest, "run_dir": str(run_dir)}


def _safe_run_artifact(run_dir: Path, name: object, expected: str) -> Path:
    if name != expected:
        _fail("NONBLIND_RUN_ARTIFACT_PATH_INVALID")
    path = run_dir / expected
    if _contains_link(path.absolute()):
        _fail("NONBLIND_RUN_ARTIFACT_LINK_FORBIDDEN")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        _fail("NONBLIND_RUN_ARTIFACT_UNREADABLE", exc)
    if not _is_inside(resolved, run_dir.resolve()):
        _fail("NONBLIND_RUN_ARTIFACT_PATH_ESCAPE")
    return resolved


def validate_nonblind_run_group_v4(
    *,
    run_dir: Path,
    repo_root: Path,
    require_formal: bool = True,
    dataset_paths: NonblindDatasetPathsV4 | None = None,
) -> tuple[dict[str, Any], list[Any], list[P5TerminalOutputV4], dict[str, dict[str, Any]]]:
    """Validate active/seal/git/dataset/artifacts before v4 scoring."""

    root = repo_root.resolve()
    absolute_run = run_dir.absolute()
    if _contains_link(absolute_run):
        _fail("NONBLIND_RUN_DIRECTORY_LINK_FORBIDDEN")
    try:
        resolved_run = absolute_run.resolve(strict=True)
    except OSError as exc:
        _fail("NONBLIND_RUN_DIRECTORY_UNREADABLE", exc)
    if _is_inside(resolved_run, root):
        _fail("NONBLIND_RUN_DIRECTORY_INSIDE_REPOSITORY")
    manifest_path = _safe_run_artifact(
        resolved_run, "run_group_manifest.json", "run_group_manifest.json"
    )
    run_manifest = _load_json(manifest_path, "NONBLIND_RUN_MANIFEST_INVALID")
    if run_manifest.get("manifest_hash") != digest(
        {key: value for key, value in run_manifest.items() if key != "manifest_hash"}
    ):
        _fail("NONBLIND_RUN_MANIFEST_HASH_MISMATCH")
    if (
        run_manifest.get("schema_version") != RUN_GROUP_SCHEMA_V4
        or run_manifest.get("status") != "PASS"
        or run_manifest.get("lane") != "nonblind"
        or run_manifest.get("dataset_id") != DATASET_ID_V4
        or run_manifest.get("variant_ids") != list(VARIANT_IDS_V4)
        or run_manifest.get("variant_count") != len(VARIANT_IDS_V4)
        or run_manifest.get("case_count") != NONBLIND_CASE_COUNT_V4
        or run_manifest.get("terminal_count") != NONBLIND_TERMINAL_COUNT_V4
        or run_manifest.get("expected_terminal_count")
        != NONBLIND_TERMINAL_COUNT_V4
        or run_manifest.get("replay_executed") is not True
        or run_manifest.get("replay_match_count") != NONBLIND_TERMINAL_COUNT_V4
        or run_manifest.get("replay_readback_count")
        != NONBLIND_TERMINAL_COUNT_V4
        or run_manifest.get("replay_mismatches") != []
        or run_manifest.get("replay_hash_policy") != "p5-semantic-projection-v4"
        or run_manifest.get("dirty_tree") is not False
        or run_manifest.get("upstream_commit") != run_manifest.get("subject_commit")
        or not run_manifest.get("upstream_ref")
        or run_manifest.get("hidden_retry_count") != 0
        or run_manifest.get("external_api_calls") != 0
        or run_manifest.get("fresh_ocr_model_inferences") != 0
        or run_manifest.get("blind_labels_read") is not False
    ):
        _fail("NONBLIND_RUN_CONTRACT_INVALID")
    if require_formal and run_manifest.get("formal_evidence") is not True:
        _fail("NONBLIND_FORMAL_RUN_REQUIRED")

    paths = dataset_paths or _default_dataset_paths()
    dataset, _seal, active, raw_cases, raw_materializations, _template = (
        _validate_dataset_envelope(paths=paths, require_formal=require_formal)
    )
    if (
        run_manifest.get("dataset_manifest_hash") != dataset["manifest_hash"]
        or run_manifest.get("run_spec_template_sha256")
        != file_sha256(paths.run_spec_template)
        or run_manifest.get("rubric_sha256") != file_sha256(paths.rubric)
        or run_manifest.get("inputs_file_sha256") != file_sha256(paths.cases)
        or run_manifest.get("materializations_file_sha256")
        != file_sha256(paths.materializations)
    ):
        _fail("NONBLIND_RUN_DATASET_BINDING_MISMATCH")
    if require_formal:
        if active is None or (
            run_manifest.get("blind_seal_sha256") != file_sha256(paths.seal)
            or run_manifest.get("active_contract_file_sha256")
            != file_sha256(paths.active_contract)
            or run_manifest.get("candidate_freeze_commit")
            != active.get("candidate_freeze_commit")
        ):
            _fail("NONBLIND_RUN_ACTIVE_SEAL_BINDING_MISMATCH")
        _validate_git_subject(
            repo_root=root,
            subject_commit=str(run_manifest.get("subject_commit")),
            upstream_ref=str(run_manifest.get("upstream_ref")),
            upstream_commit=str(run_manifest.get("upstream_commit")),
            candidate_freeze_commit=str(run_manifest.get("candidate_freeze_commit")),
        )

    terminal_path = _safe_run_artifact(
        resolved_run,
        run_manifest.get("terminal_outputs_path"),
        "terminal_outputs.jsonl",
    )
    replay_path = _safe_run_artifact(
        resolved_run,
        run_manifest.get("replay_outputs_path"),
        "replay_readback.jsonl",
    )
    artifact_index_path = _safe_run_artifact(
        resolved_run,
        run_manifest.get("artifact_index_path"),
        "artifact_index.json",
    )
    terminal_rows = _load_jsonl(terminal_path, "NONBLIND_TERMINALS_INVALID")
    replay_rows = _load_jsonl(replay_path, "NONBLIND_REPLAY_INVALID")
    if (
        run_manifest.get("terminal_outputs_file_sha256") != file_sha256(terminal_path)
        or run_manifest.get("terminal_outputs_content_sha256") != digest(terminal_rows)
        or run_manifest.get("replay_outputs_file_sha256") != file_sha256(replay_path)
        or run_manifest.get("replay_outputs_content_sha256") != digest(replay_rows)
    ):
        _fail("NONBLIND_RUN_OUTPUT_BINDING_MISMATCH")
    artifact_index = _load_json(
        artifact_index_path, "NONBLIND_ARTIFACT_INDEX_INVALID"
    )
    if (
        artifact_index.get("schema_version") != ARTIFACT_INDEX_SCHEMA_V4
        or artifact_index.get("artifact_index_hash")
        != digest(
            {
                key: value
                for key, value in artifact_index.items()
                if key != "artifact_index_hash"
            }
        )
        or run_manifest.get("artifact_index_hash")
        != artifact_index.get("artifact_index_hash")
        or artifact_index.get("entries")
        != [
            {
                "path": terminal_path.name,
                "byte_size": terminal_path.stat().st_size,
                "sha256": file_sha256(terminal_path),
                "content_sha256": digest(terminal_rows),
            },
            {
                "path": replay_path.name,
                "byte_size": replay_path.stat().st_size,
                "sha256": file_sha256(replay_path),
                "content_sha256": digest(replay_rows),
            },
        ]
    ):
        _fail("NONBLIND_ARTIFACT_INDEX_BINDING_INVALID")

    from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3
    from evals.trip_check_v1.p5.data_contract_v4 import validate_materialization_v4

    try:
        cases = [P5CaseV3.model_validate(row) for row in raw_cases]
        outputs = [P5TerminalOutputV4.model_validate(row) for row in terminal_rows]
        replay_outputs = [P5TerminalOutputV4.model_validate(row) for row in replay_rows]
    except Exception as exc:
        _fail("NONBLIND_RUN_PAYLOAD_SCHEMA_INVALID", exc)
    case_by_id = {case.case_id: case for case in cases}
    materialization_by_id: dict[str, dict[str, Any]] = {}
    for row in raw_materializations:
        case_id = str(row.get("case_id"))
        try:
            materialization_by_id[case_id] = validate_materialization_v4(
                case_by_id[case_id], row
            )
        except Exception as exc:
            _fail("NONBLIND_RUN_MATERIALIZATION_INVALID", exc)
    expected_keys = {
        (case.case_id, variant_id)
        for case in cases
        for variant_id in VARIANT_IDS_V4
    }
    output_by_key = {(item.case_id, item.variant_id): item for item in outputs}
    replay_by_key = {
        (item.case_id, item.variant_id): item for item in replay_outputs
    }
    if (
        len(output_by_key) != NONBLIND_TERMINAL_COUNT_V4
        or set(output_by_key) != expected_keys
        or set(replay_by_key) != expected_keys
        or any(
            output_by_key[key].replay_hash != replay_by_key[key].replay_hash
            or output_by_key[key].semantic_output_hash
            != semantic_output_hash_v4(output_by_key[key])
            or replay_by_key[key].semantic_output_hash
            != semantic_output_hash_v4(replay_by_key[key])
            for key in expected_keys
        )
    ):
        _fail("NONBLIND_RUN_EXACT_OUTPUT_SET_INVALID")
    return run_manifest, cases, outputs, materialization_by_id


__all__ = [
    "NONBLIND_CASE_COUNT_V4",
    "NONBLIND_OCR_LOOKUP_COUNT_V4",
    "NONBLIND_SCREENSHOT_HASH_COUNT_V4",
    "NONBLIND_TERMINAL_COUNT_V4",
    "NonblindDatasetPathsV4",
    "NonblindExecutionEngineV4",
    "NonblindExecutionResultV4",
    "P5NonblindRunnerErrorV4",
    "V3PayloadNonblindExecutionEngineV4",
    "run_nonblind_v4",
    "validate_nonblind_run_group_v4",
]
