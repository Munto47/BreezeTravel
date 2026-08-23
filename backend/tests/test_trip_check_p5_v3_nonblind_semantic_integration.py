from __future__ import annotations

from collections import Counter

import pytest

from app.importing.entity_resolver import EntityResolver
from app.importing.models import ResolutionStatus
from app.importing.parser import ItineraryTextParser
from app.importing.screenshots import ScreenshotOcrReceipt, itinerary_text_from_ocr_receipts
from evals.trip_check_v1.p5.adapters_v3 import MaterializedResolutionProviderV3
from evals.trip_check_v1.p5.data_contract import digest, load_jsonl
from evals.trip_check_v1.p5.data_contract_v2 import (
    NONBLIND_MATERIALIZATIONS_PATH_V2,
    NONBLIND_PATH_V2,
)
from evals.trip_check_v1.p5.evidence_materialization_v3 import (
    PROVIDER_SNAPSHOT_ID_V3,
    build_evidence_materialization_v3,
)
from evals.trip_check_v1.p5.semantic_contract_v3 import (
    validate_case_semantics_v3,
    validate_nonblind_oracle_compatibility_v3,
)


@pytest.mark.asyncio
async def test_all_270_nonblind_cases_close_against_actual_product_resolution() -> None:
    cases = load_jsonl(NONBLIND_PATH_V2)
    v2_materializations = {
        item["case_id"]: item for item in load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V2)
    }
    outcomes: Counter[str] = Counter()
    product_statuses: Counter[str] = Counter()
    screenshot_count = 0

    for case in cases:
        v2_materialization = v2_materializations[case["case_id"]]
        if case["input_kind"] == "SYNTHETIC_SCREENSHOT":
            screenshot_count += 1
            receipt = ScreenshotOcrReceipt.model_validate(
                v2_materialization["ocr_baseline_receipt"]
            )
            parser_text = itinerary_text_from_ocr_receipts([receipt])
            product_input = case["product_input"]
        else:
            parser_text = case["product_input"]["raw_text"]
            product_input = case["product_input"]
        runner_control = {
            **case["runner_control"],
            "provider_snapshot_id": PROVIDER_SNAPSHOT_ID_V3,
        }
        build_input = {
            "case_id": case["case_id"],
            "city": case["city"],
            "trip_days": case["trip_days"],
            "group_size": case["group_size"],
            "input_kind": case["input_kind"],
            "product_input": product_input,
            "normalized_input_sha256": digest(product_input),
            "runner_control": runner_control,
        }
        if case["input_kind"] == "SYNTHETIC_SCREENSHOT":
            build_input.update(
                {
                    "render_receipt": v2_materialization["render_receipt"],
                    "ocr_baseline_receipt": v2_materialization["ocr_baseline_receipt"],
                    "cleanup_receipt": next(
                        item
                        for item in v2_materialization["receipts"]
                        if item.get("schema_version")
                        == "trip-check-p5-cleanup-receipt-v2"
                    ),
                }
            )
        materialization = build_evidence_materialization_v3(build_input)
        provider = MaterializedResolutionProviderV3(materialization)
        raw_stops = ItineraryTextParser().parse(
            parser_text, import_id=f"v3-integration-{case['case_id']}"
        ).raw_stops
        resolved = await EntityResolver(provider).resolve_all(raw_stops, city=case["city"])
        declared = materialization["source_payload"]["entity_resolutions"]
        assert validate_case_semantics_v3(
            {
                **{
                    key: value
                    for key, value in case.items()
                    if key not in {"oracle", "oracle_sha256"}
                },
                "runner_control": runner_control,
            },
            materialization,
        ) == []
        assert validate_nonblind_oracle_compatibility_v3(case, materialization) == []
        for expected, actual in zip(declared, resolved, strict=True):
            expected_status = {
                "AUTO_RESOLVED": ResolutionStatus.AUTO_MATCHED,
                "NEEDS_CONFIRMATION": ResolutionStatus.AMBIGUOUS,
                "HARD_REJECTED": ResolutionStatus.NOT_FOUND,
                "NO_CANDIDATE": ResolutionStatus.NOT_FOUND,
            }[expected["outcome"]]
            assert actual.resolution_status == expected_status
            outcomes[expected["outcome"]] += 1
            product_statuses[actual.resolution_status.value] += 1

    assert len(cases) == 270
    assert screenshot_count == 126
    assert outcomes == Counter(
        {"AUTO_RESOLVED": 809, "NEEDS_CONFIRMATION": 3, "HARD_REJECTED": 3}
    )
    assert product_statuses == Counter(
        {"AUTO_MATCHED": 809, "AMBIGUOUS": 3, "NOT_FOUND": 3}
    )
