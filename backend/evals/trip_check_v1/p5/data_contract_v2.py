"""P5 v2 dataset construction and hash-binding contract.

This module is the only implementation allowed to turn the frozen P4 sources
and label-free blind specifications into P5 v2 cases and materializations.
Materializers receive an explicit projection that cannot contain oracle or
expected fields.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from app.importing.screenshots import OcrBoundingBox, OcrEngine, OcrTextLine, PaddleOcrEngine
from evals.trip_check_v1.p5.concurrency_materialization_v2 import build_concurrency_fault_script
from evals.trip_check_v1.p5.contracts_v2 import P5CaseV2, P5OracleV2
from evals.trip_check_v1.p5.data_contract import (
    BACKEND_ROOT,
    PILOT_PATH,
    P4_ROOT,
    P5_ROOT,
    digest,
    file_sha256,
    load_jsonl,
    write_json,
    write_jsonl,
)
from evals.trip_check_v1.p5.evidence_materialization_v2 import build_evidence_materialization
from evals.trip_check_v1.p5.ocr_materialization_v2 import materialize_ocr_input


CASE_SCHEMA_VERSION = "trip-check-p5-eval-case-v2"
MATERIALIZATION_SCHEMA_VERSION = "trip-check-p5-materialization-v2"
MANIFEST_SCHEMA_VERSION = "trip-check-p5-dataset-manifest-v2"
DATASET_ID = "trip-check-p5-360-v2"
HASH_POLICY_VERSION = "p5-canonical-json-nfc-lf-v2"
GENERATOR_VERSION = "p5-dataset-builder-v2"
PROVIDER_SNAPSHOT_ID = "trip-check-p5-controlled-snapshot-v2"
FAULT_REGISTRY_VERSION = "trip-check-p5-fault-registry-v2"

NONBLIND_PATH_V2 = P5_ROOT / "cases_nonblind_v2.jsonl"
BLIND_INPUT_PATH_V2 = P5_ROOT / "frozen_blind.v2.inputs.jsonl"
NONBLIND_MATERIALIZATIONS_PATH_V2 = P5_ROOT / "materializations_nonblind_v2.jsonl"
BLIND_MATERIALIZATIONS_PATH_V2 = P5_ROOT / "frozen_blind.v2.materializations.jsonl"
MANIFEST_PATH_V2 = P5_ROOT / "dataset_v2.manifest.json"
BLIND_SEAL_PATH_V2 = P5_ROOT / "sealed" / "frozen_blind.v2.seal.json"
RUN_SPEC_TEMPLATE_PATH_V2 = P5_ROOT / "run_spec_template_v2.json"
JUDGE_RUBRIC_PATH_V2 = P5_ROOT / "judge_rubric_v2.json"

CITIES = ("北京", "上海", "杭州")
SPLIT_COUNTS = {"pilot": 18, "dev": 180, "regression": 72, "frozen_blind": 90}
SCREENSHOT_COUNTS = {"pilot": 0, "dev": 90, "regression": 36, "frozen_blind": 45}
FAULT_PROFILES = (
    "advice_completeness",
    "empty_candidate_set",
    "candidate_receipt_missing",
    "route_conflict",
    "duplicate_apply",
    "concurrent_apply",
    "solver_unsat",
    "solver_timeout",
    "solver_fallback",
)
DIFFICULTIES = ("CLEAN", "MEDIUM", "HARD")

_CITY_PLACES = {
    "北京": ("故宫博物院", "天坛公园", "颐和园", "景山公园", "中国国家博物馆"),
    "上海": ("外滩", "豫园", "东方明珠广播电视塔", "田子坊", "上海迪士尼乐园"),
    "杭州": ("西湖风景名胜区", "灵隐寺", "雷峰塔", "西溪湿地国家公园", "河坊街·清河坊"),
}

OcrMode = Literal["development", "actual"]
CHECKPOINT_SCHEMA_VERSION = "trip-check-p5-materialization-checkpoint-v1"


class DevelopmentOcrEngine:
    """Fast test engine whose receipts can never satisfy formal validation."""

    name = "p5-development-ocr"
    version = "2.0.0"

    def __init__(self, source_text: str) -> None:
        self._source_text = source_text

    async def recognize(self, image_path: Path) -> list[OcrTextLine]:
        if not image_path.is_file():
            raise RuntimeError("development OCR did not receive a materialized image")
        return [
            OcrTextLine(
                text=self._source_text,
                confidence=0.99,
                box=OcrBoundingBox(x_min=24, y_min=72, x_max=1024, y_max=360),
                requires_confirmation=False,
            )
        ]


def _prefix(city: str) -> str:
    return {"北京": "bj", "上海": "sh", "杭州": "hz"}[city]


def _candidate_set_mode(fault_profile_id: str) -> str:
    return {
        "advice_completeness": "VALID",
        "empty_candidate_set": "EMPTY",
        "candidate_receipt_missing": "MISSING_RECEIPT",
    }.get(fault_profile_id, "NOT_APPLICABLE")


def _concurrency_expectation(fault_profile_id: str) -> str:
    if fault_profile_id == "duplicate_apply":
        return "IDEMPOTENT_REPLAY"
    if fault_profile_id == "concurrent_apply":
        return "SINGLE_WINNER"
    return "NONE"


def _raw_text(*, city: str, days: int, group_size: int, sequence: int, fault_profile_id: str) -> str:
    places = _CITY_PLACES[city]
    first = places[sequence % len(places)]
    second = places[(sequence + 1) % len(places)]
    third = places[(sequence + 2) % len(places)]
    minute = sequence % 60
    tail = (sequence // 60) % 60
    if fault_profile_id == "route_conflict":
        return (
            f"{city}{group_size}人，{days}天，编号{sequence:04d}。"
            f"第1天 09:{minute:02d}-11:{minute:02d} {first}，"
            f"10:{tail:02d}-12:{tail:02d} {second}；"
            f"第2天 09:{tail:02d}-11:{tail:02d} {third}。"
        )
    return (
        f"{city}{group_size}人，{days}天，编号{sequence:04d}。"
        f"第1天 09:{minute:02d}-11:{minute:02d} {first}，"
        f"13:{tail:02d}-15:{tail:02d} {second}；"
        f"第2天 09:{tail:02d}-11:{tail:02d} {third}。"
    )


def _product_input(*, raw_text: str, input_kind: str, sequence: int) -> dict[str, Any]:
    if input_kind == "TEXT":
        return {"source_type": "MANUAL_TEXT", "raw_text": raw_text}
    return {
        "source_type": "SYNTHETIC_SCREENSHOT",
        "source_text": raw_text,
        "render_spec": {
            "schema_version": "trip-check-p5-render-spec-v2",
            "format": ("PNG", "JPEG", "WEBP")[sequence % 3],
            "theme": ("LIGHT", "DARK")[sequence % 2],
            "layout": ("CHAT", "MEMO", "GUIDE")[sequence % 3],
            "width": 1080,
            "height": 1920,
            "seed": 20260823 + sequence,
            "text_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        },
    }


def _lineage(*, split: str, city: str, sequence: int, source_case_id: str) -> dict[str, Any]:
    identity = {"split": split, "city": city, "sequence": sequence, "source_case_id": source_case_id}
    return {
        "source_family_id": f"p5v2-source-{digest(identity)[:24]}",
        "content_family_id": f"p5v2-content-{digest({**identity, 'kind': 'content'})[:24]}",
        "mutation_ancestry_id": f"p5v2-ancestry-{digest({**identity, 'kind': 'ancestry'})[:24]}",
        "mutation_parent_case_id": None,
        "generator_family_id": GENERATOR_VERSION,
        "lineage_status": "RECORDED",
    }


def _oracle_from_pilot(source: Mapping[str, Any], *, ocr_required: bool) -> dict[str, Any]:
    expected = source["expected"]
    return P5OracleV2(
        task_success_required=True,
        requires_user_resolution=bool(expected["requires_user_resolution"]),
        required_reason_codes=list(expected["required_reason_codes"]),
        wrong_city_or_poi_max=int(expected["wrong_poi_auto_accept_max"]),
        max_new_blocker_high_unknown=int(expected["repair_new_high_max"]) + int(expected["repair_new_unknown_max"]),
        unknown_must_be_preserved=False,
        advice_required=not bool(expected["requires_user_resolution"]),
        specific_place_allowed=True,
        candidate_receipt_mode="REQUIRED",
        expected_strategy_outcome="FEASIBLE",
        concurrency_expectation="NONE",
        ocr_required=ocr_required,
    ).model_dump(mode="json")


def _oracle_from_p4(source: Mapping[str, Any], *, ocr_required: bool) -> dict[str, Any]:
    fault_profile_id = str(source["fault_class"])
    candidate_mode = _candidate_set_mode(fault_profile_id)
    fixture = source["fixture"]
    oracle = source["oracle"]
    return P5OracleV2(
        task_success_required=True,
        requires_user_resolution=candidate_mode in {"EMPTY", "MISSING_RECEIPT"},
        required_reason_codes=[str(fixture["finding"]["reason_code"])],
        wrong_city_or_poi_max=0,
        max_new_blocker_high_unknown=int(oracle["max_new_blocker_high_unknown"]),
        unknown_must_be_preserved=fixture["finding"]["status"] == "UNKNOWN",
        advice_required=bool(oracle["advice_required"]),
        specific_place_allowed=bool(oracle["specific_place_allowed"]),
        candidate_receipt_mode=(
            "REQUIRED"
            if candidate_mode == "VALID"
            else "FORBIDDEN"
            if candidate_mode != "NOT_APPLICABLE"
            else "NOT_APPLICABLE"
        ),
        expected_strategy_outcome=str(oracle["expected_strategy_outcome"]),
        concurrency_expectation=_concurrency_expectation(fault_profile_id),
        ocr_required=ocr_required,
    ).model_dump(mode="json")


def _draft_case(
    *,
    case_id: str,
    split: str,
    city: str,
    trip_days: int,
    group_size: int,
    input_kind: str,
    difficulty: str,
    fault_profile_id: str,
    sequence: int,
    source_ref: dict[str, Any],
    raw_text: str,
    unknown_required: bool,
    oracle: dict[str, Any] | None,
) -> dict[str, Any]:
    product_input = _product_input(raw_text=raw_text, input_kind=input_kind, sequence=sequence)
    candidate_set_mode = _candidate_set_mode(fault_profile_id)
    tags = [fault_profile_id, input_kind.casefold(), difficulty.casefold()]
    if candidate_set_mode != "NOT_APPLICABLE":
        tags.append(f"candidate_set_{candidate_set_mode.casefold()}")
    if unknown_required:
        tags.append("unknown_required")
    if fault_profile_id in {"duplicate_apply", "concurrent_apply"}:
        tags.append("concurrency")
    return {
        "schema_version": CASE_SCHEMA_VERSION,
        "case_id": case_id,
        "split": split,
        "city": city,
        "trip_days": trip_days,
        "group_size": group_size,
        "input_kind": input_kind,
        "difficulty": difficulty,
        "coverage_tags": tags,
        "product_input": product_input,
        "normalized_input_sha256": digest(product_input),
        "runner_control": {
            "provider_snapshot_id": PROVIDER_SNAPSHOT_ID,
            "fault_profile_id": fault_profile_id,
            "fault_registry_version": FAULT_REGISTRY_VERSION,
            "candidate_set_mode": candidate_set_mode,
            "evidence_freshness": "UNAVAILABLE"
            if unknown_required
            else "CONFLICTING"
            if fault_profile_id == "route_conflict"
            else "FRESH",
            "unknown_required": unknown_required,
            "seed": 20260823 + sequence,
            "budget_profile": "p5-zero-api-v2",
        },
        "lineage": _lineage(
            split=split,
            city=city,
            sequence=sequence,
            source_case_id=str(source_ref["case_id"]),
        ),
        "source_ref": source_ref,
        "provenance": {
            "generated_by": GENERATOR_VERSION,
            "reviewed_by": "independent_p5_v2_contract_review",
            "contains_human_data": False,
            "evidence_class": "controlled_fixture",
        },
        "oracle": oracle,
    }


def build_nonblind_drafts() -> list[dict[str, Any]]:
    drafts: list[dict[str, Any]] = []
    for index, source in enumerate(load_jsonl(PILOT_PATH)):
        city = source["city"]
        sequence = index
        drafts.append(
            _draft_case(
                case_id=f"p5.pilot.{_prefix(city)}.{index % 6 + 1:03d}",
                split="pilot",
                city=city,
                trip_days=source["days"],
                group_size=source["traveler_count"],
                input_kind="TEXT",
                difficulty=DIFFICULTIES[(index // 6) % 3],
                fault_profile_id="advice_completeness",
                sequence=sequence,
                source_ref={"contract": source["schema_version"], "case_id": source["case_id"]},
                raw_text=source["raw_text"],
                unknown_required=False,
                oracle=_oracle_from_pilot(source, ocr_required=False),
            )
        )
    for split, source_path, sequence_base, city_size in (
        ("dev", P4_ROOT / "dev_v1.jsonl", 1000, 60),
        ("regression", P4_ROOT / "regression_v1.jsonl", 3000, 24),
    ):
        for index, source in enumerate(load_jsonl(source_path)):
            city = source["city"]
            city_index = index % city_size
            sequence = sequence_base + index
            input_kind = "SYNTHETIC_SCREENSHOT" if city_index % 2 else "TEXT"
            fault_profile_id = source["fault_class"]
            raw_text = _raw_text(
                city=city,
                days=source["fixture"]["days"],
                group_size=source["fixture"]["traveler_count"],
                sequence=sequence,
                fault_profile_id=fault_profile_id,
            )
            drafts.append(
                _draft_case(
                    case_id=f"p5.{split}.{_prefix(city)}.{city_index + 1:03d}",
                    split=split,
                    city=city,
                    trip_days=source["fixture"]["days"],
                    group_size=source["fixture"]["traveler_count"],
                    input_kind=input_kind,
                    difficulty=DIFFICULTIES[city_index % 3],
                    fault_profile_id=fault_profile_id,
                    sequence=sequence,
                    source_ref={
                        "contract": source["schema_version"],
                        "case_id": source["case_id"],
                        "case_hash": source["case_hash"],
                        "fixture_hash": source["fixture_hash"],
                        "oracle_hash": source["oracle_hash"],
                    },
                    raw_text=raw_text,
                    unknown_required=source["fixture"]["finding"]["status"] == "UNKNOWN",
                    oracle=_oracle_from_p4(source, ocr_required=input_kind == "SYNTHETIC_SCREENSHOT"),
                )
            )
    return drafts


def build_blind_drafts() -> list[dict[str, Any]]:
    drafts: list[dict[str, Any]] = []
    for city_index, city in enumerate(CITIES):
        for city_case_index in range(30):
            global_index = city_index * 30 + city_case_index
            sequence = 5000 + global_index
            fault_profile_id = FAULT_PROFILES[global_index % len(FAULT_PROFILES)]
            input_kind = "SYNTHETIC_SCREENSHOT" if city_case_index % 2 else "TEXT"
            unknown_required = global_index % 5 == 0
            days = 2 + city_case_index % 4
            group_size = 2 + (city_case_index // 2) % 4
            drafts.append(
                _draft_case(
                    case_id=f"p5.blind.{_prefix(city)}.{city_case_index + 1:03d}",
                    split="frozen_blind",
                    city=city,
                    trip_days=days,
                    group_size=group_size,
                    input_kind=input_kind,
                    difficulty=DIFFICULTIES[city_case_index % 3],
                    fault_profile_id=fault_profile_id,
                    sequence=sequence,
                    source_ref={
                        "contract": "trip-check-p5-blind-source-v2",
                        "case_id": f"blind-source-{_prefix(city)}-{city_case_index + 1:03d}",
                    },
                    raw_text=_raw_text(
                        city=city,
                        days=days,
                        group_size=group_size,
                        sequence=sequence,
                        fault_profile_id=fault_profile_id,
                    ),
                    unknown_required=unknown_required,
                    oracle=None,
                )
            )
    return drafts


def materialization_input_projection(draft: Mapping[str, Any]) -> dict[str, Any]:
    """Return the only fields materializers may observe."""

    return {
        "case_id": draft.get("case_id"),
        "city": draft.get("city"),
        "trip_days": draft.get("trip_days"),
        "group_size": draft.get("group_size"),
        "input_kind": draft.get("input_kind"),
        "product_input": draft.get("product_input"),
        "normalized_input_sha256": draft.get("normalized_input_sha256"),
        "runner_control": draft.get("runner_control"),
    }


def _build_evidence_adapter_input(draft: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt v2 screenshot source text to the frozen Wave1 evidence interface."""

    projected = materialization_input_projection(draft)
    product_input = dict(projected["product_input"])
    if projected["input_kind"] == "SYNTHETIC_SCREENSHOT":
        product_input = {
            "source_type": product_input["source_type"],
            "ocr_text": product_input["source_text"],
            "render_spec": product_input["render_spec"],
        }
    projected["product_input"] = product_input
    projected["normalized_input_sha256"] = digest(product_input)
    return projected


def _adapt_evidence_materialization(
    evidence: dict[str, Any],
    *,
    draft: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebind Wave1 output to the v2 source input and CandidateSet path."""

    source_payload = evidence["source_payload"]
    product_input = draft["product_input"]
    source_payload["product_input"] = product_input
    source_payload["normalized_input_sha256"] = draft["normalized_input_sha256"]
    source_payload["projected_input_sha256"] = digest(product_input)
    source_payload["content_sha256"] = digest(
        {key: value for key, value in source_payload.items() if key != "content_sha256"}
    )

    candidate_set_mode = draft["runner_control"]["candidate_set_mode"]
    if candidate_set_mode != "VALID":
        evidence["candidate_sets"] = []
        evidence["receipts"] = [
            receipt for receipt in evidence["receipts"] if receipt.get("operation") != "route.candidate"
        ]
        provider_snapshot = evidence["provider_snapshot"]
        provider_snapshot["receipt_ids"] = [receipt["receipt_id"] for receipt in evidence["receipts"]]
        provider_snapshot["content_sha256"] = digest(
            {key: value for key, value in provider_snapshot.items() if key != "content_sha256"}
        )
    evidence["evidence_materialization_hash"] = digest(
        {key: value for key, value in evidence.items() if key != "evidence_materialization_hash"}
    )
    return evidence


def _artifact_binding(artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact["artifact_id"],
        "schema_version": artifact["schema_version"],
        "content_sha256": artifact["content_sha256"],
    }


def _receipt_binding(receipt: Mapping[str, Any], *, artifact_id: str) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "schema_version": receipt["schema_version"],
        "content_sha256": digest(receipt),
    }


def _binding_for_materialization(draft: Mapping[str, Any], materialization: Mapping[str, Any]) -> dict[str, Any]:
    binding: dict[str, Any] = {
        "schema_version": "trip-check-p5-materialization-binding-v2",
        "materialization_id": materialization["materialization_id"],
        "materialization_sha256": materialization["materialization_hash"],
        "source_payload": _artifact_binding(materialization["source_payload"]),
        "render_receipt": None,
        "ocr_baseline_receipt": None,
        "provider_snapshot": _artifact_binding(materialization["provider_snapshot"]),
        "evidence_snapshot": _artifact_binding(materialization["evidence_snapshot"]),
        "candidate_sets": [_artifact_binding(item) for item in materialization["candidate_sets"]],
        "fault_script": _artifact_binding(materialization["fault_script"]),
    }
    if materialization["ocr_baseline_receipt"] is not None:
        binding["render_receipt"] = _receipt_binding(
            materialization["render_receipt"],
            artifact_id=f"render-{draft['case_id']}",
        )
        binding["ocr_baseline_receipt"] = _receipt_binding(
            materialization["ocr_baseline_receipt"],
            artifact_id=f"ocr-{draft['case_id']}",
        )
    return binding


def _fault_artifact(draft: Mapping[str, Any]) -> dict[str, Any]:
    case_id = str(draft["case_id"])
    fault_profile_id = str(draft["runner_control"]["fault_profile_id"])
    script: dict[str, Any]
    if fault_profile_id in {"duplicate_apply", "concurrent_apply"}:
        script = build_concurrency_fault_script(
            case_id=case_id,
            workspace_id=f"eval-workspace-{case_id}",
            repair_id=f"eval-repair-{case_id}",
            base_revision=1,
            fault_profile_id=fault_profile_id,  # type: ignore[arg-type]
        )
    else:
        script = {
            "schema_version": "trip-check-p5-nonconcurrent-fault-script-v2",
            "case_id": case_id,
            "fault_profile_id": fault_profile_id,
            "concurrency_expectation": "NONE",
        }
        script["script_sha256"] = digest(script)
    artifact = {
        "schema_version": "trip-check-p5-fault-artifact-v2",
        "artifact_id": f"fault-{case_id}",
        "fault_profile_id": fault_profile_id,
        "script": script,
    }
    artifact["content_sha256"] = digest(artifact)
    return artifact


def _fixed_now(seed: int) -> datetime:
    return datetime(2026, 8, 23, tzinfo=timezone.utc) + timedelta(seconds=seed % 86_400)


async def _materialize_one(
    draft: Mapping[str, Any],
    *,
    ocr_mode: OcrMode,
    work_root: Path,
    actual_ocr_engine: OcrEngine | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = _adapt_evidence_materialization(
        build_evidence_materialization(_build_evidence_adapter_input(draft)),
        draft=draft,
    )
    ocr = None
    if draft["input_kind"] == "SYNTHETIC_SCREENSHOT":
        source_text = str(draft["product_input"]["source_text"])
        engine: OcrEngine | None = actual_ocr_engine if ocr_mode == "actual" else DevelopmentOcrEngine(source_text)
        seed = int(draft["runner_control"]["seed"])
        ocr = await materialize_ocr_input(
            draft["product_input"],
            case_id=str(draft["case_id"]),
            work_root=work_root / str(draft["case_id"]),
            ocr_engine=engine,
            now_factory=lambda seed=seed: _fixed_now(seed),
        )
    fault_artifact = _fault_artifact(draft)
    receipts = list(evidence["receipts"])
    if ocr is not None:
        receipts.append(ocr["cleanup_receipt"])
    materialization: dict[str, Any] = {
        "schema_version": MATERIALIZATION_SCHEMA_VERSION,
        "materialization_id": f"materialization-{draft['case_id']}",
        "case_id": draft["case_id"],
        "source_payload": evidence["source_payload"],
        "render_receipt": ocr["render_receipt"] if ocr is not None else None,
        "ocr_baseline_receipt": ocr["ocr_baseline_receipt"] if ocr is not None else None,
        "provider_snapshot": evidence["provider_snapshot"],
        "evidence_snapshot": evidence["evidence_snapshot"],
        "candidate_sets": evidence["candidate_sets"],
        "fault_script": fault_artifact,
        "receipts": receipts,
    }
    materialization["materialization_hash"] = digest(materialization)
    binding = _binding_for_materialization(draft, materialization)
    return materialization, binding


def _case_from_materialization(draft: Mapping[str, Any], materialization: Mapping[str, Any]) -> dict[str, Any]:
    case = {
        **{key: value for key, value in draft.items() if key != "oracle"},
        "materialization": _binding_for_materialization(draft, materialization),
    }
    oracle = draft.get("oracle")
    if oracle is not None:
        case["oracle"] = oracle
        case["oracle_sha256"] = digest(oracle)
    case["case_hash"] = "0" * 64
    validated = P5CaseV2.model_validate(case).model_dump(mode="json", exclude_none=True)
    validated["case_hash"] = digest({key: value for key, value in validated.items() if key != "case_hash"})
    return P5CaseV2.model_validate(validated).model_dump(mode="json", exclude_none=True)


def _checkpoint_path(checkpoint_root: Path, case_id: str) -> Path:
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789.-_" for character in case_id):
        raise ValueError(f"unsafe checkpoint case_id: {case_id}")
    return checkpoint_root / f"{case_id}.json"


def _checkpoint_payload(
    *,
    draft: Mapping[str, Any],
    case: Mapping[str, Any],
    materialization: Mapping[str, Any],
    ocr_mode: OcrMode,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "ocr_mode": ocr_mode,
        "case_id": draft["case_id"],
        "draft_sha256": digest(draft),
        "materialization_input_sha256": digest(materialization_input_projection(draft)),
        "case": dict(case),
        "materialization": dict(materialization),
    }
    payload["checkpoint_sha256"] = digest(payload)
    return payload


def _validate_checkpoint_artifacts(materialization: Mapping[str, Any], *, ocr_mode: OcrMode) -> None:
    if materialization.get("materialization_hash") != digest(
        {key: value for key, value in materialization.items() if key != "materialization_hash"}
    ):
        raise ValueError("checkpoint materialization hash mismatch")
    artifacts = [
        materialization.get("source_payload"),
        materialization.get("provider_snapshot"),
        materialization.get("evidence_snapshot"),
        materialization.get("fault_script"),
        *materialization.get("candidate_sets", []),
    ]
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or artifact.get("content_sha256") != digest(
            {key: value for key, value in artifact.items() if key != "content_sha256"}
        ):
            raise ValueError("checkpoint artifact hash mismatch")

    ocr = materialization.get("ocr_baseline_receipt")
    render = materialization.get("render_receipt")
    cleanup = [
        receipt
        for receipt in materialization.get("receipts", [])
        if isinstance(receipt, Mapping) and receipt.get("schema_version") == "trip-check-p5-cleanup-receipt-v2"
    ]
    if ocr is None:
        if render is not None or cleanup:
            raise ValueError("text checkpoint contains screenshot artifacts")
        return
    if not isinstance(ocr, Mapping) or not isinstance(render, Mapping) or len(cleanup) != 1:
        raise ValueError("screenshot checkpoint is incomplete")
    cleanup_receipt = cleanup[0]
    if (
        cleanup_receipt.get("cleanup_status") != "DELETED"
        or cleanup_receipt.get("original_removed") is not True
        or cleanup_receipt.get("asset_hash") != render.get("image_sha256")
        or cleanup_receipt.get("asset_hash") != ocr.get("asset_hash")
    ):
        raise ValueError("screenshot checkpoint cleanup is not fail-closed")
    expected_engine = ("paddleocr", "3.7.0") if ocr_mode == "actual" else ("p5-development-ocr", "2.0.0")
    if (ocr.get("engine"), ocr.get("engine_version")) != expected_engine:
        raise ValueError("checkpoint OCR engine binding mismatch")


def _load_checkpoint_pair(
    *, draft: Mapping[str, Any], ocr_mode: OcrMode, checkpoint_root: Path
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    path = _checkpoint_path(checkpoint_root, str(draft["case_id"]))
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError(f"checkpoint symlink is forbidden: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"checkpoint is unreadable: {path.name}") from exc
    expected_fields = {
        "schema_version",
        "ocr_mode",
        "case_id",
        "draft_sha256",
        "materialization_input_sha256",
        "case",
        "materialization",
        "checkpoint_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise ValueError(f"checkpoint schema mismatch: {path.name}")
    if payload["checkpoint_sha256"] != digest(
        {key: value for key, value in payload.items() if key != "checkpoint_sha256"}
    ):
        raise ValueError(f"checkpoint hash mismatch: {path.name}")
    if (
        payload["schema_version"] != CHECKPOINT_SCHEMA_VERSION
        or payload["ocr_mode"] != ocr_mode
        or payload["case_id"] != draft["case_id"]
        or payload["draft_sha256"] != digest(draft)
        or payload["materialization_input_sha256"] != digest(materialization_input_projection(draft))
    ):
        raise ValueError(f"checkpoint input binding mismatch: {path.name}")
    case = payload["case"]
    materialization = payload["materialization"]
    if not isinstance(case, dict) or not isinstance(materialization, dict):
        raise ValueError(f"checkpoint payload mismatch: {path.name}")
    _validate_checkpoint_artifacts(materialization, ocr_mode=ocr_mode)
    expected_case = _case_from_materialization(draft, materialization)
    if case != expected_case:
        raise ValueError(f"checkpoint case binding mismatch: {path.name}")
    return case, materialization


def _write_checkpoint_pair(
    *,
    draft: Mapping[str, Any],
    case: Mapping[str, Any],
    materialization: Mapping[str, Any],
    ocr_mode: OcrMode,
    checkpoint_root: Path,
) -> None:
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    path = _checkpoint_path(checkpoint_root, str(draft["case_id"]))
    payload = _checkpoint_payload(
        draft=draft,
        case=case,
        materialization=materialization,
        ocr_mode=ocr_mode,
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=checkpoint_root,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


async def build_dataset_v2(
    *,
    ocr_mode: OcrMode,
    work_root: Path,
    checkpoint_root: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if ocr_mode not in {"development", "actual"}:
        raise ValueError("ocr_mode must be development or actual")
    nonblind_drafts = build_nonblind_drafts()
    blind_drafts = build_blind_drafts()
    actual_engine: OcrEngine | None = PaddleOcrEngine() if ocr_mode == "actual" else None

    async def complete(draft: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if checkpoint_root is not None:
            cached = _load_checkpoint_pair(
                draft=draft,
                ocr_mode=ocr_mode,
                checkpoint_root=checkpoint_root,
            )
            if cached is not None:
                return cached
        # Remove the label before any materialization helper receives the case.
        # This is a structural isolation boundary, not a convention enforced by
        # individual materializers.
        label_free_draft = dict(draft)
        label_free_draft.pop("oracle")
        materialization, _binding = await _materialize_one(
            label_free_draft,
            ocr_mode=ocr_mode,
            work_root=work_root,
            actual_ocr_engine=actual_engine,
        )
        case = _case_from_materialization(draft, materialization)
        if checkpoint_root is not None:
            _write_checkpoint_pair(
                draft=draft,
                case=case,
                materialization=materialization,
                ocr_mode=ocr_mode,
                checkpoint_root=checkpoint_root,
            )
        return case, materialization

    nonblind_pairs = [await complete(draft) for draft in nonblind_drafts]
    blind_pairs = [await complete(draft) for draft in blind_drafts]
    return (
        [item[0] for item in nonblind_pairs],
        [item[0] for item in blind_pairs],
        [item[1] for item in nonblind_pairs],
        [item[1] for item in blind_pairs],
    )


def case_set_hash(rows: list[dict[str, Any]]) -> str:
    return digest(
        sorted(
            ({"case_id": row["case_id"], "case_hash": row["case_hash"]} for row in rows),
            key=lambda item: item["case_id"],
        )
    )


def materialization_set_hash(rows: list[dict[str, Any]]) -> str:
    return digest(
        sorted(
            (
                {
                    "case_id": row["case_id"],
                    "materialization_id": row["materialization_id"],
                    "materialization_hash": row["materialization_hash"],
                }
                for row in rows
            ),
            key=lambda item: item["case_id"],
        )
    )


def _file_entry(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "path": path.relative_to(BACKEND_ROOT).as_posix(),
        "row_count": len(rows),
        "file_sha256": file_sha256(path),
        "content_sha256": digest(rows),
    }


def legacy_overlap_debt_v2() -> dict[str, Any]:
    dev = load_jsonl(P4_ROOT / "dev_v1.jsonl")
    regression = load_jsonl(P4_ROOT / "regression_v1.jsonl")
    dev_fixture_hashes = {row["fixture_hash"] for row in dev}
    dev_oracle_hashes = {row["oracle_hash"] for row in dev}
    return {
        "status": "RECORDED_NOT_USED_AS_P5_ISOLATION_PROOF",
        "regression_fixture_hashes_overlapping_dev": sum(
            row["fixture_hash"] in dev_fixture_hashes for row in regression
        ),
        "regression_oracle_hashes_overlapping_dev": sum(row["oracle_hash"] in dev_oracle_hashes for row in regression),
        "p5_v2_cross_split_overlap_allowed": 0,
    }


def build_manifest_v2(
    *,
    nonblind_cases: list[dict[str, Any]],
    blind_cases: list[dict[str, Any]],
    nonblind_materializations: list[dict[str, Any]],
    blind_materializations: list[dict[str, Any]],
    ocr_mode: OcrMode,
    sealing_commitment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    all_cases = [*nonblind_cases, *blind_cases]
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "frozen": ocr_mode == "actual",
        "generation": {
            "builder_version": GENERATOR_VERSION,
            "ocr_mode": ocr_mode,
            "formal_validation_eligible": ocr_mode == "actual",
        },
        "hash_policy_version": HASH_POLICY_VERSION,
        "counts": {
            "total": len(all_cases),
            "by_split": dict(sorted(Counter(row["split"] for row in all_cases).items())),
            "by_city": dict(sorted(Counter(row["city"] for row in all_cases).items())),
            "screenshots_by_split": dict(
                sorted(
                    Counter(row["split"] for row in all_cases if row["input_kind"] == "SYNTHETIC_SCREENSHOT").items()
                )
            ),
        },
        "files": {
            "nonblind_cases": _file_entry(NONBLIND_PATH_V2, nonblind_cases),
            "blind_cases": _file_entry(BLIND_INPUT_PATH_V2, blind_cases),
            "nonblind_materializations": _file_entry(
                NONBLIND_MATERIALIZATIONS_PATH_V2,
                nonblind_materializations,
            ),
            "blind_materializations": _file_entry(BLIND_MATERIALIZATIONS_PATH_V2, blind_materializations),
        },
        "lanes": {
            "nonblind": {
                "case_count": len(nonblind_cases),
                "materialization_count": len(nonblind_materializations),
                "case_set_hash": case_set_hash(nonblind_cases),
                "materialization_set_hash": materialization_set_hash(nonblind_materializations),
            },
            "frozen_blind": {
                "case_count": len(blind_cases),
                "materialization_count": len(blind_materializations),
                "case_set_hash": case_set_hash(blind_cases),
                "materialization_set_hash": materialization_set_hash(blind_materializations),
                "label_storage": "external_bundle_only",
                "label_access": "isolated_scorer_only",
                "label_payload_present": False,
            },
        },
        "contract_hashes": {
            "run_spec_template_sha256": file_sha256(RUN_SPEC_TEMPLATE_PATH_V2),
            "judge_rubric_sha256": file_sha256(JUDGE_RUBRIC_PATH_V2),
        },
        "source_artifacts": {
            "pilot": {
                "path": PILOT_PATH.relative_to(BACKEND_ROOT).as_posix(),
                "file_sha256": file_sha256(PILOT_PATH),
            },
            "p4_dev": {
                "path": (P4_ROOT / "dev_v1.jsonl").relative_to(BACKEND_ROOT).as_posix(),
                "file_sha256": file_sha256(P4_ROOT / "dev_v1.jsonl"),
            },
            "p4_regression": {
                "path": (P4_ROOT / "regression_v1.jsonl").relative_to(BACKEND_ROOT).as_posix(),
                "file_sha256": file_sha256(P4_ROOT / "regression_v1.jsonl"),
            },
            "p4_manifest": {
                "path": (P4_ROOT / "dataset_v1.manifest.json").relative_to(BACKEND_ROOT).as_posix(),
                "file_sha256": file_sha256(P4_ROOT / "dataset_v1.manifest.json"),
            },
        },
        "legacy_overlap_debt": legacy_overlap_debt_v2(),
        "evidence_boundary": {
            "controlled_fixture": "MATERIALIZED",
            "actual_ocr": "PASS" if ocr_mode == "actual" else "NOT_RUN",
            "live_provider": "NOT_RUN",
            "public_e2e": "NOT_RUN",
            "human_evidence": "NOT_RUN",
        },
    }
    if sealing_commitment is not None:
        manifest["sealing_commitment"] = dict(sealing_commitment)
    manifest["manifest_hash"] = digest(manifest)
    return manifest


def write_dataset_v2(
    *,
    nonblind_cases: list[dict[str, Any]],
    blind_cases: list[dict[str, Any]],
    nonblind_materializations: list[dict[str, Any]],
    blind_materializations: list[dict[str, Any]],
    ocr_mode: OcrMode,
) -> dict[str, Any]:
    sealing_commitment: Mapping[str, Any] | None = None
    if MANIFEST_PATH_V2.is_file():
        current_manifest = json.loads(MANIFEST_PATH_V2.read_text(encoding="utf-8"))
        current_commitment = current_manifest.get("sealing_commitment")
        if current_commitment is not None:
            if not isinstance(current_commitment, dict):
                raise ValueError("P5_V2_SEALING_COMMITMENT_INVALID")
            sealing_commitment = current_commitment
    write_jsonl(NONBLIND_PATH_V2, nonblind_cases)
    write_jsonl(BLIND_INPUT_PATH_V2, blind_cases)
    write_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V2, nonblind_materializations)
    write_jsonl(BLIND_MATERIALIZATIONS_PATH_V2, blind_materializations)
    manifest = build_manifest_v2(
        nonblind_cases=nonblind_cases,
        blind_cases=blind_cases,
        nonblind_materializations=nonblind_materializations,
        blind_materializations=blind_materializations,
        ocr_mode=ocr_mode,
        sealing_commitment=sealing_commitment,
    )
    write_json(MANIFEST_PATH_V2, manifest)
    return manifest


__all__ = [
    "BLIND_INPUT_PATH_V2",
    "BLIND_MATERIALIZATIONS_PATH_V2",
    "BLIND_SEAL_PATH_V2",
    "FAULT_PROFILES",
    "JUDGE_RUBRIC_PATH_V2",
    "MANIFEST_PATH_V2",
    "NONBLIND_MATERIALIZATIONS_PATH_V2",
    "NONBLIND_PATH_V2",
    "RUN_SPEC_TEMPLATE_PATH_V2",
    "SCREENSHOT_COUNTS",
    "SPLIT_COUNTS",
    "build_blind_drafts",
    "build_dataset_v2",
    "build_manifest_v2",
    "build_nonblind_drafts",
    "case_set_hash",
    "materialization_input_projection",
    "materialization_set_hash",
    "write_dataset_v2",
]
