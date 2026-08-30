from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.trip_understanding.screenshot_ocr import (
    ScreenshotImageResultV1,
    ScreenshotOcrEngineBindingV1,
    ScreenshotSourceDocumentV1,
    ScreenshotSourceLineV1,
    SemanticSpanV1,
)
from evals.g04_screenshot.baseline import (
    G04LicensedScreenshotBaselineV1,
    G04LicensedScreenshotCaseV1,
    canonical_sha256,
    oracle_projection_payload,
)
from evals.g04_screenshot.contracts import (
    G04ScreenshotParityCaseV1,
    G04ScreenshotParityManifestV1,
)
from evals.g04_screenshot.scorer import (
    G04ScreenshotManifestError,
    score_g04_screenshot_manifest,
)
from scripts.run_g04_paddle_gate import (
    DEFAULT_BASELINE,
    FieldObservation,
    FormalGateError,
    OcrSuiteResult,
    _build_real_cases,
    _formal_projection,
    _git,
    _load_baseline,
    _lines_for_field,
    _observe_case,
    _observe_document,
    _require_formal_evaluable_baseline,
    _run_ocr_suite,
    _serious_errors,
    _synthetic_manifest_cases,
    sanitize_external_receipt,
)


def _baseline() -> G04LicensedScreenshotBaselineV1:
    return G04LicensedScreenshotBaselineV1.model_validate(json.loads(DEFAULT_BASELINE.read_text(encoding="utf-8")))


def _simulated_frozen_baseline() -> G04LicensedScreenshotBaselineV1:
    """Unit-only frozen fixture; it is never serialized as formal evidence."""

    payload = _baseline().model_dump(mode="json")
    payload["parity_status"] = "EVALUABLE_LICENSED_REAL"
    payload["review"]["status"] = "FROZEN_HASH_BOUND"
    payload["review"]["adjudication_artifact_sha256"] = "9" * 64
    for index, binding in enumerate(payload["review"]["bindings"], start=1):
        binding["prompt_sha256"] = f"{index:x}" * 64
        binding["output_sha256"] = f"{index + 3:x}" * 64
    place_names = ("红石峡", "袁家界", "太白山", "万善寺")
    for index, (case, place_name) in enumerate(zip(payload["cases"], place_names, strict=True)):
        digest = f"{index + 10:x}" * 64
        paired_text = f"{case['license']['source_title']}\nDay 1\n游览 {place_name}"
        case["case_status"] = "FROZEN_HASH_BOUND"
        capture = case["capture_provenance"]
        capture.update(
            {
                "capture_status": "FROZEN_HASH_BOUND",
                "capture_sha256": digest,
                "source_file": f"case-{index + 1}.png",
                "source_size_bytes": 1024 + index,
                "image_width": 1080,
                "image_height": 1920,
                "captured_at": "2026-08-30T12:00:00Z",
                "capture_method": "BROWSER_SCREENSHOT_THEN_CROP",
                "viewport_css_width": 1080,
                "viewport_css_height": 1920,
                "device_scale_factor": 1.0,
                "crop_xywh": [0, 0, 1080, 1920],
                "paired_visible_text": paired_text,
                "paired_visible_text_sha256": hashlib.sha256(paired_text.encode("utf-8")).hexdigest(),
            }
        )
        case["fields"] = [
            {
                "field_id": f"case-{index + 1}-destination",
                "field_type": "DESTINATION",
                "expected_text": case["license"]["source_title"],
                "region_xyxy": [20, 20, 400, 120],
                "extraction": "WHOLE_LINE",
                "reading_order_index": 0,
            },
            {
                "field_id": f"case-{index + 1}-day",
                "field_type": "DAY_MARKER",
                "expected_text": "Day 1",
                "region_xyxy": [20, 140, 400, 240],
                "extraction": "WHOLE_LINE",
                "reading_order_index": 1,
            },
            {
                "field_id": f"case-{index + 1}-place",
                "field_type": "ACTIVITY_PLACE",
                "expected_text": place_name,
                "region_xyxy": [20, 260, 500, 360],
                "extraction": "CONTAINS_EXPECTED_TEXT",
                "reading_order_index": 2,
                "activity_role": "PLANNED",
                "place_metric_eligibility": (
                    "NOT_APPLICABLE" if index == 3 else "ELIGIBLE"
                ),
            },
        ]
        if index == 3:
            for field in case["fields"]:
                field["must_confirm"] = True
        case["forbidden_place_predictions"] = [
            "这是描述句而不是地点",
            "https://example.invalid/place",
        ]
    projection = oracle_projection_payload(
        tuple(
            G04LicensedScreenshotCaseV1.model_validate(case)
            for case in payload["cases"]
        )
    )
    payload["review"]["oracle_projection_sha256"] = canonical_sha256(
        projection
    )
    return G04LicensedScreenshotBaselineV1.model_validate(payload)


def _exact_geometry_document(
    baseline: G04LicensedScreenshotBaselineV1,
) -> ScreenshotSourceDocumentV1:
    semantic_parts: list[str] = []
    source_lines: list[ScreenshotSourceLineV1] = []
    image_results: list[ScreenshotImageResultV1] = []
    cursor = 0
    reading_index = 0
    for image_index, case in enumerate(baseline.cases):
        image_line_count = 0
        for field in case.fields:
            if semantic_parts:
                semantic_parts.append("\n")
                cursor += 1
            start = cursor
            line_text = (
                f"游览 {field.expected_text}" if field.extraction == "CONTAINS_EXPECTED_TEXT" else field.expected_text
            )
            semantic_parts.append(line_text)
            cursor += len(line_text)
            left, top, right, bottom = field.region_xyxy
            source_lines.append(
                ScreenshotSourceLineV1(
                    image_index=image_index,
                    reading_index=reading_index,
                    text=line_text,
                    confidence=0.83712 if field.must_confirm else 0.99,
                    bbox=((left, top), (right, top), (right, bottom), (left, bottom)),
                    semantic_span=SemanticSpanV1(start=start, end=cursor),
                    requires_confirmation=field.must_confirm,
                )
            )
            reading_index += 1
            image_line_count += 1
        image_results.append(
            ScreenshotImageResultV1(
                image_index=image_index,
                content_hash=case.source_sha256,
                status="SUCCEEDED",
                line_count=image_line_count,
            )
        )
    return ScreenshotSourceDocumentV1.create(
        semantic_text="".join(semantic_parts),
        partial=False,
        images=tuple(image_results),
        lines=tuple(source_lines),
        engine_binding=ScreenshotOcrEngineBindingV1.create(
            engine="paddleocr",
            engine_version="3.7.0",
            runtime="paddlepaddle",
            runtime_version="3.3.1",
            configuration={"fixture_scope": "geometry_only"},
            low_confidence_threshold=0.85,
        ),
    )


def _passing_manifest_payload() -> dict[str, object]:
    licensed_cases = []
    for index, digest in enumerate(("a", "d", "e"), start=1):
        place = f"planned-place-{index}"
        licensed_cases.append(
            {
                "case_id": f"licensed-{index:02d}",
                "source": {
                    "evidence_tier": "LICENSED_REAL_SCREENSHOT",
                    "source_sha256": digest * 64,
                    "license_name": "Controlled test fixture license",
                    "license_reference": f"receipt:licensed-{index:02d}",
                },
                "metric_scope": ["REAL_OCR_READING", "REAL_PLANNED_PARITY"],
                "expected_key_fields": ["day=1", f"place={place}", "time=09:00"],
                "observed_key_fields": ["day=1", f"place={place}", "time=09:00"],
                "expected_reading_order": ["day", "place", "time"],
                "observed_reading_order": ["day", "place", "time"],
                "expected_low_confidence_fields": [],
                "observed_confirmation_fields": [],
                "place_metric": {
                    "status": "EVALUATED",
                    "oracle_items": [
                        {
                            "oracle_id": f"planned-{index}",
                            "expected_text": place,
                            "activity_role": "PLANNED",
                            "metric_eligibility": "ELIGIBLE",
                        }
                    ],
                    "reference_executable_places": [place],
                    "text_executable_places": [place],
                    "screenshot_executable_places": [place],
                },
                "serious_errors": [],
            }
        )
    return {
        "schema_version": "g04-screenshot-parity-manifest-v1",
        "cases": [
            *licensed_cases,
            {
                "case_id": "licensed-low-confidence-control",
                "source": {
                    "evidence_tier": "LICENSED_REAL_SCREENSHOT",
                    "source_sha256": "f" * 64,
                    "license_name": "Controlled test fixture license",
                    "license_reference": "receipt:licensed-low-confidence-control",
                },
                "metric_scope": ["REAL_LOW_CONFIDENCE_CONTROL"],
                "expected_key_fields": ["place=guarded"],
                "observed_key_fields": ["place=guarded"],
                "expected_reading_order": ["place=guarded"],
                "observed_reading_order": ["place=guarded"],
                "expected_low_confidence_fields": ["guarded:confirmation"],
                "observed_confirmation_fields": ["guarded:confirmation"],
                "place_metric": {
                    "status": "NOT_APPLICABLE",
                    "reason": "LOW_CONFIDENCE_CONTROL",
                    "oracle_items": [
                        {
                            "oracle_id": "guarded-place",
                            "expected_text": "guarded",
                            "activity_role": "PLANNED",
                            "metric_eligibility": "NOT_APPLICABLE",
                        }
                    ],
                    "reference_executable_places": "NOT_APPLICABLE",
                    "text_executable_places": "NOT_APPLICABLE",
                    "screenshot_executable_places": "NOT_APPLICABLE",
                },
                "serious_errors": [],
            },
            {
                "case_id": "synthetic-01",
                "source": {
                    "evidence_tier": "SYNTHETIC_FORMAT_ONLY",
                    "source_sha256": "b" * 64,
                    "synthetic_spec_sha256": "c" * 64,
                },
                "metric_scope": ["SYNTHETIC_FORMAT_CONTROL"],
                "expected_key_fields": ["format=chat"],
                "observed_key_fields": [],
                "expected_reading_order": ["format"],
                "observed_reading_order": [],
                "expected_low_confidence_fields": [],
                "observed_confirmation_fields": [],
                "place_metric": {
                    "status": "NOT_APPLICABLE",
                    "reason": "SYNTHETIC_FORMAT_ONLY",
                    "oracle_items": [],
                    "reference_executable_places": "NOT_APPLICABLE",
                    "text_executable_places": "NOT_APPLICABLE",
                    "screenshot_executable_places": "NOT_APPLICABLE",
                },
                "serious_errors": [],
            },
        ],
        "performance": {
            "warmup_runs": 2,
            "measured_runs": 20,
            "image_count": 3,
            "image_width": 1080,
            "image_height": 1920,
            "max_concurrency": 1,
            "durations_ms": list(range(1, 21)),
        },
    }


def _passing_manifest() -> G04ScreenshotParityManifestV1:
    return G04ScreenshotParityManifestV1.model_validate(_passing_manifest_payload())


def test_frozen_baseline_binds_exact_revisions_and_adjudicated_capture_truth() -> None:
    baseline = _baseline()

    assert baseline.dataset_id == "g04-wikivoyage-real-planned-capture-v2"
    assert baseline.parity_status == "EVALUABLE_LICENSED_REAL"
    assert baseline.review.status == "FROZEN_HASH_BOUND"
    assert baseline.review.human_evidence is False
    assert baseline.review.oracle_projection_sha256 == (
        "4981cd620f6bb6eb2d5bc4ed3c1583c4829ad5a628b896091b94787721470dfa"
    )
    assert [(case.license.source_title, case.license.source_revision_id) for case in baseline.cases] == [
        ("云台山", 220410),
        ("张家界", 221547),
        ("秦岭", 133737),
        ("云台山", 220410),
    ]
    assert all(
        case.capture_provenance is not None
        and case.capture_provenance.capture_status == "FROZEN_HASH_BOUND"
        and case.capture_provenance.capture_sha256 == case.source_sha256
        and case.capture_provenance.paired_visible_text == case.paired_visible_text
        and len(case.fields) >= 2
        and case.forbidden_place_predictions
        for case in baseline.cases
    )
    assert {
        (case.source_file, case.image_width, case.image_height) for case in baseline.synthetic_performance_cases
    } == {
        ("beijing.png", 1080, 1920),
        ("shanghai.png", 1080, 1920),
        ("hangzhou.png", 1080, 1920),
    }
    assert all(
        case.evidence_tier == "SYNTHETIC_PERFORMANCE_ONLY" and case.quality_metric_eligibility == "NOT_APPLICABLE"
        for case in baseline.synthetic_performance_cases
    )
    loaded = _load_baseline(DEFAULT_BASELINE)
    _require_formal_evaluable_baseline(loaded)
    assert sum(len(case.fields) for case in loaded.cases) == 84


def test_capture_and_render_provenance_are_a_strict_union() -> None:
    payload = _baseline().model_dump(mode="json")
    payload["cases"][0]["render_provenance"] = {
        "provenance_kind": "RENDER_PROVENANCE",
        "source_material_kind": "DERIVED_TEXT_DAY",
        "derivative_disclosure": "unit-only invalid mixed provenance",
        "source_text": "unit",
        "source_text_sha256": hashlib.sha256(b"unit").hexdigest(),
        "render_script_repository_path": "unit-only",
        "render_script_sha256": "a" * 64,
        "source_file": "unit.png",
        "source_sha256": "b" * 64,
        "source_size_bytes": 100,
        "image_width": 1080,
        "image_height": 1920,
    }

    with pytest.raises(ValidationError, match="strict exclusive union"):
        G04LicensedScreenshotBaselineV1.model_validate(payload)


def test_manifest_schema_and_scorer_split_ocr_and_planned_parity_scopes() -> None:
    schema_path = DEFAULT_BASELINE.parents[2] / "evals/g04_screenshot/manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(_passing_manifest_payload())

    report = score_g04_screenshot_manifest(_passing_manifest())

    assert report.gate_pass is True
    assert report.licensed_real_case_count == 4
    assert report.licensed_real_ocr_case_count == 3
    assert report.licensed_real_planned_parity_case_count == 3
    assert report.reference_place_count == 3
    assert report.performance_sample_count == 20


@pytest.mark.parametrize("missing_scope", ["REAL_OCR_READING", "REAL_PLANNED_PARITY"])
def test_scorer_requires_three_real_cases_for_each_metric_scope(
    missing_scope: str,
) -> None:
    payload = _passing_manifest_payload()
    case = payload["cases"][0]
    assert isinstance(case, dict)
    case["metric_scope"] = [scope for scope in case["metric_scope"] if scope != missing_scope]
    if missing_scope == "REAL_PLANNED_PARITY":
        case["place_metric"] = {
            "status": "NOT_APPLICABLE",
            "reason": "OCR_ONLY_SCOPE",
            "oracle_items": [
                {
                    "oracle_id": "ocr-only-place",
                    "expected_text": "planned-place-1",
                    "activity_role": "PLANNED",
                    "metric_eligibility": "NOT_APPLICABLE",
                }
            ],
            "reference_executable_places": "NOT_APPLICABLE",
            "text_executable_places": "NOT_APPLICABLE",
            "screenshot_executable_places": "NOT_APPLICABLE",
        }

    with pytest.raises(G04ScreenshotManifestError, match="three LICENSED_REAL_SCREENSHOT"):
        score_g04_screenshot_manifest(payload)


def test_reference_control_is_frozen_but_never_enters_planned_denominator() -> None:
    payload = _passing_manifest_payload()
    case = payload["cases"][0]
    assert isinstance(case, dict)
    case["metric_scope"].append("REFERENCE_CONTROL")
    case["place_metric"]["oracle_items"].append(
        {
            "oracle_id": "reference-list-item",
            "expected_text": "普通景点列表",
            "activity_role": "REFERENCE",
            "metric_eligibility": "NOT_APPLICABLE",
        }
    )

    report = score_g04_screenshot_manifest(payload)

    assert report.gate_pass is True
    assert report.reference_control_case_count == 1
    assert report.reference_place_count == 3


def test_non_planned_oracle_cannot_be_marked_eligible() -> None:
    payload = _passing_manifest_payload()
    oracle = payload["cases"][0]["place_metric"]["oracle_items"][0]
    oracle["activity_role"] = "REFERENCE"

    with pytest.raises(ValidationError, match="only PLANNED"):
        G04ScreenshotParityManifestV1.model_validate(payload)


def test_ocr_only_place_fields_are_explicitly_not_applicable() -> None:
    payload = _passing_manifest_payload()["cases"][0]
    payload["metric_scope"] = ["REAL_OCR_READING"]
    payload["place_metric"] = {
        "status": "NOT_APPLICABLE",
        "reason": "OCR_ONLY_SCOPE",
        "oracle_items": [
            {
                "oracle_id": "ocr-only-place",
                "expected_text": "planned-place-1",
                "activity_role": "PLANNED",
                "metric_eligibility": "NOT_APPLICABLE",
            }
        ],
        "reference_executable_places": "NOT_APPLICABLE",
        "text_executable_places": "NOT_APPLICABLE",
        "screenshot_executable_places": "NOT_APPLICABLE",
    }

    case = G04ScreenshotParityCaseV1.model_validate(payload)

    assert case.place_metric.status == "NOT_APPLICABLE"
    assert case.place_metric.text_executable_places == "NOT_APPLICABLE"


@pytest.mark.parametrize(
    "mutation",
    [
        "zero_duration",
        "fewer_measured",
        "fewer_warmups",
        "single_reading_item",
        "bilateral_empty_places",
        "empty_low_confidence_denominator",
    ],
)
def test_formal_manifest_cannot_pass_vacuous_evidence(mutation: str) -> None:
    payload = _passing_manifest_payload()
    real_cases = payload["cases"][:3]
    all_licensed_cases = payload["cases"][:4]
    if mutation == "zero_duration":
        payload["performance"]["durations_ms"][0] = 0
    elif mutation == "fewer_measured":
        payload["performance"]["durations_ms"] = list(range(1, 20))
    elif mutation == "fewer_warmups":
        payload["performance"]["warmup_runs"] = 1
    elif mutation == "single_reading_item":
        real_cases[0]["expected_reading_order"] = ["day"]
    elif mutation == "bilateral_empty_places":
        real_cases[0]["place_metric"]["text_executable_places"] = []
        real_cases[0]["place_metric"]["screenshot_executable_places"] = []
    elif mutation == "empty_low_confidence_denominator":
        for case in all_licensed_cases:
            case["expected_low_confidence_fields"] = []
            case["observed_confirmation_fields"] = []
    with pytest.raises((ValidationError, G04ScreenshotManifestError)):
        score_g04_screenshot_manifest(payload)


def test_geometry_extraction_does_not_search_for_expected_substrings() -> None:
    case = _simulated_frozen_baseline().cases[0]
    field = case.fields[0]
    left, top, right, bottom = field.region_xyxy
    line = ScreenshotSourceLineV1(
        image_index=0,
        reading_index=0,
        text="完全错误的观测",
        confidence=0.99,
        bbox=((left, top), (right, top), (right, bottom), (left, bottom)),
        semantic_span=SemanticSpanV1(start=0, end=len("完全错误的观测")),
        requires_confirmation=False,
    )

    observation: FieldObservation = _observe_case(case, (line,))

    assert len(observation.observed_key_fields) == 1
    assert observation.observed_key_fields[0] != observation.expected_key_fields[0]


def test_same_line_reading_order_comes_from_observed_text_position() -> None:
    case = _simulated_frozen_baseline().cases[0]
    template = next(item for item in case.fields if item.field_type == "ACTIVITY_PLACE")
    first = template.model_copy(
        update={
            "field_id": "same-line-first",
            "expected_text": "红石峡",
            "reading_order_index": 0,
        }
    )
    second = template.model_copy(
        update={
            "field_id": "same-line-second",
            "expected_text": "游客中心",
            "reading_order_index": 1,
        }
    )
    same_line_case = case.model_copy(update={"fields": (first, second)})
    left, top, right, bottom = template.region_xyxy
    line = ScreenshotSourceLineV1(
        image_index=0,
        reading_index=0,
        text="游客中心 红石峡",
        confidence=0.99,
        bbox=((left, top), (right, top), (right, bottom), (left, bottom)),
        semantic_span=SemanticSpanV1(start=0, end=len("游客中心 红石峡")),
        requires_confirmation=False,
    )

    observation = _observe_case(same_line_case, (line,))

    assert observation.observed_reading_order == tuple(
        reversed(observation.expected_reading_order)
    )


def test_repeated_same_line_text_uses_the_nearest_observed_occurrence() -> None:
    case = _simulated_frozen_baseline().cases[0]
    template = next(item for item in case.fields if item.field_type == "ACTIVITY_PLACE")
    first = template.model_copy(
        update={
            "field_id": "z-first-occurrence",
            "region_xyxy": (0, 0, 80, 20),
            "reading_order_index": 0,
        }
    )
    second = template.model_copy(
        update={
            "field_id": "a-second-occurrence",
            "region_xyxy": (250, 0, 350, 20),
            "reading_order_index": 1,
        }
    )
    repeated_case = case.model_copy(update={"fields": (first, second)})
    line = ScreenshotSourceLineV1(
        image_index=0,
        reading_index=0,
        text="红石峡 中间 红石峡",
        confidence=0.99,
        bbox=((0, 0), (400, 0), (400, 20), (0, 20)),
        semantic_span=SemanticSpanV1(start=0, end=len("红石峡 中间 红石峡")),
        requires_confirmation=False,
    )

    observation = _observe_case(repeated_case, (line,))

    assert observation.observed_reading_order == observation.expected_reading_order


def test_unrelated_low_confidence_noise_cannot_satisfy_a_field_confirmation() -> None:
    case = _simulated_frozen_baseline().cases[3]
    field = next(item for item in case.fields if item.field_type == "ACTIVITY_PLACE")
    single_field_case = case.model_copy(update={"fields": (field,)})
    left, top, right, bottom = field.region_xyxy
    target = ScreenshotSourceLineV1(
        image_index=0,
        reading_index=0,
        text=f"游览 {field.expected_text}",
        confidence=0.99,
        bbox=((left, top), (right, top), (right, bottom), (left, bottom)),
        semantic_span=SemanticSpanV1(start=0, end=len(f"游览 {field.expected_text}")),
        requires_confirmation=False,
    )
    noise = ScreenshotSourceLineV1(
        image_index=0,
        reading_index=1,
        text="|",
        confidence=0.1,
        bbox=((left + 1, top + 1), (left + 5, top + 1), (left + 5, top + 5), (left + 1, top + 5)),
        semantic_span=SemanticSpanV1(start=0, end=1),
        requires_confirmation=True,
    )

    with pytest.raises(FormalGateError, match="LOW_CONFIDENCE_FIELD_BINDING_MISMATCH"):
        _observe_case(single_field_case, (target, noise))


def test_frozen_baseline_rejects_any_oracle_projection_mutation() -> None:
    original = _simulated_frozen_baseline().model_dump(mode="json")
    mutations: list[dict[str, object]] = []

    expected_text = json.loads(json.dumps(original))
    expected_text["cases"][0]["fields"][0]["expected_text"] += "变更"
    mutations.append(expected_text)

    region = json.loads(json.dumps(original))
    region["cases"][0]["fields"][0]["region_xyxy"][0] += 1
    mutations.append(region)

    extraction = json.loads(json.dumps(original))
    extraction["cases"][0]["fields"][0]["extraction"] = "CONTAINS_EXPECTED_TEXT"
    mutations.append(extraction)

    deleted_field = json.loads(json.dumps(original))
    deleted_field["cases"][0]["fields"].pop(1)
    deleted_field["cases"][0]["fields"][1]["reading_order_index"] = 1
    mutations.append(deleted_field)

    role = json.loads(json.dumps(original))
    place = role["cases"][0]["fields"][2]
    place["activity_role"] = "REFERENCE"
    place["place_metric_eligibility"] = "NOT_APPLICABLE"
    planned_copy = json.loads(json.dumps(place))
    planned_copy.update(
        {
            "field_id": "case-1-place-planned-copy",
            "activity_role": "PLANNED",
            "place_metric_eligibility": "ELIGIBLE",
            "reading_order_index": 3,
        }
    )
    role["cases"][0]["fields"].append(planned_copy)
    mutations.append(role)

    for payload in mutations:
        with pytest.raises(ValidationError, match="baseline oracle projection"):
            G04LicensedScreenshotBaselineV1.model_validate(payload)


def test_loader_rejects_adjudication_artifact_projection_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import scripts.run_g04_paddle_gate as gate

    baseline = _simulated_frozen_baseline()
    artifact_projection = oracle_projection_payload(baseline.cases)
    artifact_projection[0]["fields"][0]["expected_text"] += "篡改"
    transcribers = sorted(
        (
            item
            for item in baseline.review.bindings
            if "VISUAL_TRANSCRIBER" in item.role
        ),
        key=lambda item: item.task_id,
    )
    adjudicator = next(
        item
        for item in baseline.review.bindings
        if item.role == "G04_ORACLE_INDEPENDENT_ADJUDICATOR"
    )
    artifact = {
        "schema_version": "g04-adjudicated-oracle-v1",
        "evidence_label": "MULTI_AGENT_SIMULATED_REVIEW",
        "human_evidence": False,
        "adjudication_payload_sha256": adjudicator.output_sha256,
        "transcriber_output_sha256s": [item.output_sha256 for item in transcribers],
        "oracle_projection": artifact_projection,
    }
    artifact_bytes = json.dumps(
        artifact,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = baseline.model_dump(mode="json")
    payload["review"]["adjudication_artifact_sha256"] = hashlib.sha256(
        artifact_bytes
    ).hexdigest()
    baseline_path = tmp_path / "baseline.json"
    artifact_path = (
        tmp_path / "backend/eval_data/g04_screenshot/adjudicated_oracle_v1.json"
    )
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(artifact_bytes)
    baseline_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    render_hash = baseline.synthetic_performance_cases[0].render_script_sha256
    monkeypatch.setattr(gate, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(gate, "_sha256_file", lambda _path: render_hash)

    with pytest.raises(FormalGateError, match="ORACLE_PROJECTION_MISMATCH"):
        gate._load_baseline(baseline_path)


def test_extra_planned_place_is_a_hard_serious_error() -> None:
    case = _simulated_frozen_baseline().cases[0]
    unexpected = SimpleNamespace(
        compiled=SimpleNamespace(
            mention=SimpleNamespace(
                role="PLANNED",
                raw_text="游客中心",
                atomic_place_name="游客中心",
                span_start=0,
                span_end=4,
                day_index=1,
            ),
            eligible_for_place_search=True,
        ),
        resolver_receipt={"status": "RESOLVED"},
    )
    output = SimpleNamespace(
        destination={"name": "云台山"},
        activities=(unexpected,),
    )

    errors = _serious_errors(case=case, output=output)

    assert [error.category for error in errors] == ["WRONG_CATEGORY"]


def test_missing_destination_oracle_forbids_an_invented_city() -> None:
    case = _simulated_frozen_baseline().cases[0]
    without_destination = case.model_copy(
        update={
            "fields": tuple(
                field
                for field in case.fields
                if field.field_type != "DESTINATION"
            )
        }
    )
    output = SimpleNamespace(destination={"name": "上海"}, activities=())

    errors = _serious_errors(case=without_destination, output=output)

    assert [error.category for error in errors] == ["WRONG_CITY"]


def test_confirmation_recall_binds_field_identity_even_when_ocr_text_is_wrong() -> None:
    baseline = _simulated_frozen_baseline()
    case = baseline.cases[3]
    field = next(item for item in case.fields if item.field_type == "ACTIVITY_PLACE")
    case_lines = tuple(
        line
        for line in _exact_geometry_document(baseline).lines
        if line.image_index == 3
    )
    lines = tuple(
        line.model_copy(update={"text": "游览 游客中必"})
        if _lines_for_field(field, (line,))
        else line
        for line in case_lines
    )

    observation = _observe_case(case, lines)

    confirmation_token = f"{field.field_id}:confirmation"
    assert confirmation_token in observation.expected_low_confidence_fields
    assert confirmation_token in observation.observed_confirmation_fields
    assert not set(observation.expected_key_fields) <= set(
        observation.observed_key_fields
    )


def test_formal_runner_rejects_a_frozen_low_confidence_field_without_actual_confirmation() -> None:
    baseline = _simulated_frozen_baseline()
    document = _exact_geometry_document(baseline)
    unguarded_lines = tuple(
        line.model_copy(
            update={"confidence": 0.99, "requires_confirmation": False}
        )
        if line.requires_confirmation
        else line
        for line in document.lines
    )
    unguarded_document = document.model_copy(update={"lines": unguarded_lines})
    with pytest.raises(
        FormalGateError,
        match="LOW_CONFIDENCE_FIELD_BINDING_MISMATCH",
    ):
        _observe_document(baseline, unguarded_document)


def test_non_deep_city_parity_uses_planned_mentions_without_claiming_city_resolution() -> None:
    baseline = _simulated_frozen_baseline()
    document = _exact_geometry_document(baseline)
    suite = OcrSuiteResult(
        document=document,
        observations=_observe_document(baseline, document),
        durations_ms=tuple(float(value) for value in range(1, 21)),
        staged_cleanup_count=70,
        measured_output_signature="a" * 64,
    )

    cases, low_confidence_poi_calls, internal_leak_count = asyncio.run(
        _build_real_cases(baseline=baseline, suite=suite)
    )

    assert low_confidence_poi_calls == 0
    assert internal_leak_count == 0
    assert all(case.serious_errors == () for case in cases)
    assert all(case.place_metric.status == "EVALUATED" for case in cases[:3])
    assert cases[3].place_metric.status == "NOT_APPLICABLE"
    assert all(
        case.place_metric.text_executable_places
        == case.place_metric.screenshot_executable_places
        == case.place_metric.reference_executable_places
        for case in cases[:3]
    )


def test_synthetic_manifest_cases_use_not_applicable_place_fields() -> None:
    cases = _synthetic_manifest_cases(_simulated_frozen_baseline())

    assert all(case.metric_scope == ("SYNTHETIC_FORMAT_CONTROL",) for case in cases)
    assert all(case.place_metric.status == "NOT_APPLICABLE" for case in cases)


def test_runner_uses_real_crops_for_quality_and_synthetic_only_for_2_plus_20(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = _simulated_frozen_baseline()
    quality_document = _exact_geometry_document(baseline)
    calls = {"quality": 0, "performance": 0}

    async def fake_ocr_once(*, source_paths, cases, engine, work_root):
        del source_paths, engine, work_root
        if getattr(cases[0], "evidence_tier") == "SYNTHETIC_PERFORMANCE_ONLY":
            calls["performance"] += 1
            return quality_document, float(calls["performance"]), 3
        calls["quality"] += 1
        return quality_document, 999.0, 4

    monkeypatch.setattr(
        "scripts.run_g04_paddle_gate._ocr_once",
        fake_ocr_once,
    )
    suite = asyncio.run(
        _run_ocr_suite(
            quality_source_paths=(
                tmp_path / "q1",
                tmp_path / "q2",
                tmp_path / "q3",
                tmp_path / "q4",
            ),
            performance_source_paths=(
                tmp_path / "p1",
                tmp_path / "p2",
                tmp_path / "p3",
            ),
            baseline=baseline,
            engine=object(),
            work_root=tmp_path,
        )
    )

    assert calls == {"quality": 1, "performance": 22}
    assert suite.document == quality_document
    assert len(suite.observations) == 4
    assert suite.durations_ms == tuple(float(value) for value in range(3, 23))
    assert suite.staged_cleanup_count == 70


def test_external_receipt_sanitizer_preserves_exact_git_schema_and_samples() -> None:
    hash_a = "a" * 64
    hash_b = "b" * 64
    formal = {
        "schema_version": "g04-screenshot-parity-receipt-v1",
        "goal_id": "TC-VNEXT-G04-SCREENSHOT",
        "evidence_level": "REAL_PADDLE_LICENSED_SCREENSHOT_PARITY",
        "execution_mode": "REAL_PADDLE_LOCAL",
        "sanitized": True,
        "candidate": {
            "commit": "a" * 40,
            "tree": "b" * 40,
            "product_fingerprint": hash_a,
        },
        "evaluator": {
            "baseline_manifest_sha256": hash_a,
            "expected_transcript_sha256": hash_b,
            "runspec_sha256": hash_a,
            "runner_sha256": hash_b,
            "scorer_sha256": hash_a,
            "text_only_day_dataset_sha256": hash_b,
            "oracle_adjudication_sha256": hash_a,
            "metric_inputs_sha256": hash_b,
            "scored_outputs_sha256": hash_a,
        },
        "source_policy": {
            "originals_in_git": False,
            "originals_storage": "OUTSIDE_GIT_EPHEMERAL",
            "license_manifest_sha256": hash_a,
            "cleanup_receipts_sha256": hash_b,
            "terminal_cleanup_receipts_complete": True,
            "parity_metric_scope": "LICENSED_REAL_ONLY",
            "review_label": "MULTI_AGENT_SIMULATED_REVIEW",
            "human_review": False,
            "candidate_outputs_used_for_oracle": False,
        },
        "paddle": {
            "paddleocr_version": "3.7.0",
            "paddlepaddle_version": "3.3.1",
            "model_sha256": hash_a,
            "config_sha256": hash_b,
        },
        "hardware": {
            "device_class": "LOCAL_GPU",
            "hardware_sha256": hash_a,
            "driver_sha256": hash_b,
        },
        "performance": {
            "image_count": 3,
            "width_px": 1080,
            "height_px": 1920,
            "concurrency": 1,
            "warmup_runs": 2,
            "measured_runs": 20,
            "measurement_ms": list(range(1000, 1020)),
            "p95_ms": 1018,
        },
        "metric_counts": {
            "case_count": 5,
            "licensed_real_case_count": 4,
            "synthetic_case_count": 1,
            "text_only_day_case_count": 3,
            "critical_field_count": 9,
            "low_confidence_critical_field_count": 3,
            "reading_adjacency_count": 6,
            "location_baseline_count": 3,
            "cleanup_terminal_count": 70,
            "cleanup_receipt_count": 70,
        },
        "metrics": {
            "critical_field_f1": 0.96,
            "low_confidence_confirmation_recall": 1.0,
            "reading_order_adjacency_f1": 0.98,
            "location_precision_drop_pp": 0.5,
            "location_recall_drop_pp": 0.5,
            "wrong_city_count": 0,
            "wrong_category_count": 0,
            "sentence_as_place_count": 0,
            "internal_leak_count": 0,
            "cleanup_receipt_coverage": 1.0,
        },
        "decision": {"status": "PASS", "failures": []},
        "receipt_hash": "",
    }
    from governance.g04_screenshot_parity import canonical_receipt_hash

    formal["receipt_hash"] = canonical_receipt_hash(formal)
    external = {
        "schema_version": "g04-paddle-external-execution-receipt-v1",
        "status": "PASS",
        "formal_projection": formal,
        "diagnostics": {
            "runtime_binding": {},
            "measured_output_signature_sha256": hash_a,
            "actual_max_ocr_concurrency": 1,
            "low_confidence_poi_call_count": 0,
            "metric_scope_counts": {
                "licensed_real_ocr_case_count": 3,
                "licensed_real_planned_parity_case_count": 3,
                "licensed_real_low_confidence_control_count": 1,
                "reference_control_case_count": 1,
            },
            "source_bindings": {
                "quality_source_hashes": [
                    "1" * 64,
                    "2" * 64,
                    "3" * 64,
                    "7" * 64,
                ],
                "performance_source_hashes": ["4" * 64, "5" * 64, "6" * 64],
                "synthetic_performance_quality_eligibility": "NOT_APPLICABLE",
            },
            "planned_parity_basis": ("SAME_TRIP_UNDERSTANDING_PIPELINE_ACTIVITY_MENTIONS"),
            "deep_city_provider_evidence": ("NOT_APPLICABLE_NON_DEEP_CITY_SOURCE_SET"),
            "qwen_vl_status": "NOT_RUN_NO_EXACT_BINDING",
            "human_usability": "NOT_RUN",
            "production": "NOT_RUN",
            "public_internet_e2e": "NOT_RUN",
            "commercial": "NOT_RUN",
        },
    }
    external["external_receipt_hash"] = canonical_sha256(external)

    sanitized = sanitize_external_receipt(external)

    assert set(sanitized) == set(formal)
    assert sanitized["performance"]["measurement_ms"] == list(range(1000, 1020))


def test_runner_projection_matches_current_governance_receipt_schema() -> None:
    from governance.g04_screenshot_parity import _validate_formal_receipt

    baseline = _simulated_frozen_baseline()
    document = _exact_geometry_document(baseline)
    suite = OcrSuiteResult(
        document=document,
        observations=_observe_document(baseline, document),
        durations_ms=tuple(float(value) for value in range(1000, 1020)),
        staged_cleanup_count=70,
        measured_output_signature="a" * 64,
    )
    manifest = _passing_manifest()
    score = score_g04_screenshot_manifest(manifest)
    formal = _formal_projection(
        candidate_commit=_git("rev-parse", "HEAD"),
        baseline=baseline,
        runtime_binding={
            "paddleocr_version": "3.7.0",
            "paddlepaddle_version": "3.3.1",
            "gpu_driver_version": "test-driver",
            "paddle_compiled_cuda_version": "12.6",
            "paddle_compiled_cudnn_version": "9.9.0",
            "loaded_cudnn_runtime_version": "9.5.1",
        },
        model_bindings={
            "detection_model": "PP-OCRv5_mobile_det",
            "detection_model_tree_sha256": "a" * 64,
            "recognition_model": "PP-OCRv5_mobile_rec",
            "recognition_model_tree_sha256": "b" * 64,
        },
        device="gpu:0",
        manifest=manifest,
        score=score,
        suite=suite,
        source_cleanup_receipt="c" * 64,
        internal_leak_count=0,
        failures=(),
    )

    _validate_formal_receipt(formal)

    assert formal["candidate"]["tree"] == _git("rev-parse", "HEAD^{tree}")
    assert len(formal["performance"]["measurement_ms"]) == 20
