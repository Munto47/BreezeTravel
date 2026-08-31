from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from app.trip_understanding.screenshot_ocr import (
    PaddleOcrAdapter,
    RawOcrLine,
    ScreenshotOcrAllFailedError,
    ScreenshotOcrEngineBindingV1,
    ScreenshotOcrPartialError,
    ScreenshotOcrRunSpecV1,
    ScreenshotOcrTimeoutError,
    StagedScreenshotAsset,
    extract_screenshot_document,
    normalize_paddle_output,
    require_complete_document,
)
from evals.g04_screenshot import (
    G04ScreenshotManifestError,
    G04ScreenshotParityManifestV1,
    score_g04_screenshot_manifest,
)
from scripts.run_g04_screenshot_parity import main as parity_main


def _quad(x: float, y: float, width: float = 20, height: float = 10):
    return (
        (x, y),
        (x + width, y),
        (x + width, y + height),
        (x, y + height),
    )


def _line(text: str, confidence: float, x: float, y: float) -> RawOcrLine:
    return RawOcrLine(
        text=text,
        confidence=confidence,
        bbox=_quad(x, y),
    )


def _asset(tmp_path: Path, name: str, marker: str) -> StagedScreenshotAsset:
    path = tmp_path / name
    path.write_bytes(marker.encode("utf-8"))
    return StagedScreenshotAsset(
        path=path,
        content_hash=hashlib.sha256(marker.encode("utf-8")).hexdigest(),
    )


class FakeEngine:
    name = "fake-paddle"
    version = "3.7-compatible"

    def __init__(self, results: dict[str, Any]) -> None:
        self.results = results
        self.calls: list[str] = []
        self.binding = ScreenshotOcrEngineBindingV1.create(
            engine=self.name,
            engine_version=self.version,
            configuration={"fixture": True},
        )

    async def recognize(self, path: Path):
        self.calls.append(path.name)
        result = self.results[path.name]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, tuple) and result and result[0] == "sleep":
            import asyncio

            await asyncio.sleep(result[1])
            return result[2]
        return result


@pytest.mark.asyncio
async def test_paddle_adapter_lazy_loads_and_reads_new_polygon_output(tmp_path: Path) -> None:
    created: list[dict[str, Any]] = []

    class Pipeline:
        def predict(self, _: str):
            return [
                {
                    "res": {
                        "rec_texts": ["故宫"],
                        "rec_scores": [0.97],
                        "rec_polys": [[[1, 2], [11, 2], [11, 8], [1, 8]]],
                    }
                }
            ]

    def factory(**options: Any):
        created.append(options)
        return Pipeline()

    image = tmp_path / "new-output.png"
    image.write_bytes(b"fixture")
    adapter = PaddleOcrAdapter(pipeline_factory=factory)
    assert created == []

    lines = await adapter.recognize(image)
    repeated = await adapter.recognize(image)

    assert lines == repeated
    assert lines[0].bbox == ((1.0, 2.0), (11.0, 2.0), (11.0, 8.0), (1.0, 8.0))
    assert len(created) == 1
    assert created[0]["enable_mkldnn"] is False


@pytest.mark.asyncio
async def test_isolated_paddle_cancellation_aborts_and_joins_worker_before_return(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    released = threading.Event()
    finished = threading.Event()
    aborted = threading.Event()

    class BlockingWorker:
        def recognize(self, _: Path):
            started.set()
            released.wait(timeout=5)
            finished.set()
            raise RuntimeError("controlled worker termination")

        def abort(self) -> None:
            aborted.set()
            released.set()

        def close(self) -> None:
            released.set()

    worker = BlockingWorker()
    adapter = PaddleOcrAdapter(process_worker_factory=lambda _options: worker)
    image = tmp_path / "cancel.png"
    image.write_bytes(b"fixture")

    operation = asyncio.create_task(adapter.recognize(image))
    assert await asyncio.to_thread(started.wait, 2)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert aborted.is_set()
    assert finished.is_set()


@pytest.mark.asyncio
async def test_isolated_paddle_serializes_across_adapter_instances(tmp_path: Path) -> None:
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    class MeasuringWorker:
        def recognize(self, _: Path):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.03)
            with state_lock:
                active -= 1
            return (_line("完成", 0.99, 0, 0),)

        def abort(self) -> None:
            return None

        def close(self) -> None:
            return None

    first = PaddleOcrAdapter(
        process_worker_factory=lambda _options: MeasuringWorker()
    )
    second = PaddleOcrAdapter(
        process_worker_factory=lambda _options: MeasuringWorker()
    )
    image = tmp_path / "serial.png"
    image.write_bytes(b"fixture")

    first_result, second_result = await asyncio.gather(
        first.recognize(image),
        second.recognize(image),
    )

    assert first_result[0].text == "完成"
    assert second_result[0].text == "完成"
    assert maximum_active == 1


def test_paddle_adapter_reads_compatibility_shapes_and_four_point_boxes() -> None:
    rectangle = normalize_paddle_output(
        [{"res": {"rec_texts": ["颐和园"], "rec_scores": [0.9], "rec_boxes": [[2, 3, 12, 9]]}}]
    )
    legacy = normalize_paddle_output(
        [[[[[3, 4], [13, 4], [13, 10], [3, 10]], ("天坛", 0.88)]]]
    )

    assert rectangle[0].bbox == ((2.0, 3.0), (12.0, 3.0), (12.0, 9.0), (2.0, 9.0))
    assert legacy[0].text == "天坛"
    assert legacy[0].bbox == ((3.0, 4.0), (13.0, 4.0), (13.0, 10.0), (3.0, 10.0))


@pytest.mark.asyncio
async def test_extract_preserves_upload_order_and_clusters_same_y_by_x(tmp_path: Path) -> None:
    first = _asset(tmp_path, "first.png", "first")
    second = _asset(tmp_path, "second.png", "second")
    engine = FakeEngine(
        {
            "first.png": (
                _line("右", 0.99, 80, 10),
                _line("下一行", 0.99, 5, 40),
                _line("左", 0.99, 5, 11),
            ),
            "second.png": (_line("第二张", 0.99, 1, 1),),
        }
    )

    document = await extract_screenshot_document(
        [first, second],
        engine,
        ScreenshotOcrRunSpecV1(),
    )

    assert document.semantic_text == "左\n右\n下一行\n第二张"
    assert [line.image_index for line in document.lines] == [0, 0, 0, 1]
    assert [line.reading_index for line in document.lines] == [0, 1, 2, 3]
    assert engine.calls == ["first.png", "second.png"]
    assert document.partial is False


@pytest.mark.asyncio
async def test_unicode_spans_are_half_open_code_point_offsets(tmp_path: Path) -> None:
    asset = _asset(tmp_path, "unicode.png", "unicode")
    engine = FakeEngine(
        {"unicode.png": (_line("北京😀", 0.9, 0, 0), _line("西湖", 0.9, 0, 20))}
    )

    document = await extract_screenshot_document(
        [asset],
        engine,
        ScreenshotOcrRunSpecV1(),
    )

    assert document.lines[0].semantic_span.start == 0
    assert document.lines[0].semantic_span.end == 3
    assert document.lines[1].semantic_span.start == 4
    assert document.lines[1].semantic_span.end == 6
    for line in document.lines:
        assert document.semantic_text[line.semantic_span.start : line.semantic_span.end] == line.text


@pytest.mark.asyncio
async def test_low_confidence_threshold_is_strict_at_point_eight_five(tmp_path: Path) -> None:
    asset = _asset(tmp_path, "threshold.png", "threshold")
    engine = FakeEngine(
        {"threshold.png": (_line("边界", 0.85, 0, 0), _line("低于", 0.849, 0, 20))}
    )

    document = await extract_screenshot_document(
        [asset],
        engine,
        ScreenshotOcrRunSpecV1(low_confidence_threshold=0.85),
    )

    assert [line.requires_confirmation for line in document.lines] == [False, True]
    assert document.engine_binding.low_confidence_threshold == 0.85


@pytest.mark.asyncio
async def test_partial_failure_keeps_successful_text_and_has_typed_check(tmp_path: Path) -> None:
    first = _asset(tmp_path, "ok.png", "ok")
    second = _asset(tmp_path, "failed.png", "failed")
    third = _asset(tmp_path, "also-ok.png", "also-ok")
    engine = FakeEngine(
        {
            "ok.png": (_line("故宫", 0.95, 0, 0),),
            "failed.png": RuntimeError("controlled engine failure"),
            "also-ok.png": (_line("天坛", 0.95, 0, 0),),
        }
    )

    document = await extract_screenshot_document(
        [first, second, third],
        engine,
        ScreenshotOcrRunSpecV1(),
    )

    assert document.semantic_text == "故宫\n天坛"
    assert document.partial is True
    assert [image.status for image in document.images] == ["SUCCEEDED", "FAILED", "SUCCEEDED"]
    with pytest.raises(ScreenshotOcrPartialError) as captured:
        require_complete_document(document)
    assert captured.value.document == document


@pytest.mark.asyncio
async def test_all_failed_and_timeout_are_distinct_typed_errors(tmp_path: Path) -> None:
    failed = _asset(tmp_path, "failed.png", "failed")
    with pytest.raises(ScreenshotOcrAllFailedError) as all_failed:
        await extract_screenshot_document(
            [failed],
            FakeEngine({"failed.png": RuntimeError("controlled")}),
            ScreenshotOcrRunSpecV1(),
        )
    assert type(all_failed.value) is ScreenshotOcrAllFailedError
    assert all_failed.value.image_results[0].status == "FAILED"

    slow = _asset(tmp_path, "slow.png", "slow")
    with pytest.raises(ScreenshotOcrTimeoutError) as timed_out:
        await extract_screenshot_document(
            [slow],
            FakeEngine({"slow.png": ("sleep", 0.05, (_line("迟到", 0.9, 0, 0),))}),
            ScreenshotOcrRunSpecV1(
                per_image_timeout_seconds=0.001,
                batch_timeout_seconds=0.01,
            ),
        )
    assert timed_out.value.image_results[0].status == "TIMED_OUT"


@pytest.mark.asyncio
async def test_timeout_can_be_partial_when_another_image_succeeds(tmp_path: Path) -> None:
    slow = _asset(tmp_path, "slow.png", "slow")
    fast = _asset(tmp_path, "fast.png", "fast")
    engine = FakeEngine(
        {
            "slow.png": ("sleep", 0.05, (_line("迟到", 0.9, 0, 0),)),
            "fast.png": (_line("保留", 0.9, 0, 0),),
        }
    )

    document = await extract_screenshot_document(
        [slow, fast],
        engine,
        ScreenshotOcrRunSpecV1(
            per_image_timeout_seconds=0.001,
            batch_timeout_seconds=0.1,
        ),
    )

    assert document.semantic_text == "保留"
    assert document.partial is True
    assert [image.status for image in document.images] == ["TIMED_OUT", "SUCCEEDED"]


@pytest.mark.asyncio
async def test_document_hash_is_canonical_and_excludes_paths_and_filenames(tmp_path: Path) -> None:
    first = _asset(tmp_path, "first-name.png", "same-content")
    nested = tmp_path / "nested"
    nested.mkdir()
    second = _asset(nested, "other-name.webp", "same-content")
    result = (_line("杭州😀", 0.9, 1, 2),)

    first_document = await extract_screenshot_document(
        [first], FakeEngine({"first-name.png": result}), ScreenshotOcrRunSpecV1()
    )
    second_document = await extract_screenshot_document(
        [second], FakeEngine({"other-name.webp": result}), ScreenshotOcrRunSpecV1()
    )

    assert first_document.document_hash == second_document.document_hash
    serialized = first_document.model_dump_json()
    assert str(tmp_path) not in serialized
    assert "first-name.png" not in serialized
    assert set(first_document.model_dump()) == {
        "version",
        "semantic_text",
        "partial",
        "images",
        "lines",
        "engine_binding",
        "document_hash",
    }


def _passing_manifest() -> dict[str, Any]:
    licensed_cases = [
        {
            "case_id": f"licensed-{index:02d}",
            "source": {
                "evidence_tier": "LICENSED_REAL_SCREENSHOT",
                "source_sha256": digest * 64,
                "license_name": "Controlled fixture license",
                "license_reference": f"receipt:licensed-{index:02d}",
            },
            "metric_scope": ["REAL_OCR_READING", "REAL_PLANNED_PARITY"],
            "expected_key_fields": ["day=1", "place=故宫", "time=09:00"],
            "observed_key_fields": ["day=1", "place=故宫", "time=09:00"],
            "expected_reading_order": ["day", "place", "time"],
            "observed_reading_order": ["day", "place", "time"],
            "expected_low_confidence_fields": [],
            "observed_confirmation_fields": [],
            "place_metric": {
                "status": "EVALUATED",
                "oracle_items": [
                    {
                        "oracle_id": f"planned-{index:02d}",
                        "expected_text": "故宫",
                        "activity_role": "PLANNED",
                        "metric_eligibility": "ELIGIBLE",
                    }
                ],
                "reference_executable_places": ["故宫"],
                "text_executable_places": ["故宫"],
                "screenshot_executable_places": ["故宫"],
            },
            "serious_errors": [],
        }
        for index, digest in enumerate(("a", "d", "e"), start=1)
    ]
    return {
        "schema_version": "g04-screenshot-parity-manifest-v1",
        "cases": [
            *licensed_cases,
            {
                "case_id": "licensed-low-confidence-control",
                "source": {
                    "evidence_tier": "LICENSED_REAL_SCREENSHOT",
                    "source_sha256": "f" * 64,
                    "license_name": "Controlled fixture license",
                    "license_reference": "receipt:licensed-low-confidence-control",
                },
                "metric_scope": ["REAL_LOW_CONFIDENCE_CONTROL"],
                "expected_key_fields": ["place=故宫"],
                "observed_key_fields": ["place=故宫"],
                "expected_reading_order": ["place=故宫"],
                "observed_reading_order": ["place=故宫"],
                "expected_low_confidence_fields": ["place=故宫"],
                "observed_confirmation_fields": ["place=故宫"],
                "place_metric": {
                    "status": "NOT_APPLICABLE",
                    "reason": "LOW_CONFIDENCE_CONTROL",
                    "oracle_items": [
                        {
                            "oracle_id": "guarded-place",
                            "expected_text": "故宫",
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


def test_manifest_schema_and_scorer_keep_evidence_tiers_separate() -> None:
    schema_path = Path(__file__).parents[1] / "evals/g04_screenshot/manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(_passing_manifest())

    manifest = G04ScreenshotParityManifestV1.model_validate(_passing_manifest())
    report = score_g04_screenshot_manifest(manifest)

    assert report.licensed_real_case_count == 4
    assert report.licensed_real_ocr_case_count == 3
    assert report.licensed_real_planned_parity_case_count == 3
    assert report.synthetic_format_case_count == 1
    assert report.key_field_f1 == 1.0
    assert report.adjacency_f1 == 1.0
    assert report.low_confidence_confirmation_recall == 1.0
    assert report.three_image_p95_ms == 19.0
    assert report.performance_sample_count == 20
    assert report.gate_pass is True


def test_scorer_reports_all_locked_quality_failures() -> None:
    payload = _passing_manifest()
    case = payload["cases"][0]
    case["observed_key_fields"] = ["day=1"]
    case["observed_reading_order"] = ["time", "place", "day"]
    case["observed_confirmation_fields"] = []
    payload["cases"][3]["observed_confirmation_fields"] = []
    case["place_metric"]["screenshot_executable_places"] = ["天坛"]
    case["serious_errors"] = [{"category": "WRONG_CITY", "item_id": "故宫"}]
    payload["performance"]["durations_ms"] = [13_000] * 20

    report = score_g04_screenshot_manifest(payload)

    assert report.gate_pass is False
    assert set(report.failures) == {
        "KEY_FIELD_F1_BELOW_95_PERCENT",
        "ADJACENCY_F1_BELOW_97_PERCENT",
        "LOW_CONFIDENCE_CONFIRMATION_RECALL_BELOW_100_PERCENT",
        "SCREENSHOT_PLACE_PRECISION_DROP_EXCEEDS_1PP",
        "SCREENSHOT_PLACE_RECALL_DROP_EXCEEDS_1PP",
        "SERIOUS_PLACE_ERROR_COUNT_NONZERO",
        "THREE_IMAGE_P95_EXCEEDS_12_SECONDS",
    }


def test_synthetic_only_manifest_cannot_claim_quality_pass() -> None:
    payload = _passing_manifest()
    payload["cases"] = [payload["cases"][1]]
    with pytest.raises(G04ScreenshotManifestError):
        score_g04_screenshot_manifest(payload)


def test_runner_reads_only_explicit_manifest_and_returns_gate_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "report.json"
    manifest_path.write_text(json.dumps(_passing_manifest()), encoding="utf-8")

    status = parity_main(
        ["--manifest", str(manifest_path), "--output", str(output_path)]
    )

    assert status == 0
    assert json.loads(capsys.readouterr().out)["gate_pass"] is True
    assert json.loads(output_path.read_text(encoding="utf-8"))["gate_pass"] is True
