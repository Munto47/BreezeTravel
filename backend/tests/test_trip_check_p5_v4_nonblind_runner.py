from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from evals.trip_check_v1.p5.adapters_v4 import ADAPTER_VERSIONS_V4
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.nonblind_runner_v4 import (
    NONBLIND_CASE_COUNT_V4,
    NONBLIND_OCR_LOOKUP_COUNT_V4,
    NONBLIND_SCREENSHOT_HASH_COUNT_V4,
    NONBLIND_TERMINAL_COUNT_V4,
    NonblindExecutionResultV4,
    P5NonblindRunnerErrorV4,
    run_nonblind_v4,
    validate_nonblind_run_group_v4,
)
from evals.trip_check_v1.p5.runner_v4 import (
    VARIANT_IDS_V4,
    build_run_spec_v4,
    semantic_output_hash_v4,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class _FixtureEngine:
    def __init__(self, *, lookup_count: int = NONBLIND_OCR_LOOKUP_COUNT_V4) -> None:
        self.lookup_count = lookup_count
        self.executed = False

    async def execute(
        self,
        *,
        case_rows: Sequence[Mapping[str, Any]],
        materialization_rows: Sequence[Mapping[str, Any]],
        run_spec_template: Mapping[str, Any],
        run_spec_context: Mapping[str, Any],
    ) -> NonblindExecutionResultV4:
        self.executed = True
        materialization_by_id = {
            str(row["case_id"]): row for row in materialization_rows
        }
        specs = {
            variant_id: build_run_spec_v4(
                lane="nonblind",
                subject_commit=str(run_spec_context["subject_commit"]),
                dirty_tree=False,
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
        terminals: list[dict[str, Any]] = []
        screenshot_hashes: set[str] = set()
        for variant_id in VARIANT_IDS_V4:
            spec = specs[variant_id]
            for case in case_rows:
                case_id = str(case["case_id"])
                materialization = materialization_by_id[case_id]
                binding = case["materialization"]
                screenshot = case["input_kind"] == "SYNTHETIC_SCREENSHOT"
                receipts: list[dict[str, Any]] = []
                if screenshot:
                    screenshot_hashes.add(
                        str(materialization["ocr_baseline_receipt"]["asset_hash"])
                    )
                    if variant_id in {"core_b", "solver_c"}:
                        receipts.append(
                            {
                                "type": "ocr_replay_provenance",
                                "mode": "FROZEN_ACTUAL_OCR_RECEIPT_REPLAY",
                                "fresh_model_inference": False,
                                "receipt_match": True,
                                "cleanup_status": "DELETED",
                                "cleanup_error_category": None,
                                "temporary_original_absent": True,
                            }
                        )
                payload: dict[str, Any] = {
                    "schema_version": "trip-check-p5-terminal-output-v4",
                    "case_id": case_id,
                    "split": case["split"],
                    "city": case["city"],
                    "input_kind": case["input_kind"],
                    "input_hash": case["normalized_input_sha256"],
                    "materialization_hash": binding["materialization_sha256"],
                    "render_receipt_hash": (
                        binding["render_receipt"]["content_sha256"]
                        if screenshot
                        else None
                    ),
                    "ocr_receipt_hash": (
                        binding["ocr_baseline_receipt"]["content_sha256"]
                        if screenshot
                        else None
                    ),
                    "provider_snapshot_hash": binding["provider_snapshot"][
                        "content_sha256"
                    ],
                    "evidence_snapshot_hash": binding["evidence_snapshot"][
                        "content_sha256"
                    ],
                    "candidate_set_hashes": [
                        item["content_sha256"] for item in binding["candidate_sets"]
                    ],
                    "fault_script_hash": binding["fault_script"]["content_sha256"],
                    "run_spec_hash": spec.run_spec_hash,
                    "variant_id": variant_id,
                    "adapter_version": spec.adapter_version,
                    "repair_strategy": spec.repair_strategy,
                    "terminal_status": "SUCCEEDED",
                    "capability_outcomes": {
                        "authoritative_oracle_access": "DENIED",
                        "blind_label_access": "DENIED",
                        "external_api_calls": "0",
                    },
                    "native_output": {},
                    "evaluation_projection": {},
                    "findings": [],
                    "advice": [],
                    "postcheck": None,
                    "receipts": receipts,
                    "latency_ms": 0.0,
                    "token_count": 0,
                    "cost_usd": 0.0,
                    "error_category": None,
                    "raw_artifact_hash": digest({}),
                }
                semantic_hash = semantic_output_hash_v4(payload)
                payload["semantic_output_hash"] = semantic_hash
                payload["replay_hash"] = semantic_hash
                terminals.append(payload)
        provenance = {
            "lookup_count": self.lookup_count,
            "hit_count": NONBLIND_OCR_LOOKUP_COUNT_V4,
            "receipt_match_count": NONBLIND_OCR_LOOKUP_COUNT_V4,
            "cleanup_deleted_count": NONBLIND_OCR_LOOKUP_COUNT_V4,
            "miss_count": 0,
            "fallback_count": 0,
            "fresh_prediction_count": 0,
            "unique_hash_count": NONBLIND_SCREENSHOT_HASH_COUNT_V4,
        }
        return NonblindExecutionResultV4(
            terminals=tuple(terminals),
            replay_terminals=tuple(dict(item) for item in terminals),
            run_specs={
                key: value.model_dump(mode="json") for key, value in specs.items()
            },
            screenshot_hashes=frozenset(screenshot_hashes),
            ocr_provenance=provenance,
        )


def test_nonblind_runner_writes_exact_v4_manifest_and_readback(tmp_path: Path) -> None:
    subject = "a" * 40
    result = asyncio.run(
        run_nonblind_v4(
            repo_root=REPO_ROOT,
            output_root=tmp_path,
            run_id="fixture-v4-nonblind",
            subject_commit=subject,
            upstream_ref="origin/fixture",
            upstream_commit=subject,
            dirty_tree=False,
            engine=_FixtureEngine(),
            require_formal=False,
        )
    )

    assert result["schema_version"] == "trip-check-p5-run-group-v4"
    assert result["case_count"] == NONBLIND_CASE_COUNT_V4
    assert result["terminal_count"] == NONBLIND_TERMINAL_COUNT_V4
    assert result["replay_readback_count"] == NONBLIND_TERMINAL_COUNT_V4
    assert result["upstream_commit"] == result["subject_commit"]
    assert result["dirty_tree"] is False
    assert result["hidden_retry_count"] == 0
    assert result["external_api_calls"] == 0
    assert result["blind_labels_read"] is False
    assert result["ocr_replay_provenance"]["lookup_count"] == 504
    assert result["ocr_replay_provenance"]["nonblind_unique_image_hashes"] == 126

    manifest, cases, outputs, materializations = validate_nonblind_run_group_v4(
        run_dir=Path(str(result["run_dir"])),
        repo_root=REPO_ROOT,
        require_formal=False,
    )
    assert manifest["artifact_index_hash"] == result["artifact_index_hash"]
    assert len(cases) == 270
    assert len(outputs) == 810
    assert len(materializations) == 270


def test_nonblind_runner_rejects_wrong_ocr_lookup_count(tmp_path: Path) -> None:
    subject = "b" * 40
    with pytest.raises(
        P5NonblindRunnerErrorV4,
        match="NONBLIND_OCR_REPLAY_PROVENANCE_INVALID",
    ):
        asyncio.run(
            run_nonblind_v4(
                repo_root=REPO_ROOT,
                output_root=tmp_path,
                run_id="fixture-v4-bad-ocr",
                subject_commit=subject,
                upstream_ref="origin/fixture",
                upstream_commit=subject,
                dirty_tree=False,
                engine=_FixtureEngine(lookup_count=503),
                require_formal=False,
            )
        )


def test_formal_runner_fails_before_execution_until_v4_seal_is_active(
    tmp_path: Path,
) -> None:
    engine = _FixtureEngine()
    subject = "c" * 40
    with pytest.raises(
        P5NonblindRunnerErrorV4,
        match="NONBLIND_BLIND_SEAL_INVALID",
    ):
        asyncio.run(
            run_nonblind_v4(
                repo_root=REPO_ROOT,
                output_root=tmp_path,
                run_id="fixture-v4-formal-not-ready",
                subject_commit=subject,
                upstream_ref="origin/fixture",
                upstream_commit=subject,
                dirty_tree=False,
                engine=engine,
                require_formal=True,
            )
        )
    assert engine.executed is False
