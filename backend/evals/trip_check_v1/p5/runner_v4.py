"""One-shot, repository-external runner for the sealed P5 v4 blind lane.

The v4 envelope deliberately keeps the frozen v3 case/materialization payload
contract.  Imports of the product adapters and v3 Pydantic models are delayed
until execution or readback so the v4 dataset/seal slice can be integrated
independently.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import ConfigDict

from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest, file_sha256


DATASET_ID_V4 = "trip-check-p5-360-v4"
ACTIVE_CONTRACT_V4 = "trip-check-p5-v4"
VARIANT_IDS_V4 = ("legacy_a", "core_b", "solver_c")
BLIND_CASE_COUNT_V4 = 90
BLIND_TERMINAL_COUNT_V4 = 270
BLIND_SCREENSHOT_HASH_COUNT_V4 = 45
BLIND_OCR_REPLAY_COUNT_V4 = 180
RUN_GROUP_SCHEMA_V4 = "trip-check-p5-blind-run-group-v4"
NONCE_SCHEMA_V4 = "trip-check-p5-blind-run-nonce-v4"
CONSUMPTION_SCHEMA_V4 = "trip-check-p5-blind-run-consumption-receipt-v4"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")


class P5BlindRunnerErrorV4(RuntimeError):
    """Stable fail-closed error without blind case details."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _fail(reason_code: str, exc: Exception | None = None) -> None:
    if exc is None:
        raise P5BlindRunnerErrorV4(reason_code)
    raise P5BlindRunnerErrorV4(reason_code) from exc


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


def _is_inside(path: Path, root: Path) -> bool:
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


def _require_external_directory(path: Path, repo_root: Path, reason_code: str) -> Path:
    absolute = path.absolute()
    if ".." in absolute.parts or _contains_link(absolute):
        _fail(reason_code)
    try:
        resolved_parent = absolute.parent.resolve(strict=True)
    except OSError as exc:
        _fail(reason_code, exc)
    resolved = resolved_parent / absolute.name
    if _is_inside(resolved, repo_root.resolve()):
        _fail(reason_code)
    return resolved


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


@dataclass(frozen=True)
class BlindDatasetPathsV4:
    inputs: Path
    materializations: Path
    manifest: Path
    seal: Path
    run_spec_template: Path
    rubric: Path
    active_contract: Path


@dataclass(frozen=True)
class BlindExecutionResultV4:
    terminals: tuple[Mapping[str, Any], ...]
    replay_terminals: tuple[Mapping[str, Any], ...]
    run_specs: Mapping[str, Mapping[str, Any]]
    screenshot_hashes: frozenset[str]
    ocr_provenance: Mapping[str, int]


class BlindExecutionEngineV4(Protocol):
    async def execute(
        self,
        *,
        case_rows: Sequence[Mapping[str, Any]],
        materialization_rows: Sequence[Mapping[str, Any]],
        run_spec_template: Mapping[str, Any],
        run_spec_context: Mapping[str, Any],
    ) -> BlindExecutionResultV4: ...


# These two envelope models live beside the executor because v4 intentionally
# does not change the frozen case/materialization payload contract.
from evals.trip_check_v1.p5.contracts_v3 import (  # noqa: E402
    P5TerminalOutputV3,
    P5VariantRunSpecV3,
    TerminalStatusV3,
)


class P5VariantRunSpecV4(P5VariantRunSpecV3):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-variant-run-spec-v4"] = (
        "trip-check-p5-variant-run-spec-v4"
    )
    replay_hash_policy: Literal["p5-semantic-projection-v4"] = (
        "p5-semantic-projection-v4"
    )


class P5TerminalOutputV4(P5TerminalOutputV3):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-terminal-output-v4"] = (
        "trip-check-p5-terminal-output-v4"
    )


def build_run_spec_v4(
    *,
    lane: Literal["nonblind", "frozen_blind"],
    subject_commit: str,
    dirty_tree: bool,
    dataset_manifest_hash: str,
    case_set_hash: str,
    materialization_set_hash: str,
    run_spec_template_hash: str,
    rubric_hash: str,
    template: Mapping[str, Any],
    variant_id: str,
    adapter_versions: Mapping[str, tuple[str, str]] | None = None,
) -> P5VariantRunSpecV4:
    """Build one lane-neutral v4 RunSpec with an exact variant whitelist."""

    if adapter_versions is None:
        from evals.trip_check_v1.p5.adapters_v4 import ADAPTER_VERSIONS_V4

        adapter_versions = ADAPTER_VERSIONS_V4
    if variant_id not in VARIANT_IDS_V4 or set(adapter_versions) != set(VARIANT_IDS_V4):
        _fail("P5_V4_ADAPTER_SET_INVALID")
    adapter_version, repair_strategy = adapter_versions[variant_id]
    if not adapter_version.endswith("-v4"):
        _fail("P5_V4_ADAPTER_VERSION_INVALID")
    if template.get("variant_specs", {}).get(variant_id) != {
        "adapter_version": adapter_version,
        "repair_strategy": repair_strategy,
    }:
        _fail("P5_V4_RUN_SPEC_ADAPTER_BINDING_INVALID")
    historical_ocr = template.get("historical_ocr_evidence")
    if not isinstance(historical_ocr, Mapping):
        _fail("P5_V4_RUN_SPEC_OCR_POLICY_MISSING")
    if template.get("replay_hash_policy") != "p5-semantic-projection-v4":
        _fail("P5_V4_REPLAY_HASH_POLICY_INVALID")
    return P5VariantRunSpecV4(
        subject_commit=subject_commit,
        dirty_tree=dirty_tree,
        lane=lane,
        dataset_manifest_hash=dataset_manifest_hash,
        case_set_hash=case_set_hash,
        materialization_set_hash=materialization_set_hash,
        run_spec_template_hash=run_spec_template_hash,
        rubric_hash=rubric_hash,
        renderer_version=str(template["renderer"]["version"]),
        ocr_engine_version=str(historical_ocr["engine_version"]),
        evidence_policy_version=str(template["evidence_policy_version"]),
        fault_registry_version=str(template["fault_registry_version"]),
        random_seed=int(template["random_seed"]),
        budget=dict(template["budget"]),
        replay_hash_policy="p5-semantic-projection-v4",
        variant_id=variant_id,
        adapter_version=adapter_version,
        repair_strategy=repair_strategy,
    )


def _semantic_payload_v4(output: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "replay_hash_policy": "p5-semantic-projection-v4",
        "case_id": output["case_id"],
        "input_hash": output["input_hash"],
        "materialization_hash": output["materialization_hash"],
        "run_spec_hash": output["run_spec_hash"],
        "variant_id": output["variant_id"],
        "adapter_version": output["adapter_version"],
        "repair_strategy": output["repair_strategy"],
        "terminal_status": output["terminal_status"],
        "capability_outcomes": output["capability_outcomes"],
        "native_output": output["native_output"],
        "evaluation_projection": output["evaluation_projection"],
        "findings": output["findings"],
        "advice": output["advice"],
        "postcheck": output["postcheck"],
        "receipts": output["receipts"],
        "token_count": output["token_count"],
        "cost_usd": output["cost_usd"],
        "error_category": output["error_category"],
    }


def semantic_output_hash_v4(output: P5TerminalOutputV4 | Mapping[str, Any]) -> str:
    payload = (
        output.model_dump(mode="json")
        if isinstance(output, P5TerminalOutputV4)
        else dict(output)
    )
    return digest(_semantic_payload_v4(payload))


async def execute_terminal_v4(
    *,
    case: Any,
    materialization: Mapping[str, Any],
    run_spec: P5VariantRunSpecV4,
    adapter: Any,
) -> P5TerminalOutputV4:
    """Execute one v4 terminal in either lane with v4-only semantic hashing."""

    from evals.trip_check_v1.p5.adapters_v2 import _HarnessResult
    from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3
    from evals.trip_check_v1.p5.data_contract_v4 import validate_materialization_v4
    from time import perf_counter

    validated_case = case if isinstance(case, P5CaseV3) else P5CaseV3.model_validate(case)
    if (
        adapter.variant_id != run_spec.variant_id
        or adapter.adapter_version != run_spec.adapter_version
        or adapter.repair_strategy != run_spec.repair_strategy
    ):
        _fail("P5_V4_ADAPTER_RUN_SPEC_MISMATCH")
    if materialization.get("case_id") != validated_case.case_id:
        _fail("P5_V4_CASE_MATERIALIZATION_ID_MISMATCH")
    try:
        validated_materialization = validate_materialization_v4(
            validated_case, materialization
        )
    except Exception as exc:
        _fail("P5_V4_MATERIALIZATION_INVALID", exc)
    started = perf_counter()
    error_category: str | None = None
    try:
        result = await asyncio.wait_for(
            adapter.execute(validated_case, validated_materialization, run_spec),
            timeout=run_spec.budget.timeout_seconds,
        )
    except TimeoutError:
        error_category = "ADAPTER_DEADLINE_EXCEEDED"
        result = _HarnessResult(
            terminal_status=TerminalStatusV3.TIMEOUT,
            native_output={},
            evaluation_projection={},
            findings=[],
            advice=[],
            postcheck=None,
            receipts=[{"type": "runner_timeout", "contract": "v4"}],
            raw_artifact={},
        )
    except Exception as exc:
        error_category = type(exc).__name__
        result = _HarnessResult(
            terminal_status=TerminalStatusV3.ERROR,
            native_output={},
            evaluation_projection={},
            findings=[],
            advice=[],
            postcheck=None,
            receipts=[
                {
                    "type": "runner_error",
                    "contract": "v4",
                    "category": type(exc).__name__,
                }
            ],
            raw_artifact={},
        )
    screenshot_mode = (
        "DENIED_LEGACY_BOUNDARY"
        if validated_case.input_kind == "SYNTHETIC_SCREENSHOT"
        and run_spec.variant_id == "legacy_a"
        else "FROZEN_ACTUAL_OCR_RECEIPT_REPLAY"
        if validated_case.input_kind == "SYNTHETIC_SCREENSHOT"
        else "NOT_APPLICABLE"
    )
    binding = validated_case.materialization
    terminal_payload: dict[str, Any] = {
        "schema_version": "trip-check-p5-terminal-output-v4",
        "case_id": validated_case.case_id,
        "split": validated_case.split,
        "city": validated_case.city,
        "input_kind": validated_case.input_kind,
        "input_hash": validated_case.normalized_input_sha256,
        "materialization_hash": binding.materialization_sha256,
        "render_receipt_hash": (
            binding.render_receipt.content_sha256 if binding.render_receipt else None
        ),
        "ocr_receipt_hash": (
            binding.ocr_baseline_receipt.content_sha256
            if binding.ocr_baseline_receipt
            else None
        ),
        "provider_snapshot_hash": binding.provider_snapshot.content_sha256,
        "evidence_snapshot_hash": binding.evidence_snapshot.content_sha256,
        "candidate_set_hashes": [
            item.content_sha256 for item in binding.candidate_sets
        ],
        "fault_script_hash": binding.fault_script.content_sha256,
        "run_spec_hash": run_spec.run_spec_hash,
        "variant_id": run_spec.variant_id,
        "adapter_version": run_spec.adapter_version,
        "repair_strategy": run_spec.repair_strategy,
        "terminal_status": result.terminal_status.value,
        "capability_outcomes": {
            "authoritative_oracle_access": "DENIED",
            "blind_label_access": "DENIED",
            "external_api_calls": "0",
            "product_import": (
                "UNSUPPORTED"
                if result.native_output.get("product_import") is None
                else "PRODUCTION_SERVICE"
            ),
            "screenshot_execution": screenshot_mode,
        },
        "native_output": result.native_output,
        "evaluation_projection": result.evaluation_projection,
        "findings": result.findings,
        "advice": result.advice,
        "postcheck": result.postcheck,
        "receipts": result.receipts,
        "latency_ms": (perf_counter() - started) * 1000,
        "token_count": 0,
        "cost_usd": 0.0,
        "error_category": error_category,
        "raw_artifact_hash": digest(result.raw_artifact),
    }
    semantic_hash = digest(_semantic_payload_v4(terminal_payload))
    terminal_payload["semantic_output_hash"] = semantic_hash
    terminal_payload["replay_hash"] = semantic_hash
    return P5TerminalOutputV4.model_validate(terminal_payload)


class V3PayloadBlindExecutionEngineV4:
    """Execute the unchanged v3 payload through the frozen v3 product adapters."""

    async def execute(
        self,
        *,
        case_rows: Sequence[Mapping[str, Any]],
        materialization_rows: Sequence[Mapping[str, Any]],
        run_spec_template: Mapping[str, Any],
        run_spec_context: Mapping[str, Any],
    ) -> BlindExecutionResultV4:
        # Delayed by design: the v4 envelope does not own these payload contracts.
        from evals.trip_check_v1.p5.adapters_v3 import (
            EvaluationCachingPaddleOcrEngineV3,
        )
        from evals.trip_check_v1.p5.adapters_v4 import ADAPTERS_V4, ADAPTER_VERSIONS_V4
        from evals.trip_check_v1.p5.contracts_v3 import (
            P5CaseV3,
        )
        from evals.trip_check_v1.p5.data_contract_v4 import validate_materialization_v4

        try:
            cases = [P5CaseV3.model_validate(row) for row in case_rows]
        except Exception as exc:
            _fail("BLIND_CASE_PAYLOAD_INVALID", exc)
        if any(case.split != "frozen_blind" or case.oracle is not None for case in cases):
            _fail("BLIND_CASE_TRUTH_BOUNDARY_INVALID")
        materialization_by_case = {
            str(row.get("case_id")): row for row in materialization_rows
        }
        if len(materialization_by_case) != len(materialization_rows) or set(
            materialization_by_case
        ) != {case.case_id for case in cases}:
            _fail("BLIND_MATERIALIZATION_CASE_SET_INVALID")

        validated_materializations: dict[str, Mapping[str, Any]] = {}
        shared_ocr = EvaluationCachingPaddleOcrEngineV3()
        screenshot_hashes: set[str] = set()
        for case in cases:
            try:
                materialization = validate_materialization_v4(
                    case, materialization_by_case[case.case_id]
                )
            except Exception as exc:
                _fail("BLIND_MATERIALIZATION_PAYLOAD_INVALID", exc)
            validated_materializations[case.case_id] = materialization
            receipt = materialization.get("ocr_baseline_receipt")
            render = materialization.get("render_receipt")
            if case.input_kind == "SYNTHETIC_SCREENSHOT":
                if not isinstance(receipt, Mapping) or not isinstance(render, Mapping):
                    _fail("BLIND_SCREENSHOT_RECEIPT_MISSING")
                if receipt.get("asset_hash") != render.get("image_sha256"):
                    _fail("BLIND_SCREENSHOT_RECEIPT_HASH_MISMATCH")
                shared_ocr.preload(receipt)
                screenshot_hashes.add(str(receipt["asset_hash"]))
            elif receipt is not None or render is not None:
                _fail("BLIND_TEXT_CASE_HAS_SCREENSHOT_RECEIPT")

        historical_ocr = run_spec_template.get("historical_ocr_evidence")
        if not isinstance(historical_ocr, Mapping):
            _fail("BLIND_RUN_SPEC_OCR_POLICY_MISSING")
        if run_spec_template.get("allowed_variant_differences") != [
            "variant_id",
            "adapter_version",
            "repair_strategy",
        ]:
            _fail("BLIND_RUN_SPEC_VARIANT_WHITELIST_INVALID")
        if run_spec_template.get("execution_mode") != "controlled_snapshot":
            _fail("BLIND_RUN_SPEC_EXECUTION_MODE_INVALID")
        if run_spec_template.get("replay_hash_policy") != "p5-semantic-projection-v4":
            _fail("BLIND_RUN_SPEC_REPLAY_POLICY_INVALID")

        specs: dict[str, Any] = {}
        serialized_specs: dict[str, Mapping[str, Any]] = {}
        for variant_id in VARIANT_IDS_V4:
            spec = build_run_spec_v4(
                lane="frozen_blind",
                subject_commit=run_spec_context["subject_commit"],
                dirty_tree=run_spec_context["dirty_tree"],
                dataset_manifest_hash=run_spec_context["dataset_manifest_hash"],
                case_set_hash=run_spec_context["case_set_hash"],
                materialization_set_hash=run_spec_context["materialization_set_hash"],
                run_spec_template_hash=run_spec_context["run_spec_template_sha256"],
                rubric_hash=run_spec_context["rubric_sha256"],
                template=run_spec_template,
                variant_id=variant_id,
                adapter_versions=ADAPTER_VERSIONS_V4,
            )
            specs[variant_id] = spec
            serialized_specs[variant_id] = spec.model_dump(mode="json")

        terminals: list[Mapping[str, Any]] = []
        replays: list[Mapping[str, Any]] = []
        for variant_id in VARIANT_IDS_V4:
            adapter = (
                ADAPTERS_V4[variant_id](ocr_engine=shared_ocr)
                if variant_id in {"core_b", "solver_c"}
                else ADAPTERS_V4[variant_id]()
            )
            for case in cases:
                first = await execute_terminal_v4(
                    case=case,
                    materialization=validated_materializations[case.case_id],
                    run_spec=specs[variant_id],
                    adapter=adapter,
                )
                replay = await execute_terminal_v4(
                    case=case,
                    materialization=validated_materializations[case.case_id],
                    run_spec=specs[variant_id],
                    adapter=adapter,
                )
                terminals.append(first.model_dump(mode="json"))
                replays.append(replay.model_dump(mode="json"))
        return BlindExecutionResultV4(
            terminals=tuple(terminals),
            replay_terminals=tuple(replays),
            run_specs=serialized_specs,
            screenshot_hashes=frozenset(screenshot_hashes),
            ocr_provenance=shared_ocr.provenance(),
        )


def _validate_dataset_envelope(
    *, paths: BlindDatasetPathsV4
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    manifest = _load_json(paths.manifest, "BLIND_DATASET_MANIFEST_INVALID")
    seal = _load_json(paths.seal, "BLIND_SEAL_INVALID")
    active = _load_json(paths.active_contract, "BLIND_ACTIVE_CONTRACT_INVALID")
    template = _load_json(paths.run_spec_template, "BLIND_RUN_SPEC_TEMPLATE_INVALID")
    if (
        manifest.get("schema_version") != "trip-check-p5-dataset-manifest-v4"
        or manifest.get("dataset_id") != DATASET_ID_V4
        or manifest.get("manifest_hash")
        != digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
        or manifest.get("frozen") is not True
        or manifest.get("formal_validation_eligible") is not True
        or manifest.get("seal_status") != "SEALED"
    ):
        _fail("BLIND_DATASET_ENVELOPE_INVALID")
    if (
        seal.get("schema_version") != "trip-check-p5-blind-seal-v4"
        or seal.get("split") != "frozen_blind"
        or seal.get("case_count") != BLIND_CASE_COUNT_V4
        or seal.get("label_storage") != "external_bundle_only"
        or seal.get("label_access") != "isolated_scorer_only"
        or seal.get("scoring_payload_present") is not False
        or seal.get("human_evidence") is not False
    ):
        _fail("BLIND_SEAL_ENVELOPE_INVALID")
    if (
        active.get("schema_version") != "trip-check-p5-active-contract-v1"
        or active.get("active_contract") != ACTIVE_CONTRACT_V4
        or active.get("formal_evidence_status") != "READY"
        or active.get("dataset_manifest_hash") != manifest["manifest_hash"]
        or active.get("blind_seal_v4_sha256") != file_sha256(paths.seal)
    ):
        _fail("P5_V4_FORMAL_CONTRACT_NOT_READY")
    commitment = manifest.get("sealing_commitment")
    if not isinstance(commitment, Mapping) or (
        commitment.get("blind_seal_file_sha256") != file_sha256(paths.seal)
        or commitment.get("external_bundle_sha256")
        != seal.get("external_bundle_sha256")
        or commitment.get("labels_canonical_sha256")
        != seal.get("labels_canonical_sha256")
    ):
        _fail("BLIND_SEAL_COMMITMENT_MISMATCH")

    files = manifest.get("files")
    lane = manifest.get("lanes", {}).get("frozen_blind")
    if not isinstance(files, Mapping) or not isinstance(lane, Mapping):
        _fail("BLIND_DATASET_FILE_BINDING_MISSING")
    inputs = _load_jsonl(paths.inputs, "BLIND_INPUTS_INVALID")
    materializations = _load_jsonl(
        paths.materializations, "BLIND_MATERIALIZATIONS_INVALID"
    )
    for name, path, rows in (
        ("blind_cases", paths.inputs, inputs),
        ("blind_materializations", paths.materializations, materializations),
    ):
        binding = files.get(name)
        if not isinstance(binding, Mapping) or (
            binding.get("row_count") != len(rows)
            or binding.get("file_sha256") != file_sha256(path)
            or binding.get("content_sha256") != digest(rows)
        ):
            _fail("BLIND_DATASET_FILE_BINDING_MISMATCH")
    case_ids = [row.get("case_id") for row in inputs]
    materialization_ids = [row.get("case_id") for row in materializations]
    if (
        len(inputs) != BLIND_CASE_COUNT_V4
        or len(materializations) != BLIND_CASE_COUNT_V4
        or len(set(case_ids)) != BLIND_CASE_COUNT_V4
        or len(set(materialization_ids)) != BLIND_CASE_COUNT_V4
        or set(case_ids) != set(materialization_ids)
        or any(row.get("split") != "frozen_blind" for row in inputs)
        or any("oracle" in row or "oracle_sha256" in row for row in inputs)
    ):
        _fail("BLIND_CASE_SET_INVALID")
    if (
        lane.get("case_count") != BLIND_CASE_COUNT_V4
        or lane.get("materialization_count") != BLIND_CASE_COUNT_V4
        or lane.get("case_set_hash") != manifest["lanes"]["frozen_blind"]["case_set_hash"]
        or lane.get("label_payload_present") is not False
    ):
        _fail("BLIND_LANE_BINDING_INVALID")
    if (
        seal.get("inputs_file_sha256") != file_sha256(paths.inputs)
        or seal.get("inputs_content_sha256") != digest(inputs)
        or seal.get("materializations_file_sha256")
        != file_sha256(paths.materializations)
        or seal.get("materializations_content_sha256") != digest(materializations)
        or seal.get("run_spec_template_sha256")
        != file_sha256(paths.run_spec_template)
        or seal.get("rubric_sha256") != file_sha256(paths.rubric)
        or seal.get("variant_ids_sha256") != digest(list(VARIANT_IDS_V4))
    ):
        _fail("BLIND_SEAL_ARTIFACT_BINDING_MISMATCH")
    return manifest, seal, active, inputs, materializations, template


def _claim_nonce(
    *, nonce_file: Path, consumption_dir: Path, dataset_manifest_hash: str
) -> tuple[str, Path, dict[str, Any]]:
    nonce_envelope = _load_json(nonce_file, "BLIND_NONCE_INVALID")
    if set(nonce_envelope) != {
        "schema_version",
        "purpose",
        "dataset_id",
        "active_contract",
        "nonce",
    } or (
        nonce_envelope.get("schema_version") != NONCE_SCHEMA_V4
        or nonce_envelope.get("purpose") != "execute_frozen_blind_once"
        or nonce_envelope.get("dataset_id") != DATASET_ID_V4
        or nonce_envelope.get("active_contract") != ACTIVE_CONTRACT_V4
        or not isinstance(nonce_envelope.get("nonce"), str)
        or _HEX_64.fullmatch(str(nonce_envelope.get("nonce"))) is None
    ):
        _fail("BLIND_NONCE_CONTRACT_INVALID")
    nonce_sha256 = digest(nonce_envelope["nonce"])
    receipt_path = consumption_dir / f"{nonce_sha256}.consumed.json"
    claimed_at = datetime.now(timezone.utc).isoformat()
    claim = {
        "schema_version": CONSUMPTION_SCHEMA_V4,
        "status": "CLAIMED",
        "dataset_id": DATASET_ID_V4,
        "dataset_manifest_hash": dataset_manifest_hash,
        "nonce_sha256": nonce_sha256,
        "claimed_at": claimed_at,
        "completed_at": None,
        "run_id": None,
        "run_binding_hash": None,
        "artifact_index_hash": None,
        "failure_reason_code": None,
    }
    consumption_dir.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            receipt_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError as exc:
        _fail("BLIND_NONCE_ALREADY_CONSUMED", exc)
    except OSError as exc:
        _fail("BLIND_NONCE_CLAIM_FAILED", exc)
    try:
        payload = canonical_bytes(claim) + b"\n"
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return nonce_sha256, receipt_path, claim


def _terminal_key(row: Mapping[str, Any]) -> tuple[str, str]:
    case_id = row.get("case_id")
    variant_id = row.get("variant_id")
    if not isinstance(case_id, str) or variant_id not in VARIANT_IDS_V4:
        _fail("BLIND_TERMINAL_KEY_INVALID")
    return case_id, str(variant_id)


def _validate_execution_result(result: BlindExecutionResultV4) -> list[dict[str, str]]:
    terminals = list(result.terminals)
    replays = list(result.replay_terminals)
    if len(terminals) != BLIND_TERMINAL_COUNT_V4 or len(replays) != BLIND_TERMINAL_COUNT_V4:
        _fail("BLIND_TERMINAL_COUNT_INVALID")
    terminal_by_key = {_terminal_key(row): row for row in terminals}
    replay_by_key = {_terminal_key(row): row for row in replays}
    if (
        len(terminal_by_key) != BLIND_TERMINAL_COUNT_V4
        or len(replay_by_key) != BLIND_TERMINAL_COUNT_V4
        or set(terminal_by_key) != set(replay_by_key)
        or set(result.run_specs) != set(VARIANT_IDS_V4)
    ):
        _fail("BLIND_TERMINAL_EXACT_SET_INVALID")
    mismatches = []
    terminal_provenance_count = 0
    for key in sorted(terminal_by_key):
        terminal = terminal_by_key[key]
        replay_terminal = replay_by_key[key]
        first = terminal.get("replay_hash")
        replay = replay_terminal.get("replay_hash")
        semantic_valid = (
            terminal.get("semantic_output_hash") == semantic_output_hash_v4(terminal)
            and replay_terminal.get("semantic_output_hash")
            == semantic_output_hash_v4(replay_terminal)
        )
        if not isinstance(first, str) or first != replay or not semantic_valid:
            mismatches.append(
                {
                    "case_id": key[0],
                    "variant_id": key[1],
                    "first": str(first),
                    "second": str(replay),
                }
            )
        for row in (terminal, replay_terminal):
            receipts = row.get("receipts", [])
            matching = [
                receipt
                for receipt in receipts
                if isinstance(receipt, Mapping)
                and receipt.get("type") == "ocr_replay_provenance"
            ]
            if row.get("input_kind") == "SYNTHETIC_SCREENSHOT" and row.get(
                "variant_id"
            ) in {"core_b", "solver_c"}:
                if len(matching) != 1:
                    _fail("BLIND_TERMINAL_OCR_PROVENANCE_MISSING")
                receipt = matching[0]
                if (
                    receipt.get("mode") != "FROZEN_ACTUAL_OCR_RECEIPT_REPLAY"
                    or receipt.get("fresh_model_inference") is not False
                    or receipt.get("receipt_match") is not True
                    or receipt.get("cleanup_status") != "DELETED"
                    or receipt.get("cleanup_error_category") is not None
                    or receipt.get("temporary_original_absent") is not True
                ):
                    _fail("BLIND_TERMINAL_OCR_PROVENANCE_INVALID")
                terminal_provenance_count += 1
            elif matching:
                _fail("BLIND_TERMINAL_OCR_PROVENANCE_UNEXPECTED")
    if mismatches:
        _fail("BLIND_REPLAY_HASH_MISMATCH")
    expected_ocr = {
        "lookup_count": BLIND_OCR_REPLAY_COUNT_V4,
        "hit_count": BLIND_OCR_REPLAY_COUNT_V4,
        "receipt_match_count": BLIND_OCR_REPLAY_COUNT_V4,
        "cleanup_deleted_count": BLIND_OCR_REPLAY_COUNT_V4,
        "miss_count": 0,
        "fallback_count": 0,
        "fresh_prediction_count": 0,
        "unique_hash_count": BLIND_SCREENSHOT_HASH_COUNT_V4,
    }
    if len(result.screenshot_hashes) != BLIND_SCREENSHOT_HASH_COUNT_V4 or any(
        result.ocr_provenance.get(key) != value for key, value in expected_ocr.items()
    ) or terminal_provenance_count != BLIND_OCR_REPLAY_COUNT_V4:
        _fail("BLIND_OCR_REPLAY_PROVENANCE_INVALID")
    return mismatches


async def run_blind_once_v4(
    *,
    repo_root: Path,
    dataset_paths: BlindDatasetPathsV4,
    output_root: Path,
    consumption_dir: Path,
    nonce_file: Path,
    run_id: str,
    subject_commit: str,
    upstream_ref: str,
    upstream_commit: str,
    dirty_tree: bool,
    engine: BlindExecutionEngineV4 | None = None,
) -> dict[str, Any]:
    """Execute the exact 90 x 3 blind lane once, never reading label material."""

    if _RUN_ID.fullmatch(run_id) is None:
        _fail("BLIND_RUN_ID_INVALID")
    if dirty_tree or upstream_commit != subject_commit or not upstream_ref:
        _fail("BLIND_SUBJECT_NOT_CLEAN_UPSTREAM_COMMIT")
    root = repo_root.resolve()
    external_output = _require_external_directory(
        output_root, root, "BLIND_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY"
    )
    external_consumption = _require_external_directory(
        consumption_dir, root, "BLIND_CONSUMPTION_LEDGER_MUST_BE_OUTSIDE_REPOSITORY"
    )
    run_dir = external_output / run_id
    if run_dir.exists():
        _fail("BLIND_RUN_DIRECTORY_ALREADY_EXISTS")

    manifest, seal, active, inputs, materializations, template = (
        _validate_dataset_envelope(paths=dataset_paths)
    )
    candidate_commit = str(
        manifest.get("sealing_commitment", {}).get("candidate_freeze_commit", "")
    )
    if active.get("candidate_freeze_commit") != candidate_commit:
        _fail("BLIND_CANDIDATE_FREEZE_MISMATCH")

    nonce_sha256, consumption_receipt_path, claim = _claim_nonce(
        nonce_file=nonce_file,
        consumption_dir=external_consumption,
        dataset_manifest_hash=str(manifest["manifest_hash"]),
    )
    run_dir.mkdir(parents=True)
    try:
        context = {
            "subject_commit": subject_commit,
            "dirty_tree": dirty_tree,
            "dataset_manifest_hash": manifest["manifest_hash"],
            "case_set_hash": manifest["lanes"]["frozen_blind"]["case_set_hash"],
            "materialization_set_hash": manifest["lanes"]["frozen_blind"][
                "materialization_set_hash"
            ],
            "run_spec_template_sha256": file_sha256(dataset_paths.run_spec_template),
            "rubric_sha256": file_sha256(dataset_paths.rubric),
        }
        execution = await (engine or V3PayloadBlindExecutionEngineV4()).execute(
            case_rows=inputs,
            materialization_rows=materializations,
            run_spec_template=template,
            run_spec_context=context,
        )
        replay_mismatches = _validate_execution_result(execution)
        terminals = list(execution.terminals)
        replays = list(execution.replay_terminals)
        terminals_path = run_dir / "terminal_outputs.jsonl"
        replays_path = run_dir / "replay_readback.jsonl"
        _write_jsonl_atomic(terminals_path, terminals)
        _write_jsonl_atomic(replays_path, replays)
        artifacts = [
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
            "schema_version": "trip-check-p5-blind-artifact-index-v4",
            "subject_commit": subject_commit,
            "dirty_tree": False,
            "entries": artifacts,
        }
        artifact_index["artifact_index_hash"] = digest(artifact_index)
        artifact_index_path = run_dir / "artifact_index.json"
        _atomic_write_json(artifact_index_path, artifact_index)

        manifest_core: dict[str, Any] = {
            "schema_version": RUN_GROUP_SCHEMA_V4,
            "run_id": run_id,
            "status": "PASS",
            "formal_evidence": True,
            "lane": "frozen_blind",
            "subject_commit": subject_commit,
            "upstream_ref": upstream_ref,
            "upstream_commit": upstream_commit,
            "dirty_tree": False,
            "dataset_id": DATASET_ID_V4,
            "dataset_manifest_hash": manifest["manifest_hash"],
            "blind_seal_sha256": file_sha256(dataset_paths.seal),
            "run_spec_template_sha256": file_sha256(
                dataset_paths.run_spec_template
            ),
            "rubric_sha256": file_sha256(dataset_paths.rubric),
            "inputs_file_sha256": file_sha256(dataset_paths.inputs),
            "materializations_file_sha256": file_sha256(
                dataset_paths.materializations
            ),
            "case_count": BLIND_CASE_COUNT_V4,
            "case_set_hash": context["case_set_hash"],
            "materialization_set_hash": context["materialization_set_hash"],
            "variant_ids": list(VARIANT_IDS_V4),
            "variant_count": len(VARIANT_IDS_V4),
            "run_specs": dict(execution.run_specs),
            "terminal_count": BLIND_TERMINAL_COUNT_V4,
            "expected_terminal_count": BLIND_TERMINAL_COUNT_V4,
            "terminal_outputs_path": terminals_path.name,
            "terminal_outputs_file_sha256": file_sha256(terminals_path),
            "terminal_outputs_content_sha256": digest(terminals),
            "replay_outputs_path": replays_path.name,
            "replay_outputs_file_sha256": file_sha256(replays_path),
            "replay_outputs_content_sha256": digest(replays),
            "replay_executed": True,
            "replay_readback_count": BLIND_TERMINAL_COUNT_V4,
            "replay_mismatches": replay_mismatches,
            "replay_hash_policy": "p5-semantic-projection-v4",
            "artifact_index_path": artifact_index_path.name,
            "artifact_index_hash": artifact_index["artifact_index_hash"],
            "ocr_replay_provenance": {
                **dict(execution.ocr_provenance),
                "blind_unique_image_hashes": len(execution.screenshot_hashes),
                "terminal_provenance_count": BLIND_OCR_REPLAY_COUNT_V4,
                "expected_formal_lookup_count": BLIND_OCR_REPLAY_COUNT_V4,
            },
            "hidden_retry_count": 0,
            "blind_labels_read": False,
            "external_api_calls": 0,
            "fresh_ocr_model_inferences": 0,
            "human_calibration_performed": False,
            "human_evidence": False,
            "live_provider_evidence": False,
            "public_e2e_evidence": False,
            "active_contract_file_sha256": file_sha256(
                dataset_paths.active_contract
            ),
            "candidate_freeze_commit": candidate_commit,
            "nonce_sha256": nonce_sha256,
        }
        run_binding_hash = digest(manifest_core)
        consumed = {
            **claim,
            "status": "CONSUMED",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "run_binding_hash": run_binding_hash,
            "artifact_index_hash": artifact_index["artifact_index_hash"],
        }
        _atomic_write_json(consumption_receipt_path, consumed)
        run_manifest = {
            **manifest_core,
            "run_binding_hash": run_binding_hash,
            "nonce_consumption_receipt_sha256": file_sha256(
                consumption_receipt_path
            ),
        }
        run_manifest["manifest_hash"] = digest(run_manifest)
        _atomic_write_json(run_dir / "run_group_manifest.json", run_manifest)
        return {**run_manifest, "run_dir": str(run_dir)}
    except Exception as exc:
        reason = getattr(exc, "reason_code", "BLIND_EXECUTION_FAILED")
        failed = {
            **claim,
            "status": "FAILED",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "failure_reason_code": str(reason),
        }
        _atomic_write_json(consumption_receipt_path, failed)
        if isinstance(exc, P5BlindRunnerErrorV4):
            raise
        _fail("BLIND_EXECUTION_FAILED", exc)
    raise AssertionError("unreachable")


def _safe_run_artifact(run_dir: Path, name: object, expected: str) -> Path:
    if name != expected:
        _fail("BLIND_RUN_ARTIFACT_PATH_INVALID")
    path = run_dir / expected
    if _contains_link(path.absolute()):
        _fail("BLIND_RUN_ARTIFACT_LINK_FORBIDDEN")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        _fail("BLIND_RUN_ARTIFACT_UNREADABLE", exc)
    if not _is_inside(resolved, run_dir.resolve()):
        _fail("BLIND_RUN_ARTIFACT_PATH_ESCAPE")
    return resolved


def validate_blind_run_group_v4(
    *,
    run_dir: Path,
    repo_root: Path,
    require_formal: bool = True,
    dataset_paths: BlindDatasetPathsV4 | None = None,
) -> tuple[dict[str, Any], list[Any], list[Any], dict[str, dict[str, Any]]]:
    """Read back the blind run for Judge/scorer without touching label material."""

    if dataset_paths is None:
        # The dataset/seal slice owns these constants; keep the import optional
        # until that slice is integrated.
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

        dataset_paths = BlindDatasetPathsV4(
            inputs=BLIND_INPUT_PATH_V4,
            materializations=BLIND_MATERIALIZATIONS_PATH_V4,
            manifest=MANIFEST_PATH_V4,
            seal=BLIND_SEAL_PATH_V4,
            run_spec_template=RUN_SPEC_TEMPLATE_PATH_V4,
            rubric=JUDGE_RUBRIC_PATH_V2,
            active_contract=ACTIVE_CONTRACT_PATH,
        )
    root = repo_root.resolve()
    absolute_run = run_dir.absolute()
    if _contains_link(absolute_run):
        _fail("BLIND_RUN_DIRECTORY_LINK_FORBIDDEN")
    try:
        resolved_run = absolute_run.resolve(strict=True)
    except OSError as exc:
        _fail("BLIND_RUN_DIRECTORY_UNREADABLE", exc)
    if _is_inside(resolved_run, root):
        _fail("BLIND_RUN_DIRECTORY_INSIDE_REPOSITORY")
    manifest_path = _safe_run_artifact(
        resolved_run, "run_group_manifest.json", "run_group_manifest.json"
    )
    run_manifest = _load_json(manifest_path, "BLIND_RUN_MANIFEST_INVALID")
    if run_manifest.get("manifest_hash") != digest(
        {key: value for key, value in run_manifest.items() if key != "manifest_hash"}
    ):
        _fail("BLIND_RUN_MANIFEST_HASH_MISMATCH")
    if (
        run_manifest.get("schema_version") != RUN_GROUP_SCHEMA_V4
        or run_manifest.get("lane") != "frozen_blind"
        or run_manifest.get("status") != "PASS"
        or run_manifest.get("dataset_id") != DATASET_ID_V4
        or run_manifest.get("variant_ids") != list(VARIANT_IDS_V4)
        or run_manifest.get("variant_count") != len(VARIANT_IDS_V4)
        or run_manifest.get("case_count") != BLIND_CASE_COUNT_V4
        or run_manifest.get("terminal_count") != BLIND_TERMINAL_COUNT_V4
        or run_manifest.get("expected_terminal_count")
        != BLIND_TERMINAL_COUNT_V4
        or run_manifest.get("replay_executed") is not True
        or run_manifest.get("replay_readback_count")
        != BLIND_TERMINAL_COUNT_V4
        or run_manifest.get("replay_mismatches") != []
        or run_manifest.get("replay_hash_policy") != "p5-semantic-projection-v4"
        or run_manifest.get("blind_labels_read") is not False
        or run_manifest.get("hidden_retry_count") != 0
        or run_manifest.get("external_api_calls") != 0
        or run_manifest.get("fresh_ocr_model_inferences") != 0
    ):
        _fail("BLIND_RUN_CONTRACT_INVALID")
    if require_formal and (
        run_manifest.get("formal_evidence") is not True
        or run_manifest.get("dirty_tree") is not False
        or run_manifest.get("upstream_commit") != run_manifest.get("subject_commit")
        or not run_manifest.get("upstream_ref")
    ):
        _fail("BLIND_FORMAL_SUBJECT_BINDING_INVALID")

    dataset, _seal, _active, raw_cases, raw_materializations, _template = (
        _validate_dataset_envelope(paths=dataset_paths)
    )
    if (
        run_manifest.get("dataset_manifest_hash") != dataset.get("manifest_hash")
        or run_manifest.get("blind_seal_sha256") != file_sha256(dataset_paths.seal)
        or run_manifest.get("run_spec_template_sha256")
        != file_sha256(dataset_paths.run_spec_template)
        or run_manifest.get("inputs_file_sha256") != file_sha256(dataset_paths.inputs)
        or run_manifest.get("materializations_file_sha256")
        != file_sha256(dataset_paths.materializations)
    ):
        _fail("BLIND_RUN_DATASET_BINDING_MISMATCH")

    terminal_path = _safe_run_artifact(
        resolved_run,
        run_manifest.get("terminal_outputs_path"),
        "terminal_outputs.jsonl",
    )
    replay_path = _safe_run_artifact(
        resolved_run, run_manifest.get("replay_outputs_path"), "replay_readback.jsonl"
    )
    artifact_index_path = _safe_run_artifact(
        resolved_run, run_manifest.get("artifact_index_path"), "artifact_index.json"
    )
    terminal_rows = _load_jsonl(terminal_path, "BLIND_TERMINALS_INVALID")
    replay_rows = _load_jsonl(replay_path, "BLIND_REPLAY_READBACK_INVALID")
    if (
        run_manifest.get("terminal_outputs_file_sha256") != file_sha256(terminal_path)
        or run_manifest.get("terminal_outputs_content_sha256") != digest(terminal_rows)
        or run_manifest.get("replay_outputs_file_sha256") != file_sha256(replay_path)
        or run_manifest.get("replay_outputs_content_sha256") != digest(replay_rows)
    ):
        _fail("BLIND_RUN_OUTPUT_BINDING_MISMATCH")
    artifact_index = _load_json(
        artifact_index_path, "BLIND_ARTIFACT_INDEX_INVALID"
    )
    if (
        artifact_index.get("artifact_index_hash")
        != digest(
            {
                key: value
                for key, value in artifact_index.items()
                if key != "artifact_index_hash"
            }
        )
        or run_manifest.get("artifact_index_hash")
        != artifact_index.get("artifact_index_hash")
    ):
        _fail("BLIND_ARTIFACT_INDEX_HASH_MISMATCH")

    from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3
    from evals.trip_check_v1.p5.data_contract_v4 import validate_materialization_v4

    try:
        cases = [P5CaseV3.model_validate(row) for row in raw_cases]
        outputs = [P5TerminalOutputV4.model_validate(row) for row in terminal_rows]
        replay_outputs = [P5TerminalOutputV4.model_validate(row) for row in replay_rows]
    except Exception as exc:
        _fail("BLIND_RUN_PAYLOAD_SCHEMA_INVALID", exc)
    case_by_id = {case.case_id: case for case in cases}
    materialization_by_id: dict[str, dict[str, Any]] = {}
    for row in raw_materializations:
        case_id = str(row.get("case_id"))
        try:
            materialization_by_id[case_id] = validate_materialization_v4(
                case_by_id[case_id], row
            )
        except Exception as exc:
            _fail("BLIND_RUN_MATERIALIZATION_INVALID", exc)
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
        len(output_by_key) != BLIND_TERMINAL_COUNT_V4
        or set(output_by_key) != expected_keys
        or set(replay_by_key) != expected_keys
        or any(
            output_by_key[key].replay_hash != replay_by_key[key].replay_hash
            for key in expected_keys
        )
    ):
        _fail("BLIND_RUN_EXACT_OUTPUT_SET_INVALID")
    return run_manifest, cases, outputs, materialization_by_id


__all__ = [
    "ACTIVE_CONTRACT_V4",
    "BLIND_CASE_COUNT_V4",
    "BLIND_OCR_REPLAY_COUNT_V4",
    "BLIND_SCREENSHOT_HASH_COUNT_V4",
    "BLIND_TERMINAL_COUNT_V4",
    "BlindDatasetPathsV4",
    "BlindExecutionEngineV4",
    "BlindExecutionResultV4",
    "DATASET_ID_V4",
    "P5BlindRunnerErrorV4",
    "V3PayloadBlindExecutionEngineV4",
    "VARIANT_IDS_V4",
    "run_blind_once_v4",
    "validate_blind_run_group_v4",
]
