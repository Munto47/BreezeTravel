from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.importing.screenshots import PaddleOcrEngine
from evals.trip_check_v1.p5.adapters_v2 import CoreAdapterV2, LegacyAdapterV2, SolverAdapterV2
from evals.trip_check_v1.p5.contracts_v2 import P5CaseV2, P5VariantRunSpecV2, TerminalStatusV2
from evals.trip_check_v1.p5.concurrency_materialization_v2 import build_concurrency_fault_script
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.evidence_materialization_v2 import build_evidence_materialization
from evals.trip_check_v1.p5.ocr_materialization_v2 import materialize_ocr_input
from evals.trip_check_v1.p5.runner_v2 import (
    execute_terminal_v2,
    validate_exact_terminal_set_v2,
    validate_run_spec_whitelist_v2,
    write_jsonl_atomic_v2,
)
from scripts.run_trip_check_p5_v2_eval import RUN_GROUP_FIELDS, execute_run


def _artifact(artifact_id: str, schema_version: str, content: dict) -> dict:
    return {
        "artifact_id": artifact_id,
        "schema_version": schema_version,
        **content,
        "content_sha256": digest({"artifact_id": artifact_id, "schema_version": schema_version, **content}),
    }


def _fixture(input_kind: str = "TEXT") -> tuple[P5CaseV2, dict]:
    product_input = (
        {
            "source_type": "MANUAL_TEXT",
            "raw_text": "北京2人，2天。第1天 09:00-10:00 故宫博物院，11:00-12:00 天坛公园。",
        }
        if input_kind == "TEXT"
        else {
            "source_type": "SYNTHETIC_SCREENSHOT",
            "source_text": "北京2人，2天。第1天 09:00-10:00 故宫博物院，11:00-12:00 天坛公园。",
            "render_spec": {
                "schema_version": "trip-check-p5-render-spec-v2",
                "format": "PNG",
                "theme": "LIGHT",
                "layout": "MEMO",
                "width": 800,
                "height": 1200,
                "seed": 7,
                "text_sha256": hashlib.sha256(
                    "北京2人，2天。第1天 09:00-10:00 故宫博物院，11:00-12:00 天坛公园。".encode()
                ).hexdigest(),
            },
        }
    )
    evidence_input = {
        "source_type": "MANUAL_TEXT",
        "raw_text": product_input.get("raw_text", product_input.get("source_text")),
    }
    evidence_case = {
        "case_id": "p5.dev.bj.001",
        "city": "北京",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": "TEXT",
        "product_input": evidence_input,
        "normalized_input_sha256": digest(evidence_input),
        "runner_control": {
            "provider_snapshot_id": "trip-check-p5-controlled-snapshot-v2",
            "fault_profile_id": "none",
            "seed": 7,
        },
    }
    evidence_materialization = build_evidence_materialization(evidence_case)
    source = evidence_materialization["source_payload"]
    provider = evidence_materialization["provider_snapshot"]
    evidence = evidence_materialization["evidence_snapshot"]
    fault = _artifact("fault-case", "trip-check-p5-fault-artifact-v2", {"fault_profile_id": "none"})
    render = (
        {"schema_version": "trip-check-p5-render-receipt-v2", "case_id": "p5.dev.bj.001"}
        if input_kind != "TEXT"
        else None
    )
    ocr = (
        {"schema_version": "trip-check-p5-ocr-baseline-receipt-v2", "case_id": "p5.dev.bj.001"}
        if input_kind != "TEXT"
        else None
    )
    body = {
        "schema_version": "trip-check-p5-materialization-v2",
        "materialization_id": "materialization-case",
        "case_id": "p5.dev.bj.001",
        "source_payload": source,
        "render_receipt": render,
        "ocr_baseline_receipt": ocr,
        "provider_snapshot": provider,
        "evidence_snapshot": evidence,
        "candidate_sets": evidence_materialization["candidate_sets"],
        "fault_script": fault,
        "receipts": evidence_materialization["receipts"],
    }
    materialization = {**body, "materialization_hash": digest(body)}
    oracle = {
        "schema_version": "trip-check-p5-oracle-v2",
        "task_success_required": True,
        "requires_user_resolution": False,
        "required_reason_codes": [],
        "wrong_city_or_poi_max": 0,
        "max_new_blocker_high_unknown": 0,
        "unknown_must_be_preserved": True,
        "advice_required": False,
        "specific_place_allowed": False,
        "candidate_receipt_mode": "NOT_APPLICABLE",
        "expected_strategy_outcome": "FEASIBLE",
        "concurrency_expectation": "NONE",
        "ocr_required": input_kind != "TEXT",
    }
    case_body = {
        "schema_version": "trip-check-p5-eval-case-v2",
        "case_id": "p5.dev.bj.001",
        "split": "dev",
        "city": "北京",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": input_kind,
        "difficulty": "CLEAN",
        "coverage_tags": [],
        "product_input": product_input,
        "normalized_input_sha256": digest(product_input),
        "materialization": {
            "materialization_id": "materialization-case",
            "materialization_sha256": materialization["materialization_hash"],
            "source_payload": {key: source[key] for key in ("artifact_id", "schema_version", "content_sha256")},
            "render_receipt": (
                {
                    "artifact_id": "render-p5.dev.bj.001",
                    "schema_version": render["schema_version"],
                    "content_sha256": digest(render),
                }
                if render
                else None
            ),
            "ocr_baseline_receipt": (
                {
                    "artifact_id": "ocr-p5.dev.bj.001",
                    "schema_version": ocr["schema_version"],
                    "content_sha256": digest(ocr),
                }
                if ocr
                else None
            ),
            "provider_snapshot": {key: provider[key] for key in ("artifact_id", "schema_version", "content_sha256")},
            "evidence_snapshot": {key: evidence[key] for key in ("artifact_id", "schema_version", "content_sha256")},
            "candidate_sets": [
                {key: item[key] for key in ("artifact_id", "schema_version", "content_sha256")}
                for item in evidence_materialization["candidate_sets"]
            ],
            "fault_script": {key: fault[key] for key in ("artifact_id", "schema_version", "content_sha256")},
        },
        "runner_control": {"fault_profile_id": "none", "seed": 7},
        "lineage": {},
        "source_ref": {},
        "provenance": {},
        "oracle": oracle,
        "oracle_sha256": digest(oracle),
        "case_hash": "7" * 64,
    }
    case_body["case_hash"] = digest({key: value for key, value in case_body.items() if key != "case_hash"})
    return P5CaseV2.model_validate(case_body), materialization


def _spec(variant_id: str = "legacy_a", timeout: float = 1) -> P5VariantRunSpecV2:
    values = {
        "legacy_a": ("legacy-a-v2", "legacy_native_only"),
        "core_b": ("core-b-v2", "bounded_repair_v1"),
        "solver_c": ("solver-c-v2", "cp_sat_v1"),
    }
    version, strategy = values[variant_id]
    return P5VariantRunSpecV2(
        subject_commit="a" * 40,
        dirty_tree=False,
        lane="nonblind",
        dataset_manifest_hash="b" * 64,
        case_set_hash="c" * 64,
        materialization_set_hash="d" * 64,
        run_spec_template_hash="e" * 64,
        rubric_hash="f" * 64,
        renderer_version="2.0.0",
        ocr_engine_version="3.7.0",
        evidence_policy_version="v2",
        fault_registry_version="v2",
        random_seed=7,
        budget={"timeout_seconds": timeout, "max_cost_usd": 0},
        variant_id=variant_id,
        adapter_version=version,
        repair_strategy=strategy,
    )


def _real_product_fixture() -> tuple[P5CaseV2, dict]:
    product_input = {
        "source_type": "MANUAL_TEXT",
        "raw_text": "北京2人，2天。第1天 09:00-10:00 故宫博物院，10:05-11:05 天坛公园。",
    }
    base = {
        "case_id": "p5.dev.bj.product-001",
        "city": "北京",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": "TEXT",
        "product_input": product_input,
        "normalized_input_sha256": digest(product_input),
        "runner_control": {
            "provider_snapshot_id": "trip-check-p5-controlled-snapshot-v2",
            "fault_profile_id": "none",
            "seed": 7,
        },
    }
    evidence = build_evidence_materialization(base)
    fault = _artifact("fault-product", "trip-check-p5-apply-fault-script-v2", {"fault_profile_id": "none"})
    body = {
        "schema_version": "trip-check-p5-materialization-v2",
        "materialization_id": "materialization-product",
        "case_id": base["case_id"],
        "source_payload": evidence["source_payload"],
        "render_receipt": None,
        "ocr_baseline_receipt": None,
        "provider_snapshot": evidence["provider_snapshot"],
        "evidence_snapshot": evidence["evidence_snapshot"],
        "candidate_sets": evidence["candidate_sets"],
        "fault_script": fault,
        "receipts": evidence["receipts"],
    }
    materialization = {**body, "materialization_hash": digest(body)}
    oracle = {
        "schema_version": "trip-check-p5-oracle-v2",
        "task_success_required": True,
        "requires_user_resolution": False,
        "required_reason_codes": [],
        "wrong_city_or_poi_max": 0,
        "max_new_blocker_high_unknown": 0,
        "unknown_must_be_preserved": True,
        "advice_required": True,
        "specific_place_allowed": True,
        "candidate_receipt_mode": "REQUIRED",
        "expected_strategy_outcome": "FEASIBLE",
        "concurrency_expectation": "NONE",
        "ocr_required": False,
    }

    def binding(artifact: dict) -> dict:
        return {key: artifact[key] for key in ("artifact_id", "schema_version", "content_sha256")}

    case_payload = {
        "schema_version": "trip-check-p5-eval-case-v2",
        **base,
        "split": "dev",
        "difficulty": "CLEAN",
        "coverage_tags": [],
        "materialization": {
            "materialization_id": body["materialization_id"],
            "materialization_sha256": materialization["materialization_hash"],
            "source_payload": binding(evidence["source_payload"]),
            "provider_snapshot": binding(evidence["provider_snapshot"]),
            "evidence_snapshot": binding(evidence["evidence_snapshot"]),
            "candidate_sets": [binding(item) for item in evidence["candidate_sets"]],
            "fault_script": binding(fault),
        },
        "lineage": {},
        "source_ref": {},
        "provenance": {},
        "oracle": oracle,
        "oracle_sha256": digest(oracle),
        "case_hash": "0" * 64,
    }
    case_payload["case_hash"] = digest({key: value for key, value in case_payload.items() if key != "case_hash"})
    case = P5CaseV2.model_validate(case_payload)
    return case, materialization


def _with_apply_fault(case: P5CaseV2, materialization: dict, fault_profile: str):
    script = build_concurrency_fault_script(
        case_id=case.case_id,
        workspace_id=f"eval-workspace-{case.case_id}",
        repair_id=f"frozen-repair-{case.case_id}",
        base_revision=1,
        fault_profile_id=fault_profile,
    )
    artifact = _artifact(
        f"fault-{case.case_id}-{fault_profile}",
        "trip-check-p5-fault-artifact-v2",
        {"fault_profile_id": fault_profile, "script": script},
    )
    changed = deepcopy(materialization)
    changed["fault_script"] = artifact
    changed["materialization_hash"] = digest(
        {key: value for key, value in changed.items() if key != "materialization_hash"}
    )
    binding = {key: artifact[key] for key in ("artifact_id", "schema_version", "content_sha256")}
    case_payload = case.model_dump(mode="json")
    case_payload["runner_control"]["fault_profile_id"] = fault_profile
    case_payload["materialization"]["materialization_sha256"] = changed["materialization_hash"]
    case_payload["materialization"]["fault_script"] = binding
    changed_case = P5CaseV2.model_validate(case_payload)
    return changed_case, changed


class _GuardedMaterialization(dict):
    def __getitem__(self, key):
        if key in {"source_payload", "ocr_baseline_receipt", "render_receipt"}:
            raise AssertionError("Legacy inspected screenshot/OCR materialization")
        return super().__getitem__(key)


@pytest.mark.asyncio
async def test_legacy_screenshot_is_immediately_unsupported_without_ocr_access() -> None:
    case, materialization = _fixture("SYNTHETIC_SCREENSHOT")
    direct = await LegacyAdapterV2().execute(
        case,
        _GuardedMaterialization(materialization),
        _spec(),
    )
    assert direct.terminal_status == TerminalStatusV2.UNSUPPORTED_CAPABILITY
    output = await execute_terminal_v2(
        case=case,
        materialization=materialization,
        run_spec=_spec(),
        adapter=LegacyAdapterV2(),
    )
    assert output.terminal_status == TerminalStatusV2.UNSUPPORTED_CAPABILITY
    assert output.receipts == [{"type": "legacy_boundary", "screenshot_ocr_access": "DENIED"}]


@pytest.mark.asyncio
async def test_timeout_and_error_always_emit_terminal_rows_without_details() -> None:
    case, materialization = _fixture()
    adapter = SimpleNamespace(
        variant_id="legacy_a", adapter_version="legacy-a-v2", repair_strategy="legacy_native_only"
    )

    async def slow(*args):
        await asyncio.sleep(0.02)

    adapter.execute = slow
    timeout = await execute_terminal_v2(
        case=case, materialization=materialization, run_spec=_spec(timeout=0.001), adapter=adapter
    )
    assert timeout.terminal_status == TerminalStatusV2.TIMEOUT

    async def broken(*args):
        raise RuntimeError("secret detail")

    adapter.execute = broken
    error = await execute_terminal_v2(case=case, materialization=materialization, run_spec=_spec(), adapter=adapter)
    assert error.terminal_status == TerminalStatusV2.ERROR
    assert error.error_category == "RuntimeError"
    assert "secret detail" not in error.model_dump_json()


@pytest.mark.asyncio
async def test_tampered_materialization_emits_error_before_adapter_execution() -> None:
    case, materialization = _fixture()
    tampered = deepcopy(materialization)
    tampered["source_payload"]["raw_text"] = "tampered"
    called = False

    async def execute(*args):
        nonlocal called
        called = True

    adapter = SimpleNamespace(
        variant_id="legacy_a",
        adapter_version="legacy-a-v2",
        repair_strategy="legacy_native_only",
        execute=execute,
    )
    output = await execute_terminal_v2(
        case=case,
        materialization=tampered,
        run_spec=_spec(),
        adapter=adapter,
    )
    assert output.terminal_status == TerminalStatusV2.ERROR
    assert output.error_category == "ValueError"
    assert called is False


def test_exact_set_and_atomic_writer(tmp_path: Path) -> None:
    case, materialization = _fixture("SYNTHETIC_SCREENSHOT")
    output = asyncio.run(
        execute_terminal_v2(case=case, materialization=materialization, run_spec=_spec(), adapter=LegacyAdapterV2())
    )
    validate_exact_terminal_set_v2([output], case_ids={case.case_id}, variant_ids={"legacy_a"})
    with pytest.raises(ValueError, match="duplicate"):
        validate_exact_terminal_set_v2([output, output], case_ids={case.case_id}, variant_ids={"legacy_a"})
    target = tmp_path / "outputs.jsonl"
    content_hash = write_jsonl_atomic_v2(target, [output])
    assert len(content_hash) == 64
    assert target.exists()
    assert not target.with_name(target.name + ".tmp").exists()


def test_run_specs_may_only_differ_by_variant_adapter_and_repair_strategy() -> None:
    validate_run_spec_whitelist_v2([_spec("legacy_a"), _spec("core_b"), _spec("solver_c")])
    changed = _spec("core_b").model_copy(update={"random_seed": 8})
    with pytest.raises(ValueError, match="outside"):
        validate_run_spec_whitelist_v2([_spec("legacy_a"), changed])


@pytest.mark.asyncio
async def test_semantic_replay_ignores_latency_and_raw_artifact() -> None:
    case, materialization = _fixture()
    adapter = SimpleNamespace(
        variant_id="legacy_a", adapter_version="legacy-a-v2", repair_strategy="legacy_native_only"
    )

    async def first(*args):
        from evals.trip_check_v1.p5.adapters_v2 import _HarnessResult

        return _HarnessResult(
            terminal_status=TerminalStatusV2.SUCCEEDED,
            native_output={"stable": True},
            evaluation_projection={"stable": True},
            findings=[],
            advice=[],
            postcheck=None,
            receipts=[],
            raw_artifact={"volatile": 1},
        )

    adapter.execute = first
    left = await execute_terminal_v2(case=case, materialization=materialization, run_spec=_spec(), adapter=adapter)

    async def second(*args):
        result = await first(*args)
        return result.__class__(**{**result.__dict__, "raw_artifact": {"volatile": 2}})

    adapter.execute = second
    right = await execute_terminal_v2(
        case=case, materialization=deepcopy(materialization), run_spec=_spec(), adapter=adapter
    )
    assert left.raw_artifact_hash != right.raw_artifact_hash
    assert left.semantic_output_hash == right.semantic_output_hash
    assert left.replay_hash == right.replay_hash


@pytest.mark.asyncio
async def test_core_uses_product_import_and_frozen_evidence_in_isolated_repositories() -> None:
    case, materialization = _real_product_fixture()
    output = await execute_terminal_v2(
        case=case,
        materialization=materialization,
        run_spec=_spec("core_b", timeout=5),
        adapter=CoreAdapterV2(),
    )
    assert output.terminal_status in {
        TerminalStatusV2.SUCCEEDED,
        TerminalStatusV2.NEEDS_USER_RESOLUTION,
    }
    assert output.error_category is None
    assert output.capability_outcomes["product_import"] == "PRODUCTION_SERVICE"
    assert output.evaluation_projection["replay_side_effect_counts_equal"] is True
    assert output.evidence_snapshot_hash == case.materialization.evidence_snapshot.content_sha256
    assert output.postcheck is not None
    replay = await execute_terminal_v2(
        case=case,
        materialization=materialization,
        run_spec=_spec("core_b", timeout=5),
        adapter=CoreAdapterV2(),
    )
    assert output.raw_artifact_hash != replay.raw_artifact_hash
    assert output.semantic_output_hash == replay.semantic_output_hash


@pytest.mark.asyncio
async def test_legacy_text_runs_frozen_read_only_router_planner_chain() -> None:
    case, materialization = _real_product_fixture()
    output = await execute_terminal_v2(
        case=case,
        materialization=materialization,
        run_spec=_spec("legacy_a", timeout=10),
        adapter=LegacyAdapterV2(),
    )
    assert output.error_category is None
    assert output.terminal_status in {
        TerminalStatusV2.SUCCEEDED,
        TerminalStatusV2.NEEDS_USER_RESOLUTION,
    }
    assert output.native_output["run"]["itinerary"]
    assert output.receipts[0] == {
        "type": "legacy_isolation",
        "authoritative_write": "DENIED",
    }
    replay = await execute_terminal_v2(
        case=case,
        materialization=materialization,
        run_spec=_spec("legacy_a", timeout=10),
        adapter=LegacyAdapterV2(),
    )
    assert replay.semantic_output_hash == output.semantic_output_hash


@pytest.mark.asyncio
async def test_solver_only_changes_repair_strategy_and_keeps_p4_reject() -> None:
    case, materialization = _real_product_fixture()
    output = await execute_terminal_v2(
        case=case,
        materialization=materialization,
        run_spec=_spec("solver_c", timeout=10),
        adapter=SolverAdapterV2(),
    )
    assert output.error_category is None
    assert output.repair_strategy == "cp_sat_v1"
    assert output.native_output["solver_strategy"] is not None
    assert output.native_output["solver_strategy"]["p4_admission"] == "REJECT"
    assert output.evaluation_projection["p4_solver_admission"] == "REJECT"
    replay = await execute_terminal_v2(
        case=case,
        materialization=materialization,
        run_spec=_spec("solver_c", timeout=10),
        adapter=SolverAdapterV2(),
    )
    assert replay.semantic_output_hash == output.semantic_output_hash


@pytest.mark.parametrize("fault_profile", ["duplicate_apply", "concurrent_apply"])
@pytest.mark.asyncio
async def test_adapter_executes_real_apply_fault_harness(fault_profile: str) -> None:
    case, materialization = _real_product_fixture()
    case, materialization = _with_apply_fault(case, materialization, fault_profile)
    output = await execute_terminal_v2(
        case=case,
        materialization=materialization,
        run_spec=_spec("core_b", timeout=10),
        adapter=CoreAdapterV2(),
    )
    receipt = next(item for item in output.receipts if item.get("type") == "concurrency")
    assert receipt["schema_version"] == "trip-check-p5-apply-fault-receipt-v2"
    assert receipt["fault_profile_id"] == fault_profile
    assert receipt["status"] == "PASS"
    assert receipt["semantic_projection"]["all_invariants_passed"] is True
    replay = await execute_terminal_v2(
        case=case,
        materialization=materialization,
        run_spec=_spec("core_b", timeout=10),
        adapter=CoreAdapterV2(),
    )
    assert replay.semantic_output_hash == output.semantic_output_hash


@pytest.mark.asyncio
async def test_core_screenshot_replays_same_bytes_through_production_paddle_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_text = "北京2人，2天。第1天 09:00-10:00 故宫博物院，11:00-12:00 天坛公园。"
    product_input = {
        "source_type": "SYNTHETIC_SCREENSHOT",
        "source_text": source_text,
        "render_spec": {
            "schema_version": "trip-check-p5-render-spec-v2",
            "format": "PNG",
            "theme": "LIGHT",
            "layout": "MEMO",
            "width": 800,
            "height": 1200,
            "seed": 11,
            "text_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
        },
    }

    def fake_predict(self, image_path):
        assert Path(image_path).read_bytes().startswith(b"\x89PNG")
        return [
            {
                "res": {
                    "rec_texts": [source_text],
                    "rec_scores": [0.99],
                    "rec_boxes": [[0, 0, 760, 80]],
                }
            }
        ]

    monkeypatch.setattr(PaddleOcrEngine, "_predict", fake_predict)
    ocr = await materialize_ocr_input(
        product_input, case_id="p5.dev.bj.screenshot-001", work_root=tmp_path / "baseline"
    )
    evidence_case = {
        "case_id": "p5.dev.bj.screenshot-001",
        "city": "北京",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": "TEXT",
        "product_input": {"source_type": "MANUAL_TEXT", "raw_text": source_text},
        "normalized_input_sha256": digest({"source_type": "MANUAL_TEXT", "raw_text": source_text}),
        "runner_control": {
            "provider_snapshot_id": "trip-check-p5-controlled-snapshot-v2",
            "fault_profile_id": "none",
            "seed": 11,
        },
    }
    evidence = build_evidence_materialization(evidence_case)
    render = ocr["render_receipt"]
    baseline = ocr["ocr_baseline_receipt"]
    fault = _artifact("fault-screenshot", "trip-check-p5-apply-fault-script-v2", {"fault_profile_id": "none"})
    body = {
        "schema_version": "trip-check-p5-materialization-v2",
        "materialization_id": "materialization-screenshot",
        "case_id": "p5.dev.bj.screenshot-001",
        "source_payload": evidence["source_payload"],
        "render_receipt": render,
        "ocr_baseline_receipt": baseline,
        "provider_snapshot": evidence["provider_snapshot"],
        "evidence_snapshot": evidence["evidence_snapshot"],
        "candidate_sets": evidence["candidate_sets"],
        "fault_script": fault,
        "receipts": evidence["receipts"],
    }
    materialization = {**body, "materialization_hash": digest(body)}

    def binding(artifact):
        return {key: artifact[key] for key in ("artifact_id", "schema_version", "content_sha256")}

    oracle = {
        "schema_version": "trip-check-p5-oracle-v2",
        "task_success_required": True,
        "requires_user_resolution": False,
        "required_reason_codes": [],
        "wrong_city_or_poi_max": 0,
        "max_new_blocker_high_unknown": 0,
        "unknown_must_be_preserved": True,
        "advice_required": True,
        "specific_place_allowed": True,
        "candidate_receipt_mode": "REQUIRED",
        "expected_strategy_outcome": "FEASIBLE",
        "concurrency_expectation": "NONE",
        "ocr_required": True,
    }
    case_payload = {
        "schema_version": "trip-check-p5-eval-case-v2",
        "case_id": body["case_id"],
        "split": "dev",
        "city": "北京",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": "SYNTHETIC_SCREENSHOT",
        "difficulty": "CLEAN",
        "coverage_tags": ["screenshot"],
        "product_input": product_input,
        "normalized_input_sha256": digest(product_input),
        "materialization": {
            "materialization_id": body["materialization_id"],
            "materialization_sha256": materialization["materialization_hash"],
            "source_payload": binding(evidence["source_payload"]),
            "render_receipt": {
                "artifact_id": f"render-{body['case_id']}",
                "schema_version": render["schema_version"],
                "content_sha256": digest(render),
            },
            "ocr_baseline_receipt": {
                "artifact_id": f"ocr-{body['case_id']}",
                "schema_version": baseline["schema_version"],
                "content_sha256": digest(baseline),
            },
            "provider_snapshot": binding(evidence["provider_snapshot"]),
            "evidence_snapshot": binding(evidence["evidence_snapshot"]),
            "candidate_sets": [binding(item) for item in evidence["candidate_sets"]],
            "fault_script": binding(fault),
        },
        "runner_control": evidence_case["runner_control"],
        "lineage": {},
        "source_ref": {},
        "provenance": {},
        "oracle": oracle,
        "oracle_sha256": digest(oracle),
        "case_hash": "0" * 64,
    }
    case_payload["case_hash"] = digest({key: value for key, value in case_payload.items() if key != "case_hash"})
    case = P5CaseV2.model_validate(case_payload)
    output = await execute_terminal_v2(
        case=case,
        materialization=materialization,
        run_spec=_spec("core_b", timeout=10),
        adapter=CoreAdapterV2(),
    )
    assert output.error_category is None
    assert output.render_receipt_hash == digest(render)
    assert output.ocr_receipt_hash == digest(baseline)
    assert any(item.get("type") == "ocr" for item in output.receipts)
    replay = await execute_terminal_v2(
        case=case,
        materialization=materialization,
        run_spec=_spec("core_b", timeout=10),
        adapter=CoreAdapterV2(),
    )
    assert replay.semantic_output_hash == output.semantic_output_hash


@pytest.mark.asyncio
async def test_run_script_writes_exact_v2_manifest_and_variant_hashes(tmp_path: Path) -> None:
    case, materialization = _fixture("SYNTHETIC_SCREENSHOT")
    cases_path = tmp_path / "cases.jsonl"
    materials_path = tmp_path / "materializations.jsonl"
    dataset_path = tmp_path / "dataset.json"
    template_path = tmp_path / "run_spec.json"
    rubric_path = tmp_path / "rubric.json"
    cases_path.write_text(case.model_dump_json() + "\n", encoding="utf-8")
    materials_path.write_text(json.dumps(materialization, ensure_ascii=False) + "\n", encoding="utf-8")
    template = {
        "allowed_variant_differences": [
            "variant_id",
            "adapter_version",
            "repair_strategy",
        ],
        "renderer": {"name": "fixture-renderer", "version": "2.0.0"},
        "ocr_engine": {"name": "paddleocr", "version": "3.7.0"},
        "evidence_policy_version": "v2",
        "fault_registry_version": "v2",
        "random_seed": 7,
        "budget": {"timeout_seconds": 2, "max_cost_usd": 0},
        "replay_hash_policy": "p5-semantic-projection-v2",
        "variant_specs": {
            "legacy_a": {
                "adapter_version": "legacy-a-v2",
                "repair_strategy": "legacy_native_only",
            }
        },
    }
    template_path.write_text(json.dumps(template), encoding="utf-8")
    rubric_path.write_text(
        json.dumps({"schema_version": "trip-check-p5-judge-rubric-v2"}),
        encoding="utf-8",
    )
    dataset = {
        "schema_version": "trip-check-p5-dataset-manifest-v2",
        "contract_hashes": {
            "judge_rubric_sha256": hashlib.sha256(rubric_path.read_bytes()).hexdigest(),
            "run_spec_template_sha256": hashlib.sha256(template_path.read_bytes()).hexdigest(),
        },
        "files": {
            "nonblind_cases": {"file_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest()},
            "nonblind_materializations": {"file_sha256": hashlib.sha256(materials_path.read_bytes()).hexdigest()},
        },
        "frozen": False,
        "generation": {"formal_validation_eligible": False},
        "evidence_boundary": {"actual_ocr": "NOT_RUN"},
    }
    dataset["manifest_hash"] = digest(dataset)
    dataset_path.write_text(
        json.dumps(dataset),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        lane="nonblind",
        cases_file=str(cases_path),
        materializations_file=str(materials_path),
        dataset_manifest=str(dataset_path),
        run_spec_template=str(template_path),
        rubric=str(rubric_path),
        active_contract=None,
        blind_seal=None,
        case_id=None,
        limit=1,
        variants="legacy",
        allow_dirty=True,
        require_formal=False,
        replay=True,
        output_dir=str(tmp_path / "outputs"),
        run_id="local-fixture-run",
    )
    result = await execute_run(args)
    manifest = json.loads((Path(result["run_dir"]) / "run_group_manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == RUN_GROUP_FIELDS
    assert manifest["case_count"] == 1
    assert manifest["terminal_count"] == 1
    assert set(manifest["variant_output_sha256"]) == {"legacy_a"}
    assert manifest["blind_labels_read"] is False
    assert manifest["manifest_hash"] == digest(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    )

    tampered_dataset = deepcopy(dataset)
    tampered_dataset["manifest_hash"] = "0" * 64
    dataset_path.write_text(json.dumps(tampered_dataset), encoding="utf-8")
    args.run_id = "tampered-dataset-run"
    with pytest.raises(ValueError, match="canonical hash"):
        await execute_run(args)
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")

    rubric_path.write_text('{"tampered":true}', encoding="utf-8")
    args.run_id = "tampered-rubric-run"
    with pytest.raises(ValueError, match="rubric bytes"):
        await execute_run(args)

    rubric_path.write_text(
        json.dumps({"schema_version": "trip-check-p5-judge-rubric-v2"}),
        encoding="utf-8",
    )
    cases_path.write_text(cases_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    args.run_id = "tampered-cases-run"
    with pytest.raises(ValueError, match="selected nonblind_cases bytes"):
        await execute_run(args)


@pytest.mark.asyncio
async def test_formal_run_rejects_pending_active_contract_before_execution(tmp_path: Path) -> None:
    pending = tmp_path / "active_contract.json"
    pending.write_text(
        json.dumps(
            {
                "schema_version": "trip-check-p5-active-contract-v1",
                "active_contract": "trip-check-p5-v2",
                "formal_evidence_status": "PENDING_V2_SEAL",
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        lane="nonblind",
        cases_file="does-not-matter",
        materializations_file="does-not-matter",
        dataset_manifest="does-not-matter",
        run_spec_template="does-not-matter",
        rubric="does-not-matter",
        active_contract=str(pending),
        blind_seal=None,
        case_id=None,
        limit=None,
        variants="legacy,core,solver",
        allow_dirty=False,
        require_formal=True,
        replay=True,
        output_dir=str(tmp_path / "outputs"),
        run_id="must-not-run",
    )
    with pytest.raises(RuntimeError, match="P5_V2_FORMAL_CONTRACT_NOT_READY"):
        await execute_run(args)
    assert not (tmp_path / "outputs").exists()
