"""Label-free, receipt-bound Provider materialization for P5 v3."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from functools import lru_cache
from typing import Any

from app.audit.models import EvidenceFact, EvidenceFreshness, EvidenceSnapshot, ProviderFailure
from app.importing.confidence import candidate_confidence, normalize_place_name
from app.importing.parser import ItineraryTextParser
from app.importing.screenshots import ScreenshotOcrReceipt, itinerary_text_from_ocr_receipts
from app.importing.service import parse_time_range
from app.repairs.candidates import FrozenRepairCandidate, freeze_candidate_set
from app.trip_check.provider_integrity import ProviderCallReceipt
from evals.trip_check_v1.p5.data_contract import (
    BACKEND_ROOT,
    P5_ROOT,
    digest,
    file_sha256,
    load_jsonl,
)
from evals.trip_check_v1.p5.evidence_materialization_v2 import (
    _artifact,
    _fact,
    _mapping,
    _observed_at,
    _required_text,
    _route_freshness,
    _sha256,
    validate_evidence_materialization,
)
from evals.trip_check_v1.p5.ocr_materialization_v2 import (
    CONFIRMATION_THRESHOLD,
    _font_path,
    _render_image,
    _validate_product_input,
)


EVIDENCE_MATERIALIZATION_SCHEMA_V3 = "trip-check-p5-evidence-materialization-v3"
EVIDENCE_POLICY_VERSION_V3 = "trip-check-p5-controlled-evidence-v3"
PROVIDER_V3 = "trip-check-p5-controlled-provider-v3"
PROVIDER_SNAPSHOT_ID_V3 = "trip-check-p5-controlled-snapshot-v3"
FAULT_REGISTRY_VERSION_V3 = "trip-check-p5-fault-registry-v2"
BUDGET_PROFILE_V3 = "p5-zero-api-v2"
FAULT_PROFILES_V3 = {
    "advice_completeness",
    "empty_candidate_set",
    "candidate_receipt_missing",
    "route_conflict",
    "duplicate_apply",
    "concurrent_apply",
    "solver_unsat",
    "solver_timeout",
    "solver_fallback",
}
_FORBIDDEN_FIELD_NAMES_V3 = {
    "answer",
    "api_key",
    "blind_label",
    "bundle_path",
    "credential",
    "expected",
    "external_bundle",
    "label",
    "oracle",
    "oracle_sha256",
    "private_key",
    "secret",
    "token",
}


def _reject_forbidden_fields_v3(value: Any, *, path: str = "materialization") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _FORBIDDEN_FIELD_NAMES_V3:
                raise ValueError(f"{path} contains forbidden field: {key}")
            _reject_forbidden_fields_v3(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_forbidden_fields_v3(item, path=f"{path}[{index}]")


def _require_exact_keys_v3(value: Mapping[str, Any], expected: set[str], *, field: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{field} fields mismatch: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )


def _validate_runner_control_v3(
    runner_control: Mapping[str, Any],
) -> tuple[str, str, str, EvidenceFreshness, bool]:
    _require_exact_keys_v3(
        runner_control,
        {
            "provider_snapshot_id",
            "fault_profile_id",
            "fault_registry_version",
            "candidate_set_mode",
            "evidence_freshness",
            "unknown_required",
            "seed",
            "budget_profile",
        },
        field="runner_control",
    )
    provider_snapshot_id = _required_text(
        runner_control.get("provider_snapshot_id"), field="runner_control.provider_snapshot_id"
    )
    if provider_snapshot_id != PROVIDER_SNAPSHOT_ID_V3:
        raise ValueError("runner_control.provider_snapshot_id is not the frozen v3 snapshot")
    fault_profile_id = _required_text(
        runner_control.get("fault_profile_id"), field="runner_control.fault_profile_id"
    )
    if fault_profile_id not in FAULT_PROFILES_V3:
        raise ValueError("runner_control.fault_profile_id is unsupported")
    candidate_set_mode = _required_text(
        runner_control.get("candidate_set_mode"), field="runner_control.candidate_set_mode"
    )
    expected_candidate_mode = {
        "advice_completeness": "VALID",
        "empty_candidate_set": "EMPTY",
        "candidate_receipt_missing": "MISSING_RECEIPT",
    }.get(fault_profile_id, "NOT_APPLICABLE")
    if candidate_set_mode != expected_candidate_mode:
        raise ValueError("runner_control candidate mode contradicts the fault profile")
    if runner_control.get("fault_registry_version") != FAULT_REGISTRY_VERSION_V3:
        raise ValueError("runner_control fault registry version mismatch")
    if runner_control.get("budget_profile") != BUDGET_PROFILE_V3:
        raise ValueError("runner_control budget profile mismatch")
    unknown_required = runner_control.get("unknown_required")
    if not isinstance(unknown_required, bool):
        raise ValueError("runner_control.unknown_required must be boolean")
    route_freshness = _route_freshness(runner_control)
    expected_freshness = (
        EvidenceFreshness.UNAVAILABLE
        if unknown_required
        else EvidenceFreshness.CONFLICTING
        if fault_profile_id == "route_conflict"
        else EvidenceFreshness.FRESH
    )
    if route_freshness != expected_freshness:
        raise ValueError("runner_control evidence freshness contradicts the fault profile")
    return (
        provider_snapshot_id,
        fault_profile_id,
        candidate_set_mode,
        route_freshness,
        unknown_required,
    )


def _screenshot_parser_input_v3(
    *, case_id: str, product_input: Mapping[str, Any], case_payload: Mapping[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    render_receipt = _mapping(case_payload.get("render_receipt"), field="render_receipt")
    ocr_payload = _mapping(
        case_payload.get("ocr_baseline_receipt"), field="ocr_baseline_receipt"
    )
    cleanup_receipt = _mapping(case_payload.get("cleanup_receipt"), field="cleanup_receipt")
    _require_exact_keys_v3(
        render_receipt,
        {
            "schema_version",
            "render_id",
            "case_id",
            "renderer",
            "rendered_at",
            "render_spec_sha256",
            "source_text_sha256",
            "image_sha256",
            "media_type",
            "format",
            "width",
            "height",
            "byte_size",
            "font_sha256",
            "font_size",
            "line_count",
        },
        field="render_receipt",
    )
    _require_exact_keys_v3(
        ocr_payload,
        {
            "schema_version",
            "asset_id",
            "asset_hash",
            "media_type",
            "byte_size",
            "engine",
            "engine_version",
            "observed_at",
            "lines",
        },
        field="ocr_baseline_receipt",
    )
    raw_lines = ocr_payload.get("lines")
    if not isinstance(raw_lines, list):
        raise ValueError("P5 v3 OCR receipt lines are invalid")
    for index, line in enumerate(raw_lines):
        line = _mapping(line, field=f"ocr_baseline_receipt.lines[{index}]")
        _require_exact_keys_v3(
            line,
            {"text", "confidence", "box", "requires_confirmation"},
            field=f"ocr_baseline_receipt.lines[{index}]",
        )
        box = _mapping(line.get("box"), field=f"ocr_baseline_receipt.lines[{index}].box")
        _require_exact_keys_v3(
            box,
            {"x_min", "y_min", "x_max", "y_max"},
            field=f"ocr_baseline_receipt.lines[{index}].box",
        )
        confidence = line.get("confidence")
        requires_confirmation = line.get("requires_confirmation")
        if not isinstance(requires_confirmation, bool):
            raise ValueError("P5 v3 OCR confirmation flag must be boolean")
        if (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and float(confidence) < CONFIRMATION_THRESHOLD
            and not requires_confirmation
        ):
            raise ValueError("P5 v3 low-confidence OCR line requires confirmation")
    _require_exact_keys_v3(
        cleanup_receipt,
        {
            "schema_version",
            "receipt_id",
            "asset_id",
            "asset_hash",
            "terminal_reason",
            "cleanup_status",
            "cleanup_error_category",
            "cleanup_attempted_at",
            "original_removed",
        },
        field="cleanup_receipt",
    )
    source_text, render_spec = _validate_product_input(product_input)
    image_sha256, image_byte_size, render_facts = _render_contract_v3(
        source_text, json.dumps(render_spec, ensure_ascii=False, sort_keys=True)
    )
    media_type = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "WEBP": "image/webp",
    }[str(render_spec["format"])]
    expected_render = {
        "case_id": case_id,
        "renderer": {"name": "pillow-p5-synthetic-screenshot", "version": "2.0.0"},
        "render_spec_sha256": digest(render_spec),
        "source_text_sha256": render_spec["text_sha256"],
        "image_sha256": image_sha256,
        "media_type": media_type,
        "format": render_spec["format"],
        "width": render_spec["width"],
        "height": render_spec["height"],
        "byte_size": image_byte_size,
        **render_facts,
    }
    for field, expected in expected_render.items():
        if render_receipt.get(field) != expected:
            raise ValueError(f"P5 v3 render receipt mismatch: {field}")
    if render_receipt.get("schema_version") != "trip-check-p5-render-receipt-v2":
        raise ValueError("P5 v3 render receipt schema mismatch")
    ocr_receipt = ScreenshotOcrReceipt.model_validate(ocr_payload)
    if (
        ocr_payload.get("schema_version") != "trip-check-p5-ocr-baseline-receipt-v2"
        or ocr_receipt.asset_hash != image_sha256
        or ocr_receipt.byte_size != image_byte_size
        or ocr_receipt.media_type != media_type
        or (ocr_receipt.engine, ocr_receipt.engine_version) != ("paddleocr", "3.7.0")
        or not ocr_receipt.lines
    ):
        raise ValueError("P5 v3 OCR receipt does not bind the rendered screenshot")
    if (
        cleanup_receipt.get("schema_version") != "trip-check-p5-cleanup-receipt-v2"
        or cleanup_receipt.get("asset_id") != ocr_receipt.asset_id
        or cleanup_receipt.get("asset_hash") != image_sha256
        or cleanup_receipt.get("cleanup_status") != "DELETED"
        or cleanup_receipt.get("original_removed") is not True
        or cleanup_receipt.get("cleanup_error_category") is not None
        or cleanup_receipt.get("terminal_reason") != "SUCCEEDED"
    ):
        raise ValueError("P5 v3 screenshot cleanup receipt is not fail-closed")
    parser_text = itinerary_text_from_ocr_receipts([ocr_receipt])
    if not parser_text.strip():
        raise ValueError("P5 v3 OCR receipt produced no parser text")
    source_binding = _bind_sealed_v2_screenshot_receipts(
        case_id=case_id,
        render_receipt=render_receipt,
        ocr_receipt=ocr_payload,
        cleanup_receipt=cleanup_receipt,
    )
    return (
        parser_text,
        dict(render_receipt),
        dict(ocr_payload),
        dict(cleanup_receipt),
        source_binding,
    )


@lru_cache(maxsize=512)
def _render_contract_v3(
    source_text: str, render_spec_json: str
) -> tuple[str, int, dict[str, Any]]:
    render_spec = json.loads(render_spec_json)
    image_bytes, render_facts = _render_image(
        source_text, render_spec, font_path=_font_path()
    )
    return hashlib.sha256(image_bytes).hexdigest(), len(image_bytes), render_facts


@lru_cache(maxsize=1)
def _sealed_v2_materialization_index() -> dict[
    str, tuple[dict[str, Any], str, str, str, str, str, str, str, str]
]:
    manifest_path = P5_ROOT / "dataset_v2.manifest.json"
    seal_path = P5_ROOT / "sealed" / "frozen_blind.v2.seal.json"
    active_contract_path = P5_ROOT / "active_contract.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_hash = manifest.get("manifest_hash")
    if (
        manifest.get("schema_version") != "trip-check-p5-dataset-manifest-v2"
        or manifest.get("dataset_id") != "trip-check-p5-360-v2"
        or manifest.get("frozen") is not True
        or manifest.get("generation", {}).get("formal_validation_eligible") is not True
        or manifest_hash
        != digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
        or manifest.get("sealing_commitment", {}).get("status") != "SEALED"
    ):
        raise ValueError("sealed v2 dataset manifest is invalid")
    manifest_file_hash = file_sha256(manifest_path)
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    seal_file_hash = file_sha256(seal_path)
    commitment = manifest["sealing_commitment"]
    if (
        seal.get("schema_version") != "trip-check-p5-blind-seal-v2"
        or seal.get("split") != "frozen_blind"
        or seal.get("scoring_payload_present") is not False
        or seal.get("case_count") != 90
        or commitment.get("blind_seal_v2_sha256") != seal_file_hash
    ):
        raise ValueError("sealed v2 blind seal is invalid")
    active_contract = json.loads(active_contract_path.read_text(encoding="utf-8"))
    candidate_freeze_commit = commitment.get("candidate_freeze_commit")
    if (
        active_contract.get("schema_version") != "trip-check-p5-active-contract-v1"
        or active_contract.get("active_contract") != "trip-check-p5-v2"
        or active_contract.get("formal_evidence_status") != "READY"
        or active_contract.get("dataset_manifest_hash") != manifest_hash
        or active_contract.get("blind_seal_v2_sha256") != seal_file_hash
        or active_contract.get("candidate_freeze_commit") != candidate_freeze_commit
        or not isinstance(candidate_freeze_commit, str)
        or len(candidate_freeze_commit) != 40
    ):
        raise ValueError("active P5 v2 contract does not anchor the sealed dataset")
    active_contract_hash = digest(active_contract)
    active_contract_file_hash = file_sha256(active_contract_path)

    index: dict[
        str, tuple[dict[str, Any], str, str, str, str, str, str, str, str]
    ] = {}
    path_specs = (
        (
            P5_ROOT / "materializations_nonblind_v2.jsonl",
            "nonblind_materializations",
        ),
        (
            P5_ROOT / "frozen_blind.v2.materializations.jsonl",
            "blind_materializations",
        ),
    )
    for path, manifest_key in path_specs:
        rows = load_jsonl(path)
        path_hash = file_sha256(path)
        relative_path = path.relative_to(BACKEND_ROOT).as_posix()
        file_entry = manifest.get("files", {}).get(manifest_key, {})
        if (
            file_entry.get("path") != relative_path
            or file_entry.get("file_sha256") != path_hash
            or file_entry.get("row_count") != len(rows)
            or file_entry.get("content_sha256") != digest(rows)
        ):
            raise ValueError("sealed v2 materialization file differs from its manifest")
        if manifest_key == "blind_materializations" and (
            seal.get("materializations_file_sha256") != path_hash
            or seal.get("materializations_content_sha256") != digest(rows)
            or seal.get("case_count") != len(rows)
        ):
            raise ValueError("sealed v2 blind materializations differ from the seal")
        for row in rows:
            case_id = _required_text(row.get("case_id"), field="v2_materialization.case_id")
            if case_id in index:
                raise ValueError("sealed v2 materialization case ids must be unique")
            if row.get("materialization_hash") != digest(
                {key: value for key, value in row.items() if key != "materialization_hash"}
            ):
                raise ValueError("sealed v2 materialization hash mismatch")
            index[case_id] = (
                row,
                relative_path,
                path_hash,
                str(manifest_hash),
                manifest_file_hash,
                seal_file_hash,
                active_contract_hash,
                active_contract_file_hash,
                candidate_freeze_commit,
            )
    return index


def _bind_sealed_v2_screenshot_receipts(
    *,
    case_id: str,
    render_receipt: Mapping[str, Any],
    ocr_receipt: Mapping[str, Any],
    cleanup_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    source = _sealed_v2_materialization_index().get(case_id)
    if source is None:
        raise ValueError("P5 v3 screenshot has no sealed v2 source materialization")
    (
        materialization,
        source_path,
        source_file_sha256,
        manifest_hash,
        manifest_file_sha256,
        blind_seal_file_sha256,
        active_contract_hash,
        active_contract_file_sha256,
        candidate_freeze_commit,
    ) = source
    cleanup_rows = [
        item
        for item in materialization.get("receipts", [])
        if isinstance(item, Mapping)
        and item.get("schema_version") == "trip-check-p5-cleanup-receipt-v2"
    ]
    if (
        materialization.get("render_receipt") != render_receipt
        or materialization.get("ocr_baseline_receipt") != ocr_receipt
        or len(cleanup_rows) != 1
        or cleanup_rows[0] != cleanup_receipt
    ):
        raise ValueError("P5 v3 screenshot receipts differ from the sealed v2 source")
    return {
        "schema_version": "trip-check-p5-v3-ocr-source-binding-v1",
        "source_dataset_id": "trip-check-p5-360-v2",
        "source_manifest_hash": manifest_hash,
        "source_manifest_file_sha256": manifest_file_sha256,
        "source_blind_seal_file_sha256": blind_seal_file_sha256,
        "source_active_contract_sha256": active_contract_hash,
        "source_active_contract_file_sha256": active_contract_file_sha256,
        "source_candidate_freeze_commit": candidate_freeze_commit,
        "source_path": source_path,
        "source_file_sha256": source_file_sha256,
        "source_materialization_hash": materialization["materialization_hash"],
        "render_receipt_sha256": digest(render_receipt),
        "ocr_receipt_sha256": digest(ocr_receipt),
        "cleanup_receipt_sha256": digest(cleanup_receipt),
    }


def _place(
    place_id: str,
    name: str,
    city: str,
    lng: float,
    lat: float,
    *,
    aliases: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "place_id": place_id,
        "name": name,
        "city": city,
        "district": "controlled-fixture",
        "address": "controlled-fixture",
        "category": "attraction",
        "coords": {"lng": lng, "lat": lat},
        "aliases": list(aliases),
    }


_PLACES = (
    _place("bj-forbidden-city", "故宫博物院", "北京", 116.397, 39.918, aliases=("故宫",)),
    _place("bj-temple-of-heaven", "天坛公园", "北京", 116.417, 39.883),
    _place("bj-summer-palace", "颐和园", "北京", 116.273, 39.999),
    _place("bj-jingshan", "景山公园", "北京", 116.396, 39.925),
    _place("bj-national-museum", "中国国家博物馆", "北京", 116.407, 39.905),
    _place("bj-capital-museum", "首都博物馆", "北京", 116.350, 39.907),
    _place("bj-tiananmen-square", "天安门广场", "北京", 116.397, 39.904),
    _place("bj-badaling", "八达岭长城", "北京", 116.016, 40.356, aliases=("长城（八达岭）",)),
    _place("bj-nanluoguxiang", "南锣鼓巷", "北京", 116.403, 39.938),
    _place("bj-sanlitun-taikooli", "三里屯太古里", "北京", 116.455, 39.933),
    _place("sh-bund", "外滩", "上海", 121.490, 31.241),
    _place("sh-yuyuan", "豫园", "上海", 121.493, 31.227),
    _place("sh-oriental-pearl", "东方明珠广播电视塔", "上海", 121.500, 31.240),
    _place("sh-oriental-pearl-park", "东方明珠公园", "上海", 121.501, 31.239),
    _place("sh-tianzifang", "田子坊", "上海", 121.475, 31.211),
    _place("sh-disney", "上海迪士尼乐园", "上海", 121.657, 31.144),
    _place("hz-west-lake", "西湖风景名胜区", "杭州", 120.148, 30.244),
    _place("hz-west-lake-cultural-square", "西湖文化广场", "杭州", 120.165, 30.281),
    _place("hz-lingyin", "灵隐寺", "杭州", 120.102, 30.240),
    _place("hz-leifeng", "雷峰塔", "杭州", 120.149, 30.231),
    _place("hz-xixi", "西溪湿地国家公园", "杭州", 120.063, 30.268),
    _place("hz-hefang", "河坊街·清河坊", "杭州", 120.174, 30.238),
    _place("hz-longjing", "龙井村", "杭州", 120.104, 30.219, aliases=("龙井村（茶园）",)),
)
_PLACE_BY_ID = {item["place_id"]: item for item in _PLACES}
_ALIASES = {
    normalize_place_name(alias): _PLACE_BY_ID[place_id]
    for alias, place_id in {
        "故宫博物院": "bj-forbidden-city",
        "故宫": "bj-forbidden-city",
        "天坛公园": "bj-temple-of-heaven",
        "颐和园": "bj-summer-palace",
        "景山公园": "bj-jingshan",
        "中国国家博物馆": "bj-national-museum",
        "首都博物馆": "bj-capital-museum",
        "天安门广场": "bj-tiananmen-square",
        "长城（八达岭）": "bj-badaling",
        "八达岭长城": "bj-badaling",
        "南锣鼓巷": "bj-nanluoguxiang",
        "三里屯太古里": "bj-sanlitun-taikooli",
        "外滩": "sh-bund",
        "豫园": "sh-yuyuan",
        "东方明珠广播电视塔": "sh-oriental-pearl",
        "田子坊": "sh-tianzifang",
        "上海迪士尼乐园": "sh-disney",
        "西湖风景名胜区": "hz-west-lake",
        "灵隐寺": "hz-lingyin",
        "雷峰塔": "hz-leifeng",
        "西溪湿地国家公园": "hz-xixi",
        "河坊街·清河坊": "hz-hefang",
        "龙井村（茶园）": "hz-longjing",
        "龙井村": "hz-longjing",
    }.items()
}
_AMBIGUOUS = {
    normalize_place_name("博物馆"): ("bj-national-museum", "bj-capital-museum"),
    normalize_place_name("东方明珠"): ("sh-oriental-pearl", "sh-oriental-pearl-park"),
    normalize_place_name("西湖"): ("hz-west-lake", "hz-west-lake-cultural-square"),
}


def _receipt_semantic_payload(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "provider": receipt.get("provider"),
        "operation": receipt.get("operation"),
        "execution_mode": receipt.get("execution_mode"),
        "status": receipt.get("status"),
        "request_hash": receipt.get("request_hash"),
        "response_hash": receipt.get("response_hash"),
        "observed_at": receipt.get("observed_at"),
        "source_url": receipt.get("source_url"),
        "affected_fields": receipt.get("affected_fields", []),
        "failure_category": receipt.get("failure_category"),
    }


def _provider_receipt(
    *,
    provider: str,
    operation: str,
    status: str,
    request: Mapping[str, Any],
    response: Mapping[str, Any],
    observed_at: datetime,
    affected_fields: Sequence[str] = (),
    failure_category: str | None = None,
) -> ProviderCallReceipt:
    payload = {
        "provider": provider,
        "operation": operation,
        "execution_mode": "fixture",
        "status": status,
        "request_hash": digest(request),
        "response_hash": digest(response),
        "observed_at": observed_at,
        "source_url": f"fixture://trip-check-p5-v3/{operation}",
        "affected_fields": list(affected_fields),
        "failure_category": failure_category,
    }
    json_payload = ProviderCallReceipt(**payload, receipt_id="pending").model_dump(mode="json")
    return ProviderCallReceipt(**payload, receipt_id=digest(_receipt_semantic_payload(json_payload)))


def _resolution_candidates(
    raw_name: str, normalized_name: str, city: str
) -> tuple[str, list[dict[str, Any]]]:
    ambiguous_ids = _AMBIGUOUS.get(normalized_name)
    if ambiguous_ids is not None:
        candidates = [dict(_PLACE_BY_ID[item]) for item in ambiguous_ids]
    else:
        exact = _ALIASES.get(normalized_name)
        if exact is None:
            return "NO_CANDIDATE", []
        candidates = [dict(exact)]
    same_city = [item for item in candidates if item["city"] == city]
    if not same_city:
        return "HARD_REJECTED", candidates
    scored = sorted(
        (
            (candidate_confidence(raw_name, item, city=city)[0], str(item["place_id"]), item)
            for item in same_city
        ),
        key=lambda item: (-item[0], item[1]),
    )
    top_score = scored[0][0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    outcome = (
        "AUTO_RESOLVED"
        if top_score >= 0.90 and top_score - second_score >= 0.08
        else "NEEDS_CONFIRMATION"
    )
    return outcome, [item[2] for item in scored]


def _resolution_plan(
    *, text: str, city: str, trip_days: int, case_id: str, observed_at: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[ProviderCallReceipt]]:
    parsed = ItineraryTextParser().parse(text, import_id=f"materialize-v3-{case_id}")
    if parsed.errors:
        raise ValueError("controlled v3 materialization parser rejected input")
    if any(stop.day_index < 0 or stop.day_index >= trip_days for stop in parsed.raw_stops):
        raise ValueError("controlled v3 materialization stop is outside trip_days")
    stops: list[dict[str, Any]] = []
    resolutions: list[dict[str, Any]] = []
    receipts: list[ProviderCallReceipt] = []
    for ordinal, raw_stop in enumerate(parsed.raw_stops):
        normalized_name = normalize_place_name(raw_stop.raw_name)
        outcome, candidates = _resolution_candidates(raw_stop.raw_name, normalized_name, city)
        response = {"outcome": outcome, "candidates": candidates}
        search_receipt = _provider_receipt(
            provider=PROVIDER_V3,
            operation="place.search",
            status="SUCCEEDED",
            request={"query": raw_stop.raw_name, "city": city},
            response=response,
            observed_at=observed_at,
            affected_fields=(f"entity_resolutions.{ordinal}",),
        )
        receipts.append(search_receipt)
        selected = candidates[0]["place_id"] if outcome == "AUTO_RESOLVED" else None
        resolutions.append(
            {
                "ordinal": ordinal,
                "day_index": raw_stop.day_index,
                "raw_name": raw_stop.raw_name,
                "normalized_name": normalized_name,
                "outcome": outcome,
                "selected_place_id": selected,
                "search_receipt_id": search_receipt.receipt_id,
                "candidates": candidates,
            }
        )
        if selected is None:
            continue
        candidate = candidates[0]
        start_time, end_time, _duration = parse_time_range(raw_stop.raw_time)
        stops.append(
            {
                "stop_id": f"stop-{ordinal + 1}",
                "place_id": selected,
                "display_name": candidate["name"],
                "city": city,
                "day_index": raw_stop.day_index,
                "order_index": sum(item["day_index"] == raw_stop.day_index for item in stops),
                "start_time": start_time,
                "end_time": end_time,
                "coords": candidate["coords"],
            }
        )
    if not parsed.raw_stops:
        raise ValueError("controlled v3 materialization parsed no stops")
    return stops, resolutions, receipts


def _build_evidence_materialization_v3_unvalidated(
    case_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one v3 artifact from label-free product and runner inputs."""

    _reject_forbidden_fields_v3(case_payload, path="case_payload")
    case_id = _required_text(case_payload.get("case_id"), field="case_id")
    city = _required_text(case_payload.get("city"), field="city")
    if city not in {"北京", "上海", "杭州"}:
        raise ValueError("city is outside the controlled P5 scope")
    input_kind = _required_text(case_payload.get("input_kind"), field="input_kind")
    if input_kind not in {"TEXT", "SYNTHETIC_SCREENSHOT"}:
        raise ValueError("input_kind is unsupported")
    expected_case_fields = {
        "case_id",
        "city",
        "trip_days",
        "group_size",
        "input_kind",
        "product_input",
        "normalized_input_sha256",
        "runner_control",
    }
    if input_kind == "SYNTHETIC_SCREENSHOT":
        expected_case_fields.update(
            {"render_receipt", "ocr_baseline_receipt", "cleanup_receipt"}
        )
    _require_exact_keys_v3(case_payload, expected_case_fields, field="case_payload")
    trip_days = case_payload.get("trip_days")
    group_size = case_payload.get("group_size")
    if not isinstance(trip_days, int) or isinstance(trip_days, bool) or not 2 <= trip_days <= 5:
        raise ValueError("trip_days must be between 2 and 5")
    if not isinstance(group_size, int) or isinstance(group_size, bool) or not 2 <= group_size <= 5:
        raise ValueError("group_size must be between 2 and 5")
    normalized_input_sha256 = _sha256(
        case_payload.get("normalized_input_sha256"), field="normalized_input_sha256"
    )
    runner_control = _mapping(case_payload.get("runner_control"), field="runner_control")
    seed = runner_control.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("runner_control.seed must be an integer")
    (
        provider_snapshot_id,
        fault_profile_id,
        candidate_set_mode,
        route_freshness,
        unknown_required,
    ) = _validate_runner_control_v3(runner_control)
    product_input = _mapping(case_payload.get("product_input"), field="product_input")
    if normalized_input_sha256 != digest(product_input):
        raise ValueError("normalized_input_sha256 does not bind the source product input")
    render_receipt = None
    ocr_baseline_receipt = None
    cleanup_receipt = None
    ocr_source_binding = None
    if input_kind == "TEXT":
        _require_exact_keys_v3(
            product_input, {"source_type", "raw_text"}, field="product_input"
        )
        if product_input.get("source_type") != "MANUAL_TEXT":
            raise ValueError("text product input source_type mismatch")
        raw_text = _required_text(product_input.get("raw_text"), field="product_input.raw_text")
    else:
        _require_exact_keys_v3(
            product_input,
            {"source_type", "source_text", "render_spec"},
            field="product_input",
        )
        if product_input.get("source_type") != "SYNTHETIC_SCREENSHOT":
            raise ValueError("screenshot product input source_type mismatch")
        (
            raw_text,
            render_receipt,
            ocr_baseline_receipt,
            cleanup_receipt,
            ocr_source_binding,
        ) = _screenshot_parser_input_v3(
            case_id=case_id,
            product_input=product_input,
            case_payload=case_payload,
        )
    observed_at = _observed_at(seed)
    stops, resolutions, receipts = _resolution_plan(
        text=raw_text,
        city=city,
        trip_days=trip_days,
        case_id=case_id,
        observed_at=observed_at,
    )
    source_payload = _artifact(
        "trip-check-p5-source-payload-v3",
        f"source-{case_id}",
        case_id=case_id,
        city=city,
        trip_days=trip_days,
        group_size=group_size,
        input_kind=case_payload.get("input_kind"),
        normalized_input_sha256=normalized_input_sha256,
        source_input_sha256=digest(product_input),
        parser_input_sha256=digest(
            {
                "parser_text": raw_text,
                "ocr_receipt_sha256": (
                    digest(ocr_baseline_receipt) if ocr_baseline_receipt is not None else None
                ),
            }
        ),
        ocr_source_binding=ocr_source_binding,
        product_input=product_input,
        stops=stops,
        entity_resolutions=resolutions,
    )
    snapshot_id = f"snapshot-{digest({'case_id': case_id, 'input': normalized_input_sha256, 'v': 3})[:24]}"
    facts: list[EvidenceFact] = []
    identity_receipts: dict[str, ProviderCallReceipt] = {}
    for stop in stops:
        identity_response = {
            "place_id": stop["place_id"],
            "name": stop["display_name"],
            "city": city,
            "coords": stop["coords"],
        }
        identity_receipt = _provider_receipt(
            provider=PROVIDER_V3,
            operation="place.resolve",
            status="SUCCEEDED",
            request={"query": stop["display_name"], "city": city},
            response=identity_response,
            observed_at=observed_at,
            affected_fields=(f"places.{stop['place_id']}.identity",),
        )
        if stop["place_id"] in identity_receipts:
            continue
        receipts.append(identity_receipt)
        identity_receipts[stop["place_id"]] = identity_receipt
        facts.append(
            _fact(
                snapshot_id=snapshot_id,
                subject_type="PLACE",
                subject_id=stop["place_id"],
                fact_type="POI_IDENTITY",
                value=identity_response,
                receipt=identity_receipt,
                freshness=EvidenceFreshness.FRESH,
                confidence=1,
            )
        )
        opening_receipt = _provider_receipt(
            provider=PROVIDER_V3,
            operation="place.opening_hours",
            status="SUCCEEDED",
            request={"place_id": stop["place_id"]},
            response={"opening_hours": "07:00-22:00"},
            observed_at=observed_at,
            affected_fields=(f"places.{stop['place_id']}.opening_hours",),
        )
        receipts.append(opening_receipt)
        facts.append(
            _fact(
                snapshot_id=snapshot_id,
                subject_type="PLACE",
                subject_id=stop["place_id"],
                fact_type="OPENING_HOURS",
                value="07:00-22:00",
                receipt=opening_receipt,
                freshness=EvidenceFreshness.FRESH,
                confidence=1,
            )
        )

    for left, right in zip(stops, stops[1:]):
        if left["day_index"] != right["day_index"]:
            continue
        edge_id = f"{left['stop_id']}->{right['stop_id']}"
        for conflict_index, duration in enumerate(
            (20, 55) if route_freshness == EvidenceFreshness.CONFLICTING else (20,)
        ):
            unavailable = route_freshness == EvidenceFreshness.UNAVAILABLE
            response = (
                {"reason_code": "PROVIDER_ROUTE_UNAVAILABLE"}
                if unavailable
                else {"mode": "driving", "duration_minutes": duration, "distance_km": 3.0}
            )
            route_receipt = _provider_receipt(
                provider=PROVIDER_V3 if conflict_index == 0 else f"{PROVIDER_V3}-alternate",
                operation="route.audit",
                status="UNAVAILABLE" if unavailable else "SUCCEEDED",
                request={"edge_id": edge_id, "mode": "driving", "source": conflict_index},
                response=response,
                observed_at=observed_at,
                affected_fields=(f"route_edges.{edge_id}",),
                failure_category="PROVIDER_ROUTE_UNAVAILABLE" if unavailable else None,
            )
            receipts.append(route_receipt)
            facts.append(
                _fact(
                    snapshot_id=snapshot_id,
                    subject_type="ROUTE_EDGE",
                    subject_id=edge_id,
                    fact_type="ROUTE_TIME",
                    value=response,
                    receipt=route_receipt,
                    freshness=route_freshness,
                    confidence=0 if unavailable else 1,
                )
            )

    candidate_sets = []
    if candidate_set_mode == "VALID":
        candidates: list[FrozenRepairCandidate] = []
        for stop in {item["place_id"]: item for item in stops}.values():
            candidate_route_receipt = _provider_receipt(
                provider=PROVIDER_V3,
                operation="route.candidate",
                status="SUCCEEDED",
                request={"anchor": "trip-center", "candidate_place_id": stop["place_id"]},
                response={
                    "anchor": "trip-center",
                    "candidate_place_id": stop["place_id"],
                    "mode": "driving",
                    "duration_minutes": 15,
                },
                observed_at=observed_at,
                affected_fields=(f"candidate_routes.{stop['place_id']}",),
            )
            receipts.append(candidate_route_receipt)
            candidates.append(
                FrozenRepairCandidate(
                    canonical_place_id=stop["place_id"],
                    display_name=stop["display_name"],
                    place_receipt_id=identity_receipts[stop["place_id"]].receipt_id,
                    route_receipt_ids=(candidate_route_receipt.receipt_id,),
                )
            )
        if candidates:
            frozen = freeze_candidate_set(f"candidate-set-{case_id}", candidates)
            candidate_sets.append(
                _artifact(
                    "trip-check-p5-candidate-set-v3",
                    frozen.candidate_set_id,
                    candidate_set=frozen.model_dump(mode="json"),
                )
            )
    provider_failures = []
    if route_freshness == EvidenceFreshness.UNAVAILABLE:
        provider_failures.append(
            ProviderFailure(
                provider=PROVIDER_V3,
                error_category="PROVIDER_ROUTE_UNAVAILABLE",
                retryable=False,
                detail="controlled fault keeps affected route fields UNKNOWN",
            )
        )
    snapshot = EvidenceSnapshot(
        snapshot_id=snapshot_id,
        workspace_id=f"eval-workspace-{case_id}",
        itinerary_revision=1,
        provider_set=sorted({receipt.provider for receipt in receipts}),
        policy_version=EVIDENCE_POLICY_VERSION_V3,
        facts=facts,
        provider_failures=provider_failures,
        created_at=observed_at,
    )
    materialization = {
        "schema_version": EVIDENCE_MATERIALIZATION_SCHEMA_V3,
        "case_id": case_id,
        "source_payload": source_payload,
        "provider_snapshot": _artifact(
            "trip-check-p5-provider-snapshot-v3",
            provider_snapshot_id,
            execution_mode="fixture",
            fault_profile_id=fault_profile_id,
            evidence_freshness=route_freshness.value,
            candidate_set_mode=candidate_set_mode,
            unknown_required=unknown_required,
            fault_registry_version=FAULT_REGISTRY_VERSION_V3,
            budget_profile=BUDGET_PROFILE_V3,
            seed=seed,
            runner_control_sha256=digest(runner_control),
            receipt_ids=[receipt.receipt_id for receipt in receipts],
        ),
        "evidence_snapshot": _artifact(
            "trip-check-p5-evidence-snapshot-v3",
            snapshot.snapshot_id,
            snapshot=snapshot.model_dump(mode="json"),
        ),
        "render_receipt": render_receipt,
        "ocr_baseline_receipt": ocr_baseline_receipt,
        "cleanup_receipt": cleanup_receipt,
        "candidate_sets": candidate_sets,
        "receipts": [receipt.model_dump(mode="json") for receipt in receipts],
    }
    materialization["evidence_materialization_hash"] = digest(materialization)
    return materialization


def validate_evidence_materialization_v3(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed on v3 hashes, receipts, schemas, and semantic closure."""

    canonical = deepcopy(dict(payload))
    _reject_forbidden_fields_v3(canonical)
    _require_exact_keys_v3(
        canonical,
        {
            "schema_version",
            "case_id",
            "source_payload",
            "provider_snapshot",
            "evidence_snapshot",
            "render_receipt",
            "ocr_baseline_receipt",
            "cleanup_receipt",
            "candidate_sets",
            "receipts",
            "evidence_materialization_hash",
        },
        field="materialization",
    )
    if canonical.get("schema_version") != EVIDENCE_MATERIALIZATION_SCHEMA_V3:
        raise ValueError("unsupported P5 v3 evidence materialization schema")
    expected_hash = digest(
        {
            key: value
            for key, value in canonical.items()
            if key != "evidence_materialization_hash"
        }
    )
    if canonical.get("evidence_materialization_hash") != expected_hash:
        raise ValueError("P5 v3 evidence materialization hash mismatch")
    legacy_projection = {
        key: deepcopy(canonical[key])
        for key in (
            "schema_version",
            "case_id",
            "source_payload",
            "provider_snapshot",
            "evidence_snapshot",
            "candidate_sets",
            "receipts",
            "evidence_materialization_hash",
        )
    }
    legacy_projection["schema_version"] = "trip-check-p5-evidence-materialization-v2"
    legacy_projection["evidence_materialization_hash"] = digest(
        {
            key: value
            for key, value in legacy_projection.items()
            if key != "evidence_materialization_hash"
        }
    )
    validate_evidence_materialization(legacy_projection)

    source = _mapping(canonical.get("source_payload"), field="source_payload")
    provider_snapshot = _mapping(
        canonical.get("provider_snapshot"), field="provider_snapshot"
    )
    evidence = _mapping(canonical.get("evidence_snapshot"), field="evidence_snapshot")
    _require_exact_keys_v3(
        source,
        {
            "schema_version",
            "artifact_id",
            "content_sha256",
            "case_id",
            "city",
            "trip_days",
            "group_size",
            "input_kind",
            "normalized_input_sha256",
            "source_input_sha256",
            "parser_input_sha256",
            "ocr_source_binding",
            "product_input",
            "stops",
            "entity_resolutions",
        },
        field="source_payload",
    )
    _require_exact_keys_v3(
        provider_snapshot,
        {
            "schema_version",
            "artifact_id",
            "content_sha256",
            "execution_mode",
            "fault_profile_id",
            "evidence_freshness",
            "candidate_set_mode",
            "unknown_required",
            "fault_registry_version",
            "budget_profile",
            "seed",
            "runner_control_sha256",
            "receipt_ids",
        },
        field="provider_snapshot",
    )
    _require_exact_keys_v3(
        evidence,
        {"schema_version", "artifact_id", "content_sha256", "snapshot"},
        field="evidence_snapshot",
    )
    if source.get("schema_version") != "trip-check-p5-source-payload-v3":
        raise ValueError("P5 v3 source payload schema mismatch")
    if provider_snapshot.get("schema_version") != "trip-check-p5-provider-snapshot-v3":
        raise ValueError("P5 v3 provider snapshot schema mismatch")
    if provider_snapshot.get("execution_mode") != "fixture":
        raise ValueError("P5 v3 provider snapshot execution mode mismatch")
    if provider_snapshot.get("artifact_id") != PROVIDER_SNAPSHOT_ID_V3:
        raise ValueError("P5 v3 provider snapshot id mismatch")
    fault_profile_id = provider_snapshot.get("fault_profile_id")
    if fault_profile_id not in FAULT_PROFILES_V3:
        raise ValueError("P5 v3 provider snapshot fault profile mismatch")
    expected_candidate_mode = {
        "advice_completeness": "VALID",
        "empty_candidate_set": "EMPTY",
        "candidate_receipt_missing": "MISSING_RECEIPT",
    }.get(str(fault_profile_id), "NOT_APPLICABLE")
    if provider_snapshot.get("candidate_set_mode") != expected_candidate_mode:
        raise ValueError("P5 v3 provider snapshot candidate mode mismatch")
    if provider_snapshot.get("fault_registry_version") != FAULT_REGISTRY_VERSION_V3:
        raise ValueError("P5 v3 provider snapshot fault registry mismatch")
    if provider_snapshot.get("budget_profile") != BUDGET_PROFILE_V3:
        raise ValueError("P5 v3 provider snapshot budget profile mismatch")
    unknown_required = provider_snapshot.get("unknown_required")
    if not isinstance(unknown_required, bool):
        raise ValueError("P5 v3 provider snapshot unknown policy mismatch")
    evidence_freshness = provider_snapshot.get("evidence_freshness")
    if evidence_freshness not in {"FRESH", "CONFLICTING", "UNAVAILABLE"}:
        raise ValueError("P5 v3 provider snapshot evidence freshness mismatch")
    expected_freshness = (
        "UNAVAILABLE"
        if unknown_required
        else "CONFLICTING"
        if fault_profile_id == "route_conflict"
        else "FRESH"
    )
    if evidence_freshness != expected_freshness:
        raise ValueError("P5 v3 evidence freshness contradicts the fault profile")
    if evidence.get("schema_version") != "trip-check-p5-evidence-snapshot-v3":
        raise ValueError("P5 v3 evidence snapshot schema mismatch")
    snapshot = _mapping(evidence.get("snapshot"), field="evidence_snapshot.snapshot")
    _require_exact_keys_v3(
        snapshot,
        {
            "snapshot_id",
            "workspace_id",
            "itinerary_revision",
            "provider_set",
            "policy_version",
            "facts",
            "provider_failures",
            "created_at",
            "supersedes_snapshot_id",
        },
        field="evidence_snapshot.snapshot",
    )
    if snapshot.get("policy_version") != EVIDENCE_POLICY_VERSION_V3:
        raise ValueError("P5 v3 evidence policy mismatch")
    receipts = canonical.get("receipts")
    if not isinstance(receipts, list) or any(
        not isinstance(item, Mapping)
        or not str(item.get("source_url", "")).startswith("fixture://trip-check-p5-v3/")
        for item in receipts
    ):
        raise ValueError("P5 v3 receipt provenance mismatch")
    receipt_fields = {
        "receipt_id",
        "provider",
        "operation",
        "execution_mode",
        "status",
        "request_hash",
        "response_hash",
        "observed_at",
        "source_url",
        "affected_fields",
        "failure_category",
    }
    for index, receipt in enumerate(receipts):
        _require_exact_keys_v3(receipt, receipt_fields, field=f"receipts[{index}]")
    candidate_sets = canonical.get("candidate_sets")
    if not isinstance(candidate_sets, list):
        raise ValueError("P5 v3 candidate sets must be an array")
    for index, candidate_artifact in enumerate(candidate_sets):
        candidate_artifact = _mapping(
            candidate_artifact, field=f"candidate_sets[{index}]"
        )
        _require_exact_keys_v3(
            candidate_artifact,
            {"schema_version", "artifact_id", "content_sha256", "candidate_set"},
            field=f"candidate_sets[{index}]",
        )

    product_input = _mapping(source.get("product_input"), field="source_payload.product_input")
    case_id = _required_text(canonical.get("case_id"), field="case_id")
    if source.get("case_id") != case_id or source.get("artifact_id") != f"source-{case_id}":
        raise ValueError("P5 v3 source payload case binding mismatch")
    if source.get("city") not in {"北京", "上海", "杭州"}:
        raise ValueError("P5 v3 source payload city mismatch")
    if (
        not isinstance(source.get("trip_days"), int)
        or isinstance(source.get("trip_days"), bool)
        or not 2 <= source["trip_days"] <= 5
        or not isinstance(source.get("group_size"), int)
        or isinstance(source.get("group_size"), bool)
        or not 2 <= source["group_size"] <= 5
    ):
        raise ValueError("P5 v3 source payload trip bounds mismatch")
    input_kind = source.get("input_kind")
    if input_kind not in {"TEXT", "SYNTHETIC_SCREENSHOT"}:
        raise ValueError("P5 v3 source payload input kind mismatch")
    source_type = product_input.get("source_type")
    if input_kind == "TEXT" and source_type != "MANUAL_TEXT":
        raise ValueError("P5 v3 text source type mismatch")
    if input_kind == "SYNTHETIC_SCREENSHOT" and source_type != "SYNTHETIC_SCREENSHOT":
        raise ValueError("P5 v3 screenshot source type mismatch")
    expected_product_fields = (
        {"source_type", "raw_text"}
        if input_kind == "TEXT"
        else {"source_type", "source_text", "render_spec"}
    )
    _require_exact_keys_v3(
        product_input,
        expected_product_fields,
        field="source_payload.product_input",
    )
    if input_kind == "SYNTHETIC_SCREENSHOT":
        render_spec = _mapping(
            product_input.get("render_spec"), field="source_payload.product_input.render_spec"
        )
        _require_exact_keys_v3(
            render_spec,
            {
                "schema_version",
                "format",
                "theme",
                "layout",
                "width",
                "height",
                "seed",
                "text_sha256",
            },
            field="source_payload.product_input.render_spec",
        )
    projected_hash = digest(product_input)
    if (
        source.get("normalized_input_sha256") != projected_hash
        or source.get("source_input_sha256") != projected_hash
    ):
        raise ValueError("P5 v3 source payload input hash mismatch")
    if input_kind == "TEXT":
        if any(
            canonical.get(field) is not None
            for field in ("render_receipt", "ocr_baseline_receipt", "cleanup_receipt")
        ):
            raise ValueError("P5 v3 text materialization contains screenshot receipts")
        raw_text = product_input.get("raw_text")
    else:
        raw_text, _render, _ocr, _cleanup, expected_source_binding = _screenshot_parser_input_v3(
            case_id=case_id,
            product_input=product_input,
            case_payload=canonical,
        )
        if source.get("ocr_source_binding") != expected_source_binding:
            raise ValueError("P5 v3 OCR source binding mismatch")
    if input_kind == "TEXT" and source.get("ocr_source_binding") is not None:
        raise ValueError("P5 v3 text source contains an OCR source binding")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("P5 v3 source payload has no parser text")
    expected_parser_hash = digest(
        {
            "parser_text": raw_text,
            "ocr_receipt_sha256": (
                digest(canonical["ocr_baseline_receipt"])
                if canonical["ocr_baseline_receipt"] is not None
                else None
            ),
        }
    )
    if source.get("parser_input_sha256") != expected_parser_hash:
        raise ValueError("P5 v3 parser input lineage mismatch")
    resolutions = source.get("entity_resolutions")
    if not isinstance(resolutions, list):
        raise ValueError("P5 v3 source payload has no entity resolutions")
    for resolution in resolutions:
        if not isinstance(resolution, Mapping):
            raise ValueError("P5 v3 source payload entity resolution is invalid")
        raw_name = resolution.get("raw_name")
        if not isinstance(raw_name, str):
            raise ValueError("P5 v3 source payload entity resolution name is invalid")
        _require_exact_keys_v3(
            resolution,
            {
                "ordinal",
                "day_index",
                "raw_name",
                "normalized_name",
                "outcome",
                "selected_place_id",
                "search_receipt_id",
                "candidates",
            },
            field="source_payload.entity_resolutions[]",
        )
        raw_candidates = resolution.get("candidates")
        if not isinstance(raw_candidates, list):
            raise ValueError("P5 v3 source payload entity candidates are invalid")
        for candidate in raw_candidates:
            candidate = _mapping(candidate, field="source_payload.entity_resolutions[].candidates[]")
            _require_exact_keys_v3(
                candidate,
                {
                    "place_id",
                    "name",
                    "city",
                    "district",
                    "address",
                    "category",
                    "coords",
                    "aliases",
                },
                field="source_payload.entity_resolutions[].candidates[]",
            )
        normalized_name = normalize_place_name(raw_name)
        expected_outcome, expected_candidates = _resolution_candidates(
            raw_name, normalized_name, str(source.get("city"))
        )
        if (
            resolution.get("normalized_name") != normalized_name
            or resolution.get("outcome") != expected_outcome
            or resolution.get("candidates") != expected_candidates
        ):
            raise ValueError("P5 v3 entity resolution contradicts the frozen provider catalog")
    semantic_case = {
        "case_id": canonical.get("case_id"),
        "city": source.get("city"),
        "trip_days": source.get("trip_days"),
        "group_size": source.get("group_size"),
        "input_kind": input_kind,
        "product_input": product_input,
        "normalized_input_sha256": source.get("normalized_input_sha256"),
        "runner_control": {
            "provider_snapshot_id": provider_snapshot.get("artifact_id"),
            "fault_profile_id": provider_snapshot.get("fault_profile_id"),
            "fault_registry_version": provider_snapshot.get("fault_registry_version"),
            "candidate_set_mode": provider_snapshot.get("candidate_set_mode"),
            "evidence_freshness": provider_snapshot.get("evidence_freshness"),
            "unknown_required": provider_snapshot.get("unknown_required"),
            "seed": provider_snapshot.get("seed"),
            "budget_profile": provider_snapshot.get("budget_profile"),
        },
    }
    from evals.trip_check_v1.p5.semantic_contract_v3 import validate_case_semantics_v3

    errors = validate_case_semantics_v3(semantic_case, canonical)
    if errors:
        raise ValueError("; ".join(errors))
    if provider_snapshot.get("runner_control_sha256") != digest(
        semantic_case["runner_control"]
    ):
        raise ValueError("P5 v3 provider snapshot runner control binding mismatch")
    rebuild_input = {
        "case_id": case_id,
        "city": source.get("city"),
        "trip_days": source.get("trip_days"),
        "group_size": source.get("group_size"),
        "input_kind": input_kind,
        "product_input": product_input,
        "normalized_input_sha256": source.get("normalized_input_sha256"),
        "runner_control": semantic_case["runner_control"],
    }
    if input_kind == "SYNTHETIC_SCREENSHOT":
        rebuild_input.update(
            {
                "render_receipt": canonical["render_receipt"],
                "ocr_baseline_receipt": canonical["ocr_baseline_receipt"],
                "cleanup_receipt": canonical["cleanup_receipt"],
            }
        )
    expected_materialization = _build_evidence_materialization_v3_unvalidated(rebuild_input)
    if canonical != expected_materialization:
        raise ValueError("P5 v3 materialization differs from deterministic frozen rebuild")
    return canonical


def build_evidence_materialization_v3(case_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build and read back one label-free, deterministic v3 materialization."""

    return validate_evidence_materialization_v3(
        _build_evidence_materialization_v3_unvalidated(case_payload)
    )


__all__ = [
    "EVIDENCE_MATERIALIZATION_SCHEMA_V3",
    "EVIDENCE_POLICY_VERSION_V3",
    "BUDGET_PROFILE_V3",
    "FAULT_PROFILES_V3",
    "FAULT_REGISTRY_VERSION_V3",
    "PROVIDER_V3",
    "PROVIDER_SNAPSHOT_ID_V3",
    "build_evidence_materialization_v3",
    "validate_evidence_materialization_v3",
]
